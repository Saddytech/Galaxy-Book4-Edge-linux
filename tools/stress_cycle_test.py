#!/usr/bin/env python3
"""
stress_cycle_test.py — long-duration thermal test for Galaxy Book4 Edge

Runs stress-ng at full CPU load and cycles through SILENT / AUTO / MAXPERF
performance modes on a fixed schedule, while continuously logging telemetry
every 2 seconds to a CSV file and to stdout.

Schedule (default total = 600s = 10 min):
    Phase 1  [  0s - 120s]  baseline stress on AUTO (warm-up & equilibrium)
    Phase 2  [120s - 240s]  MAXPERF (fan should be most aggressive)
    Phase 3  [240s - 360s]  SILENT  (fan quieter, CPU throttles)
    Phase 4  [360s - 480s]  AUTO    (back to balanced)
    Phase 5  [480s - 600s]  cool-down (stop stress, observe recovery)

Telemetry per 2s tick:
    t            — seconds since start
    mode         — current perf mode
    phase        — current phase name
    cpu_mhz      — max CPU frequency across all cores (sysfs)
    kernel_temp  — max thermal_zone temp (sysfs)
    ec_cet1      — EC thermistor 1 reading (°C)
    ec_cet2      — EC thermistor 2 reading (°C)
    fans_reg     — EC register 0x87 raw value
    ecmd_reg     — EC register 0xA2 (last command, for debugging)

Safety:
    - SIGINT / SIGTERM → reset to AUTO and kill stress-ng immediately
    - finally block always resets to AUTO on normal exit

Run with:
    sudo ./stress_cycle_test.py                 # default 10 min
    sudo ./stress_cycle_test.py --duration 1200 # 20 min
    sudo ./stress_cycle_test.py --brief         # 3-minute quick cycle

Reports are saved to:
    logs/stress-cycle-YYYYMMDD-HHMMSS.csv
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
import threading
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Protocol + I2C (copied from test_ec_mbox.py — kept self-contained)
# --------------------------------------------------------------------------- #

I2C_BUS   = 2              # b80000 = ACPI I2C1 (Mbox/perf mode)
EC_ADDR   = 0x64           # EC second I2C endpoint
I2C_SLAVE = 0x0703
I2C_RDWR  = 0x0707
I2C_M_RD  = 0x0001

# Mbox protocol constants (verified via Ghidra decompile)
WRITE_PREFIX = 0x40
READ_PREFIX  = 0x30
READ_SUCCESS = 0x50

# EC register map (from DSDT OperationRegion ECR)
FANS_REG = 0x87
CTMP_REG = 0xC1
CET1_REG = 0xC2
CET2_REG = 0xC3
ECMD_REG = 0xA2
MBUF0_REG = 0xA3
MBUF1_REG = 0xA4

# Performance modes (verified via DSDT PRF3 SUBN=LIST)
PERF_AUTO    = 0x02
PERF_SILENT  = 0x0A
PERF_MAXPERF = 0x15
PERF_NAMES = {PERF_AUTO: "auto", PERF_SILENT: "silent", PERF_MAXPERF: "maxperf"}

# Ioctl path (i2c-5@0x62) — for emergency override only
IOCTL_BUS = 5
IOCTL_EC_ADDR = 0x62


# --------------------------------------------------------------------------- #
# I2C primitives
# --------------------------------------------------------------------------- #

class I2cMsg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16),
                ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16),
                ("buf", ctypes.POINTER(ctypes.c_char))]

class I2cRdwrIoctlData(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(I2cMsg)),
                ("nmsgs", ctypes.c_uint32)]


def open_i2c_bus(bus: int, slave: int) -> int:
    fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
    fcntl.ioctl(fd, I2C_SLAVE, slave)
    return fd


def i2c_write(fd: int, data: bytes, settle_ms: int = 2) -> None:
    os.write(fd, data)
    time.sleep(settle_ms / 1000.0)


def i2c_write_then_read(fd: int, wdata: bytes, rlen: int) -> list[int]:
    wbuf = ctypes.create_string_buffer(wdata, len(wdata))
    rbuf = ctypes.create_string_buffer(rlen)
    msgs = (I2cMsg * 2)(
        I2cMsg(addr=EC_ADDR, flags=0,
               len=len(wdata),
               buf=ctypes.cast(wbuf, ctypes.POINTER(ctypes.c_char))),
        I2cMsg(addr=EC_ADDR, flags=I2C_M_RD,
               len=rlen,
               buf=ctypes.cast(rbuf, ctypes.POINTER(ctypes.c_char))),
    )
    data = I2cRdwrIoctlData(msgs=msgs, nmsgs=2)
    fcntl.ioctl(fd, I2C_RDWR, data)
    return list(rbuf.raw)


# --------------------------------------------------------------------------- #
# Mbox protocol wrappers
# --------------------------------------------------------------------------- #

def mbox_write(fd: int, cmd_hi: int, cmd_lo: int, data: int) -> None:
    pkt = bytes([WRITE_PREFIX, 0x00,
                 cmd_hi & 0xff, cmd_lo & 0xff, data & 0xff])
    i2c_write(fd, pkt)


def mbox_read_byte(fd: int) -> tuple[int, int]:
    """After mbox_write(F4, 80, reg) + mbox_write(FF, 10, 88), read result."""
    wpkt = bytes([READ_PREFIX, 0x00, 0xF4, 0x80])
    resp = i2c_write_then_read(fd, wpkt, 2)
    return resp[0], resp[1]


def ec_reg_read(fd: int, reg: int) -> int:
    """Read one byte from EC register. Returns 0xFF on failure."""
    try:
        mbox_write(fd, 0xF4, 0x80, reg)    # set target
        mbox_write(fd, 0xFF, 0x10, 0x88)   # exec read
        status, data = mbox_read_byte(fd)
        if status == READ_SUCCESS:
            return data
    except OSError:
        pass
    return 0xFF


def ec_reg_write(fd: int, reg: int, value: int) -> None:
    mbox_write(fd, 0xF4, 0x80, reg)     # set target
    mbox_write(fd, 0xF4, 0x81, value)   # stage value
    mbox_write(fd, 0xFF, 0x10, 0x89)    # exec write


def set_perf_mode(fd: int, mode_value: int) -> None:
    """CMDD(0xEE, [0x80, mode]) — sets Samsung performance mode."""
    ec_reg_write(fd, MBUF0_REG, 0x80)
    ec_reg_write(fd, MBUF1_REG, mode_value)
    ec_reg_write(fd, ECMD_REG, 0xEE)


# --------------------------------------------------------------------------- #
# Observability
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


def read_kernel_temp_max() -> float:
    t = 0.0
    for p in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            v = int(p.read_text().strip()) / 1000.0
            t = max(t, v)
        except Exception:
            continue
    return t


# --------------------------------------------------------------------------- #
# Emergency cleanup
# --------------------------------------------------------------------------- #

def kill_stress() -> None:
    try:
        subprocess.run(["pkill", "-9", "stress-ng"],
                       check=False, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def emergency_reset(fd_mbox: int) -> None:
    kill_stress()
    try:
        set_perf_mode(fd_mbox, PERF_AUTO)
    except Exception as e:
        print(f"  [!] couldn't reset to AUTO via Mbox: {e}", file=sys.stderr)
    # Also try legacy ioctl-path fanzone=0 as a belt-and-braces
    try:
        fd_io = open_i2c_bus(IOCTL_BUS, IOCTL_EC_ADDR)
        try:
            os.write(fd_io, bytes([0x08, 0x00]))
        finally:
            os.close(fd_io)
    except Exception as e:
        print(f"  [!] couldn't send legacy FANZONE reset: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Main test
# --------------------------------------------------------------------------- #

def build_schedule(duration_s: int) -> list[tuple[str, int, int | None]]:
    """Return list of (phase_name, end_time_s, perf_mode_or_None_for_cooldown)."""
    # 5 equal phases by default
    q = duration_s / 5.0
    return [
        ("warmup_AUTO",     int(q * 1),  PERF_AUTO),
        ("max_MAXPERF",     int(q * 2),  PERF_MAXPERF),
        ("silent_SILENT",   int(q * 3),  PERF_SILENT),
        ("back_AUTO",       int(q * 4),  PERF_AUTO),
        ("cooldown_NOLOAD", int(q * 5),  None),        # stop stress, leave on AUTO
    ]


def run(duration_s: int) -> None:
    assert os.geteuid() == 0, "must run as root"

    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = log_dir / f"stress-cycle-{stamp}.csv"
    txt_path = log_dir / f"stress-cycle-{stamp}.log"

    schedule = build_schedule(duration_s)

    print(f"CSV:  {csv_path}")
    print(f"LOG:  {txt_path}")
    print(f"Duration: {duration_s}s  ({duration_s/60.0:.1f} min)")
    print("Phase schedule:")
    for name, t_end, mode in schedule:
        m = PERF_NAMES.get(mode, "no-stress/AUTO") if mode else "stop stress"
        print(f"   [{t_end - (schedule[schedule.index((name, t_end, mode)) - 1][1] if schedule.index((name, t_end, mode)) > 0 else 0):4d}s] {name:<20s} mode={m}")

    # --- Open I2C ----------------------------------------------------------
    fd_mbox = open_i2c_bus(I2C_BUS, EC_ADDR)
    csv_fh = open(csv_path, "w", buffering=1)
    txt_fh = open(txt_path, "w", buffering=1)

    csv_fh.write("t_s,phase,mode,cpu_mhz,kernel_c,ec_cet1_c,ec_cet2_c,fans_reg,ecmd_reg\n")

    # --- Stress ------------------------------------------------------------
    # Start stress-ng with +10s beyond the last stress phase so it runs through
    stress_end = schedule[-2][1]  # phase 4 end (before cooldown)
    nproc = os.cpu_count() or 12
    stress_proc: subprocess.Popen | None = None

    def start_stress():
        nonlocal stress_proc
        stress_proc = subprocess.Popen(
            ["stress-ng", "--cpu", str(nproc), "--timeout", str(stress_end + 5)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- Signal handlers ---------------------------------------------------
    def _sig(signum, frame):
        print(f"\n[!] signal {signum} — aborting test and cleaning up")
        emergency_reset(fd_mbox)
        os.close(fd_mbox)
        csv_fh.close()
        txt_fh.close()
        sys.exit(130)
    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)

    # --- Go! ---------------------------------------------------------------
    try:
        print("\n== starting stress-ng ==")
        start_stress()

        print("== starting telemetry loop (2s interval) ==")
        hdr = (f"{'t':>5s} | {'phase':<18s} | {'mode':<8s} | "
               f"{'cpu':>5s} | {'kern':>5s} | {'cet1':>5s} | {'cet2':>5s} | "
               f"{'fans':>4s} | {'ecmd':>4s}")
        print(hdr)
        txt_fh.write(hdr + "\n")
        print("-" * len(hdr))

        t_start = time.time()
        phase_idx = 0
        current_phase_name, current_phase_end, current_mode = schedule[0]

        # Set initial mode
        if current_mode is not None:
            set_perf_mode(fd_mbox, current_mode)

        while True:
            t = time.time() - t_start
            if t >= schedule[-1][1]:
                break

            # Advance phase?
            while phase_idx < len(schedule) and t >= schedule[phase_idx][1]:
                phase_idx += 1
                if phase_idx >= len(schedule):
                    break
                current_phase_name, current_phase_end, current_mode = schedule[phase_idx]
                print(f"\n-- phase change at t={t:.0f}s -> {current_phase_name} "
                      f"(mode={PERF_NAMES.get(current_mode, 'cooldown')}) --")
                if current_mode is not None:
                    set_perf_mode(fd_mbox, current_mode)
                else:
                    # cooldown phase: kill stress-ng and force AUTO
                    kill_stress()
                    set_perf_mode(fd_mbox, PERF_AUTO)

            # Read telemetry
            cpu_mhz = read_cpu_mhz_max()
            kern_c = read_kernel_temp_max()
            ec1 = ec_reg_read(fd_mbox, CET1_REG)
            ec2 = ec_reg_read(fd_mbox, CET2_REG)
            fans = ec_reg_read(fd_mbox, FANS_REG)
            ecmd = ec_reg_read(fd_mbox, ECMD_REG)

            mode_name = PERF_NAMES.get(current_mode, "AUTO(nostress)") if current_mode else "AUTO(nostress)"
            row = (f"{int(t):5d} | {current_phase_name:<18s} | {mode_name:<8s} | "
                   f"{cpu_mhz:>5d} | {kern_c:>5.1f} | {ec1:>5d} | {ec2:>5d} | "
                   f"{fans:>4d} | 0x{ecmd:02x}")
            print(row)
            txt_fh.write(row + "\n")
            csv_fh.write(f"{t:.1f},{current_phase_name},{mode_name},{cpu_mhz},"
                         f"{kern_c:.1f},{ec1},{ec2},{fans},0x{ecmd:02x}\n")

            time.sleep(2)

        print("\n== test complete ==")

    finally:
        print("[cleanup] reset to AUTO, kill stress-ng")
        emergency_reset(fd_mbox)
        os.close(fd_mbox)
        csv_fh.close()
        txt_fh.close()
        print(f"\nCSV saved: {csv_path}")
        print(f"LOG saved: {txt_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=int, default=600,
                    help="total test duration in seconds (default 600 = 10 min)")
    ap.add_argument("--brief", action="store_true",
                    help="shortcut for --duration 180 (3-minute quick cycle)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("ERROR: run as root (needs /dev/i2c-2)", file=sys.stderr)
        sys.exit(1)

    if subprocess.call(["which", "stress-ng"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        print("ERROR: stress-ng not installed. Try: sudo apt install stress-ng",
              file=sys.stderr)
        sys.exit(1)

    duration = 180 if args.brief else args.duration
    if duration < 60:
        print("Duration must be >= 60s for meaningful phases", file=sys.stderr)
        sys.exit(2)

    run(duration)


if __name__ == "__main__":
    main()
