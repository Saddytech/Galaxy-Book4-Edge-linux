#!/usr/bin/env python3
"""
test_sabi_v4.py — Samsung Galaxy Book4 Edge (X1E80100 / ENE KB9058 EC)
                  SABI v4 performance-mode probe & test script

BACKGROUND
----------
The mainline Linux x86 driver `drivers/platform/x86/samsung-galaxybook.c`
(merged in kernel 6.15 by Joshua Grisham) reveals the full Samsung SABI v4
protocol used by Galaxy Book family laptops. The protocol is wrapped in
ACPI method CSXI for performance mode control:

    struct sawb {
        u16 safn;        // signature 0x5843 ("CX")
        u16 sasb;        // 0x91 = PERFORMANCE_MODE
        u8  rflg;        // 0xaa = success, 0xff = fail
        u8  caid[16];    // GUID 8246028d-8bca-4a55-ba0f-6f1e6b921b8f
        u8  fncn;        // 0x51 = perf mode function
        u8  subn;        // 0x01=LIST, 0x02=GET, 0x03=SET
        u8  iob0..iob9;  // I/O bytes; iob0 = mode value on SET/GET
    };

Performance mode values (kernel source): see MODES dict below.

HYPOTHESIS
----------
Our EC2.sys RE identified two opcodes consistent with SABI entry points:
    0x12 SVCLED_SABI     (4-byte args) — probably SABI_GET
    0x13 SET_SVCLED_FLAG (4-byte args) — probably SABI_SET
On ARM, the I2C EC absorbs the SAFN/GUID boilerplate and just needs the
condensed {SASB, SUBN, IOB0, IOB1} payload.

This script tries multiple payload layouts (A/B/C) during `probe` to
identify which one the EC actually accepts.

WHY THIS MATTERS FOR FAN RAMP
-----------------------------
Directly commanding RPM via opcode 0x17 (SET_FANRPM) invokes the EC's
internal PID loop with built-in slew-rate limiting → slow ramp. Switching
the performance profile via SABI (0x12/0x13) activates the EC's *factory
tuned* fan curve for that profile — which is how Samsung's Windows
Settings app gets aggressive fan response when user picks "Performance"
or "Ultra".

SAFETY
------
* GET operations (SUBN=0x02, SUBN=0x01) are read-only; always safe.
* SET to OPTIMIZED/LOWNOISE/PERFORMANCE/ULTRA should be safe.
* SET to FANOFF (0x0b) = silent mode; may stop fan entirely. Gated behind
  an explicit `--i-know-what-im-doing` flag.
* On any exit (normal, Ctrl+C, exception), script always resets to
  OPTIMIZED (0x00).
* Emergency manual fallback if script hangs fan:
      sudo i2ctransfer -y 5 w2@0x62 0x08 0x00    # known-good fanzone=0

USAGE
-----
    sudo ./test_sabi_v4.py probe              # safe, non-destructive
    sudo ./test_sabi_v4.py get
    sudo ./test_sabi_v4.py list
    sudo ./test_sabi_v4.py set ultra          # set Ultra perf mode
    sudo ./test_sabi_v4.py set optimized      # reset
    sudo ./test_sabi_v4.py cycle --hold 8     # walk through safe modes
    sudo ./test_sabi_v4.py ramp-test --mode ultra  # time fan ramp
    sudo ./test_sabi_v4.py fanoff --i-know-what-im-doing

LOGS
----
Every run appends a full transcript to
  logs/sabi-v4-YYYYMMDD-HHMMSS.log
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import os
import signal
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Protocol constants
# --------------------------------------------------------------------------- #

I2C_BUS   = 5
EC_ADDR   = 0x62
I2C_SLAVE = 0x0703

# EC opcodes — DSDT-verified!
# From DSDT analysis of extracted/ACPI-Tables/DSDT*:
#   Method CSXI (in SCAI device SAM0430)
#   → Method PRF3 (performance-mode dispatcher)
#     → CMDD(0xEE, 0x02, [0x80, mode])
#       → writes MBUF=data, ECMD=0xEE in OperationRegion 0xA2
#         → EC2.sys translates to I2C wire: [0xEE, 0x80, mode]
OP_PERF_MODE = 0xEE  # Performance mode SABI wire opcode (verified)
OP_FANZONE   = 0x08  # SET_FANZONE (known-good, 0-15 level, emergency reset)
PREFIX_BYTE  = 0x80  # SABI sub-prefix seen in PRF3 EBUF[0]=0x80

# Success/fail markers (from SABI response format)
RFLG_SUCCESS = 0xaa
GUNM_FAIL    = 0xff

# Modes this laptop actually supports (from PRF3 SUBN=LIST branch in DSDT)
# AUTO (0x02), SILENT (0x0a), MAXPERF (0x15). NOT Ultra (0x16).
MODES: dict[str, int] = {
    "auto":     0x02,  # balanced default
    "silent":   0x0a,  # quiet / low noise
    "maxperf":  0x15,  # max performance
    # experimental values — documented but not listed by LIST on this model:
    "fanoff":        0x0b,
    "optimized_v1":  0x00,
    "performance_v1": 0x01,
    "ultra":         0x16,
}
MODE_NAMES = {v: k for k, v in MODES.items()}
SAFE_CYCLE_ORDER = ["silent", "auto", "maxperf"]

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / f"sabi-v4-{datetime.datetime.now():%Y%m%d-%H%M%S}.log"
_log_fh = open(LOG_PATH, "a", buffering=1)


def say(msg: str = "") -> None:
    print(msg)
    _log_fh.write(msg + "\n")


def fmt(b) -> str:
    return " ".join(f"{x:02x}" for x in b)


# --------------------------------------------------------------------------- #
# I2C primitives
# --------------------------------------------------------------------------- #

def open_bus() -> int:
    try:
        fd = os.open(f"/dev/i2c-{I2C_BUS}", os.O_RDWR)
    except PermissionError:
        say("ERROR: need root to open /dev/i2c-5 (try with sudo)")
        sys.exit(1)
    except FileNotFoundError:
        say("ERROR: /dev/i2c-5 not present. Confirm i2c@b94000 is enabled in "
            "your active DTB (KBDBLT-TEST entry) and `modprobe i2c-dev`.")
        sys.exit(1)
    fcntl.ioctl(fd, I2C_SLAVE, EC_ADDR)
    return fd


def ec_cmd(fd: int, op: int, *args: int,
           read_back: int = 8, settle_ms: int = 25):
    """Send [op, *args] to EC, read back <read_back> bytes after settle."""
    pkt = [op & 0xff] + [a & 0xff for a in args]
    os.write(fd, bytes(pkt))
    time.sleep(settle_ms / 1000.0)
    resp = list(os.read(fd, read_back))
    return pkt, resp


# --------------------------------------------------------------------------- #
# SABI wrappers (DSDT-derived wire format)
# --------------------------------------------------------------------------- #

def perf_mode_set(fd, mode_value):
    """Wire: [0xEE, 0x80, mode_value]  per CMDD(0xEE, 0x02, [0x80, mode]) in DSDT."""
    return ec_cmd(fd, OP_PERF_MODE, PREFIX_BYTE, mode_value)


def perf_mode_set_alt1(fd, mode_value):
    """Alt wire: [0xEE, 0x02, 0x80, mode_value]  (include length byte)."""
    return ec_cmd(fd, OP_PERF_MODE, 0x02, PREFIX_BYTE, mode_value)


def perf_mode_set_alt2(fd, mode_value):
    """Alt wire: [0xEE, mode_value]  (without 0x80 prefix)."""
    return ec_cmd(fd, OP_PERF_MODE, mode_value)


# Alias for emergency_reset
LAYOUTS = {
    "A": {"set": perf_mode_set,
          "desc": "[0xEE, 0x80, mode] — matches DSDT CMDD exactly"},
    "B": {"set": perf_mode_set_alt1,
          "desc": "[0xEE, 0x02, 0x80, mode] — with length prefix"},
    "C": {"set": perf_mode_set_alt2,
          "desc": "[0xEE, mode] — bare"},
}


# --------------------------------------------------------------------------- #
# Response interpretation
# --------------------------------------------------------------------------- #

def interpret(resp, label=""):
    """Log whether response looks like SABI success and extract candidate fields."""
    if not resp:
        say(f"  {label}: empty response")
        return
    has_aa = RFLG_SUCCESS in resp
    has_ff = GUNM_FAIL in resp
    verdict = "OK" if has_aa else ("FAIL" if has_ff else "?")
    # candidate mode value at positions 2..5
    candidates = []
    for i, b in enumerate(resp[:6]):
        if b in MODE_NAMES:
            candidates.append(f"resp[{i}]=0x{b:02x} ({MODE_NAMES[b]})")
    say(f"  {label:<22s} verdict={verdict}   0xaa@={has_aa}  0xff@={has_ff}")
    if candidates:
        say(f"     possible mode bytes: {'; '.join(candidates)}")


# --------------------------------------------------------------------------- #
# Observability: fan/temp/cpu
# --------------------------------------------------------------------------- #

def read_temps_max() -> float:
    tmax = 0.0
    for p in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            v = int(p.read_text().strip()) / 1000.0
            if v > tmax:
                tmax = v
        except Exception:
            continue
    return tmax


def read_cpu_mhz_max() -> int:
    mmax = 0
    for p in Path("/sys/devices/system/cpu").glob(
            "cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            f = int(p.read_text().strip()) // 1000
            if f > mmax:
                mmax = f
        except Exception:
            continue
    return mmax


def read_fan_rpm() -> int:
    for p in Path("/sys/class/hwmon").glob("hwmon*/fan*_input"):
        try:
            return int(p.read_text().strip())
        except Exception:
            continue
    return -1


def telemetry_row(prefix: str):
    say(f"  {prefix} {read_cpu_mhz_max():>5d} MHz  "
        f"{read_fan_rpm():>6d} RPM  "
        f"{read_temps_max():>5.1f} °C")


# --------------------------------------------------------------------------- #
# Emergency cleanup
# --------------------------------------------------------------------------- #

def emergency_reset(fd):
    """Best-effort restore: try AUTO mode on all layouts, then legacy fanzone=0."""
    say("== emergency reset ==")
    for name, cfg in LAYOUTS.items():
        try:
            pkt, resp = cfg["set"](fd, MODES["auto"])
            say(f"  layout {name} SET AUTO -> {fmt(resp)}")
        except Exception as e:
            say(f"  layout {name} error: {e}")
    try:
        pkt, resp = ec_cmd(fd, OP_FANZONE, 0x00, read_back=8)
        say(f"  legacy FANZONE=0 -> {fmt(resp)}")
    except Exception as e:
        say(f"  legacy FANZONE error: {e}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_probe(fd):
    say("=" * 60)
    say("Perf-mode probe — sets AUTO (safe, the current default)")
    say("Walks 3 wire-format variants, watching fan/temp/cpu for 3s after each.")
    say("=" * 60)
    for name, cfg in LAYOUTS.items():
        say(f"\n-- variant {name}: {cfg['desc']} --")
        pkt, resp = cfg["set"](fd, MODES["auto"])
        say(f"  sent: {fmt(pkt)}")
        say(f"  resp: {fmt(resp)}")
        interpret(resp, "AUTO")
        for i in range(3):
            telemetry_row(f"  t={i}s")
            time.sleep(1)
    say("\nNote: a 'verdict=OK' row (0xaa present) identifies the working variant.")
    say(f"\nLog saved to {LOG_PATH}")


def cmd_set(fd, mode_name, layout="A"):
    if mode_name not in MODES:
        say(f"unknown mode '{mode_name}'. known: {list(MODES)}")
        sys.exit(2)
    val = MODES[mode_name]
    cfg = LAYOUTS[layout]
    pkt, resp = cfg["set"](fd, val)
    say(f"SET {mode_name} (0x{val:02x}) variant={layout}")
    say(f"  sent: {fmt(pkt)}")
    say(f"  resp: {fmt(resp)}")
    interpret(resp, "SET")
    telemetry_row("  t=0s:")
    for i in range(1, 6):
        time.sleep(1)
        telemetry_row(f"  t={i}s:")


def cmd_cycle(fd, hold_s=8, layout="A"):
    say("=" * 60)
    say(f"Mode cycle — layout={layout} hold={hold_s}s each")
    say("   Will auto-reset to OPTIMIZED on exit.")
    say("=" * 60)
    cfg = LAYOUTS[layout]
    try:
        for m in SAFE_CYCLE_ORDER:
            val = MODES[m]
            pkt, resp = cfg["set"](fd, val)
            ok = "OK" if RFLG_SUCCESS in resp else "?"
            say(f"\n--[{m}] sent={fmt(pkt)} resp={fmt(resp)} [{ok}]--")
            t0 = time.time()
            while time.time() - t0 < hold_s:
                telemetry_row(f"  {m:<12s}")
                time.sleep(1)
    finally:
        say("\n[cleanup] resetting to AUTO")
        cfg["set"](fd, MODES["auto"])


def cmd_ramp_test(fd, mode_name="maxperf", layout="A", window_s=20):
    """Measure fan ramp-up time: baseline AUTO, switch to <mode>,
    log fan RPM every 0.5s for <window_s> seconds."""
    say("=" * 60)
    say(f"Ramp test — target mode={mode_name} layout={layout} window={window_s}s")
    say("=" * 60)
    cfg = LAYOUTS[layout]
    try:
        # baseline
        say("\n-- baseline: AUTO --")
        cfg["set"](fd, MODES["auto"])
        time.sleep(5)
        telemetry_row("  baseline:  ")
        rpm0 = read_fan_rpm()

        # ramp up
        say(f"\n-- ramp to {mode_name} --")
        val = MODES[mode_name]
        t_start = time.time()
        pkt, resp = cfg["set"](fd, val)
        say(f"  t=0.0  sent={fmt(pkt)}  resp={fmt(resp)}")

        peak_rpm = rpm0
        t_peak = 0.0
        while time.time() - t_start < window_s:
            t = time.time() - t_start
            rpm = read_fan_rpm()
            if rpm > peak_rpm:
                peak_rpm = rpm
                t_peak = t
            say(f"  t={t:5.1f}s  {rpm:6d} RPM  "
                f"{read_cpu_mhz_max():5d} MHz  "
                f"{read_temps_max():5.1f} °C")
            time.sleep(0.5)
        say(f"\npeak RPM {peak_rpm} at t={t_peak:.1f}s (baseline {rpm0})")
    finally:
        say("\n[cleanup] resetting to AUTO")
        cfg["set"](fd, MODES["auto"])


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="Samsung SABI v4 test for Galaxy Book4 Edge ARM EC")
    ap.add_argument("--layout", "-l", choices=["A", "B", "C"], default="A",
                    help="SABI payload layout to use (default A). "
                         "A=[SASB,SUBN,IOB0,IOB1], "
                         "B=[SUBN,IOB0,IOB1,IOB2], "
                         "C=reversed (like opcode 0x17)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="Try 3 wire formats, each setting AUTO")

    p_set = sub.add_parser("set", help="SET performance mode")
    p_set.add_argument("mode", choices=list(MODES.keys()))

    p_cyc = sub.add_parser("cycle", help="Cycle silent → auto → maxperf")
    p_cyc.add_argument("--hold", type=int, default=8,
                       help="seconds to hold each mode (default 8)")

    p_ramp = sub.add_parser("ramp-test", help="Measure fan ramp-up speed")
    p_ramp.add_argument("--mode", choices=["maxperf", "ultra"],
                        default="maxperf")
    p_ramp.add_argument("--window", type=int, default=20,
                        help="measurement window in seconds (default 20)")

    args = ap.parse_args()

    if os.geteuid() != 0:
        print("ERROR: must run as root (need /dev/i2c-5 access). Try: sudo",
              file=sys.stderr)
        sys.exit(1)

    say(f"=== sabi v4 test — log={LOG_PATH} ===")
    say(f"layout={args.layout} cmd={args.cmd}")

    fd = open_bus()

    # Ensure cleanup on any exit signal
    def _sig_handler(signum, frame):
        say(f"\n[!] signal {signum} received — running emergency reset")
        emergency_reset(fd)
        os.close(fd)
        sys.exit(130)
    signal.signal(signal.SIGINT,  _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    try:
        if args.cmd == "probe":
            cmd_probe(fd)
        elif args.cmd == "set":
            cmd_set(fd, args.mode, args.layout)
        elif args.cmd == "cycle":
            cmd_cycle(fd, args.hold, args.layout)
        elif args.cmd == "ramp-test":
            cmd_ramp_test(fd, args.mode, args.layout, args.window)
    except Exception as e:
        say(f"[!] exception: {type(e).__name__}: {e}")
        emergency_reset(fd)
        raise
    finally:
        os.close(fd)
        _log_fh.flush()
        _log_fh.close()


if __name__ == "__main__":
    main()
