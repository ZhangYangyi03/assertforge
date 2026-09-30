"""Deterministic repair: turn a model's standard-SVA instinct into what the
backend actually accepts.

This module exists because of a measured asymmetry, not an opinion. The industry
standard for hardware assertions is SVA (IEEE 1800). The open-source formal flow
implements a strict subset of it -- `grammar.py` holds the measured matrix, and
the gap is where a language model fails hardest, because a model is trained on
SVA and the backend is not SVA.

The naive answer is "ask again and hope". The measured answer is that most of the
gap is mechanical: `a |-> b` is `(!a) || b` on the same cycle, `$rose(x)` is
`x && !$past(x)`, `disable iff (rst)` has an equivalent guarded form, and a
bare module-level assertion just needs a clock and a reset prologue. Every one of
those is a rewrite a program can do, with no model in the loop.

So the loop becomes: generate once (the model's job, because the *claim* needs
judgement), repair deterministically (a program's job, because the *syntax* does
not), then hand the solver the only thing a program cannot decide -- whether the
claim is true of the design.

Every rewrite is recorded by name, so its effect is measurable rather than
asserted. `assertforge ablate --arms raw,repair` is what measures it.
"""

import re

# Rewrites that fire, in the order they are attempted. The names are the keys in
# the ablation histogram, so they are part of the tool's output format.
APPLIED_ORDER = [
    "unwrap_module", "drop_clocking_event", "drop_disable_iff",
    "implication_to_boolean", "rose_expand", "fell_expand", "stable_expand",
    "property_block_inlined", "next_cycle_implication",
    "assert_property_to_immediate",
    "guard_always_block", "guard_bare_statement",
]


class Repair:
    """The result of one normalisation: the source, and what had to be done."""

    def __init__(self, source, applied=None, notes=None):
        self.source = source
        self.applied = list(applied or [])
        self.notes = list(notes or [])

    @property
    def changed(self):
        return bool(self.applied)

    def to_dict(self):
        from collections import Counter
        return {"applied": self.applied,
                "histogram": dict(Counter(self.applied)),
                "notes": self.notes,
                "source": self.source}

    def summary(self):
        if not self.applied:
            return "no rewrite needed"
        from collections import Counter
        c = Counter(self.applied)
        return ", ".join("%s%s" % (k, ("x%d" % v) if v > 1 else "")
                         for k, v in sorted(c.items()))


def _scan_calls(src):
    """Find assert/assume/cover calls with their balanced argument spans.

    Parenthesis-aware, because `assert ((a && b) || c);` is the normal shape and
    a regex that stops at the first `)` silently truncates the claim -- which
    produces a *wrong assertion that still proves*, the worst possible failure.
    """
    out = []
    for m in re.finditer(r"\b(assert|assume|cover)\s*(?:property\s*)?\(", src):
        open_at = src.index("(", m.end() - 1)
        depth, j = 0, open_at
        while j < len(src):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            continue
        out.append({"kind": m.group(1), "call_start": m.start(),
                    "arg_start": open_at + 1, "arg_end": j,
                    "arg": src[open_at + 1:j]})
    return out


def _top_level_find(s, op):
    """Index of `op` outside any bracket depth, or None."""
    depth = 0
    i = 0
    while i <= len(s) - len(op):
        c = s[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and s.startswith(op, i):
            # `->` inside `-` `>` is ambiguous with a subtraction, but inside an
            # assertion argument a comparison never has a bare `->`; measured
            # against every form in grammar.REJECTED.
            return i
        i += 1
    return None


_CLOCK_EVENT = re.compile(r"^@\s*\((?:[^()]|\([^()]*\))*\)\s*")
_DISABLE_IFF = re.compile(r"\bdisable\s+iff\s*\((?:[^()]|\([^()]*\))*\)\s*")
_ROSE = re.compile(r"\$rose\s*\(([^()]*(?:\([^()]*\))?[^()]*)\)")
_FELL = re.compile(r"\$fell\s*\(([^()]*(?:\([^()]*\))?[^()]*)\)")
_STABLE = re.compile(r"\$stable\s*\(([^()]*(?:\([^()]*\))?[^()]*)\)")


def _rewrite_arg(arg, applied):
    a = arg.strip()

    m = _CLOCK_EVENT.match(a)
    if m:
        a = a[m.end():].strip()
        applied.append("drop_clocking_event")

    while True:
        m = _DISABLE_IFF.search(a)
        if not m:
            break
        a = (a[:m.start()] + a[m.end():]).strip()
        applied.append("drop_disable_iff")

    for op in ("|->", "|=>", "->"):
        idx = _top_level_find(a, op)
        if idx is not None:
            lhs, rhs = a[:idx].strip(), a[idx + len(op):].strip()
            # `a |-> ##N b` is not the same claim as `a |-> b`, and silently
            # dropping the delay would swap a timing property for a combinational
            # one -- a rewrite that changes the claim is worse than a rejection,
            # because it proves something the user did not ask for. The delay is
            # instead made explicit with $past, which IS supported.
            m = re.match(r"^##\s*(\d+)\s*(.*)$", rhs, re.S)
            if m and op in ("|->", "|=>"):
                n, rest = int(m.group(1)), m.group(2).strip()
                if n >= 1:
                    shifted = lhs if n == 1 else "$past(%s, %d)" % (lhs, n)
                    if n == 1:
                        shifted = "$past(%s)" % lhs
                    a = "(!(%s)) || (%s)" % (shifted, rest)
                    applied.append("next_cycle_implication")
                    break
            a = "(!(%s)) || (%s)" % (lhs, rhs)
            applied.append("implication_to_boolean")
            break

    n = len(_ROSE.findall(a))
    if n:
        a = _ROSE.sub(lambda m: "((%s) && !$past(%s))" % (m.group(1).strip(), m.group(1).strip()), a)
        applied.extend(["rose_expand"] * n)

    n = len(_FELL.findall(a))
    if n:
        a = _FELL.sub(lambda m: "(!(%s) && $past(%s))" % (m.group(1).strip(), m.group(1).strip()), a)
        applied.extend(["fell_expand"] * n)

    n = len(_STABLE.findall(a))
    if n:
        a = _STABLE.sub(lambda m: "((%s) == $past(%s))" % (m.group(1).strip(), m.group(1).strip()), a)
        applied.extend(["stable_expand"] * n)

    return a


def _unwrap_module(src, applied):
    """Keep only the inside of a `module ... endmodule` if the model wrote one.

    The harness supplies the module, its parameters and its ports. A model that
    also emits a module header is not wrong, it is just answering a different
    question -- and pasting it inside another module is a parse error, so this
    has to be stripped before anything else runs.
    """
    m = re.search(r"\bmodule\b.*?\bendmodule\b", src, re.S)
    if not m:
        return src
    body = m.group(0)
    body = re.sub(r"^\s*module\b[^;]*;", "", body, count=1, flags=re.S)
    body = re.sub(r"\bendmodule\s*$", "", body.strip(), count=1)
    applied.append("unwrap_module")
    return body.strip()


def _inline_property_blocks(src, applied):
    """`property p; @(posedge clk) X; endproperty` + `assert property (p);`

    A named property is a named bundle of the same expression; inlining it is
    exact, not heuristic. If the block cannot be parsed it is left alone and the
    caller falls back to asking the model, because guessing here would change the
    claim.
    """
    blocks = {}
    def grab(m):
        name, body = m.group(1), m.group(2)
        m2 = _CLOCK_EVENT.match(body.strip())
        if m2:
            body = body.strip()[m2.end():]
        blocks[name] = body.strip().rstrip(";")
        return ""
    src = re.sub(r"\bproperty\s+(\w+)\s*;(.*?)\bendproperty\b", grab, src, flags=re.S)
    if blocks:
        applied.append("property_block_inlined")
    for name, body in blocks.items():
        src = re.sub(r"\bassert\s+property\s*\(\s*%s\s*\)" % re.escape(name),
                     "assert (%s)" % body, src)
    return src


def _guard_always(src, applied):
    """Every assertion about the design needs the `af_started` guard.

    Measured, and it is the most common failure: at cycle 0 the DUT powers up in
    an arbitrary state, so an unguarded claim about an output is refuted by a
    counterexample no correct design can reach. The prologue that fixes it is in
    the harness; applying the guard is mechanical.

    Three shapes have to be covered, and missing any one of them leaves an
    assertion proved against a power-on state the design never occupies:
      always @(posedge clk) assert (...)            the direct form
      always @(posedge clk) begin ... end           the block form
      always @(posedge clk) if (cond) assert (...)  the conditional form, where
                                                    the guard goes outside the
                                                    condition so the condition
                                                    is still evaluated
    """
    if "af_started" in src:
        return src
    # One pass, with a negative lookahead. Two passes double-guard: the first
    # inserts `if (af_started)`, the second sees the resulting `always (...) if (`
    # and wraps it again -- `if (af_started) if (af_started)`, which builds and
    # behaves identically while inflating every rewrite count in the ablation.
    src, n = re.subn(
        r"(always\s*@\s*\(\s*posedge\s+clk\s*\)\s*)"
        r"(?!if \(af_started\))(assert\b|assume\b|cover\b|begin\b|if\s*\()",
        lambda m: m.group(1) + "if (af_started) " + m.group(2), src)
    applied.extend(["guard_always_block"] * _count_guards(src))
    return src


_GUARDED = re.compile(r"always\s*@\s*\(\s*posedge\s+clk\s*\)\s*if \(af_started\)")


def _count_guards(src):
    return len(_GUARDED.findall(src))


def _guard_bare(src, applied):
    """A module-level `assert (e);` has no clock. Give it one.

    This is the shape a model produces when it decides the claim is an
    invariant: correct SVA intent, and in a `read -formal` file it is a
    combinational assertion with no reset semantics at all -- it is checked
    against every input combination from time zero, which is not what the author
    meant.

    Balanced-paren scan rather than a regex, because `assert ((a) || (b));` is
    normal and a regex that stops at the first `)` truncates it.
    """
    if "af_started" in src:
        return src
    calls = _scan_calls(src)
    out = src
    for c in reversed(calls):
        stmt_start = out.rfind(";", 0, c["call_start"]) + 1
        stmt_end = out.find(";", c["arg_end"]) + 1
        if stmt_end <= 0:
            continue
        prefix = out[stmt_start:c["call_start"]]
        if "always" in prefix or "af_started" in prefix:
            continue
        if prefix.strip() and not prefix.strip().startswith(c["kind"]):
            # some other construct owns this statement; leave it to the solver
            continue
        stmt = out[stmt_start:stmt_end].strip()
        out = (out[:stmt_start] + "\n    always @(posedge clk) if (af_started) "
               + stmt + out[stmt_end:])
        applied.append("guard_bare_statement")
    return out


def normalise(src, guard=True):
    """Rewrite `src` into the accepted subset. Returns a Repair."""
    applied, notes = [], []
    s = _unwrap_module(src.strip(), applied)
    s = _inline_property_blocks(s, applied)

    calls = _scan_calls(s)
    for c in reversed(calls):
        new_arg = _rewrite_arg(c["arg"], applied)
        if new_arg != c["arg"]:
            s = s[:c["arg_start"]] + new_arg + s[c["arg_end"]:]

    # A statement inside an `always` block has its guard; a statement that is
    # still at module level after the clock event was stripped does not. The
    # distinction has to be made on the source, not per argument: stripping
    # `@(posedge clk)` from `always @(posedge clk) assert property (...)` leaves
    # an assert that is syntactically inside a block and must NOT be wrapped,
    # or the result is an `always` inside an `always` -- which is exactly the
    # bug this branch was written to remove.
    if re.search(r"assert\s+property", s):
        s = re.sub(r"assert\s+property\s*\(", "assert (", s)
        applied.append("assert_property_to_immediate")
    if guard:
        s = _guard_always(s, applied)
        s = _guard_bare(s, applied)

    if not re.search(r"\b(assert|assume|cover)\b", s):
        notes.append("no assertion survived repair")
    return Repair(s.strip(), applied, notes)
