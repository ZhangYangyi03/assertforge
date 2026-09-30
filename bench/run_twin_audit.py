"""How many accepted proofs are actually checking the design?

Every candidate the loop accepts as PROVED is re-run against the known-broken
twin of the same design. A proof that survives on a design known to be wrong is
not evidence about the design, and a tool that reports only "PROVED" cannot tell
the difference. This script produces that count, per candidate, from the recorded
transcripts -- so the number is reproducible and not a worked example.

    python bench/run_twin_audit.py

Writes bench/twin_audit.json and prints one line per candidate.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assertforge import formal, refine, vacuity  # noqa: E402
from run_ablation import DESIGNS, recorded_replies  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
def twin_of(name):
    """The other design in the pair: the correct one, or its broken twin.

    Derived from the file names rather than listed, so adding a pair to
    `bench/designs` adds it to the audit instead of being silently skipped.
    """
    if name.endswith("_buggy"):
        other = name[:-len("_buggy")]
    else:
        other = name + "_buggy"
    path = os.path.join(HERE, "designs", other + ".v")
    if not os.path.exists(path):
        return (None, None)
    # The module name is read out of the file rather than assumed from the file
    # name: `sync_fifo_buggy.v` declares `sync_fifo_buggy` while `counter_buggy.v`
    # deliberately declares `counter`. Assuming made the audit report ERROR for
    # half the twins, and an ERROR is silent -- it reads as "no evidence" rather
    # than as a bug in the audit.
    import re
    m = re.search(r"\bmodule\s+([A-Za-z_]\w*)",
                  open(path, encoding="utf-8", errors="replace").read())
    return ((m.group(1) if m else other), path)


def audit_candidate(design, code_src, tag, workdir, depth=12, timeout=300):
    dut = design["dut"]
    top = design["top"]
    os.makedirs(workdir, exist_ok=True)
    text = open(dut, encoding="utf-8", errors="replace").read()
    rep = refine.repair.normalise(code_src)

    def run(sources_text, topname, name):
        wd = os.path.join(workdir, name)
        os.makedirs(wd, exist_ok=True)
        w = refine.build_harness(sources_text, topname, rep.source, "af_" + name)
        open(os.path.join(wd, "af_%s.sv" % name), "w",
             encoding="utf-8", newline="\n").write(w)
        open(os.path.join(wd, "dut.v"), "w",
             encoding="utf-8", newline="\n").write(sources_text)
        return formal.run(wd, "af_" + name, ["dut.v"], ["af_%s.sv" % name],
                          depth=depth, timeout=timeout)

    main = run(text, top, "main")
    twin_name, twin_path = twin_of(design["name"])
    twin = None
    if twin_path:
        # The twin keeps the ORIGINAL module name -- `counter_buggy.v` declares
        # `module counter`, on purpose, so that the same wrapper and the same
        # assertion bind to it unchanged. Passing the file's name as the top
        # module is what made the first version of this audit report ERROR for
        # two of the four twins, and an ERROR read as "no evidence" instead of as
        # the bug in the audit itself.
        twin = run(open(twin_path, encoding="utf-8").read(), twin_name,
                   "twin")
    mut = vacuity.discriminating(dut, top, rep.source,
                                 os.path.join(workdir, "mut"),
                                 depth=depth, timeout=timeout, max_mutants=4,
                                 prefer=refine.signals_of(text))
    return {"design": design["name"], "tag": tag,
            "main": main.status, "twin": twin.status if twin else None,
            "rewrites": rep.summary(),
            "mutation_verdict": mut["verdict"],
            "mutants_refuted": mut["mutants_refuted"],
            "mutants_tried": mut["mutants_tried"],
            "accepted_but_missed_bug": bool(
                main.status == formal.PROVED and twin and twin.status == formal.PROVED),
            "source": rep.source}


def main():
    tpath = os.path.join(HERE, "transcripts.json")
    transcripts = json.load(open(tpath, encoding="utf-8")) if os.path.exists(tpath) else None
    rows = []
    weak_rows = []
    for d in DESIGNS:
        # 1) A control: the weakest property that still mentions a real signal --
        #    "cnt <= 4'd15", "state <= 2'd3". These are hand-written, not model
        #    output, and they are here as the null hypothesis the audit has to
        #    catch. The recorded candidates below are what the endpoint actually
        #    wrote, and they are reported separately because a control that comes
        #    from me is evidence about the check, not about the model.
        if d.get("weak"):
            r = audit_candidate(d, d["weak"], "%s_weak" % d["name"],
                                os.path.join(HERE, "_twin", "%s_weak" % d["name"]))
            r["is_handwritten_control"] = True
            weak_rows.append(r)
            print("%-18s %-8s main=%-8s twin=%-8s  <-- weak property from intent alone"
                  % (r["design"], "control", r["main"], r["twin"]))
        for prompt in ("grammar", "plain"):
            reps = recorded_replies(d, transcripts, prompt=prompt, sample=0) or []
            for i, src in enumerate(reps):
                tag = "%s_%s_s%d" % (d["name"], prompt, i)
                r = audit_candidate(d, src, tag,
                                    os.path.join(HERE, "_twin", tag))
                rows.append(r)
                print("%-18s %-8s main=%-8s twin=%-8s mutations=%-18s %s"
                      % (r["design"], r["tag"].split("_")[-2],
                         r["main"], r["twin"], r["mutation_verdict"],
                         "MISSED-BUG" if r["accepted_but_missed_bug"] else ""))
    all_rows = weak_rows + rows
    out = {"rows": rows, "weak_property_rows": weak_rows,
           "accepted_proofs": sum(1 for r in all_rows if r["main"] == "PROVED"),
           "accepted_that_missed_the_bug": sum(
               1 for r in all_rows if r["accepted_but_missed_bug"]),
           "weak_properties_proved_and_missed_the_bug": sum(
               1 for r in weak_rows if r["accepted_but_missed_bug"]),
           "designs_audited": len(DESIGNS)}
    with open(os.path.join(HERE, "twin_audit.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("")
    print("")
    print("%d of %d candidates PROVED something; %d of those proofs also survived "
          "against a design known to be broken"
          % (out["accepted_proofs"], len(all_rows), out["accepted_that_missed_the_bug"]))
    print("%d of %d hand-written weakest-possible controls behave that way "
          "(that is the check working, not the model failing)"
          % (out["weak_properties_proved_and_missed_the_bug"], len(weak_rows)))
    print("%d of %d recorded endpoint candidates did"
          % (sum(1 for r in rows if r["accepted_but_missed_bug"]),
             sum(1 for r in rows if r["main"] == "PROVED")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
