// A traffic light. The invariant is the ORDER: red may only be followed by
// red-and-yellow, and a transition that skips that state is the classic bug.
module traffic_fsm (
    input  wire       clk,
    input  wire       rst,
    input  wire       tick,
    output reg  [1:0] state
);
    localparam RED = 2'd0, RED_YELLOW = 2'd1, GREEN = 2'd2, YELLOW = 2'd3;
    always @(posedge clk) begin
        if (rst) state <= RED;
        else if (tick) begin
            case (state)
                RED:        state <= RED_YELLOW;
                RED_YELLOW: state <= GREEN;
                GREEN:      state <= YELLOW;
                default:    state <= RED;
            endcase
        end
    end
endmodule
