// A property set written for the FIFO intent: count is bounded, full and empty
// are exactly the two occupancy predicates. Handed to `assertforge twin` it is
// caught against the broken twin, which is what makes it worth trusting.
always @(posedge clk) if (af_started) begin
    assert (count <= DEPTH);
    assert (full == (count == DEPTH));
    assert (empty == (count == 0));
end
