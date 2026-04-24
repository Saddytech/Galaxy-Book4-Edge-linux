#!/usr/bin/env python3
"""
stress_max_fan.py — continuously re-assert MAX fan during every tick of throttle

Difference from stress_aggressive.py:
  * Polls at 0.25s instead of 1s (catches throttle within 250ms)
  * On every throttled tick, blasts BOTH opcodes:
        0x08 FANZONE=15         (force fan level 15)
        0x17 SET_FANRPM=20000    (command 20000 RPM; EC clamps to physical max)
  * Re-asserts EVERY TICK while throttled (EC can't revert)
  * Releases fan the instant we see cpu_mhz >= threshold (no hysteresis)
  * Sets perf mode MAXPERF at start of aggressive phase; re-sends every 5s
  * Full cleanup on SIGINT / SIGTERM / finally

Schedule:
  Phase 1 [  0s –  30s]  baseline AUTO    (no fan override)
  Phase 2 [ 30s – 180s]  aggressive       (MAXPERF + blast fan on throttle)
  Phase 3 [180s – 240s]  release AUTO     (stop stress, recovery)

Usage:
  sudo ./stress_max_fan.py              # 4-min default
  sudo ./stress_max_fan.py --duration 600
  sudo ./stress_max_fan.py --threshold 3700

Safety: always resets to FANZONE=0 + AUTO on exit.
Emergency fallback: sudo i2ctransfer -y 5 w2@0x62 0x08 0x00
"""
from __future__ import annotations
import argparse, ctypes, datetime, fcntl, os, signal, subprocess, sys, time
from pathlib import Path

# --- I2C targets --------------------------------------------------------
MBOX_BUS, MBOX_ADDR   = 2, 0x64   # CMDD/perf mode
IOCTL_BUS, IOCTL_ADDR = 5, 0x62   # direct fan control
I2C_SLAVE, I2C_RDWR, I2C_M_RD = 0x0703, 0x0707, 0x0001

# --- Mbox protocol (verified via Ghidra decompile of EC2.sys) -----------
WRITE_PREFIX, READ_PREFIX, READ_SUCCESS = 0x40, 0x30, 0x50

# --- EC registers -------------------------------------------------------
FANS_REG, CET1_REG, CET2_REG = 0x87, 0xC2, 0xC3
ECMD_REG, MBUF0_REG, MBUF1_REG = 0xA2, 0xA3, 0xA4

# --- Performance modes (from DSDT PRF3 LIST) ----------------------------
PERF_AUTO, PERF_SILENT, PERF_MAXPERF = 0x02, 0x0A, 0x15

# --- Ioctl path fan-override opcodes ------------------------------------
OP_FANZONE = 0x08   # byte: 0..15 level
OP_FANRPM  = 0x17   # 4-byte RPM target (stored reversed on wire)

# --- Tuning defaults ----------------------------------------------------
THROTTLE_MHZ = 3800
TICK_S       = 0.25
PERF_RESEND_S = 5.0
FAN_MAX_RPM  = 20000        # overshoot — EC clamps to actual max (~12000 RPM)

# --- I2C ctypes plumbing ------------------------------------------------
class I2cMsg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16),
                ("buf", ctypes.POINTER(ctypes.c_char))]
class I2cRdwrIoctlData(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(I2cMsg)), ("nmsgs", ctypes.c_uint32)]

def open_bus(bus, slave):
    fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
    fcntl.ioctl(fd, I2C_SLAVE, slave)
    return fd

def i2c_write_bytes(fd, data, settle_ms=1):
    os.write(fd, data)
    if settle_ms:
        time.sleep(settle_ms / 1000.0)

def i2c_wr_rd(fd, slave, wdata, rlen):
    wbuf = ctypes.create_string_buffer(wdata, len(wdata))
    rbuf = ctypes.create_string_buffer(rlen)
    msgs = (I2cMsg * 2)(
        I2cMsg(addr=slave, flags=0, len=len(wdata),
               buf=ctypes.cast(wbuf, ctypes.POINTER(ctypes.c_char))),
        I2cMsg(addr=slave, flags=I2C_M_RD, len=rlen,
               buf=ctypes.cast(rbuf, ctypes.POINTER(ctypes.c_char))))
    fcntl.ioctl(fd, I2C_RDWR, I2cRdwrIoctlData(msgs=msgs, nmsgs=2))
    return list(rbuf.raw)

# --- Mbox wrappers ------------------------------------------------------
def mbox_w(fd, cmd_hi, cmd_lo, data):
    i2c_write_bytes(fd, bytes([WRITE_PREFIX, 0, cmd_hi, cmd_lo, data]))

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

# --- Ioctl fan overrides (sent to i2c-5@0x62) --------------------------
def fan_zone(fd_io, level):
    try:
        os.write(fd_io, bytes([OP_FANZONE, level & 0x0f]))
    except OSError: pass

def fan_rpm(fd_io, rpm):
    # wire: [0x17, 0x00, 0x00, (N>>8)&0xFF, N&0xFF]
    hi, lo = (rpm >> 8) & 0xff, rpm & 0xff
    try:
        os.write(fd_io, bytes([OP_FANRPM, 0x00, 0x00, hi, lo]))
    except OSError: pass

def fan_release(fd_io):
    """Return fan to EC-automatic: FANZONE=0."""
    try:
        os.write(fd_io, bytes([OP_FANZONE, 0x00]))
    except OSError: pass

# --- Telemetry ----------------------------------------------------------
def cpu_max_mhz():
    m = 0
    for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try: m = max(m, int(p.read_text()) // 1000)
        except Exception: continue
    return m

def kern_max_c():
    t = 0.0
    for p in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try: t = max(t, int(p.read_text()) / 1000.0)
        except Exception: continue
    return t

# --- Cleanup ------------------------------------------------------------
_cleaned = False
def cleanup(fd_mbox, fd_io):
    global _cleaned
    if _cleaned: return
    _cleaned = True
    subprocess.run(["pkill", "-9", "stress-ng"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try: fan_release(fd_io)
    except Exception: pass
    try: set_perf(fd_mbox, PERF_AUTO)
    except Exception: pass
    for fd in (fd_mbox, fd_io):
        try: os.close(fd)
        except Exception: pass

# --- Main loop ----------------------------------------------------------
def run(duration, throttle_mhz):
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = log_dir / f"max-fan-{stamp}.csv"

    print(f"CSV: {csv_path}")
    print(f"Duration: {duration}s   throttle_thresh={throttle_mhz}MHz   tick={TICK_S}s")
    print(f"Max fan blast: FANZONE=15 + FANRPM={FAN_MAX_RPM}")

    splits = [int(duration * 0.125), int(duration * 0.75), duration]
    print(f"Phases: [0..{splits[0]}s] baseline AUTO  "
          f"[{splits[0]}..{splits[1]}s] AGGRESSIVE  "
          f"[{splits[1]}..{splits[2]}s] release AUTO")

    fd_mbox = open_bus(MBOX_BUS,  MBOX_ADDR)
    fd_io   = open_bus(IOCTL_BUS, IOCTL_ADDR)
    csv = open(csv_path, "w", buffering=1)
    csv.write("t,phase,mode,cpu_mhz,kern_c,cet1_c,cet2_c,fan_state,throttled\n")

    def _sig(signum, frame):
        print(f"\n[!] signal {signum} — cleanup")
        cleanup(fd_mbox, fd_io)
        csv.close()
        sys.exit(130)
    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)

    nproc = os.cpu_count() or 12
    stress_dur = splits[1] + 5
    subprocess.Popen(["stress-ng", "--cpu", str(nproc), "--timeout", str(stress_dur)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    hdr = (f"{'t':>6s} | {'phase':<11s} | {'mode':<8s} | {'cpu_MHz':>7s} | "
           f"{'kern':>5s} | {'cet1':>4s} | {'cet2':>4s} | {'fan':>4s} | {'T':>1s}")
    print(hdr); print("-" * len(hdr))

    t0 = time.time()
    cur_phase = None
    cur_mode = PERF_AUTO
    last_perf_resend = 0.0
    last_print = 0.0
    forced = False
    set_perf(fd_mbox, PERF_AUTO)

    try:
        while True:
            t = time.time() - t0
            if t >= duration:
                break

            # Determine phase
            if t < splits[0]:
                phase, want_mode, can_force = "baseline", PERF_AUTO, False
            elif t < splits[1]:
                phase, want_mode, can_force = "AGGRESSIVE", PERF_MAXPERF, True
            else:
                phase, want_mode, can_force = "release", PERF_AUTO, False

            if phase != cur_phase:
                print(f"\n-- phase change at t={t:.1f}s -> {phase} "
                      f"(perf={hex(want_mode)}) --")
                cur_phase = phase
                cur_mode = want_mode
                set_perf(fd_mbox, want_mode)
                last_perf_resend = t
                if not can_force and forced:
                    fan_release(fd_io)
                    forced = False
                if phase == "release":
                    subprocess.run(["pkill", "-9", "stress-ng"], check=False,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)

            # Telemetry
            cpu = cpu_max_mhz()
            throttled = (cpu < throttle_mhz)
            kern = kern_max_c()

            # Fan policy (aggressive phase only)
            if can_force:
                if throttled:
                    # BLAST FAN: FANZONE=15 + FANRPM=20000 every single tick
                    fan_zone(fd_io, 15)
                    fan_rpm(fd_io, FAN_MAX_RPM)
                    forced = True
                elif forced:
                    # CPU recovered — release IMMEDIATELY
                    fan_release(fd_io)
                    forced = False
                    print(f"  -- t={t:.1f}s CPU recovered to {cpu}MHz — fan released")
                # Re-send perf mode every few seconds (EC may drift)
                if t - last_perf_resend >= PERF_RESEND_S:
                    set_perf(fd_mbox, want_mode)
                    last_perf_resend = t

            # Print every ~1 second (not every 0.25s tick) — but always log CSV
            if t - last_print >= 1.0 or phase != cur_phase:
                last_print = t
                cet1 = ec_read(fd_mbox, CET1_REG)
                cet2 = ec_read(fd_mbox, CET2_REG)
                fan_state = "BLAST" if forced else "ec"
                mode_name = "MAXPERF" if cur_mode == PERF_MAXPERF else "AUTO"
                print(f"{t:>6.2f} | {phase:<11s} | {mode_name:<8s} | "
                      f"{cpu:>7d} | {kern:>5.1f} | {cet1:>4d} | {cet2:>4d} | "
                      f"{fan_state:>4s} | {'Y' if throttled else '.'}")
                csv.write(f"{t:.2f},{phase},{mode_name},{cpu},{kern:.1f},"
                          f"{cet1},{cet2},{fan_state},"
                          f"{'1' if throttled else '0'}\n")

            time.sleep(TICK_S)

    finally:
        print("\n[cleanup] fan release + AUTO + kill stress")
        cleanup(fd_mbox, fd_io)
        csv.close()
        print(f"CSV saved: {csv_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=int, default=240)
    ap.add_argument("--threshold", type=int, default=THROTTLE_MHZ)
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("ERROR: must run as root")
    if subprocess.call(["which", "stress-ng"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        sys.exit("ERROR: stress-ng not installed")

    run(args.duration, args.threshold)

if __name__ == "__main__":
    main()
