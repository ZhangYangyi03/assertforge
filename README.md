# assertforge

LLM-generated formal assertions, with the solver as the only judge.

## The gap this fills

Formal verification of RTL has been free for years -- Yosys, SymbiYosys, z3, all
open source, all installable in one command. What is not free is the *specification*.
An engineer sits in front of a 300-line module and writes SVA by hand: a few days
per block, and a missed property is a missed bug. The tooling bottleneck in open
formal flow is not the solver. It is the person writing the assertions.

This closes that gap with a loop that has no human in it:

```
        intent + DUT
             |
             v
      LLM proposes assertions
             |
             v
   grammar lint (rejects constructs the backend cannot parse)
             |
             v
      SymbiYosys proves or refutes
             |
      +------+------+
      |             |
   PROVED        REFUTED
      |             |
      v             v
  vacuity check   counterexample VCD parsed into a
      |           cycle-by-cycle table, handed back
      v           to the model as the reason
   ACCEPT              |
                       v
                  next round
```

Every edge in that diagram is machine-checked. No human grades an assertion
anywhere in the loop, which is the only reason the output can be trusted at a
rate faster than a person can read it.

## What is actually new here

Two things, and they are both measurements rather than opinions.

1. A measured grammar of what the open-source backend accepts. The industry
   standard is SVA (IEEE 1800). Yosys 0.33 implements a strict subset of it, and
   nothing publishes which subset. `assertforge grammar` prints the matrix;
   `assertforge grammar --measure` re-derives it against the live toolchain.
   Measured on Yosys 0.33 + SBY v0.69 + z3 4.8.12:

       construct                              backend
       always @(posedge clk) assert (e);      accepted
       always @(posedge clk) assume (e);      accepted
       always @(posedge clk) cover (e);       accepted
       assert property (e) inside always      accepted
       $past(sig), $past(sig, n)              accepted
       assert property (@(posedge clk) e)     syntax error
       property p; ... endproperty            syntax error
       disable iff (rst)                      syntax error
       a |-> b                                syntax error
       a -> b                                 syntax error (parsed as event trigger)
       $rose / $fell / $stable                syntax error
       default clocking ... endclocking       syntax error

   A model with no knowledge of this writes `assert property (@(posedge clk)
   disable iff (rst) req |-> ##1 ack)`, which is correct SVA and a hard failure
   here. That is the single most expensive fact in this project, and it is
   measured, not assumed.

2. Counterexample-driven refinement. When the solver refutes a candidate, the
   VCD is parsed into a cycle-by-cycle table and handed back as the reason. The
   model is asked to fix the *claim*, not the design. A one-shot generator has
   no way to tell "my assertion is wrong" from "the design is wrong"; this loop
   makes the distinction computable.

## Install

Solver side (WSL or Linux):

    apt-get install -y yosys z3
    git clone --depth 1 https://github.com/YosysHQ/sby /tmp/sby && make -C /tmp/sby install

Python side:

    pip install -e .

Any OpenAI-compatible endpoint works. Point it with:

    export ASSERTFORGE_BASE_URL=https://your-endpoint/v1
    export ASSERTFORGE_MODEL=your-model
    export ASSERTFORGE_API_KEY=...

## Use

    python -m assertforge doctor
    python -m assertforge grammar
    python -m assertforge run --dut bench/designs/sync_fifo.v --top sync_fifo \
        --intent "count never exceeds DEPTH; full implies count==DEPTH-1" \
        -n 2 --rounds 4 --json bench/sync_fifo_result.json

`doctor` answers two questions in one command: is the solver reachable, and is
the model endpoint reachable. Both are checked before any design is touched,
because a silent LLM failure looks exactly like a generator that writes bad
assertions.

`run` exits 0 when the accepted assertion was PROVED, 2 when four rounds elapsed
without a proof. The exit code is the verdict, so it drops straight into CI.

## Layout

    assertforge/grammar.py   the measured support matrix + the pre-solver linter
    assertforge/formal.py    the WSL/SBY bridge, verdict classification, VCD reader
    assertforge/refine.py    the generate/prove/refine loop
    assertforge/llm.py       OpenAI-compatible client with 503-tolerant retry
    bench/designs/           DUTs with the bugs a hand-written property should catch

## Honest limits

- The loop proves the assertion, not the design. A PROVED property says the
  design satisfies the claim. Whether the claim is the *right* claim is what the
  intent string is for, and the intent string is written by a person.
- Vacuity detection is a heuristic: an assertion whose text mentions no DUT
  signal is sent back. A subtler vacuity -- an unreachable antecedent -- proves
  happily and is not caught here.
- Depth is a bound, not a proof of unbounded correctness, unless you use
  `mode prove` with induction, which SBY does by default. Bounded and unbounded
  results are both reported as PROVED; read `--depth` before quoting a result.

## License

Apache-2.0.
