// A free-running counter. The property that matters is not "it counts" but
// "it advances by exactly one", which is the property a skip is invisible to.
module counter #(parameter W = 4) (
    input  wire         clk,
    input  wire         rst,
    input  wire         en,
    output reg  [W-1:0] cnt
);
    always @(posedge clk) begin
        if (rst) cnt <= {W{1'b0}};
        else if (en) cnt <= cnt + 1'b1;
    end
endmodule
