import json
import re

"""
The assertion grammar that the backend solver actually accepts.

This file is not a guess. Every entry in SUPPORTED / REJECTED was measured by
running SymbiYosys against Yosys 0.33 with z3; the raw matrix is reproduced by
`assertforge grammar --measure`, and the version it was measured on is pinned in
MEASURED_ON. Anything the generator emits that is outside SUPPORTED is a bug in
the generator, not in the user's design.

That asymmetry is the whole reason this project exists: the industry standard is
SVA (IEEE 1800), the open-source formal flow implements a strict subset of it,
and the gap is where engineers lose days.
"""

MEASURED_ON = "Yosys 0.33 (git sha1 2584903a060) + SBY v0.69 + z3 4.8.12"

# Constructs the backend proved or falsified correctly.
SUPPORTED = {
    "immediate_assert":  "always @(posedge clk) assert (expr);",
    "immediate_assume":  "always @(posedge clk) assume (expr);",
    "immediate_cover":   "always @(posedge clk) cover (expr);",
    "assert_property_in_always": "always @(posedge clk) assert property (expr);",
    "past":              "$past(sig) and $past(sig, n)",
    "guarded_by_if":     "always @(posedge clk) if (cond) assert (expr);",
}

# Constructs measured to fail. Kept as data, not prose, so the linter can cite it.
REJECTED = {
    "module_level_assert_property": "assert property (@(posedge clk) e);  # syntax error, unexpected '@'",
    "named_property":               "property p; @(posedge clk) e; endproperty  # syntax error",
    "disable_iff":                  "assert property (@(posedge clk) disable iff (rst) e);  # syntax error",
    "implication_operator":         "assert (a |-> b);  # syntax error",
    "sva_rose":                     "$rose(sig)  # syntax error",
    "sva_fell":                     "$fell(sig)",
    "sva_stable":                   "$stable(sig)",
    "clocking_block":               "default clocking cb @(posedge clk); endclocking",
    "arrow_implication":            "assert (a -> b);  # '->' parses as an event trigger; use (!a || b)",
}

# Tokens that make the linter reject a candidate before it wastes a solver run.
# `->` is here because Yosys parses it as an event trigger inside an immediate
# assert and errors out; the model reaches for it as "implies" constantly.
POISON = ["|->", "|=>", "disable iff", "$rose", "$fell", "$stable",
          "endproperty", "clocking", "sequence ", "endsequence", "->"]

HISTORY_HELP = (
    "To express a past value, declare a register and shift it in an always block "
    "instead of calling $rose/$fell/$stable. $past() itself is supported."
)

TEMPLATE = """// assertforge candidate: {name}
// intent: {intent}
module {module}_assertions(
    input clk,
    input rst,
{ports}
);
    // history registers for anything measured unsupported as an SVA function
{history}
{body}
endmodule
"""


def _position_of_tokens(candidate_src):
    """Where the poison tokens actually are, as (token, index) pairs.

    A substring test is not good enough and it produced a live false positive:
    `assert (full -> count == DEPTH - 1)` DOES contain `->`, but so does
    `count == DEPTH - 1`, and `x - 1` with a negation is a subtraction, not SVA.
    The correction is to look at the shape: an implication operator sits at the
    top level of an assertion argument, between two expressions. A subtraction
    sits inside one.
    """
    from . import repair
    hits = []
    for c in repair._scan_calls(candidate_src):
        for tok in ("|->", "|=>", "->"):
            i = repair._top_level_find(c["arg"], tok)
            if i is not None:
                hits.append((c["kind"], tok))
    return hits


def lint(candidate_src):
    """Return a list of human-readable reasons the backend will reject this.

    Cheap and total: catching a poison token here saves a ~2s solver run per
    candidate, which matters because the generate/refine loop is the hot path.
    Since `repair.normalise` exists the linter is no longer a rejection gate --
    it is the trigger for a rewrite, and its remaining job is to report what a
    rewrite could not fix.
    """
    problems = []

    # Constructs that are never valid anywhere in the accepted subset.
    for tok, key in (("|=>", None), ("disable iff", "disable_iff"),
                     ("endproperty", "named_property"),
                     ("endsequence", None), ("clocking", "clocking_block")):
        if tok in candidate_src:
            reason = REJECTED.get(key, None) if key else None
            problems.append("unsupported construct %r%s"
                            % (tok, (": " + reason) if reason else ""))

    # Tokens whose meaning depends on position or on being called.
    for tok, key in (("$rose", "sva_rose"), ("$fell", "sva_fell"),
                     ("$stable", "sva_stable")):
        if re.search(re.escape(tok) + r"\s*\(", candidate_src):
            problems.append("unsupported construct %r: %s" % (tok, REJECTED[key]))

    for _kind, tok in _position_of_tokens(candidate_src):
        problems.append("unsupported construct %r: %s"
                        % (tok, REJECTED.get(
                            {"|->": "implication_operator",
                             "->": "arrow_implication"}.get(tok, ""), "")))

    if "assert property" in candidate_src and "always" not in candidate_src \
            and "endproperty" not in candidate_src:
        problems.append("assert property at module level: %s"
                        % REJECTED["module_level_assert_property"])
    if not re.search(r"\b(assert|assume|cover)\b", candidate_src):
        problems.append("candidate contains no assert/assume/cover statement")
    return problems


def measure_script():
    """Emit the bash that reproduces the support matrix on this host.

    The README quotes numbers; this is the thing that produces them, so the
    claim stays checkable after the toolchain is upgraded.
    """
    return r"""set -e
cd "$(dirname "$0")"
rm -rf _grammar_probe && mkdir _grammar_probe && cd _grammar_probe
cat > dut.v <<'EOF'
module counter(input clk, input rst, input en, output reg [3:0] cnt);
  always @(posedge clk) if (rst) cnt <= 4'd0; else if (en) cnt <= cnt + 4'd1;
endmodule
EOF
probe() {
  name=$1; body=$2
  printf 'module %s_t(input clk, input rst, input en, output [3:0] cnt);\n  counter u(.clk(clk),.rst(rst),.en(en),.cnt(cnt));\n%s\nendmodule\n' "$name" "$body" > "$name.sv"
  printf '[options]\nmode prove\ndepth 10\n[engines]\nsmtbmc z3\n[files]\ndut.v\n%s.sv\n[script]\nread -formal dut.v\nread -formal -sv %s.sv\nprep -top %s_t\n' "$name" "$name" "$name" > "$name.sby"
  out=$(sby -f "$name.sby" 2>&1)
  if   echo "$out" | grep -q "DONE (PASS"; then r=PROVED
  elif echo "$out" | grep -q "DONE (FAIL"; then r=CEX
  elif echo "$out" | grep -q "syntax error"; then r=SYNTAX
  else r=ERROR; fi
  printf '%-24s %s\n' "$name" "$r"
}
echo "# measured on $(yosys -V), sby $(sby --version 2>&1 | head -1)"
probe immediate_assert      '  always @(posedge clk) assert (cnt <= 4'"'"'d15);'
probe immediate_assume      '  always @(posedge clk) assume (cnt <= 4'"'"'d15);'
probe immediate_cover       '  always @(posedge clk) cover (cnt == 4'"'"'d4);'
probe past                  '  always @(posedge clk) assert (cnt >= $past(cnt));'
probe assert_property_in_always '  always @(posedge clk) assert property (cnt >= $past(cnt));'
probe module_level_ap       '  assert property (@(posedge clk) cnt >= $past(cnt));'
probe named_property        '  property p1; @(posedge clk) cnt >= $past(cnt); endproperty'$'\n''  assert property (p1);'
probe disable_iff           '  assert property (@(posedge clk) disable iff (rst) cnt >= $past(cnt));'
probe implication           '  always @(posedge clk) assert (rst |-> cnt == 4'"'"'d0);'
probe sva_rose              '  always @(posedge clk) assert ($rose(en) -> 1'"'"'b1);'
"""


def as_json():
    return json.dumps(
        {"measured_on": MEASURED_ON, "supported": SUPPORTED, "rejected": REJECTED},
        indent=2,
    )


