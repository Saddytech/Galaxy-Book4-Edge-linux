#!/usr/bin/env python3
"""
stress_aggressive.py — proves that forcing the fan harder keeps the CPU at max

Samsung's EC firmware prefers to THROTTLE CPU over spinning the fan at 100%.
Even MAXPERF mode only raises the throttle trip by a few degrees. This script
takes control away from the EC:

    while running:
        if cpu_mhz <  THROTTLE_THRESHOLD → FANZONE = 15 (force max fan)
        if cpu_mhz >= CLEAR_THRESHOLD    → optionally release

The FANZONE override is written via the ioctl path at i2c-5@0x62 (opcode 0x08),
which bypasses the EC's thermal-curve logic entirely and directly sets the fan
level 0-15.

While this runs, we also switch the Samsung perf mode to MAXPERF via the Mbox
path at i2c-2@0x64 (the path we proved earlier).

Schedule:
    Phase 1 [  0s –  30s]  baseline: stress + AUTO, no fan override
                           (shows the baseline throttle behavior)
    Phase 2 [ 30s – 180s]  AGGRESSIVE: stress + MAXPERF + force FANZONE=15
                           when cpu throttles
    Phase 3 [180s – 240s]  release: stress + AUTO, let things recover

Usage:
    sudo ./stress_aggressive.py               # 4-min default
    sudo ./stress_aggressive.py --duration 600
"""
from __future__ import annotations

import argparse
import ctypes
import datetime
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# I2C endpoints + constants
# --------------------------------------------------------------------------- #

# Mbox (perf mode) — ACPI I2C1, MMIO 0xB80000
MBOX_BUS   = 2
MBOX_ADDR  = 0x64

# Ioctl (fan override) — ACPI I2C6, MMIO 0xB94000
IOCTL_BUS  = 5
IOCTL_ADDR = 0x62

I2C_SLAVE = 0x0703
I2C_RDWR  = 0x0707
I2C_M_RD  = 0x0001

# Mbox protocol
WRITE_PREFIX = 0x40
READ_PREFIX  = 0x30
READ_SUCCESS = 0x50

# EC register map
FANS_REG  = 0x87
CTMP_REG  = 0xC1
CET1_REG  = 0xC2
CET2_REG  = 0xC3
ECMD_REG  = 0xA2
MBUF0_REG = 0xA3
MBUF1_REG = 0xA4

# Perf modes
PERF_AUTO    = 0x02
PERF_SILENT  = 0x0A
PERF_MAXPERF = 0x15

# Ioctl opcodes
OP_FANZONE = 0x08   # direct fan level 0-15

# Throttle detection
BASELINE_MHZ  = 3800   # if CPU drops below this, we consider it throttled
RELEASE_MHZ   = 3900   # if CPU recovers above this (for hysteresis)
RELEASE_HOLD  = 5      # seconds of recovery before releasing forced fan
FORCE_LEVEL   = 15     # fan zone 0-15 when forcing

# --------------------------------------------------------------------------- #

class I2cMsg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16),
                ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16),
                ("buf", ctypes.POINTER(ctypes.c_char))]

class I2cRdwrIoctlData(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(I2cMsg)),
                ("nmsgs", ctypes.c_uint32)]


def open_i2c(bus: int, slave: int) -> int:
    fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
    fcntl.ioctl(fd, I2C_SLAVE, slave)
    return fd


def i2c_w(fd, data: bytes, settle_ms: int = 2):
    os.write(fd, data)
    time.sleep(settle_ms / 1000.0)


def i2c_wr_rd(fd, slave, wdata: bytes, rlen: int) -> list[int]:
    wbuf = ctypes.create_string_buffer(wdata, len(wdata))
    rbuf = ctypes.create_string_buffer(rlen)
    msgs = (I2cMsg * 2)(
        I2cMsg(addr=slave, flags=0,
               len=len(wdata),
               buf=ctypes.cast(wbuf, ctypes.POINTER(ctypes.c_char))),
        I2cMsg(addr=slave, flags=I2C_M_RD,
               len=rlen,
               buf=ctypes.cast(rbuf, ctypes.POINTER(ctypes.c_char))),
    )
    data = I2cRdwrIoctlData(msgs=msgs, nmsgs=2)
    fcntl.ioctl(fd, I2C_RDWR, data)
    return list(rbuf.raw)


# Mbox primitives (i2c-2@0x64)
def mbox_w(fd, cmd_hi, cmd_lo, data):
    i2c_w(fd, bytes([WRITE_PREFIX, 0, cmd_hi & 0xff, cmd_lo & 0xff, data & 0xff]))


def ec_read(fd, reg):
    try:
        mbox_w(fd, 0xF4, 0x80, reg)
        mbox_w(fd, 0xFF, 0x10, 0x88)
        resp = i2c_wr_rd(fd, MBOX_ADDR, bytes([READ_PREFIX, 0, 0xF4, 0x80]), 2)
        return resp[1] if resp[0] == READ_SUCCESS else -1
    except OSError:
        return -1


def ec_write(fd, reg, value):
    mbox_w(fd, 0xF4, 0x80, reg)
    mbox_w(fd, 0xF4, 0x81, value)
    mbox_w(fd, 0xFF, 0x10, 0x89)


def set_perf(fd, mode):
    ec_write(fd, MBUF0_REG, 0x80)
    ec_write(fd, MBUF1_REG, mode)
    ec_write(fd, ECMD_REG, 0xEE)


# Ioctl fan override (i2c-5@0x62)
def force_fanzone(fd_ioctl, level):
    """Legacy opcode 0x08: directly command fan level 0-15."""
    try:
        os.write(fd_ioctl, bytes([OP_FANZONE, level & 0x0f]))
    except OSError as e:
        print(f"  [!] fanzone write err: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #

def read_cpu_mhz_max() -> int:
    m = 0
    for p in Path("/sys/devices/system/cpu").glob(
            "cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            m = max(m, int(p.read_text().strip()) // 1000)
        except Exception:
            continue
    return m


def read_kern_c() -> float:
    t = 0.0
    for p in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            t = max(t, int(p.read_text().strip()) / 1000.0)
        except Exception:
            continue
    return t


# --------------------------------------------------------------------------- #

def kill_stress():
    subprocess.run(["pkill", "-9", "stress-ng"],
                   check=False, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


_cleanup_done = False

def full_cleanup(fd_mbox, fd_ioctl):
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    kill_stress()
    try:
        force_fanzone(fd_ioctl, 0)   # release manual override
    except Exception:
        pass
    try:
        set_perf(fd_mbox, PERF_AUTO)
    except Exception:
        pass
    for fd in (fd_mbox, fd_ioctl):
        try:
            os.close(fd)
        except Exception:
            pass


def run(duration: int):
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = log_dir / f"aggressive-{stamp}.csv"

    print(f"CSV: {csv_path}")
    print(f"Duration: {duration}s   "
          f"throttle_thresh={BASELINE_MHZ}MHz  release={RELEASE_MHZ}MHz")

    # phases: baseline_AUTO, aggressive_MAXPERF+override, release_AUTO
    phase_splits = [int(duration * 0.125),        # end of phase 1 = 12.5%
                    int(duration * (0.125 + 0.625)),  # end of phase 2 = 75%
                    duration]                           # end of phase 3
    print(f"Phases:  [0..{phase_splits[0]}s] baseline AUTO   "
          f"[{phase_splits[0]}..{phase_splits[1]}s] AGGRESSIVE MAXPERF+fan  "
          f"[{phase_splits[1]}..{phase_splits[2]}s] release AUTO")

    fd_mbox  = open_i2c(MBOX_BUS,  MBOX_ADDR)
    fd_ioctl = open_i2c(IOCTL_BUS, IOCTL_ADDR)
    csv_fh = open(csv_path, "w", buffering=1)
    csv_fh.write("t,phase,mode,cpu_mhz,kern_c,cet1_c,cet2_c,forced_fan,"
                 "throttled\n")

    def _sig(signum, frame):
        print(f"\n[!] signal {signum} — cleanup")
        full_cleanup(fd_mbox, fd_ioctl)
        csv_fh.close()
        sys.exit(130)
    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)

    # stress-ng for the entire duration
    nproc = os.cpu_count() or 12
    stress_duration = phase_splits[1] + 5  # through phase 2, + buffer
    stress = subprocess.Popen(
        ["stress-ng", "--cpu", str(nproc),
         "--timeout", str(stress_duration)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Column header
    hdr = (f"{'t':>4} | {'phase':<15} | {'mode':<8} | "
           f"{'cpu_MHz':>7} | {'kern':>5} | {'cet1':>4} | {'cet2':>4} | "
           f"{'fan':>3} | {'thrott':>6}")
    print(hdr)
    print("-" * len(hdr))

    t_start = time.time()
    current_phase = "baseline_AUTO"
    current_mode = PERF_AUTO
    set_perf(fd_mbox, PERF_AUTO)
    forced = False
    recovery_started = None

    try:
        while True:
            t = time.time() - t_start
            if t >= duration:
                break

            # phase logic
            new_phase = None
            if t < phase_splits[0]:
                new_phase = ("baseline_AUTO", PERF_AUTO, False)
            elif t < phase_splits[1]:
                new_phase = ("aggressive", PERF_MAXPERF, True)
            else:
                new_phase = ("release_AUTO", PERF_AUTO, False)

            phase_name, want_mode, can_force = new_phase
            if phase_name != current_phase:
                print(f"\n-- phase change at t={t:.0f}s -> {phase_name} "
                      f"(perf={hex(want_mode)}) --")
                current_phase = phase_name
                current_mode = want_mode
                set_perf(fd_mbox, want_mode)
                # transition: if leaving aggressive, release forced fan
                if not can_force and forced:
                    force_fanzone(fd_ioctl, 0)
                    forced = False
                # transition: if entering release, kill stress too
                if phase_name == "release_AUTO":
                    kill_stress()

            # telemetry
            cpu = read_cpu_mhz_max()
            kern = read_kern_c()
            cet1 = ec_read(fd_mbox, CET1_REG)
            cet2 = ec_read(fd_mbox, CET2_REG)
            throttled = (cpu < BASELINE_MHZ)

            # aggressive policy
            if can_force:
                if throttled and not forced:
                    print(f"  !! throttle detected @ t={t:.0f}s  "
                          f"cpu={cpu}MHz — forcing FANZONE={FORCE_LEVEL}")
                    force_fanzone(fd_ioctl, FORCE_LEVEL)
                    forced = True
                    recovery_started = None
                elif not throttled and forced:
                    # hysteresis: only release after stable recovery
                    if cpu >= RELEASE_MHZ:
                        if recovery_started is None:
                            recovery_started = t
                        elif t - recovery_started >= RELEASE_HOLD:
                            print(f"  ** cpu stable at max for {RELEASE_HOLD}s "
                                  f"— releasing forced fan")
                            force_fanzone(fd_ioctl, 0)
                            forced = False
                            recovery_started = None
                    else:
                        recovery_started = None
                # periodically re-assert perf mode (EC may drop it)
                if int(t) % 10 == 0:
                    set_perf(fd_mbox, want_mode)

            row = (f"{int(t):4d} | {current_phase:<15} | "
                   f"{'MAXPERF' if current_mode == PERF_MAXPERF else 'AUTO':<8} | "
                   f"{cpu:>7} | {kern:>5.1f} | {cet1:>4} | {cet2:>4} | "
                   f"{'F15' if forced else 'ec':>3} | "
                   f"{'YES' if throttled else 'no':>6}")
            print(row)
            csv_fh.write(f"{t:.1f},{current_phase},"
                         f"{'MAXPERF' if current_mode == PERF_MAXPERF else 'AUTO'},"
                         f"{cpu},{kern:.1f},{cet1},{cet2},"
                         f"{'forced15' if forced else 'ec'},"
                         f"{'1' if throttled else '0'}\n")
            time.sleep(1)

    finally:
        print("\n[cleanup] reset fan, perf=AUTO, kill stress")
        full_cleanup(fd_mbox, fd_ioctl)
        csv_fh.close()
        print(f"CSV saved: {csv_path}")


def main():
    global BASELINE_MHZ
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=int, default=240,
                    help="total test duration in seconds (default 240 = 4 min)")
    ap.add_argument("--threshold", type=int, default=BASELINE_MHZ,
                    help=f"throttle threshold MHz (default {BASELINE_MHZ})")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("ERROR: run as root", file=sys.stderr)
        sys.exit(1)
    if subprocess.call(["which", "stress-ng"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        print("ERROR: stress-ng not installed", file=sys.stderr)
        sys.exit(1)

    BASELINE_MHZ = args.threshold
    run(args.duration)


if __name__ == "__main__":
    main()
