"""The rules the loop uses to decide what an outcome means.

These are the definitions the ablation table is built from, so they are asserted
rather than described: a mislabelled outcome would make the headline number a
fiction even when every solver run behind it was real.
"""

from assertforge import refine


class FakeResult:
    def __init__(self, status, seconds=1.0, trace=None):
        self.status = status
        self.seconds = seconds
        self.trace = trace or []
        self.log = ""


class FakeAttempt:
    def __init__(self, verdict, twin=None):
        self.verdict = verdict
        self.twin = twin


def test_proved_is_proved():
    assert refine.outcome_of(FakeAttempt("PROVED")) == "PROVED"


def test_refuted_is_refuted():
    assert refine.outcome_of(FakeAttempt("REFUTED")) == "REFUTED"


def test_solver_errors_are_malformed_not_refuted():
    # the distinction the whole repair path is measured against: a candidate that
    # never reached the solver is a generator failure, and calling it REFUTED
    # would credit the tool with finding a bug in a design it never checked
    for v in ("SYNTAX", "ERROR", "NOINPUT"):
        assert refine.outcome_of(FakeAttempt(v)) == "MALFORMED"


def test_a_proof_that_missed_the_known_bug_is_a_false_prove():
    att = FakeAttempt("PROVED", twin={"verdict": "MISSED_BUG"})
    assert refine.outcome_of(att, design_has_bug=False) == "FALSE_PROVE"


def test_the_same_proof_on_the_buggy_design_is_the_correct_answer():
    # on the broken design, proving is not an achievement -- refuting is what the
    # tool is supposed to do, so the twin verdict must not be read as a failure
    att = FakeAttempt("REFUTED", twin={"verdict": "CAUGHT_BUG"})
    assert refine.outcome_of(att, design_has_bug=True) == "REFUTED"


def test_vacuous_prefix_is_stripped_before_classifying():
    assert refine.outcome_of(FakeAttempt("VACUOUS->PROVED")) == "PROVED"
    assert refine.outcome_of(FakeAttempt("VACUOUS->REFUTED")) == "REFUTED"


def test_no_attempt_is_its_own_category():
    assert refine.outcome_of(None) == "NOATTEMPT"


def test_the_four_arms_differ_only_in_the_thing_being_measured():
    assert set(refine.ARMS) == {"oneshot_raw", "lint_retry", "repair_once",
                               "repair_refine"}
    assert refine.ARMS["oneshot_raw"]["use_repair"] is False
    assert refine.ARMS["oneshot_raw"]["use_refine"] is False
    assert refine.ARMS["repair_refine"]["use_repair"] is True
    assert refine.ARMS["repair_refine"]["use_refine"] is True
