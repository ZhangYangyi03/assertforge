// A round-robin arbiter -- small enough to prove, subtle enough to get wrong.
module rr_arbiter #(parameter N = 4) (
    input  wire         clk,
    input  wire         rst,
    input  wire [N-1:0] req,
    output reg  [N-1:0] grant
);
    reg [N-1:0] mask;
    integer i;
    always @(posedge clk) begin
        if (rst) begin
            grant <= 0;
            mask  <= {N{1'b1}};
        end else begin
            grant <= 0;
            for (i = 0; i < N; i = i + 1) begin
                if (req[i] && mask[i] && (grant == 0))
                    grant[i] <= 1'b1;
            end
            if (|req) mask <= ~grant;
            else      mask <= {N{1'b1}};
        end
    end
endmodule
