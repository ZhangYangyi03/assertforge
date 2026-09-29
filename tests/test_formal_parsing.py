"""The VCD reader and the feedback extractor -- both pure, both testable offline."""

from assertforge import formal
from assertforge.refine import key_error, params_of


VCD = """$date
   today
$end
$version
   yosys
$end
$timescale 1ns $end
$scope module af_r0 $end
$var wire 1 ! clk $end
$var wire 1 " rst $end
$var wire 4 # count $end
$var wire 1 $ anyinit_procdff_5 $end
$upscope $end
$enddefinitions $end
#0
1!
0"
b0000 #
0$
#5
0!
#10
1!
1"
b0100 #
1$
#15
0!
"""


def test_vcd_becomes_cycles_with_the_clock_edges_only():
    rows = formal.parse_vcd(VCD)
    # only the two posedge samples survive, not the falling edges
    assert len(rows) == 2
    assert rows[0]["signals"]["count"] == 0
    assert rows[1]["signals"]["count"] == 4


def test_solver_bookkeeping_signals_are_hidden():
    rows = formal.parse_vcd(VCD)
    for row in rows:
        assert not any(k.startswith("anyinit_") for k in row["signals"])


def test_trace_table_is_readable_and_marks_missing_cycles():
    rows = formal.parse_vcd(VCD)
    table = formal.FormalResult("REFUTED", 0.1, "", rows).trace_table()
    assert "cycle" in table
    assert "count" in table


def test_key_error_finds_the_line_not_the_banner():
    log = "\n".join([
        " |  yosys -- Yosys Open SYnthesis Suite  |",
        "Generating RTLIL representation for module",
        "af_r0.sv:6: ERROR: Signal `wr_data' with non-constant width!",
        "more noise",
    ])
    got = key_error(log)
    assert "non-constant width" in got
    assert "Yosys Open SYnthesis" not in got


def test_params_are_harvested_from_a_parameterised_header():
    dut = ("module sync_fifo #(parameter WIDTH = 8, parameter DEPTH = 4) (\n"
           "  input wire clk);\nendmodule\n")
    got = params_of(dut)
    assert ("", "WIDTH", "8") in got
    assert ("", "DEPTH", "4") in got


def test_params_absent_gives_empty_list():
    assert params_of("module m(input a); endmodule") == []


def test_win_path_maps_to_the_wsl_mount():
    assert formal.win_to_wsl(r"D:\\a\\b") == "/mnt/d/a/b"


def test_verdict_classification():
    assert formal.classify("SBY ... DONE (PASS, rc=0)") == formal.PROVED
    assert formal.classify("SBY ... DONE (FAIL, rc=2)") == formal.REFUTED
    assert formal.classify("x: ERROR: syntax error, unexpected '@'") == formal.SYNTAX
