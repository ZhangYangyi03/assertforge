// The weakest assertion that still mentions a real design signal. It PROVES on
// the FIFO and it PROVES on the FIFO whose count register is two bits wide, which
// is the whole point: a proof is not evidence until it has been shown it could
// have failed.
always @(posedge clk) if (af_started) assert (count <= DEPTH);
