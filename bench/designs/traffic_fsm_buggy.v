// The same traffic light, with the safety bug that matters: red goes straight to
// green. Every "one-hot state" and "state is legal" property still proves.
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
                RED:        state <= GREEN;
                RED_YELLOW: state <= GREEN;
                GREEN:      state <= YELLOW;
                default:    state <= RED;
            endcase
        end
    end
endmodule
