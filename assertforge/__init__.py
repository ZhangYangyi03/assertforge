"""assertforge -- LLM-generated formal assertions, with the solver as the judge.

The claim this package makes is narrow and checkable: an assertion is accepted
only when SymbiYosys proves it, and it is sent back for another round only with
a machine-produced reason (a counterexample cycle, or a vacuity flag). No human
grades the output anywhere in the loop.
"""

__version__ = "0.1.0"

from . import compose, formal, grammar, llm, refine, repair, vacuity  # noqa: F401

__all__ = ["compose", "formal", "grammar", "llm", "refine", "repair", "vacuity"]
