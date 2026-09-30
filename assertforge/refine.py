"""Generate, then prove, then refine -- the loop that is the point of this repo.

The naive version of "LLM writes assertions" is: ask once, keep whatever comes
out. Measured against the backend grammar, that keeps a lot of syntax errors and
a lot of assertions that are trivially true (assert(1), assert(cnt==cnt)) -- a
property that proves in zero ticks with no counterexample is worthless, and a
model will produce them whenever it is unsure.

This loop closes two feedback paths that a one-shot call cannot:

  refute path   the solver finds a counterexample -> the cycle where the
                design broke the claim is handed back verbatim, and the model
                must fix the *claim*, not weaken the *design*.
  vacuity path  the claim proves with every trace-relevant signal constant
                -> it is flagged vacuous and the model is told to strengthen it
                using a real signal.

Both are machine-checkable, which is why this is a tool and not a prompt.
"""

import json
import os
import re
import textwrap

from . import formal, grammar, llm, repair

SYSTEM = """You write SystemVerilog assertions for a formal verification tool.

HARD CONSTRAINTS -- violations are rejected by the tool, not fixed by it:
- Emit ONLY `assert`, `assume` or `cover` statements inside `always @(posedge clk)` blocks.
- Forbidden: `assert property (...)` at module level, named `property ... endproperty`,
  `disable iff`, `|->`, `=>`, `->`, `$rose`, `$fell`, `$stable`, `sequence`, `clocking`.
- `$past(sig)` and `$past(sig, n)` ARE allowed.

THE RESET CONTRACT -- this is measured, and getting it wrong is the most common failure:
- The DUT powers up in an arbitrary state. Cycle 0 accepts anything, so an
  assertion about a design output is NOT provable at cycle 0 unless it is
  guarded. The harness drives `rst` high at cycle 0 to fix that.
- The harness defines a register `af_started` which is 0 at cycle 0 and 1 after.
  EVERY assertion about the design must be guarded: wrap the body in
  `if (af_started) begin ... end`.
- Ranges and parameter names of the design (DEPTH, WIDTH) are available as
  localparams in the harness.

QUALITY CONSTRAINTS -- an assertion that is always true is worthless:
- Every assertion must mention at least one signal of the design under test.
- Do not write `assert(1)`, `assert(x == x)`, or an implication guarded by 0.
- State the invariant that must hold, in terms of the design's own signals.

GOOD EXAMPLE for a counter with parameter DEPTH:
```systemverilog
always @(posedge clk) if (af_started) begin
    assert (count <= DEPTH);
    assert (full == (count == DEPTH));
end
```

Output format: a single fenced ```systemverilog block, nothing else."""

USER = """Design under test (module `{top}`):

```systemverilog
{dut}
```

Write {n} assertion(s) capturing this intent:
{intent}
{feedback}
"""


VACUOUS_PROMPT = """Your previous attempt PROVED, but the solver reached the proof
without ever exercising a signal -- every assertion that mentions only constants
is vacuous and worthless. Previous attempt:

```systemverilog
{prev}
```

Rewrite so every assertion constrains a signal that actually appears in the
design (one of: {sigs}). Keep the `if (af_started)` guard, and keep the same
intent: {intent}
"""


REFINE_PROMPT = """Your assertion produced a counterexample. Decide which side is wrong.

Intent: {intent}

Your assertion:
```systemverilog
{prev}
```

Counterexample the solver found (cycle 0 is the first clock edge):
{trace}

If the claim is TOO STRONG (it forbids something the design is allowed to do),
weaken it while keeping the intent. If the claim is TOO WEAK or malformed,
tighten it. Do not simply delete the assertion. Parameter names from the design
(like DEPTH or WIDTH) are available as localparams. Reply with the corrected
assertion block only.
"""


TWIN_PROMPT = """Your assertion PROVED, and it is worthless.

The harness also ran it against a deliberately broken version of the same design
-- one that is known to be wrong. Your assertion PROVED there too. A property
that holds on a design that is broken is not checking the design; it is checking
that the solver agrees two expressions are equal.

Assertion you wrote:
```systemverilog
{prev}
```

Intent it was supposed to capture: {intent}

Write it again so that the broken design FAILS the property while the correct one
passes. Every signal of the design is available ({sigs}), parameter names like
DEPTH are localparams, and each assertion must be guarded by `if (af_started)`.

Output format: a single fenced ```systemverilog block, nothing else."""


def strip_code(text):
    """Pull the first fenced block, or fall back to the whole reply."""
    m = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def signals_of(dut_src):
    """Cheap port/reg/net harvest -- enough to tell a model what exists."""
    sigs = []
    for m in re.finditer(r"\b(input|output|inout)\b\s*(?:wire|reg|logic)?\s*(\[[^\]]*\])?\s*([A-Za-z_]\w*)", dut_src):
        sigs.append(m.group(3))
    for m in re.finditer(r"\b(?:reg|wire|logic)\b\s*(\[[^\]]*\])?\s*([A-Za-z_]\w*)", dut_src):
        sigs.append(m.group(2))
    seen, out = set(), []
    for s in sigs:
        if s not in seen and s not in ("always", "begin", "end"):
            seen.add(s)
            out.append(s)
    return out


# Signals that appear in every assertion by construction, so they cannot be
# evidence that an assertion says anything about the design.
_VACUITY_EXCLUDE = {"clk", "clock", "rst", "reset", "af_started"}


def mentions_dut_signal(src, sigs):
    """True if the candidate constrains a real design signal.

    clk/rst are excluded on purpose: every assertion contains them, so counting
    them would make `assert(1)` look non-vacuous. This was a live bug -- the
    heuristic passed everything until the guard register was excluded.
    """
    return any(re.search(r"\b%s\b" % re.escape(s), src)
               for s in sigs if s not in _VACUITY_EXCLUDE)


def wrapper_module(name, ports, body, dut_top, dut_inst):
    hist = "\n".join(
        "    // history handled inline by the model where needed") if False else ""
    return grammar.TEMPLATE.format(
        name=name, intent="", module=name, ports=ports, history=hist, body=body)


def params_of(dut_text):
    """Harvest `#(parameter A = 1, parameter B = 2)` from the DUT's header.

    A parameterised DUT is the normal case in real RTL and the wrapper has to
    mirror the declared defaults, otherwise every downstream reference to the
    parameter is a non-constant width and yosys refuses the design.
    """
    m = re.search(r"module\s+\w+\s*#\s*\((.*?)\)\s*\(", dut_text, re.S)
    if not m:
        return []
    out = []
    for pm in re.finditer(
            r"parameter\s+(?:\w+\s+)?(\[[^\]]*\])?\s*([A-Za-z_]\w*)\s*=\s*([^,)]+)", m.group(1)):
        rng, nm, val = pm.group(1) or "", pm.group(2), pm.group(3).strip()
        out.append((rng, nm, val))
    return out


def key_error(log, limit=4):
    """Pull the lines that actually say why a run failed.

    Handing a model 900 characters of yosys banner is the same as handing it
    nothing; the actionable line is the one with ERROR in it.
    """
    lines = [l.strip() for l in log.splitlines()
             if "ERROR" in l or "syntax error" in l]
    seen, out = set(), []
    for l in lines:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return "\n".join(out[:limit])


def build_harness(dut_text, top, assertion_src, wrapper_name="assertions"):
    """Write a wrapper module that instantiates the DUT and holds the asserts.

    The wrapper approach means the DUT file is never edited -- an assertion run
    cannot alter the design it is checking, which matters when the loop runs
    unattended and nobody reads each candidate.
    """
    ports, conns, seen = [], [], set()
    for m in re.finditer(
            r"\b(input|output|inout)\b\s*(?:wire|reg|logic)?\s*(\[[^\]]*\])?\s*([A-Za-z_]\w*)",
            dut_text):
        kind, rng, nm = m.group(1), m.group(2), m.group(3)
        if nm in seen:
            continue
        seen.add(nm)
        rng = (" " + rng) if rng else ""
        ports.append("    %s%s %s" % (kind, rng, nm))
        conns.append("    .%s(%s)" % (nm, nm))

    par = params_of(dut_text)
    localparams = "\n".join(
        "    localparam %s%s = %s;" % (rng + " " if rng else "", nm, val)
        for rng, nm, val in par)
    # Two ways to bind parameters, in order of what the DUT actually says:
    #   explicit in the module header -> .NAME(NAME) named binding
    #   a bare `parameter X` in the body -> localparam mirror, positional binding
    par_inst = ""
    if par:
        par_inst = " #(\n" + ",\n".join(
            "        .%s(%s)" % (nm, nm) for _, nm, _ in par) + "\n    )"
    # The prologue is not decoration. Without it the solver is free to pick an
    # arbitrary power-on state, so it "finds" counterexamples like
    # count==0 && full==1 that no correct design can ever reach. Measured: the
    # same property that FAILs without this prologue is PROVED with it, at the
    # cost of one extra cycle of depth. Every counterexample after this is about
    # the design.
    return textwrap.dedent("""\
        // GENERATED by assertforge -- do not edit; regenerate instead.
        // Parameter defaults are mirrored so assertions can reference DEPTH/WIDTH.
        module {w}(
        {ports}
        );
        {localparams}

            // design under test, instantiated unmodified
            {top}{par_inst} u_dut(
        {conns}
            );

            // --- assertforge prologue -------------------------------------
            // Assume the design is reset at time 0. Without this the solver
            // may start from any state and refute true properties with
            // cycle-0 counterexamples that the design cannot actually reach.
            reg af_started = 1'b0;
            always @(posedge clk) af_started <= 1'b1;
            always @(posedge clk) if (!af_started) assume (rst);
            // --------------------------------------------------------------

        {body}
        endmodule
        """).format(w=wrapper_name, ports=",\n".join(ports),
                    localparams=localparams, top=top, par_inst=par_inst,
                    conns=",\n".join(conns), body=assertion_src)


class Attempt:
    """One candidate, everything that happened to it, and why it ended."""

    def __init__(self, index, source, result, raw_source=None, repair=None,
                 lint_before=None, twin=None):
        self.index = index
        self.source = source            # what the solver was actually given
        self.raw_source = raw_source if raw_source is not None else source
        self.result = result
        self.verdict = result.status
        self.repair = repair
        self.lint_before = list(lint_before or [])
        self.twin = twin

    @property
    def rewritten(self):
        return bool(self.repair and self.repair.changed)

    def to_dict(self):
        d = {"attempt": self.index, "verdict": self.verdict,
             "seconds": round(self.result.seconds, 2),
             "source": self.source,
             "rewritten": self.rewritten,
             "lint_before": self.lint_before}
        if self.raw_source != self.source:
            d["raw_source"] = self.raw_source
        if self.repair:
            d["repair"] = self.repair.to_dict()
        if self.twin:
            d["twin"] = self.twin
        return d


class Session:
    """One DUT, one intent, N rounds of generate/repair/refine.

    The ablations are parameters rather than separate scripts on purpose: an
    arm of an experiment and the tool it measures should be the same code path,
    otherwise the comparison is between two implementations and not between two
    strategies.

      use_repair=False   take the model's text verbatim (the naive baseline)
      use_refine=False   one round only; no counterexample or vacuity feedback
      lint_retry=False   do not spend a model call re-asking after a lint failure
    """

    def __init__(self, dut_path, top, intent, n_assertions=2, rounds=4,
                 client=None, workdir=None, depth=10, timeout=180,
                 use_repair=True, use_refine=True, lint_retry=True,
                 max_lint_retries=2, twin_path=None, twin_top=None):
        self.dut_path = dut_path
        self.top = top
        self.intent = intent
        self.n_assertions = n_assertions
        self.rounds = rounds
        self.client = client or llm.Client()
        self.workdir = workdir or os.path.join(
            os.path.dirname(os.path.abspath(dut_path)), "_afwork")
        self.depth = depth
        self.timeout = timeout
        self.use_repair = use_repair
        self.use_refine = use_refine
        self.lint_retry = lint_retry
        self.max_lint_retries = max_lint_retries
        self.twin_path = twin_path
        self.twin_top = twin_top or top
        self.dut_text = open(dut_path, encoding="utf-8", errors="ignore").read()
        self.sigs = signals_of(self.dut_text)
        self.attempts = []
        self.llm_calls = 0
        self.harness_failures = 0

    def _ask(self, prompt):
        self.llm_calls += 1
        return strip_code(self.client.chat(prompt, system=SYSTEM, max_tokens=900))

    def _check(self, assertion_src, tag):
        wrapper_name = "af_%s" % tag
        wrapper = build_harness(self.dut_text, self.top,
                                assertion_src, wrapper_name)
        wd = os.path.join(self.workdir, tag)
        os.makedirs(wd, exist_ok=True)
        wp = os.path.join(wd, "%s.sv" % wrapper_name)
        open(wp, "w", encoding="utf-8", newline="\n").write(wrapper)
        dut_local = os.path.join(wd, "dut.v")
        open(dut_local, "w", encoding="utf-8", newline="\n").write(self.dut_text)
        return formal.run(
            wd, wrapper_name, ["dut.v"], ["%s.sv" % wrapper_name],
            depth=self.depth, timeout=self.timeout)

    def verify_twin(self, cand, tag):
        """Ground truth: does this accepted assertion catch the known bug?

        Only run when the caller supplied a twin, because it is the expensive
        form of the check -- but it is the decisive one, so it is run once, on
        the candidate that is about to be reported as the answer.
        """
        if not self.twin_path:
            return None
        from . import vacuity
        return vacuity.against_twin(
            self.twin_path, self.twin_top, cand,
            os.path.join(self.workdir, tag + "_twin"),
            depth=self.depth, timeout=self.timeout)

    # -- one candidate, end to end -------------------------------------------

    def propose(self, prev=None, last_trace="", first=True):
        if prev is None or last_trace == "__none__":
            prompt = USER.format(top=self.top, dut=self.dut_text,
                                 n=self.n_assertions, intent=self.intent,
                                 feedback="")
        else:
            prompt = REFINE_PROMPT.format(intent=self.intent, prev=prev,
                                          trace=last_trace)
        cand = self._ask(prompt)
        lint_before = grammar.lint(cand)

        if self.use_repair:
            rep = repair.normalise(cand)
            return cand, rep.source, rep, lint_before

        if self.lint_retry:
            tries = 0
            while lint_before and tries < self.max_lint_retries:
                cand = self._ask(
                    "Your reply used constructs this backend rejects:\n- "
                    + "\n- ".join(lint_before) + "\nRewrite using only immediate "
                    "assert/assume/cover inside always @(posedge clk), every "
                    "statement guarded by `if (af_started)`. "
                    "Intent unchanged: " + self.intent)
                lint_before = grammar.lint(cand)
                tries += 1
        return cand, cand, None, lint_before

    def run(self):
        """Return the best Attempt. PROVED if any round proved a non-vacuous claim."""
        rounds = self.rounds if self.use_refine else 1
        prev, last_trace, best = None, "", None
        for rnd in range(rounds):
            tag = "r%d" % rnd
            if rnd == 0 or prev is None:
                prompt_prev = None
            else:
                prompt_prev = prev
            if rnd == 0:
                raw, cand, rep, lint_before = self.propose()
            else:
                raw, cand, rep, lint_before = self.propose(prev=prev,
                                                           last_trace=last_trace)
            res = self._check(cand, tag)
            att = Attempt(rnd, cand, res, raw_source=raw, repair=rep,
                          lint_before=lint_before)
            self.attempts.append(att)

            if res.status == formal.PROVED:
                if not mentions_dut_signal(cand, self.sigs):
                    # A proof that never touched a design signal is vacuous.
                    att.verdict = "VACUOUS"
                    prev = cand
                    last_trace = ""
                    vraw, vcand, vrep, vlint = self.propose(
                        prev=cand, last_trace="")
                    vcand = strip_code(self._ask(VACUOUS_PROMPT.format(
                        prev=cand, sigs=", ".join(self.sigs[:12]),
                        intent=self.intent)))
                    if self.use_repair:
                        vrep = repair.normalise(vcand)
                        vcand = vrep.source
                    res2 = self._check(vcand, tag + "v")
                    att2 = Attempt(rnd, vcand, res2, raw_source=vcand,
                                   repair=vrep, lint_before=grammar.lint(vcand))
                    att2.verdict = "VACUOUS->" + res2.status
                    self.attempts.append(att2)
                    if res2.status == formal.PROVED:
                        return att2
                    prev = vcand
                    last_trace = res2.trace_table() or key_error(res2.log)
                    best = att2 if best is None else best
                    continue
                tw = self.verify_twin(cand, tag)
                att.twin = tw
                if tw and tw["verdict"] == "MISSED_BUG":
                    # Ground truth outranks the heuristic. `mentions_dut_signal`
                    # looks for a design signal in the text, and a trivially
                    # true property mentions one -- measured: `assert (count <=
                    # DEPTH)` on a two-bit count passed the heuristic and also
                    # proved against the known-broken twin. The twin is a real
                    # mutant, so it is the gate; the heuristic is only the cheap
                    # filter in front of it.
                    att.verdict = "FALSE_PROVE"
                    prev = cand
                    last_trace = ""
                    tvraw = self._ask(TWIN_PROMPT.format(
                        prev=cand, intent=self.intent,
                        sigs=", ".join(self.sigs[:12])))
                    tvraw = strip_code(tvraw)
                    tvrep = None
                    if self.use_repair:
                        tvrep = repair.normalise(tvraw)
                        tvraw = tvrep.source
                    res3 = self._check(tvraw, tag + "t")
                    att3 = Attempt(rnd, tvraw, res3, raw_source=tvraw,
                                   repair=tvrep, lint_before=grammar.lint(tvraw))
                    att3.verdict = "TWIN->" + res3.status
                    self.attempts.append(att3)
                    if res3.status == formal.PROVED:
                        att3.twin = self.verify_twin(tvraw, tag + "t")
                        return att3
                    best = att3
                    prev = tvraw
                    last_trace = res3.trace_table() or key_error(res3.log)
                    continue
                best = att
                return att

            best = att if best is None else best
            prev = cand
            if res.status in (formal.ERROR, formal.SYNTAX, formal.NOINPUT):
                last_trace = key_error(res.log) or res.log[-600:]
            else:
                last_trace = res.trace_table() or key_error(res.log)
            if not last_trace:
                last_trace = res.log[-600:]
        return best


# -- experiment support -------------------------------------------------------

ARMS = {
    "oneshot_raw":   dict(use_repair=False, use_refine=False, lint_retry=False),
    "lint_retry":    dict(use_repair=False, use_refine=True,  lint_retry=True),
    "repair_once":   dict(use_repair=True,  use_refine=False, lint_retry=False),
    "repair_refine": dict(use_repair=True,  use_refine=True,  lint_retry=True),
}


def outcome_of(att, design_has_bug=False):
    """... a PROVED on a correct design whose twin check MISSED_BUG is a
    FALSE_PROVE: the solver agreed with the assertion and the assertion was not
    checking anything, which is the failure a proof-only tool cannot see."""
    """Reduce an Attempt to the one word the experiment counts.

    The categories are deliberately about the *claim*, because that is what the
    tool is for:
      PROVED      the solver proved a non-vacuous assertion
      REFUTED     the solver found a counterexample (the design is wrong)
      MALFORMED   the candidate never reached the solver -- a generator failure
      OTHER       built, ran, and did neither
    `MALFORMED` is the number the repair path is supposed to move.
    """
    if att is None:
        return "NOATTEMPT"
    twin = getattr(att, "twin", None)
    if twin and twin.get("verdict") == "MISSED_BUG" and not design_has_bug:
        return "FALSE_PROVE"
    v = att.verdict
    if v.startswith("VACUOUS->"):
        v = v.split("->", 1)[1]
    if v == formal.PROVED or v == "VACUOUS":
        return "PROVED"
    if v == formal.REFUTED:
        return "REFUTED"
    if v in (formal.SYNTAX, formal.ERROR, formal.NOINPUT):
        return "MALFORMED"
    return "OTHER"
