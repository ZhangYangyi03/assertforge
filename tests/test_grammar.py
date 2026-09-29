"""The linter is the cheap gate in front of the solver, so it is tested without one."""

from assertforge import grammar


def test_rejects_the_sva_the_a_model_always_reaches_for():
    # this exact string is what a model writes when not constrained; measured
    # to be a syntax error in yosys 0.33
    bad = ("assert property (@(posedge clk) disable iff (rst) req |-> ##1 ack);")
    problems = grammar.lint(bad)
    assert any("disable iff" in p for p in problems)
    assert any("|->" in p for p in problems)


def test_rejects_named_property_block():
    bad = "property p1; @(posedge clk) a == b; endproperty\nassert property (p1);"
    assert grammar.lint(bad)


def test_rejects_arrow_implication():
    # '->' parses as an event trigger in an immediate assert, so a design that
    # means "implies" must write (!a || b)
    assert grammar.lint("always @(posedge clk) assert (a -> b);")


def test_accepts_what_the_backend_accepts():
    good = "always @(posedge clk) assert (count <= DEPTH);"
    assert grammar.lint(good) == []


def test_accepts_past_and_in_always_assert_property():
    assert grammar.lint("always @(posedge clk) assert (x >= $past(x));") == []
    assert grammar.lint("always @(posedge clk) assert property (x >= $past(x));") == []


def test_rejects_a_candidate_with_no_assertion():
    assert grammar.lint("// nothing here\n")


def test_module_level_assert_property_is_flagged_without_solver():
    src = "module m; assert property (@(posedge clk) a == b); endmodule"
    problems = grammar.lint(src)
    assert any("module level" in p for p in problems)


def test_matrix_is_data_not_prose():
    j = grammar.as_json()
    assert "supported" in j and "rejected" in j
    assert grammar.MEASURED_ON
