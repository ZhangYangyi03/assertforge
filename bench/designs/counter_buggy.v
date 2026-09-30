// The same counter with one realistic bug: at 7 it advances by two, so a
// property that only says "cnt is between 0 and 15" proves and a property that
// says "advances by exactly one" refutes.
module counter #(parameter W = 4) (
    input  wire         clk,
    input  wire         rst,
    input  wire         en,
    output reg  [W-1:0] cnt
);
    always @(posedge clk) begin
        if (rst) cnt <= {W{1'b0}};
        else if (en) cnt <= (cnt == 4'd7) ? cnt + 2'd2 : cnt + 1'b1;
    end
endmodule
