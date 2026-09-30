"""CLI. Kept thin -- everything with a claim in it lives in the modules."""

import argparse
import json
import os
import sys

from . import compose, formal, grammar, llm, refine, repair, vacuity


BANNER = "assertforge %s" % __import__("assertforge").__version__


def cmd_doctor(args):
    ok, ver = formal.toolchain_check()
    print(BANNER)
    print("solver reachable : %s" % ("yes" if ok else "NO"))
    print("toolchain        : %s" % (ver or "(nothing answered)"))
    print("grammar measured : %s" % grammar.MEASURED_ON)
    try:
        c = llm.Client(model=args.model) if args.model else llm.Client()
        r = c.chat("Reply with the single word OK.", max_tokens=12)
        print("llm endpoint     : %s" % c.model)
        print("llm round-trip   : %s (%.2fs)" % (r.strip()[:20], c.seconds))
    except Exception as e:
        print("llm endpoint     : UNAVAILABLE -- %s" % e)
    return 0 if ok else 1


def cmd_grammar(args):
    if args.measure:
        script = grammar.measure_script()
        out, rc = formal.wsl_bash(script, timeout=600)
        print(out)
        return rc
    print(json.dumps(json.loads(grammar.as_json()), indent=2))
    return 0


def cmd_run(args):
    sess = refine.Session(
        dut_path=args.dut, top=args.top, intent=args.intent,
        n_assertions=args.n, rounds=args.rounds,
        client=llm.Client(model=args.model) if args.model else llm.Client(),
        workdir=args.workdir, depth=args.depth, timeout=args.timeout)
    best = sess.run()
    print(BANNER)
    print("design   : %s (top=%s)" % (args.dut, args.top))
    print("intent   : %s" % args.intent)
    print("llm calls: %d" % sess.llm_calls)
    print("")
    for a in sess.attempts:
        print("  round %d: %s" % (a.index, a.result.summary()) if False else
              "  round %-2d %-8s %.2fs" % (a.index, a.verdict, a.result.seconds))
    print("")
    print("--- accepted assertions ---")
    print(best.source)
    if best.result.trace:
        print("")
        print("--- counterexample (why it was last rejected) ---")
        print(best.result.trace_table())
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"design": args.dut, "top": args.top, "intent": args.intent,
                       "verdict": best.verdict, "accepted": best.source,
                       "attempts": [a.to_dict() for a in sess.attempts],
                       "llm": sess.client.stats()}, f, indent=2)
        print("")
        print("wrote %s" % args.json)
    return 0 if best.verdict == formal.PROVED else 2




def cmd_repair(args):
    """Show what the deterministic rewriter does to a candidate, without a solver."""
    src = args.file and open(args.file, encoding="utf-8").read() or args.source
    r = repair.normalise(src)
    print("--- input ---")
    print(src.strip())
    print("")
    print("--- lint (what the backend would have rejected) ---")
    problems = grammar.lint(src)
    print("\n".join("- " + p for p in problems) if problems else "(nothing)")
    print("")
    print("--- repaired ---")
    print(r.source)
    print("")
    print("rewrites: %s" % r.summary())
    print("lint after repair: %s" % (grammar.lint(r.source) or "(clean)"))
    return 0


def cmd_twin(args):
    """Run one assertion against a design known to be broken.

    The point of the subcommand is that the check is available without the loop:
    a person can take an assertion they wrote by hand and ask the one question a
    proof cannot answer -- would this have caught the bug we already know about?
    """
    src = open(args.assertions, encoding="utf-8").read()
    r = repair.normalise(src)
    print("assertion after repair: %s" % r.summary())
    res = vacuity.against_twin(args.twin, args.twin_top or args.top, r.source,
                              args.workdir or "_af_twin", depth=args.depth,
                              timeout=args.timeout)
    print("twin            : %s" % args.twin)
    print("twin verdict    : %s" % res["twin_status"])
    print("reading         : %s" % res["verdict"])
    print("")
    print({"CAUGHT_BUG": "the assertion catches the known bug -- it is checking "
                        "the design",
           "MISSED_BUG": "the assertion PROVED on a design known to be broken -- "
                         "it is not checking anything this bug can move",
           "TWIN_UNUSABLE": "the twin did not build; no evidence either way"}[res["verdict"]])
    return 0 if res["verdict"] == "CAUGHT_BUG" else 2



def cmd_compose(args):
    """Check the assembly, not the blocks.

    Prints the generated top-level file, because the file IS the result: a diff of
    two generated files is the diff of two assemblies, and that is the artifact a
    reviewer can argue with.
    """
    spec = compose.load_spec(args.spec)
    texts = compose.block_texts_for(spec)
    print(BANNER)
    print("assembly  : %s" % spec["name"])
    if spec.get("comment"):
        print("           %s" % spec["comment"])
    print("blocks    : %s" % ", ".join("%s (as %s)"
                                       % (i["module"], i["inst"]) for i in spec["instances"]))
    ports = compose.check_ports(spec, texts)
    print("interface : %s" % ("clean" if not ports else "%d issue(s)" % len(ports)))
    if ports:
        print(compose.report_ports(ports))
    bad = compose.lint_assertions(spec)
    print("seam lint : %s" % (bad if bad else "clean -- every seam assertion is in the "
                                              "subset the backend parses"))
    res, top = compose.run(spec, texts, args.workdir or "_af_compose",
                           depth=args.depth, timeout=args.timeout)
    print("")
    print(top)
    print("--- verdict on the assembly ---")
    print("%s in %.2fs" % (res.status, res.seconds))
    if res.status == "REFUTED":
        print("")
        print("The wiring does not support the seam claims. Which cycle:")
        print(res.trace_table(max_rows=args.rows))
    elif res.status == "ERROR":
        print("")
        print("The assembly did not build. This is the tool refusing to call an "
              "unbuilt assembly a pass:")
        print((res.to_dict().get('log_tail') or '')[-1200:])
    if args.json:
        import json as _json
        with open(args.json, "w", encoding="utf-8") as f:
            _json.dump({"spec": args.spec, "assembly": spec["name"],
                        "status": res.status, "seconds": res.seconds,
                        "generated_top": top,
                        "assertions": [a["name"] for a in spec.get("assertions", [])],
                        "log_tail": (res.to_dict().get("log_tail") or "")[-4000:]},
                      f, indent=2)
    return 0 if res.status == "PROVED" else 2


def main(argv=None):
    p = argparse.ArgumentParser(prog="assertforge", description=__doc__)
    p.add_argument("--model", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="is the solver and the model endpoint alive")
    d.set_defaults(func=cmd_doctor)

    g = sub.add_parser("grammar", help="the measured supported/unsupported matrix")
    g.add_argument("--measure", action="store_true",
                   help="re-run the matrix against the live toolchain")
    g.set_defaults(func=cmd_grammar)

    r = sub.add_parser("run", help="generate -> prove -> refine for one design")
    r.add_argument("--dut", required=True)
    r.add_argument("--top", required=True)
    r.add_argument("--intent", required=True)
    r.add_argument("-n", type=int, default=2)
    r.add_argument("--rounds", type=int, default=4)
    r.add_argument("--depth", type=int, default=10)
    r.add_argument("--timeout", type=int, default=180)
    r.add_argument("--workdir", default=None)
    r.add_argument("--json", default=None)
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("repair",
                        help="what the deterministic rewriter does to a candidate")
    rp.add_argument("--file", default=None)
    rp.add_argument("source", nargs="?", default="")
    rp.set_defaults(func=cmd_repair)

    tw = sub.add_parser("twin", help="would this assertion have caught a known bug")
    tw.add_argument("--assertions", required=True)
    tw.add_argument("--twin", required=True)
    tw.add_argument("--top", required=True)
    tw.add_argument("--twin-top", default=None)
    tw.add_argument("--depth", type=int, default=12)
    tw.add_argument("--timeout", type=int, default=300)
    tw.add_argument("--workdir", default=None)
    tw.set_defaults(func=cmd_twin)

    cp = sub.add_parser("compose",
                        help="prove/refute seam assertions on an assembly of blocks")
    cp.add_argument("--spec", required=True,
                    help="integration spec: which blocks, how wired, which seam claims")
    cp.add_argument("--depth", type=int, default=14)
    cp.add_argument("--timeout", type=int, default=300)
    cp.add_argument("--rows", type=int, default=12)
    cp.add_argument("--workdir", default=None)
    cp.add_argument("--json", default=None)
    cp.set_defaults(func=cmd_compose)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
