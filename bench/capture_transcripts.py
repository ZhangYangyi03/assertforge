"""Record what a real endpoint writes, so the ablation can replay it exactly.

The ablation's arms must differ by the thing being measured -- the loop -- and not
by the model's run-to-run variance. The way to get both honesty and
reproducibility is to record the model once and replay it, rather than to invent a
transcript that flatters the loop.

This is the recording step, and it is deliberately dumb: ask the same prompt the
loop would ask, and save the raw text.

Two system prompts are recorded, because that is the experiment's second axis:

  grammar   the system prompt carries the measured subset (refine.SYSTEM)
  plain     "write industrial-style assertions", with none of it

    python bench/capture_transcripts.py                      # both prompts, all designs
    python bench/capture_transcripts.py --designs counter    # one design
    python bench/capture_transcripts.py --merge              # add to the existing file
    python bench/run_ablation.py --mode fixture              # replay it

The file is checked in. Re-recording is a deliberate act, because a table that
changes whenever the endpoint changes is not a result.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from assertforge import llm, refine  # noqa: E402
from run_ablation import DESIGNS  # noqa: E402

# The second prompt arm. This is what a typical tool asks for -- "write assertions
# for this design" -- with none of the measured subset in it. The difference
# between this and refine.SYSTEM is the whole question the ablation answers: is
# the grammar knowledge doing the work, or is the rewriter?
PLAIN_SYSTEM = """You are a hardware verification engineer.
Write SystemVerilog assertions for the design under test that capture the stated
intent, in the style used by industrial formal verification tools. Output a single
fenced ```systemverilog block containing only the assertions."""


def capture(design, samples=1, temperature=0.2, model=None, prompt="grammar"):
    client = (llm.Client(model=model, temperature=temperature) if model
              else llm.Client(temperature=temperature))
    dut = open(design["dut"], encoding="utf-8").read()
    out = []
    for i in range(samples):
        user = refine.USER.format(top=design["top"], dut=dut,
                                  n=2, intent=design["intent"], feedback="")
        system = refine.SYSTEM if prompt == "grammar" else PLAIN_SYSTEM
        t0 = time.time()
        raw = client.chat(user, system=system, max_tokens=900)
        out.append({"sample": i, "seconds": round(time.time() - t0, 2),
                    "raw": raw, "code": refine.strip_code(raw)})
    return {"design": design["name"], "model": client.model, "prompt": prompt,
            "temperature": temperature, "samples": out}


def load_existing(path):
    """Read a previous recording, migrating the flat layout it once had.

    Measured failure: the first version wrote `designs` at the top level, and a
    later `--merge` replaced the whole file, so the ablation silently fell back to
    invented transcripts for three of the five designs. The table that produced
    was part recording and part fiction, and nothing in its output said so.
    """
    payload = json.load(open(path, encoding="utf-8"))
    flat = payload.pop("designs", None) or {}
    by = payload.setdefault("by_prompt", {})
    for name, entry in flat.items():
        by.setdefault(entry.get("prompt", "grammar"), {})[name] = entry
    payload.setdefault("system_prompts", {})
    return payload


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--samples", type=int, default=1)
    p.add_argument("--designs", default="")
    p.add_argument("--model", default=None)
    p.add_argument("--prompt", choices=["grammar", "plain", "both"], default="both")
    p.add_argument("--merge", action="store_true",
                   help="add to an existing transcripts.json instead of replacing")
    p.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "transcripts.json"))
    args = p.parse_args(argv)

    designs = [d for d in DESIGNS
               if not args.designs or d["name"] in args.designs.split(",")]
    prompts = ["grammar", "plain"] if args.prompt == "both" else [args.prompt]

    if args.merge and os.path.exists(args.out):
        payload = load_existing(args.out)
    else:
        payload = {"endpoint": llm.DEFAULT_BASE, "user_prompt_template": refine.USER,
                   "by_prompt": {}}
    payload["recorded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    payload["system_prompts"] = {"grammar": refine.SYSTEM, "plain": PLAIN_SYSTEM}

    for prompt in prompts:
        bucket = payload["by_prompt"].setdefault(prompt, {})
        for d in designs:
            print("capturing %s / %s ..." % (prompt, d["name"]))
            c = capture(d, samples=args.samples, model=args.model, prompt=prompt)
            bucket[d["name"]] = c
            for s in c["samples"]:
                print("  sample %d  %.1fs  %s"
                      % (s["sample"], s["seconds"],
                         s["code"].replace("\n", " | ")[:150]))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    counts = {k: len(v) for k, v in payload["by_prompt"].items()}
    print("wrote %s  (%s)" % (args.out, counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
