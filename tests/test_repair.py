"""Repair is deterministic string surgery, so it is tested without a solver.

The case that matters most is the one that nearly shipped: `count == DEPTH - 1`
contains `->`, and a substring linter called it an implication. The same shape
appears twice in the code -- in the linter and in the rewriter -- so both are
asserted here, because a rewriter that fires on a subtraction corrupts the claim
while still producing something that compiles and proves.
"""

from assertforge import grammar, repair


def test_a_guarded_assertion_is_left_alone_apart_from_the_reset_guard():
    r = repair.normalise("always @(posedge clk) assert (count <= DEPTH);")
    assert r.source == ("always @(posedge clk) if (af_started) "
                        "assert (count <= DEPTH);")
    assert r.applied == ["guard_always_block"]


def test_already_guarded_is_untouched():
    src = "always @(posedge clk) if (af_started) assert (a == b);"
    r = repair.normalise(src)
    assert r.source == src
    assert not r.changed


def test_guard_is_not_applied_twice():
    r = repair.normalise("always @(posedge clk) assert (a);")
    assert r.source.count("af_started") == 1


def test_module_level_assert_property_gains_a_clock():
    r = repair.normalise("assert property (@(posedge clk) count <= DEPTH);")
    assert "always @(posedge clk)" in r.source
    assert "@(posedge clk)" in r.source
    assert "af_started" in r.source
    assert "property" not in r.source


def test_assert_property_inside_always_does_not_get_a_second_always():
    r = repair.normalise(
        "always @(posedge clk) assert property (@(posedge clk) a |-> b);")
    assert r.source.count("always @(posedge clk)") == 1
    assert "@(posedge clk) @(posedge clk)" not in r.source


def test_disable_iff_is_dropped_and_the_claim_survives():
    r = repair.normalise(
        "assert property (@(posedge clk) disable iff (rst) a |-> b);")
    assert "disable iff" not in r.source
    assert "a" in r.source and "b" in r.source


def test_implication_becomes_a_boolean_over_the_same_cycle():
    r = repair.normalise("always @(posedge clk) assert (req |-> ack);")
    assert "|->" not in r.source
    assert "(!(req)) || (ack)" in r.source


def test_arrow_implication_is_rewritten():
    r = repair.normalise("always @(posedge clk) assert (full -> count == DEPTH - 1);")
    assert "->" not in r.source
    assert "count == DEPTH - 1" in r.source


def test_a_subtraction_is_not_an_implication():
    # the false positive that a substring linter produces, and the reason the
    # rewriter does a top-level scan instead
    src = "always @(posedge clk) assert (count == DEPTH - 1);"
    assert grammar.lint(src) == []
    r = repair.normalise(src)
    assert "count == DEPTH - 1" in r.source
    assert "implication_to_boolean" not in r.applied


def test_next_cycle_implication_keeps_its_delay():
    r = repair.normalise("always @(posedge clk) assert (req |-> ##1 ack);")
    assert "$past(req)" in r.source
    assert "next_cycle_implication" in r.applied


def test_rose_is_expanded_using_supported_constructs():
    r = repair.normalise("always @(posedge clk) assert ($rose(en) -> count < DEPTH);")
    assert "$rose" not in r.source
    assert "$past(en)" in r.source
    assert grammar.lint(r.source) == []


def test_named_property_block_is_inlined():
    src = ("property p1; @(posedge clk) cnt <= DEPTH; endproperty\n"
           "assert property (p1);")
    r = repair.normalise(src)
    assert "endproperty" not in r.source
    assert "cnt <= DEPTH" in r.source


def test_a_module_wrapper_is_stripped():
    r = repair.normalise("module m(input clk);\n"
                         "  always @(posedge clk) assert (a == b);\nendmodule")
    assert "module" not in r.source
    assert "endmodule" not in r.source
    assert "a == b" in r.source


def test_balanced_parens_are_not_truncated():
    r = repair.normalise("always @(posedge clk) assert ((a && b) || (c && d));")
    assert "((a && b) || (c && d))" in r.source


def test_two_module_level_assertions_both_get_a_clock():
    r = repair.normalise("assert (a == b);\nassert (c == d);")
    assert r.source.count("always @(posedge clk)") == 2


def test_normalise_is_idempotent():
    src = "assert property (@(posedge clk) disable iff (rst) req |-> ##1 ack);"
    once = repair.normalise(src).source
    twice = repair.normalise(once).source
    assert once == twice
