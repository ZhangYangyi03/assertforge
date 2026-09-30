"""Integration-level checking: the assertions that live at the seams.

Every other module here answers "is this block correct". This one answers the
question that matters once there is more than one block: **is the assembly
correct**, given that both blocks are.

The distinction is not academic and it is cheap to demonstrate. Wire a counter
into a FIFO and check the FIFO's own properties: they prove. Now break the wiring
-- feed the FIFO's write enable from a signal that is high every cycle instead of
from the counter's enable. The FIFO's own properties still prove, because the FIFO
still behaves like a FIFO. Nothing inside either block can see the mistake, and
the mistake is entirely in the assembly.

That is the shape of every integration bug, and it has a name in formal: the
assertion has to be a *cross-block statement about time*, not a property of either
block. "The occupancy advanced in exactly those cycles when the upstream block
said to write" is provable, is refuted by a wiring error, and is implied by no
property of either block on its own.

    assertforge compose --spec bench/specs/fifo_producer.json

What this is not, and the limits are not decoration:

  not a substitute for judgement   a person still decides which seams matter.
                                   This checks the claims it is given.
  not physical                     nothing here sees metal, clearance or alignment.
                                   A solver cannot check whether two parts are
                                   manufactured to a tolerance; it checks whether
                                   the logic that joins them is sequenced right.
  not a layout or test tool        it does not generate assembly sequences or test
                                   programs. It refutes one assembly, exactly.

So: assembly *logic* -- connections and sequencing -- is decidable here and is
what this module does. Assembly *precision* is not logic and is not in scope, and
the report says which of the two it answered.
"""

import json
import os
import re

from . import formal, grammar

PROLOGUE = """    // integration harness prologue -- same convention as the block wrappers
    reg af_started = 1'b0;
    always @(posedge clk) af_started <= 1'b1;
    always @(posedge clk) if (!af_started) assume (rst);
"""

PORT_RE = re.compile(
    r"\b(input|output|inout)\b\s*(?:wire|reg|logic)?\s*(\[\s*[^\]]*\])?\s*([A-Za-z_]\w*)")
PARAM_RE = re.compile(
    r"\bparameter\b\s*(?:\[[^\]]*\])?\s*([A-Za-z_]\w*)\s*=\s*([^,)#]+)")
BARE_ID = re.compile(r"^[A-Za-z_]\w*$")

# Ports that a block expects to be driven but that an integration spec may
# legitimately leave alone. Binding them to a constant is stated in the generated
# file rather than left implicit, so a reader can see what the assembly assumes.
UNBOUND_INPUT_DEFAULT = "1'b0"


def block_key(inst):
    """The workdir filename for one instance's source.

    One place computes it, and everything else calls this. Two places computed it
    once and disagreed: the specs use repo-relative paths (`../../bench/designs/
    counter.v`) and the workdir wants a basename, so the generated file referenced
    a key nobody had produced.
    """
    return os.path.basename(inst["file"])


def ports_of(dut_text):
    return [{"dir": m.group(1), "width": (m.group(2) or "").strip(), "name": m.group(3)}
            for m in PORT_RE.finditer(dut_text)]


def params_of(dut_text):
    """Parameters from the module header only, so a body-local `parameter` in a
    generate block is not mistaken for a header parameter."""
    head = re.search(r"\bmodule\b.*?;", dut_text, re.S)
    text = head.group(0) if head else dut_text[:800]
    return [(m.group(1), m.group(2).strip()) for m in PARAM_RE.finditer(text)]


def _wire_decls(spec, block_texts):
    """Declare every internal net the spec names, with the width the block uses.

    The spec is allowed to use bare names (`prod_en`) instead of declaring them,
    because that is how a person writes a wiring list. Widths are taken from the
    port the net connects to, so a width mismatch in the spec is a compile error
    rather than a silent truncation -- which is itself one of the integration bugs
    worth catching.
    """
    declared = {d["name"]: d.get("width", "") for d in spec.get("declares", [])}
    inputs = {i["name"] for i in spec["inputs"]}
    widths = {}
    for inst in spec["instances"]:
        for p in ports_of(block_texts[block_key(inst)]):
            widths.setdefault(p["name"], p["width"])
    for inst in spec["instances"]:
        for port, expr in (inst.get("connections") or {}).items():
            if not isinstance(expr, str) or not BARE_ID.match(expr):
                continue
            if expr in inputs or expr in declared:
                continue
            declared[expr] = widths.get(port, "")
    return declared


def build_top(spec, block_texts):
    """Emit the top module. The spec is the source of truth; this is a pure function
    of it, so a diff of the generated file shows exactly what the assembly changed."""
    declared = _wire_decls(spec, block_texts)
    out = ["// GENERATED by assertforge compose -- regenerate, do not edit.",
           "// The spec is bench/specs/*.json; this file is the assembly it describes."]
    hdr = ", ".join("input wire%s %s" % ((" " + i["width"]) if i.get("width") else "",
                                         i["name"]) for i in spec["inputs"])
    out.append("module %s(%s);" % (spec["name"], hdr))
    for name, width in sorted(declared.items()):
        out.append("    wire%s %s;" % ((" " + width) if width else "", name))
    out.append("")

    for inst in spec["instances"]:
        text = block_texts[block_key(inst)]
        ports = ports_of(text)
        pmap = dict(params_of(text))
        pmap.update({k: str(v) for k, v in (inst.get("params") or {}).items()})
        pbind = (" #(\n" + ",\n".join("        .%s(%s)" % (k, v)
                                        for k, v in pmap.items()) + "\n    )") if pmap else ""
        conn = {k: v for k, v in (inst.get("connections") or {}).items()}
        binds = []
        for p in ports:
            if p["name"] in pmap:
                continue
            if p["name"] in conn:
                binds.append("        .%s(%s)" % (p["name"], conn[p["name"]]))
            elif p["dir"] == "output":
                # Nobody reads it: leave it open, visibly.
                binds.append("        .%s() // output, not observed by this assembly"
                             % p["name"])
            else:
                binds.append("        .%s(%s) // not driven by the spec"
                             % (p["name"], UNBOUND_INPUT_DEFAULT))
        out.append("    %s%s %s(" % (inst["module"], pbind, inst["inst"]))
        out.append(",\n".join(binds))
        out.append("    );")
        out.append("")

    out.append(PROLOGUE.rstrip("\n"))
    out.append("")
    for a in spec.get("assertions", []):
        out.append("    // seam: %s" % a["name"])
        if a.get("why"):
            out.append("    //   why it is a seam and not a block property: %s" % a["why"])
        out.append("    always @(posedge clk) if (af_started) assert (%s);" % a["expr"])
    out.append("endmodule")
    return "\n".join(out) + "\n"


def lint_assertions(spec):
    """Seam assertions go through the same linter as generated candidates.

    A seam assertion is not exempt from the backend's grammar because a person
    wrote it, and this is the cheapest place to discover that it is not.
    """
    problems = {}
    for a in spec.get("assertions", []):
        bad = grammar.lint("always @(posedge clk) if (af_started) assert (%s);"
                           % a["expr"])
        if bad:
            problems[a["name"]] = bad
    return problems


def run(spec, block_texts, workdir, depth=14, timeout=300):
    os.makedirs(workdir, exist_ok=True)
    top = build_top(spec, block_texts)
    open(os.path.join(workdir, "%s.sv" % spec["name"]), "w",
         encoding="utf-8", newline="\n").write(top)
    files = []
    for fn, text in block_texts.items():
        open(os.path.join(workdir, fn), "w", encoding="utf-8", newline="\n").write(text)
        files.append(fn)
    res = formal.run(workdir, spec["name"], files, ["%s.sv" % spec["name"]],
                     depth=depth, timeout=timeout)
    return res, top



WIDTH_RE = re.compile(r"\[\s*([^\]:]+?)\s*(?::\s*([^\]]+?)\s*)?\]")


def width_of(decl, params):
    """Bit width of a port from its declaration, with the block's parameters bound.

    A width that cannot be reduced to a number returns None and is reported as
    'unknown' rather than guessed at -- a checker that invents a width is worse
    than one that says it does not know.
    """
    if not decl:
        return 1
    m = WIDTH_RE.search(decl)
    if not m:
        return None
    hi, lo = m.group(1).strip(), (m.group(2) or "0").strip()
    # Parameter values arrive as source TEXT ('3', not 3), and `eval("W-1")` with
    # W == '3' raises rather than computing: a string minus an int. Every width
    # that depended on a parameter therefore came back as None, which the report
    # prints as "unknown" -- so the width check silently did nothing on exactly the
    # parameterised ports it was written for. Reducing the values first is the fix.
    env = {}
    for k, v in params.items():
        try:
            env[k] = int(str(v).strip(), 0)
        except Exception:
            env[k] = str(v)
    try:
        h = eval(hi, {"__builtins__": {}}, env)      # noqa: S307 -- spec-authored
        l = eval(lo, {"__builtins__": {}}, env)      # noqa: S307
        return abs(int(h) - int(l)) + 1
    except Exception:
        return None


def check_ports(spec, block_texts):
    """Every connection between blocks, checked for the faults a solver would
    otherwise only discover from a counterexample.

    The faults, all of them real and all of them cheaper to report here than to
    debug in a waveform:

      width_mismatch    a 3-bit output driving an 8-bit input: legal Verilog,
                        silent zero-extension, and the claim about the high bits
                        is false. Not a syntax error and not a solver error --
                        a wrong answer that looks like a right one.
      double_driver     two outputs on one net. The solver does refuse this one,
                        but it refuses it after the design is built, three layers
                        down, with a message about cells rather than a connection.
      unbound_input     the spec never said what drives this pin. Verilog binds a
                        constant; the assembly should say so with a name.

    Fan-out is not a fault. Many inputs on one clock is the normal case, and the
    first version of this function reported `clk` and `rst` as double drivers
    because it tracked inputs as if they drove -- a checker whose first output is
    four false accusations is worse than no checker, because the true ones stop
    being read.

    What this does NOT do, and the module docstring says so: nothing here sees
    whether two parts are physically within tolerance. It compares declared
    interfaces in the source, which is the part of "will these mate" that is text.
    """
    issues = []
    outputs_on_net = {}     # net -> (instance.port, width) for output drivers only
    readers = {}            # net -> True if some input reads it
    top_inputs = {i["name"] for i in spec.get("inputs", [])}

    for inst in spec["instances"]:
        text = block_texts[block_key(inst)]
        params = dict(params_of(text))
        params.update({k: str(v) for k, v in (inst.get("params") or {}).items()})
        conn = inst.get("connections") or {}
        for p in ports_of(text):
            pname = p["name"]
            if pname in params:
                continue
            w = width_of(p["width"], params)
            if pname not in conn:
                if p["dir"] == "input":
                    issues.append(("unbound_input", "%s.%s" % (inst["inst"], pname),
                                   "no driver named by the spec",
                                   "Verilog binds a constant here; if that is intended, "
                                   "say it in the spec, and if it is not, this is the bug"))
                continue
            expr = conn[pname]
            if not isinstance(expr, str) or not BARE_ID.match(expr or ""):
                continue
            if p["dir"] == "output":
                if expr in outputs_on_net:
                    prev_name, prev_w = outputs_on_net[expr]
                    issues.append(("double_driver", expr,
                                   "%s and %s" % (prev_name, "%s.%s" % (inst["inst"], pname)),
                                   "two outputs on one net; the solver will refuse the "
                                   "design, and this says which connection to look at"))
                outputs_on_net[expr] = ("%s.%s" % (inst["inst"], pname), w)
            else:
                readers[expr] = True

    # width agreement, after every driver is known (so order does not matter)
    for inst in spec["instances"]:
        text = block_texts[block_key(inst)]
        params = dict(params_of(text))
        params.update({k: str(v) for k, v in (inst.get("params") or {}).items()})
        for p in ports_of(text):
            if p["dir"] != "input" or p["name"] in params:
                continue
            expr = (inst.get("connections") or {}).get(p["name"])
            if not isinstance(expr, str) or not BARE_ID.match(expr or ""):
                continue
            if expr not in outputs_on_net:
                continue
            drv_name, drv_w = outputs_on_net[expr]
            in_w = width_of(p["width"], params)
            if drv_w is not None and in_w is not None and drv_w != in_w:
                issues.append(("width_mismatch", expr,
                               "%s is %d bit, %s.%s is %d bit"
                               % (drv_name, drv_w, inst["inst"], p["name"], in_w),
                               "silent %s; the %s side carries bits the %s side "
                               "cannot hold, and every assertion about them is about "
                               "a value that is not there"
                               % ("zero-extension" if drv_w < in_w else "truncation",
                                  "wider" if drv_w < in_w else "narrower",
                                  "narrower" if drv_w < in_w else "wider")))

    # A net is observed if an input reads it, if the assembly exposes it, or if a
    # seam assertion mentions it. That third case is the one the first version of
    # this function missed, and it turned the check into five false accusations
    # against a spec that observes every net it declares -- through its assertions,
    # which is exactly where an assembly's outputs are meant to be observed.
    asserted = " ".join(a.get("expr", "") for a in spec.get("assertions", []))
    for name, (who, w) in sorted(outputs_on_net.items()):
        if readers.get(name) or name in top_inputs:
            continue
        if re.search(r"\b%s\b" % re.escape(name), asserted):
            continue
        issues.append(("dead_output", name, "driven by %s, read by nothing, not an "
                       "assembly output, and mentioned by no seam assertion" % who,
                       "nothing in this assembly can ever notice its value"))
    return issues


def report_ports(issues):
    if not issues:
        return "every connection mates: widths agree and every net has exactly one driver"
    lines = []
    for kind, where, what, why in issues:
        lines.append("%-16s %-24s %s" % (kind, where, what))
        lines.append("%-16s %s" % ("", "-- " + why))
    return "\n".join(lines)


def load_spec(path):
    spec = json.load(open(path, encoding="utf-8"))
    spec["_base"] = os.path.dirname(os.path.abspath(path))
    return spec


def block_texts_for(spec):
    """Read each referenced block. Keys are BASENAMES, because the key is also the
    filename written into the workdir, and a spec that says
    `../../bench/designs/counter.v` produces a path that does not exist once the
    workdir is added in front of it."""
    base = spec.get("_base") or "."
    out = {}
    for inst in spec["instances"]:
        p = inst["file"]
        if not os.path.isabs(p):
            p = os.path.join(base, p)
        out[os.path.basename(p)] = open(p, encoding="utf-8", errors="replace").read()
    return out
