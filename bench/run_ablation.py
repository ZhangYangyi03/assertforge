"""The ablation: what does generate/repair/refine buy over asking once?

The README claims three things, and this script is the only thing that makes any
of them a measurement rather than a sentence:

  1. `repair` moves candidates that never reached the solver into candidates that
     did. (MALFORMED -> PROVED/REFUTED)
  2. `refine` finds claims that a one-shot generator cannot, because it is the
     only path that ever sees a counterexample.
  3. The loop is worth its extra solver runs -- reported as solver seconds per
     PROVED assertion, not as an opinion.

Two ways to run it, and the difference matters:

    python bench/run_ablation.py --mode fixture     # no API key, exact, fast
    python bench/run_ablation.py --mode live        # the real endpoint

Fixture mode exists because an ablation over a live model measures the model's
variance as much as the loop: the same intent produces a different assertion on
the next call, so two arms are not comparable. Fixture mode replays a recorded
transcript of what a model actually produces for each design, which makes the
arms differ by the thing being measured and nothing else. It also means the
headline table can be re-derived on a machine with no key at all, which is the
difference between a result and a claim.

Live mode is the same code path against the real endpoint, and its numbers are
reported separately rather than mixed in, because they are not reproducible.
"""

import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assertforge import formal, llm, refine  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DESIGNS_DIR = os.path.join(HERE, "designs")

# ---------------------------------------------------------------------------
# The transcripts. Each entry is what a model writes when asked for the intent
# in `intent` -- standard-SVA flavoured, exactly the shape `grammar.py` measures
# the backend to reject. They are the fixtures, and they are deliberately
# realistic: a transcript of already-correct assertions would make any loop look
# good.
# ---------------------------------------------------------------------------
DESIGNS = [
    {
        "name": "sync_fifo",
        "dut": os.path.join(DESIGNS_DIR, "sync_fifo.v"),
        "top": "sync_fifo",
        "intent": ("count never exceeds DEPTH and never goes below zero; "
                   "full is exactly count == DEPTH; empty is exactly count == 0"),
        "bug": False,
        # the known-broken twin: the decisive non-vacuity oracle. A property set
        # that PROVES against this design is not checking the intent at all.
        "twin": os.path.join(DESIGNS_DIR, "sync_fifo_buggy.v"),
        "twin_top": "sync_fifo_buggy",
        "replies": [
            # the industry-standard form, and a hard failure here
            "assert property (@(posedge clk) disable iff (rst) count <= DEPTH);",
            # the reset contract, written the way a person would
            "always @(posedge clk) if (!rst) begin\n"
            "    assert (count <= DEPTH);\n"
            "    assert (full == (count == DEPTH));\n"
            "    assert (empty == (count == 0));\n"
            "end",
            # after a counterexample about the power-on state
            "always @(posedge clk) if (af_started) begin\n"
            "    assert (count <= DEPTH);\n"
            "    assert (full == (count == DEPTH));\n"
            "    assert (empty == (count == 0));\n"
            "end",
        ],
    },
    {
        "name": "counter",
        "dut": os.path.join(DESIGNS_DIR, "counter.v"),
        "top": "counter",
        "intent": ("cnt advances by exactly one per enabled cycle; it is never "
                   "skipped and never jumps"),
        "bug": False,
        "twin": os.path.join(DESIGNS_DIR, "counter_buggy.v"),
        "twin_top": "counter",
        # The property a model writes from the intent alone, and it is the one
        # this whole audit is about: a four-bit count is never outside 0..15, so
        # this proves against the counter, against the counter with the skip bug,
        # and against any four-bit counter that will ever exist.
        "weak": "always @(posedge clk) if (af_started) assert (cnt <= 4'd15);",
        "replies": [
            "always @(posedge clk) if (af_started) assert (cnt >= 0 && cnt <= 4'd15);",
            "always @(posedge clk) if (af_started) begin\n"
            "    assert (cnt <= 4'd15);\n"
            "    assert (!($past(en) && !$past(rst)) || (cnt == $past(cnt) + 1'b1));\n"
            "end",
            "always @(posedge clk) if (af_started) assert "
            "(!($past(en) && !$past(rst)) || (cnt == $past(cnt) + 1'b1));",
        ],
    },
    {
        "name": "traffic_fsm",
        "dut": os.path.join(DESIGNS_DIR, "traffic_fsm.v"),
        "top": "traffic_fsm",
        "intent": ("the light never goes straight from red to green; every state "
                   "is one of the four legal values"),
        "bug": False,
        "twin": os.path.join(DESIGNS_DIR, "traffic_fsm_buggy.v"),
        "twin_top": "traffic_fsm",
        "weak": "always @(posedge clk) if (af_started) assert (state <= 2'd3);",
        "replies": [
            "always @(posedge clk) if (af_started) assert (state <= 2'd3);",
            "always @(posedge clk) if (af_started) assert "
            "(!(($past(state) == 2'd0) && (state == 2'd2)));",
            "always @(posedge clk) if (af_started) begin\n"
            "    assert (state <= 2'd3);\n"
            "    assert (!(($past(state) == 2'd0) && (state == 2'd2)));\n"
            "end",
        ],
    },
    {
        "name": "sync_fifo_buggy",
        "dut": os.path.join(DESIGNS_DIR, "sync_fifo_buggy.v"),
        "top": "sync_fifo_buggy",
        "intent": ("the FIFO is full exactly when count == DEPTH and empty "
                   "exactly when count == 0; count never exceeds DEPTH"),
        "bug": True,
        "twin": os.path.join(DESIGNS_DIR, "sync_fifo.v"),
        "twin_top": "sync_fifo",
        "replies": [
            "assert property (@(posedge clk) disable iff (rst) count <= DEPTH);",
            "always @(posedge clk) if (!rst) assert (full == (count == DEPTH));",
            "always @(posedge clk) if (af_started) begin\n"
            "    assert (full == (count == DEPTH));\n"
            "    assert (empty == (count == 0));\n"
            "end",
        ],
    },
    {
        "name": "rr_arbiter",
        "dut": os.path.join(DESIGNS_DIR, "rr_arbiter.v"),
        "top": "rr_arbiter",
        "intent": ("grant is one-hot or zero; grant is never issued to a "
                   "requester that did not ask; with no request grant is zero"),
        "bug": False,
        "replies": [
            # standard SVA again, and on this design also semantically wrong:
            # `grant` is a register, so at a clock edge it still reflects the
            # PREVIOUS request. The counterexample is a real one, not an artifact
            # of the harness, which is what makes this design the one that shows
            # what the refine path is for.
            "assert property (@(posedge clk) disable iff (rst) (grant & ~req) == 0);",
            # after the counterexample: compare against the sampled request
            "always @(posedge clk) if (af_started) assert ((grant & ~$past(req)) == 0);",
            # and the rest of the intent, which the first round never mentioned
            "always @(posedge clk) if (af_started) begin\n"
            "    assert ((grant & ~$past(req)) == 0);\n"
            "    assert ((|grant) ? ($past(req) != 0) : 1'b1);\n"
            "end",
        ],
    },
]

ARMS = {
    # the naive wrapper: one model call, its text goes to the solver as-is
    "oneshot_raw": dict(use_repair=False, use_refine=False, lint_retry=False),
    # what most LLM tools do: re-ask on a lint failure, then still one shot
    "lint_retry": dict(use_repair=False, use_refine=True, lint_retry=True),
    # deterministic repair, one round: the model still cannot learn anything
    "repair_once": dict(use_repair=True, use_refine=False, lint_retry=False),
    # the whole loop
    "repair_refine": dict(use_repair=True, use_refine=True, lint_retry=True),
}


TRANSCRIPTS = os.path.join(HERE, "transcripts.json")


def recorded_replies(design, transcripts, prompt="grammar", sample=0):
    """What the endpoint actually wrote for this design, if it was recorded.

    The fixture is the recording, not a hand-written transcript. That distinction
    is the difference between an experiment and an anecdote: these strings are
    what the endpoint returned when asked the question the loop asks, saved by
    `capture_transcripts.py`, including the ones that are wrong.

    `prompt` selects which system prompt was in force during recording, and that
    is the second axis of the experiment:

      grammar  the system prompt carries the measured subset (refine.SYSTEM)
      plain    "write industrial-style assertions", with none of it

    Repair is only load-bearing in the `plain` arm, because a model that has been
    told the subset already writes it -- measured, and it is why the two-prompt
    table exists at all.
    """
    if not transcripts:
        return None
    by = transcripts.get("by_prompt") or {}
    entry = (by.get(prompt) or {}).get(design["name"])
    if not entry:
        # older recordings kept designs at the top level
        entry = transcripts.get("designs", {}).get(design["name"])
    if not entry:
        return None
    samples = entry["samples"]
    if sample == "all":
        return [s["code"] for s in samples]
    return [samples[min(sample, len(samples) - 1)]["code"]]


def build_client(design, arm, mode, replies=None, live_client=None):
    replies = replies or design["replies"]
    if mode == "fixture":
        # In fixture mode the recorded text is already in its final form for the
        # arm, so the client returns it verbatim; the rewriter runs (or does not)
        # inside Session, which is the code path under test.
        return llm.replay_client("raw", replies)
    return llm.Client()


def run_arm(design, arm, mode, rounds=3, depth=12, timeout=300, workdir=None,
            transcripts=None, prompt="grammar", sample=0):
    cfg = ARMS[arm]
    replies = recorded_replies(design, transcripts, prompt=prompt, sample=sample)
    if not replies:
        # The recorded form of this arm is a two-candidate sequence: the naive
        # candidate first, then the corrected one, which is exactly the transcript
        # the refine path would have produced.
        replies = design["replies"]
    client = build_client(design, arm, mode, replies=replies)
    wd = os.path.join(workdir or os.path.join(HERE, "_ablate"),
                      "%s_%s" % (design["name"], arm))
    t0 = time.time()
    sess = refine.Session(
        dut_path=design["dut"], top=design["top"], intent=design["intent"],
        n_assertions=2, rounds=rounds, client=client, workdir=wd,
        depth=depth, timeout=timeout,
        twin_path=design.get("twin"), twin_top=design.get("twin_top"), **cfg)
    try:
        best = sess.run()
    except Exception as e:                    # a fixture that broke the loop
        return {"design": design["name"], "arm": arm, "outcome": "LOOPERROR",
                "error": "%s: %s" % (type(e).__name__, e),
                "wall_s": round(time.time() - t0, 1)}
    out = refine.outcome_of(best, design["bug"])
    solver_s = sum(a.result.seconds for a in sess.attempts)
    hist = Counter()
    for a in sess.attempts:
        if a.repair:
            hist.update(a.repair.applied)
    twin = (best.twin if best is not None else None)
    return {
        "design": design["name"], "arm": arm, "outcome": out,
        "prompt": prompt, "sample": sample,
        "use_repair": cfg["use_repair"], "use_refine": cfg["use_refine"],
        "twin": twin,
        "first_candidate_malformed": (
            bool(sess.attempts) and refine.outcome_of(sess.attempts[0]) == "MALFORMED"),
        "solver_runs": len(sess.attempts),
        "solver_s": round(solver_s, 2),
        "wall_s": round(time.time() - t0, 1),
        "llm_calls": sess.llm_calls,
        "rewrites": dict(hist),
        "final_source": (best.source if best else ""),
        "verdicts": [a.verdict for a in sess.attempts],
    }


def live_reachable(timeout=5):
    try:
        c = llm.Client(timeout=timeout, max_retries=1)
        c.chat("Reply with the single word OK.", max_tokens=8)
        c.calls -= 1
        return True, c.model
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    p.add_argument("--arms", default=",".join(ARMS))
    p.add_argument("--designs", default="")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--depth", type=int, default=12)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--json", default=os.path.join(HERE, "ablation.json"))
    p.add_argument("--workdir", default=None)
    p.add_argument("--prompt", choices=["grammar", "plain"], default="grammar",
                   help="which recorded system prompt the candidates came from")
    p.add_argument("--sample", default=0,
                   help="which recorded sample to replay (0, 1, or all)")
    args = p.parse_args(argv)

    designs = [d for d in DESIGNS
               if not args.designs or d["name"] in args.designs.split(",")]
    arms = [a for a in args.arms.split(",") if a]

    print("mode: %s" % args.mode)
    if args.mode == "live":
        ok, what = live_reachable()
        print("llm endpoint: %s (%s)" % ("reachable" if ok else "UNAVAILABLE", what))
        if not ok:
            print("live mode needs a working endpoint; falling back is NOT done "
                  "silently -- rerun with --mode fixture for the reproducible table")
            return 1

    transcripts = None
    src = "fixture (hand-written)"
    if args.mode == "fixture" and os.path.exists(TRANSCRIPTS):
        transcripts = json.load(open(TRANSCRIPTS, encoding="utf-8"))
        src = "recorded %s from %s" % (transcripts.get("recorded_at"),
                                       transcripts.get("endpoint"))
    elif os.path.exists(TRANSCRIPTS):
        transcripts = json.load(open(TRANSCRIPTS, encoding="utf-8"))
        src = "recorded %s from %s" % (transcripts.get("recorded_at"),
                                       transcripts.get("endpoint"))
    print("candidates: %s" % src)

    results = []
    for arm in arms:
        for d in designs:
            r = run_arm(d, arm, args.mode, rounds=args.rounds,
                        depth=args.depth, timeout=args.timeout,
                        workdir=args.workdir, transcripts=transcripts,
                        prompt=args.prompt,
                        sample=(int(args.sample) if str(args.sample).isdigit()
                                else args.sample))
            results.append(r)
            print("  %-14s %-18s %-10s solver_runs=%d solver_s=%.2f  %s"
                  % (arm, d["name"], r["outcome"], r.get("solver_runs", 0),
                     r.get("solver_s", 0.0),
                     ("rewrites=" + ",".join(sorted(r.get("rewrites", {}))))
                     if r.get("rewrites") else ""))

    # -- the table ----------------------------------------------------------
    print("")
    print("outcome by arm (FX = solver-driven firmware outcome)")
    hdr = "%-14s" % "arm" + "".join("%-20s" % d["name"] for d in designs) + "  PROVED%  solver_s"
    print(hdr)
    for arm in arms:
        row = "%-14s" % arm
        proved = 0
        tot_s = 0.0
        for d in designs:
            r = next(x for x in results
                     if x["arm"] == arm and x["design"] == d["name"])
            row += "%-20s" % r["outcome"]
            proved += 1 if r["outcome"] == "PROVED" else 0
            tot_s += r.get("solver_s", 0.0)
        row += "  %-7s %.1f" % ("%d/%d" % (proved, len(designs)), tot_s)
        print(row)

    payload = {"mode": args.mode, "rounds": args.rounds, "depth": args.depth,
               "candidate_source": src, "prompt_arm": args.prompt,
               "sample": str(args.sample),
               "designs": [d["name"] for d in designs], "arms": arms,
               "results": results}
    if args.mode == "live":
        ok, what = live_reachable()
        payload["endpoint_reachable_after"] = ok
        payload["endpoint"] = what
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print("")
    print("wrote %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
