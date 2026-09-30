"""Vacuity, decided by mutation rather than by reading the source.

The existing check in `refine.mentions_dut_signal` is a heuristic and it has a
measured hole: `assert (count <= DEPTH)` on a design whose `count` is two bits
wide mentions a real signal, so it passes -- and it is trivially true, because
two bits cannot hold four. It proves, no counterexample exists, and the loop
reports PROVED. That is the exact failure mode this project exists to prevent,
turned on itself.

A proof is only evidence if the property could have failed. So instead of asking
whether the assertion mentions the design, ask the question the solver can
answer: **would this property have caught a bug?**

That is checkable. Take the design, mutate one assignment in it, and re-run the
same assertion against the mutant with the same harness. A property that proves
against a broken design was not checking the design. One REFUTED mutant is
enough to show the property discriminates; PROVED against every mutant is a flag,
not a verdict, because a mutation set is always incomplete.

This is not a proof of non-vacuity either -- nothing on this budget is -- and the
report says so. It is a much better filter than a substring match, and it is
falsifiable: the tests contain a property that the heuristic passes and the
mutation check refuses.
"""

import os
import re

from . import formal, refine

# Statement-level scanning, not line-level: `count <= 0; wr_ptr <= 0;` is one
# line with two assignments, and a line-based mutator produced broken Verilog for
# it -- measured, and it silently removed the mutation that mattered.
STMT = re.compile(r"(?P<lhs>[A-Za-z_]\w*)\s*<=\s*(?P<rhs>[^;]+);")

# Each mutation is (name, function over the right-hand side). They are chosen to
# be plausible bugs rather than noise: an off-by-one in a counter, a saturated
# flag that stops being a function of its inputs, a constant. A random bit-flip
# inside an arbitrary expression mostly produces designs that no longer
# elaborate, which measures the mutator instead of the property.
MUTATIONS = [
    ("off_by_one", lambda rhs: "(%s) + 1'b1" % rhs),
    ("invert", lambda rhs: "~(%s)" % rhs),
    ("stuck_low", lambda rhs: "1'b0"),
]


def _in_reset_branch(text, at):
    """Crude but sufficient: is this statement inside an `if (rst)` branch?

    Mutating the reset value produces a mutant that the harness's reset
    assumption legalises, so the mutant is unfalsifiable no matter how good the
    property is -- it measures the mutator, not the assertion. Skipping it is
    what keeps the gate honest rather than merely strict.
    """
    window = text[max(0, at - 400):at]
    k = window.rfind("if (rst")
    if k < 0:
        k = window.rfind("if (!rst")
    if k < 0:
        return False
    return "end" not in window[k:]


def mutations_of(dut_text, max_mutants=6, prefer=None):
    """Single-statement mutations, as (name, span, replacement, lhs).

    `prefer` is the list of design signals the assertion mentions; mutations that
    touch one are tried first, because a mutation that cannot move the verdict is
    wasted solver time.
    """
    out = []
    for m in STMT.finditer(dut_text):
        lhs, rhs = m.group("lhs"), m.group("rhs").strip()
        if lhs in ("clk", "todo"):
            continue
        if _in_reset_branch(dut_text, m.start()):
            continue
        for name, fn in MUTATIONS:
            new_rhs = fn(rhs)
            if new_rhs == rhs:
                continue
            out.append(("%s@%s" % (name, lhs), m.span("rhs"), new_rhs, lhs))
    if prefer:
        pref = [x for x in out if x[3] in prefer]
        rest = [x for x in out if x[3] not in prefer]
        out = pref + rest
    return out[:max_mutants]


def discriminating(dut_path, top, assertion_src, workdir, depth=12, timeout=300,
                   max_mutants=6, prefer=None, min_mutants=1):
    """Run `assertion_src` against mutated versions of the design.

    Returns a dict:
      mutants_tried     how many mutations actually elaborated
      mutants_refuted   how many the assertion caught
      discriminating    True if at least one was caught
      verdict           DISCRIMINATING | NON_DISCRIMINATING | UNDECIDED
      detail            per-mutant one-liners, so a negative can be inspected
    """
    dut_text = open(dut_path, encoding="utf-8", errors="replace").read()
    muts = mutations_of(dut_text, max_mutants, prefer=prefer)
    lines = dut_text.splitlines()
    os.makedirs(workdir, exist_ok=True)
    tried, refuted, detail = 0, 0, []
    for name, span, new_rhs, lhs in muts:
        mutated = dut_text[:span[0]] + new_rhs + dut_text[span[1]:]
        tag = "mut_" + re.sub(r"[^A-Za-z0-9_]", "_", name)
        wd = os.path.join(workdir, tag)
        os.makedirs(wd, exist_ok=True)
        wrapper = refine.build_harness(mutated, top, assertion_src, "af_" + tag)
        open(os.path.join(wd, "af_%s.sv" % tag), "w",
             encoding="utf-8", newline="\n").write(wrapper)
        open(os.path.join(wd, "dut.v"), "w",
             encoding="utf-8", newline="\n").write(mutated)
        res = formal.run(wd, "af_" + tag, ["dut.v"], ["af_%s.sv" % tag],
                         depth=depth, timeout=timeout)
        if res.status in (formal.ERROR, formal.SYNTAX, formal.NOINPUT):
            detail.append("%-18s %-8s (did not elaborate -- no evidence)" % (name, res.status))
            continue
        tried += 1
        if res.status == formal.REFUTED:
            refuted += 1
        detail.append("%-18s %-8s %s" % (name, res.status, lhs))

    if tried == 0:
        verdict = "UNDECIDED"
    elif refuted:
        verdict = "DISCRIMINATING"
    else:
        verdict = "NON_DISCRIMINATING"
    return {"mutants_tried": tried, "mutants_refuted": refuted,
            "discriminating": bool(refuted), "verdict": verdict,
            "detail": detail}

    # `client` is accepted and unused: mutation does not need a model, which is
    # the point -- this is a gate the solver closes on its own.


def against_twin(twin_path, top, assertion_src, workdir, depth=12, timeout=300,
                 twin_top=None):
    """Run an accepted assertion against a design that is known to be broken.

    This is the only non-vacuity check here that is decisive rather than a flag,
    because the mutant is not generated -- it is ground truth. `bench/designs`
    ships `sync_fifo_buggy.v`, the same FIFO with `count` narrowed to two bits,
    and a property set that PROVES against the broken twin is not checking the
    intent it claims to check, no matter how many signals it mentions.

    Returns CAUGHT_BUG, MISSED_BUG, or TWIN_UNUSABLE (the twin did not build).
    """
    os.makedirs(workdir, exist_ok=True)
    twin_text = open(twin_path, encoding="utf-8", errors="replace").read()
    # Read the module name out of the twin instead of trusting the filename or
    # the caller. The two conventions are both legitimate and both are in
    # `bench/designs`: a twin named `sync_fifo_buggy.v` declares
    # `sync_fifo_buggy`, while a twin named `counter_buggy.v` declares `counter`
    # on purpose so the same wrapper binds to it. Guessing produced ERROR for half
    # the twins, and an ERROR is the worst possible outcome here -- it prints as
    # `TWIN_UNUSABLE`, which reads as "no evidence" when the truth is that the
    # check never ran.
    m = re.search(r"\bmodule\s+([A-Za-z_]\w*)", twin_text)
    detected = m.group(1) if m else top
    resolved_top = twin_top or detected
    wrapper = refine.build_harness(twin_text, resolved_top, assertion_src, "af_twin")
    open(os.path.join(workdir, "af_twin.sv"), "w",
         encoding="utf-8", newline="\n").write(wrapper)
    open(os.path.join(workdir, "dut.v"), "w",
         encoding="utf-8", newline="\n").write(twin_text)
    res = formal.run(workdir, "af_twin", ["dut.v"], ["af_twin.sv"],
                     depth=depth, timeout=timeout)
    verdict = {"PROVED": "MISSED_BUG", "REFUTED": "CAUGHT_BUG"}.get(
        res.status, "TWIN_UNUSABLE")
    return {"verdict": verdict, "twin_status": res.status,
            "seconds": round(res.seconds, 2),
            "trace_cycles": len(res.trace)}
