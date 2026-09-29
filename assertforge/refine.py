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

from . import formal, grammar, llm

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
    def __init__(self, index, source, result):
        self.index = index
        self.source = source
        self.result = result
        self.verdict = result.status

    def to_dict(self):
        return {"attempt": self.index, "verdict": self.verdict,
                "seconds": round(self.result.seconds, 2),
                "seconds_total": round(self.result.seconds, 2),
                "source": self.source}


class Session:
    """One DUT, one intent, N rounds of generate/refine."""

    def __init__(self, dut_path, top, intent, n_assertions=2, rounds=4,
                 client=None, workdir=None, depth=10, timeout=180):
        self.dut_path = dut_path
        self.top = top
        self.intent = intent
        self.n_assertions = n_assertions
        self.rounds = rounds
        self.client = client or llm.Client()
        self.workdir = workdir or os.path.join(os.path.dirname(os.path.abspath(dut_path)), "_afwork")
        self.depth = depth
        self.timeout = timeout
        self.dut_text = open(dut_path, encoding="utf-8", errors="ignore").read()
        self.sigs = signals_of(self.dut_text)
        self.attempts = []
        self.llm_calls = 0

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

    def run(self):
        feedback = ""
        prev = None
        for rnd in range(self.rounds):
            tag = "r%d" % rnd
            if prev is None:
                prompt = USER.format(top=self.top, dut=self.dut_text,
                                     n=self.n_assertions, intent=self.intent,
                                     feedback="")
                cand = self._ask(prompt)
            else:
                prompt = REFINE_PROMPT.format(intent=self.intent, prev=prev,
                                              trace=last_trace)
                cand = self._ask(prompt)

            problems = grammar.lint(cand)
            if problems:
                # A poison token is a generator bug. Ask again with the reasons,
                # which is much cheaper than a solver run.
                cand = self._ask(
                    "Your reply used constructs this backend rejects:\n- "
                    + "\n- ".join(problems) + "\nRewrite using only immediate "
                    "assert/assume/cover inside always @(posedge clk). "
                    "Intent unchanged: " + self.intent)
                problems = grammar.lint(cand)

            res = self._check(cand, tag)
            att = Attempt(rnd, cand, res)
            self.attempts.append(att)

            if res.status == formal.PROVED:
                if not mentions_dut_signal(cand, self.sigs):
                    prev = cand
                    last_trace = ""
                    cand = self._ask(VACUOUS_PROMPT.format(prev=cand,
                                                           sigs=", ".join(self.sigs[:12]),
                                                           intent=self.intent))
                    res2 = self._check(cand, tag + "v")
                    self.attempts[-1].verdict = "VACUOUS->" + res2.status
                    att = Attempt(rnd, cand, res2)
                    self.attempts.append(att)
                    if res2.status == formal.PROVED:
                        return att
                    prev = cand
                    last_trace = res2.trace_table() or res2.log[-800:]
                    continue
                return att

            prev = cand
            if res.status in (formal.ERROR, formal.SYNTAX, formal.NOINPUT):
                # A build failure is the generator's fault, not the design's.
                # Give it the yosys error line, not the banner.
                last_trace = key_error(res.log) or res.log[-600:]
            else:
                last_trace = res.trace_table() or key_error(res.log)
        return self.attempts[-1]
