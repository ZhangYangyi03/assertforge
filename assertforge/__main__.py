"""CLI. Kept thin -- everything with a claim in it lives in the modules."""

import argparse
import json
import os
import sys

from . import formal, grammar, llm, refine


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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
