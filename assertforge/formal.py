"""Drive SymbiYosys from Python and turn its output into a verdict plus a trace.

The solver runs inside WSL (Ubuntu) where yosys/sby/z3 are installed; this
module is the bridge. Two things it does that the raw sby CLI does not:

1. It classifies the outcome into PROVED / REFUTED / SYNTAX / ERROR / TIMEOUT
   rather than making the caller grep the log.
2. On REFUTED it parses the counterexample VCD into a cycle-by-cycle table,
   because a counterexample the generator cannot read is not feedback.

The trace table is the whole point of the refine loop: it is what gets handed
back to the model so its next assertion is informed by the exact cycle that
broke it.
"""

import base64
import os
import re
import subprocess
import time

WSL_DISTRO = os.environ.get("ASSERTFORGE_WSL", "Ubuntu")

PROVED, REFUTED, SYNTAX, ERROR, TIMEOUT, NOINPUT = (
    "PROVED", "REFUTED", "SYNTAX", "ERROR", "TIMEOUT", "NOINPUT")


class FormalResult:
    def __init__(self, status, seconds=0.0, log="", trace=None, raw_returncode=None):
        self.status = status
        self.seconds = seconds
        self.log = log
        self.trace = trace or []            # list of {time, signals}
        self.raw_returncode = raw_returncode

    @property
    def ok(self):
        return self.status == PROVED

    def summary(self):
        line = "%s in %.2fs" % (self.status, self.seconds)
        if self.trace:
            line += " (%d cycle(s) of counterexample)" % len(self.trace)
        return line

    def trace_table(self, max_rows=12, max_cols=14):
        if not self.trace:
            return ""
        sigs = []
        for row in self.trace:
            for k in row["signals"]:
                if k not in sigs:
                    sigs.append(k)
        sigs = sigs[:max_cols]
        w = max(len(s) for s in sigs) if sigs else 4
        out = ["%-6s %s" % ("cycle", " ".join("%*s" % (w, s) for s in sigs))]
        for i, row in enumerate(self.trace[:max_rows]):
            vals = []
            for s in sigs:
                v = row["signals"].get(s, "-")
                if isinstance(v, int):
                    v = "%d" % v
                vals.append("%*s" % (w, v))
            out.append("%-6d %s" % (i, " ".join(vals)))
        return "\n".join(out)

    def to_dict(self):
        return {"status": self.status, "seconds": round(self.seconds, 2),
                "trace_cycles": len(self.trace), "log_tail": self.log[-1500:]}


def win_to_wsl(path):
    """D:\\a\\b -> /mnt/d/a/b ; leaves unix paths alone."""
    p = os.path.abspath(path).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        return "/mnt/" + m.group(1).lower() + "/" + m.group(2)
    return p


def wsl_bash(script, timeout=120):
    """Run a bash script inside WSL with the script carried as base64.

    Base64 rather than argv because this host passes non-ASCII arguments
    through `wsl.exe` unreliably, and the project lives under a Chinese path.
    """
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    cmd = "echo %s | base64 -d > /tmp/af_run.sh && bash /tmp/af_run.sh" % b64
    try:
        r = subprocess.run(
            ["wsl", "-d", WSL_DISTRO, "--", "bash", "-lc", cmd],
            capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "replace"), r.returncode
    except subprocess.TimeoutExpired:
        return "", -9


def parse_vcd(text, max_signals=24):
    """Minimal VCD reader -> [{'time': int, 'signals': {name: value}}].

    Scope prefixes are stripped so `counter_t.cnt` shows up as `cnt`, which is
    what a language model can reason about without a hierarchy lecture.
    """
    ids = {}       # vcd id code -> short name
    widths = {}
    times = []
    cur = None
    t = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("$var"):
            parts = line.split()
            if len(parts) >= 5:
                width, code, name = parts[2], parts[3], parts[4]
                short = name.split(".")[-1]
                ids[code] = short
                widths[short] = int(width) if width.isdigit() else 1
        elif line.startswith("#"):
            try:
                t = int(line[1:])
            except ValueError:
                continue
            cur = {"time": t, "signals": {}}
            times.append(cur)
        elif line[0] in "01xzXZ" and len(line) > 1:
            code, val = line[0], line[1:]
            if code in ids and cur is not None:
                cur["signals"][ids[code]] = val
        elif line[0] in "bB":
            parts = line.split()
            if len(parts) == 2:
                val, code = parts[0][1:], parts[1]
                if code in ids and cur is not None:
                    try:
                        cur["signals"][ids[code]] = int(val, 2)
                    except ValueError:
                        cur["signals"][ids[code]] = val
    # Hide the solver's own bookkeeping: `anyinit_*` registers exist only to
    # model uninitialised state, and dumping them on a model produces
    # confident commentary about signals that are not in the design.
    def is_internal(n):
        return (n.startswith("anyinit_") or n.startswith("anyseq")
                or n.startswith("smt_step") or "$" in n or n.startswith("_"))

    rows = [r for r in times if "clk" not in r["signals"] or r["signals"]["clk"] in (1, "1")]
    if not rows:
        rows = times

    used = []
    for name in ids.values():
        if name in used or is_internal(name):
            continue
        vals = [row["signals"].get(name) for row in rows]
        if len(set(map(str, vals))) > 1 or name in ("rst",):
            used.append(name)
    used = used[:max_signals]
    out = []
    for row in rows:
        sig = {k: v for k, v in row["signals"].items() if k in used}
        if sig:
            out.append({"time": row["time"], "signals": sig})
    return out


def classify(log):
    if "DONE (PASS" in log:
        return PROVED
    if "DONE (FAIL" in log:
        return REFUTED
    if "syntax error" in log or "ERROR: syntax" in log:
        return SYNTAX
    if "Can't open input file" in log or "No such file" in log:
        return NOINPUT
    if "prep -top" in log and "ERROR" in log:
        return ERROR
    return ERROR


def run(workdir, top, sources, assertions, mode="prove", depth=10,
        engine="smtbmc z3", timeout=180, read_opt="-formal -sv", quiet=True):
    """Run sby over `sources` plus `assertions` and return a FormalResult.

    workdir is created if missing; the .sby job and its output stay there so a
    failed run can be inspected rather than guessed at.
    """
    os.makedirs(workdir, exist_ok=True)
    job = os.path.join(workdir, "job.sby")
    files = list(sources) + list(assertions)
    with open(job, "w", encoding="utf-8", newline="\n") as f:
        f.write("[options]\nmode %s\ndepth %d\n" % (mode, depth))
        f.write("[engines]\n%s\n" % engine)
        f.write("[files]\n" + "\n".join(files) + "\n")
        f.write("[script]\n")
        for s in sources:
            f.write("read -formal %s\n" % s)
        for a in assertions:
            f.write("read %s %s\n" % (read_opt, a))
        f.write("prep -top %s\n" % top)

    mount = win_to_wsl(workdir)
    script = (
        "cd %s || exit 7\n"
        "sby -f job.sby 2>&1\n" % mount
    )

    t0 = time.time()
    log, rc = wsl_bash(script, timeout=timeout)
    dt = time.time() - t0

    trace = []
    trace_vcd = os.path.join(workdir, "job", "engine_0", "trace.vcd")
    if os.path.exists(trace_vcd):
        try:
            trace = parse_vcd(open(trace_vcd, encoding="utf-8", errors="ignore").read())
        except Exception:
            trace = []

    status = TIMEOUT if rc == -9 else classify(log)
    return FormalResult(status, dt, log, trace, rc)


def toolchain_check():
    """Is the solver actually present? Returns (ok, version_string)."""
    out, rc = wsl_bash(
        "yosys -V 2>/dev/null; sby --version 2>&1 | head -1; z3 --version 2>/dev/null", 60)
    ok = "Yosys" in out and "SBY" in out
    return ok, " | ".join(x.strip() for x in out.splitlines() if x.strip())
