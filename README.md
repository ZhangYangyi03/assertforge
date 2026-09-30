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

Four things, and all four are measurements rather than opinions.

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

3. A deterministic rewriter that turns correct SVA into the subset the backend
   actually parses, so the model's knowledge of SVA stops being a liability. The
   ablation below measures it: on prompts that contain no knowledge of the subset,
   asking the model again recovers nothing and the rewriter recovers a design.

4. Integration as a measurable level of proof, not an aspiration. A block-level
   flow passes a mis-wired assembly, and that is demonstrated in four solver runs
   rather than asserted.

## Integration is a separate level of proof, and it is measurable

The two sections above are about one block. That is where formal verification
usually stops, and it is the wrong place to stop, because the bugs that reach
silicon are the ones that survive it.

The demonstration is four solver runs and it is in `bench/run_seam.py`. Two
assemblies are generated from specs that differ in **one wiring connection** -- a
counter feeding a FIFO, with the FIFO's write enable coming from the producer in
one and tied high in the other. The two block files are byte-identical between the
two assemblies, so each block is still exactly the counter and the FIFO it was.
Then two assertion sets are run against both assemblies:

    bench/run_seam.py
                       block props  seam props
    assembly_ok        PROVED       PROVED
    assembly_broken    PROVED       REFUTED

Read the bottom-left cell. **A complete block-level verification flow passes an
assembly with a wiring bug in it.** It has to: inside the FIFO, `wr_en` is a port,
and which wire drives it is not a fact about the FIFO. Every property either block
can state is a property of a design that is behaving correctly, and both designs
are. The mistake lives in the space between them, and no amount of block-level
work reaches it.

The seam assertions are the two-line statements that do reach it. `S3` is the
decisive one:

    // would this assertion have been able to fail?
    always @(posedge clk) if (af_started)
        assert (!( !$past(rst) && !$past(en) && !$past(rd_en) && (count != $past(count)) ));

"The FIFO's occupancy changes only in cycles when some block asked it to." The
FIFO cannot state that, because from inside the FIFO no block did or did not ask
-- there is only a port. Only the assembly can state it, and only the assembly can
be wrong about it.

The same 2x2 is what `assertforge compose` exposes as a subcommand:

    python -m assertforge compose --spec bench/specs/fifo_producer.json
    python -m assertforge compose --spec bench/specs/fifo_producer_broken.json

### The spec is the artifact

An assembly is described by a JSON spec: which blocks, how they are wired, and
which cross-block claims must hold. `compose` generates the top-level file from
it. Generating rather than writing is what makes the claim checkable -- a diff of
two generated files is the diff of two assemblies, and a reviewer can see that the
only change is one connection.

    bench/specs/fifo_producer.json              one wiring
    bench/specs/fifo_producer_broken.json       one connection different
    bench/specs/fifo_producer_width_fault.json  a shared net that cannot hold its value

A spec declares the assembly's inputs AND its outputs, names the blocks and their
parameters, gives each connection by port name, and lists the seam claims with a
one-line reason why each is a seam and not a block property. The reason is not
decoration: `S4` in the shipped spec is a control that holds on both twins, which
is how the bench reports that a seam assertion is not automatically a
discriminating one.

### The interface check, and the fault no assertion can see

Before any solver runs, `compose` compares the declared interfaces net by net:

    interface : 1 issue(s)
    width_mismatch   prod_cnt    u_prod.cnt is 3 bit, u_fifo.wr_data is 8 bit
                     -- silent zero-extension; the wider side carries bits the
                        narrower side cannot hold, and every assertion about them
                        is about a value that is not there

That is not a syntax error and not a solver error. It is legal Verilog that
answers a property about the top five bits with a constant zero, and it passes
everything until someone reads the silicon. The check reports three faults --
width mismatch, two outputs on one net, an input the spec never bound -- and it
reports nothing else, because a checker that cries wolf stops being read. Its
first version accused every shared clock of being a double driver.

What it does not do: it compares **declared interfaces in the source**. It cannot
see whether two parts are manufactured within tolerance, and it does not pretend
to. The analogue of a mirror alignment is not a property of the text.

## The ablation, and what it actually measures

The claim "the loop is what makes this work" is testable, so it is tested. Four
arms, five designs, one recorded model sample per design per arm -- the same text
replayed, so the arms differ by the mechanism and not by the model's run-to-run
variance. `bench/transcripts.json` is a real recording from the endpoint; the
ablation replays it offline in a few seconds.

    arm             what it does
    oneshot_raw     one shot: propose, lint, solve. no retry
    lint_retry      on a lint failure or a refutation, ask the model again
    repair_once     on a lint failure, apply the deterministic SVA rewriter instead
    repair_refine   the rewriter, then refinement on a refutation

    python bench/run_ablation.py --mode fixture --prompt grammar
    python bench/run_ablation.py --mode fixture --prompt plain

Two prompt arms, because the second axis is the one people argue about. The
`grammar` arm's system prompt carries the measured support matrix. The `plain` arm
is the ordinary request -- "write industrial-style assertions" -- with none of it.

    prompt=grammar            sync_fifo  counter    fsm        fifo_buggy  rr_arbiter   PROVED   solver_s
    oneshot_raw               PROVED     REFUTED    PROVED     REFUTED     REFUTED      2/5      4.2
    lint_retry                PROVED     REFUTED    PROVED     REFUTED     REFUTED      2/5      8.8
    repair_once               PROVED     REFUTED    PROVED     REFUTED     REFUTED      2/5      4.1
    repair_refine             PROVED     REFUTED    PROVED     REFUTED     REFUTED      2/5      8.9

    prompt=plain              sync_fifo  counter    fsm        fifo_buggy  rr_arbiter   PROVED   solver_s
    oneshot_raw               MALFORMED  MALFORMED  MALFORMED  MALFORMED   MALFORMED    0/5      2.3
    lint_retry                MALFORMED  MALFORMED  MALFORMED  MALFORMED   MALFORMED    0/5      7.0
    repair_once               PROVED     MALFORMED  MALFORMED  MALFORMED   REFUTED      1/5      3.3
    repair_refine             PROVED     MALFORMED  MALFORMED  MALFORMED   REFUTED      1/5      7.8

Three things in that table are worth more than the PROVED column, and two of them
are unflattering to the loop.

**1. The grammar appendix is the load-bearing part.** Wipe it out of the prompt and
the score goes from 2/5 to 0/5: every single design MALFORMED, which means the
model wrote textbook SVA that Yosys cannot parse. That is the entire argument for
section 1, and it is now a number rather than a claim.

**2. The deterministic rewriter does the retry's job at half the solver cost.**
`repair_once` and `lint_retry` reach the same 2/5 on the grammar arm, but 4.1
solver-seconds against 8.8. On the plain arm the gap is starker: `lint_retry`
spends 7.0 seconds and recovers nothing, while `repair_once` recovers one design
for 3.3. Asking the model again is the expensive way to do what a rewriter does
for free -- and on the plain arm it does not work at all, because the model has no
way to know what the backend will reject.

**3. More mechanism does not mean more proof, and the table says so.** On the
grammar arm all four arms land on 2/5. `repair_refine` costs twice the solver time
of `repair_once` and proves exactly the same set. The failures it cannot fix are
the interesting ones: `counter` and `rr_arbiter` are not malformed, they are
refuted, and no amount of reshaping the syntax touches them. Those need the claim
to change -- which is what refinement is for, and on these two designs refinement
does not find it inside the round budget. A README that reported only the PROVED
column would call that a draw.

What the table does not show, and should not be read as: five small designs is not
a benchmark, `rr_arbiter` fails for a reason specific to how the intent was
phrased, and the fixture replays one sample per cell. The numbers are honest about
what they measured; they are not a claim about a class of designs.

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
    assertforge/repair.py    rewrites correct SVA into the subset the backend parses
    assertforge/vacuity.py   does the proof survive a design known to be broken
    assertforge/compose.py   seam assertions over an assembly, plus the interface check
    assertforge/llm.py       OpenAI-compatible client with 503-tolerant retry
    bench/designs/           DUTs with the bugs a hand-written property should catch
    bench/specs/             assemblies, as specs -- the source of the generated tops
    bench/run_seam.py        the 2x2 above
    bench/run_ablation.py    one-shot vs lint-retry vs repair-refine, replayed offline
    bench/run_twin_audit.py  does a PROVED result survive the faulty twin

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
- `compose` checks assembly *logic*: connections and sequencing, as claims about
  the text of the source. Assembly *precision* -- whether a part is made to
  tolerance, whether a joint is tight, whether a mirror is aligned -- is not a
  property of any source file and is outside what a solver can see. The distinction
  is the point of the section above, not a disclaimer bolted on at the end.
- The seam specs are written by a person. `compose` proves the claims it is given;
  deciding which seams matter is judgement, and there is no measurement here that
  removes it.

## License

Apache-2.0.
