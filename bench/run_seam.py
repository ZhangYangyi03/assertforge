"""The seam experiment: does block-level verification survive an integration bug?

This is the bench behind the `compose` module, and it exists because the claim
"you need integration-level properties" is cheap to make and easy to check.

The protocol is a 2x2. Two assemblies are generated from specs that differ in ONE
wiring connection:

  assembly_ok        counter -> FIFO, the FIFO's write enable from the producer
  assembly_broken    identical, EXCEPT the same input is tied high

Both block FILES are byte-identical between the two. Both blocks therefore still
behave exactly as the counter and the FIFO they are. No property of either block
alone can distinguish the two assemblies, because inside either block the wiring
is invisible -- `wr_en` is whatever it is bound to.

Then two assertion sets are run against both assemblies:

  block properties   what the loop generates from a single block's intent
  seam assertions    cross-block, stated over the assembly's own nets

Four cells. The claim this bench tests is one cell:

  block properties x broken assembly  ==  PROVED

If that cell is PROVED, then a block-level verification flow reports a clean pass
on a machine with a wiring bug in it, and the seam cell is what closes it. If that
cell is REFUTED, the seam assertions are redundant and the README should say so.

    python bench/run_seam.py              # the 2x2
    python bench/run_seam.py --json out.json
"""

import argparse
import json
import os
import shutil
import sys
import time
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assertforge import compose  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SPECS = os.path.join(HERE, "specs")

# (row label, spec file, what the assertions in it are)
CASES = [
    ("assembly_ok", "fifo_producer.json",
     "counter -> FIFO, write enable from the producer"),
    ("assembly_broken", "fifo_producer_broken.json",
     "same, write enable tied high -- one connection differs"),
]
SETS = [
    ("block", "fifo_producer%(suffix)s_blockprops.json",
     "each block's own properties"),
    ("seam", "fifo_producer%(suffix)s.json",
     "cross-block properties over the assembly's nets"),
]
BROKEN_SUFFIX = {"assembly_ok": "", "assembly_broken": "_broken"}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--depth", type=int, default=14)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--workdir", default=os.path.join(HERE, "_seam"))
    p.add_argument("--json", default=None)
    args = p.parse_args(argv)

    if os.path.isdir(args.workdir):
        shutil.rmtree(args.workdir, ignore_errors=True)

    cells = OrderedDict()
    print("seam experiment -- block-level vs integration-level assertions on a "
          "deliberately mis-wired assembly")
    print("")
    for row, specfile, rowdesc in CASES:
        for col, tmpl, coldesc in SETS:
            name = tmpl % {"suffix": BROKEN_SUFFIX[row]}
            spec = compose.load_spec(os.path.join(SPECS, name))
            texts = compose.block_texts_for(spec)
            t0 = time.time()
            res, top = compose.run(spec, texts, os.path.join(args.workdir, row + "_" + col),
                                   depth=args.depth, timeout=args.timeout)
            cells[(row, col)] = {"status": res.status, "seconds": round(time.time() - t0, 2),
                                 "spec": name, "assertions": [a["name"] for a in spec["assertions"]],
                                 "generated_top": top}
            print("  %-16s %-6s %-9s %.2fs   (%s)"
                  % (row, col, res.status, cells[(row, col)]["seconds"], coldesc))

    print("")
    print("2x2 -- rows are the assembly, columns the assertion set")
    print("%-18s %-12s %-12s" % ("", "block props", "seam props"))
    for row, _, _ in CASES:
        print("%-18s %-12s %-12s"
              % (row, cells[(row, "block")]["status"], cells[(row, "seam")]["status"]))
    print("")
    print("columns are one wiring connection apart; the block files are byte-identical "
          "between the two rows")

    key = cells[("assembly_broken", "block")]["status"]
    print("")
    if key == "PROVED":
        print("RESULT: a block-level flow PASSES an assembly with a wiring bug in it.")
        print("        %s passes on the broken assembly, so no property of either block "
              "can see the mistake." % "block props")
        print("        The seam set refutes it on the same assembly (%s) and proves the "
              "correct one (%s),"
              % (cells[("assembly_broken", "seam")]["status"],
                 cells[("assembly_ok", "seam")]["status"]))
        print("        which is the whole reason the integration level has to exist.")
    else:
        print("RESULT: block props also refuted the broken assembly (%s)." % key)
        print("        The seam assertions are not carrying their weight in this design, "
              "and this bench would be the place to say so.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"cells": {"%s/%s" % k: v for k, v in cells.items()}}, f, indent=2)
        print("")
        print("wrote %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
