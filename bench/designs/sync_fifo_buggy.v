// The same FIFO with one realistic bug: `count` is 2 bits, so at DEPTH=4 it
// wraps 3 -> 0 while `full` is asserted. A correct property catches this; a
// one-shot generator that never sees a counterexample does not.
module sync_fifo_buggy #(parameter WIDTH = 8, parameter DEPTH = 4) (
    input  wire             clk,
    input  wire             rst,
    input  wire             wr_en,
    input  wire [WIDTH-1:0] wr_data,
    input  wire             rd_en,
    output reg  [WIDTH-1:0] rd_data,
    output reg              full,
    output reg              empty,
    output reg  [1:0]       count        // BUG: cannot hold DEPTH == 4
);
    reg [WIDTH-1:0] mem [0:DEPTH-1];
    reg [1:0] wr_ptr, rd_ptr;

    wire do_wr = wr_en && !full;
    wire do_rd = rd_en && !empty;

    always @(posedge clk) begin
        if (rst) begin
            wr_ptr <= 0; rd_ptr <= 0; count <= 0;
            full <= 0; empty <= 1; rd_data <= 0;
        end else begin
            if (do_wr) begin
                mem[wr_ptr] <= wr_data;
                wr_ptr <= wr_ptr + 1'b1;
            end
            if (do_rd) begin
                rd_data <= mem[rd_ptr];
                rd_ptr <= rd_ptr + 1'b1;
            end
            if (do_wr && !do_rd)      count <= count + 1'b1;  // wraps at DEPTH
            else if (do_rd && !do_wr) count <= count - 1'b1;
            full  <= (count == DEPTH-1) && do_wr;
            empty <= (count == 0) || (do_rd && count == 1);
        end
    end
endmodule
