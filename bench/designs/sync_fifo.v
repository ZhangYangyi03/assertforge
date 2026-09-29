// A correct synchronous FIFO. The design a hand-written property should PROVE.
// Deliberately small -- the subject is the assertion flow, not the FIFO.
module sync_fifo #(parameter WIDTH = 8, parameter DEPTH = 4) (
    input  wire             clk,
    input  wire             rst,
    input  wire             wr_en,
    input  wire [WIDTH-1:0] wr_data,
    input  wire             rd_en,
    output reg  [WIDTH-1:0] rd_data,
    output wire             full,
    output wire             empty,
    output reg  [2:0]       count
);
    reg [WIDTH-1:0] mem [0:DEPTH-1];
    reg [1:0] wr_ptr, rd_ptr;

    // occupancy is the single source of truth; full/empty are derived from it,
    // which is what makes them provable rather than merely plausible
    wire do_wr = wr_en && (count < DEPTH);
    wire do_rd = rd_en && (count > 0);
    assign full  = (count == DEPTH);
    assign empty = (count == 0);

    always @(posedge clk) begin
        if (rst) begin
            count <= 0; wr_ptr <= 0; rd_ptr <= 0; rd_data <= 0;
        end else begin
            if (do_wr) begin mem[wr_ptr] <= wr_data; wr_ptr <= wr_ptr + 1'b1; end
            if (do_rd) begin rd_data <= mem[rd_ptr]; rd_ptr <= rd_ptr + 1'b1; end
            if (do_wr && !do_rd)      count <= count + 1'b1;
            else if (do_rd && !do_wr) count <= count - 1'b1;
        end
    end
endmodule
