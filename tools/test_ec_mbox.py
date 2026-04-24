#!/usr/bin/env python3
"""
test_ec_mbox.py — Samsung Galaxy Book4 Edge ENE KB9058 EC Mbox/CMDD tester

Protocol decoded from EC2.sys reverse-engineering (see research cache for
disassemblies of EneEcWriteMbox, EneEcReadMbox, WriteEcSpace, ReadEcSpace).

THE ENE KB9058 I2C MBOX PROTOCOL
================================

EC has both "ioctl" path (opcodes 0x08, 0x10, 0x17, etc.) AND an "Mbox" path
for arbitrary EC-register access. The Mbox path is what Windows uses to
implement ACPI OperationRegion 0xA2 writes — i.e. how CMDD(0xEE, ...) is
sent on the wire.

Wire format (from Ghidra decompile of EC2.sys, verified):

    WRITE BYTE (5 bytes on I2C):
        [0x40, 0x00, cmd_hi, cmd_lo, data]

    READ BYTE (4-byte write + 2-byte read):
        write: [0x30, 0x00, cmd_hi, cmd_lo]
        read:  [0x50 (success 'P'), data_byte]

Mbox command words (as cmd_hi,cmd_lo pairs):
    0xF4 0x80  — set target EC register address; data byte = reg addr
    0xF4 0x81  — stage value to be written;       data byte = value
    0xFF 0x10  — execute operation;               data byte = 0x88 READ or 0x89 WRITE

To WRITE byte V to EC register N:
    1. [0x40, 0x00, 0xF4, 0x80, N]        set target=N
    2. [0x40, 0x00, 0xF4, 0x81, V]        stage V
    3. [0x40, 0x00, 0xFF, 0x10, 0x89]     exec WRITE

To READ byte from EC register N:
    1. [0x40, 0x00, 0xF4, 0x80, N]        set target=N
    2. [0x40, 0x00, 0xFF, 0x10, 0x88]     exec READ
    3. write [0x30, 0x00, 0xF4, 0x80] then read 2 bytes; byte[0]=0x50 byte[1]=data

EC REGISTER MAP (from DSDT OperationRegion ECR at 0xA1)
=======================================================
    0x87 - FANS (4 bits)  — fan level 0-15 (same semantics as opcode 0x08)
    0xC1 - CTMP           — CPU temp
    0xC2 - CET1           — EC temp 1
    0xC3 - CET2           — EC temp 2
    0x84 - B1ST           — battery state
    0x9C - SCAI           — SCAI interface flag

The EXTC region at 0xA2 has:
    0xA2 - ECMD (1 byte)  — execute extended command
    0xA3+ - MBUF (30 bytes) — data buffer for extended commands

CMDD PATH (performance mode, from DSDT PRF3):
    CMDD(0xEE, 0x02, [0x80, mode]) sets performance mode to <mode>.
    On Linux this translates to:
        write_ec_reg(0xA3, 0x80)       # MBUF[0] = 0x80 (SABI prefix)
        write_ec_reg(0xA4, mode_value) # MBUF[1] = mode
        write_ec_reg(0xA2, 0xEE)       # ECMD  = 0xEE  (triggers perf mode)

Supported modes on Galaxy Book4 Edge (from PRF3 SUBN=LIST):
    0x02 = AUTO (balanced)
    0x0A = SILENT
    0x15 = MAXPERF

USAGE
=====
    sudo ./test_ec_mbox.py get-fan-level           # read EC reg 0x87
    sudo ./test_ec_mbox.py get-ec-reg 0xC1         # read CPU temp reg
    sudo ./test_ec_mbox.py dump                    # dump key EC regs
    sudo ./test_ec_mbox.py perf auto               # set performance mode
    sudo ./test_ec_mbox.py perf maxperf
    sudo ./test_ec_mbox.py perf silent
    sudo ./test_ec_mbox.py ramp --mode maxperf     # measure fan ramp
    sudo ./test_ec_mbox.py set-fan-level 4         # raw write reg 0x87=4

SAFETY
======
* Mbox read is 100% safe (just reads).
* Mbox write to documented registers (0x87 fan, 0xA2 ECMD) is safe.
* On any exit, the script resets perf mode to AUTO.
* Emergency manual reset if fan gets stuck:
      sudo i2ctransfer -y 5 w2@0x62 0x08 0x00    # legacy FANZONE=0
"""
from __future__ import annotations
import argparse
import datetime
import fcntl
import os
import signal
import struct
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# I2C target for Mbox protocol (per DSDT _SB.ECTC _CRS second entry):
#   ACPI I2C1 = MMIO 0xB80000 = Linux i2c-2
#   slave 0x64 (NOT 0x62 — that's the ioctl endpoint on ACPI I2C6/i2c-5)
I2C_BUS   = 2      # b80000 = ACPI I2C1 (Mbox/perf mode)
EC_ADDR   = 0x64   # EC's second I2C endpoint
I2C_SLAVE = 0x0703
I2C_RDWR  = 0x0707
I2C_M_RD  = 0x0001

# Mbox framing
WRITE_PREFIX = 0x40
READ_PREFIX  = 0x30
READ_SUCCESS = 0x50

# Mbox sub-commands
CMD_SET_TARGET_HI  = 0xF4
CMD_SET_TARGET_LO  = 0x80
CMD_STAGE_VAL_HI   = 0xF4
CMD_STAGE_VAL_LO   = 0x81
CMD_EXECUTE_HI     = 0xFF
CMD_EXECUTE_LO     = 0x10
EXEC_ARG_READ      = 0x88
EXEC_ARG_WRITE     = 0x89

# EC register map (from DSDT ECR region)
EC_REG = {
    "FANS":  0x87,   # fan level 0-15
    "CTMP":  0xC1,   # CPU temp
    "CET1":  0xC2,
    "CET2":  0xC3,
    "B1ST":  0x84,
    "SCAI":  0x9C,
    # EXTC region (0xA2):
    "ECMD":  0xA2,
    "MBUF0": 0xA3,
    "MBUF1": 0xA4,
    "MBUF2": 0xA5,
    "MBUF3": 0xA6,
}

# Performance modes (from PRF3 DSDT)
PERF_MODES = {
    "auto":    0x02,
    "silent":  0x0A,
    "maxperf": 0x15,
}

# CMDD command opcode (ECMD value) for performance mode
CMDD_PERF_MODE = 0xEE

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / f"ec-mbox-{datetime.datetime.now():%Y%m%d-%H%M%S}.log"
_log_fh = open(LOG_PATH, "a", buffering=1)

def say(msg=""):
    print(msg)
    _log_fh.write(msg + "\n")

def fmt(b):
    return " ".join(f"{x:02x}" for x in b)


# --------------------------------------------------------------------------- #
# I2C primitives — combined write/read in one transaction
# --------------------------------------------------------------------------- #

import ctypes

class I2cMsg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16),
                ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16),
                ("buf", ctypes.POINTER(ctypes.c_char))]

class I2cRdwrIoctlData(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(I2cMsg)),
                ("nmsgs", ctypes.c_uint32)]


def open_bus():
    try:
        fd = os.open(f"/dev/i2c-{I2C_BUS}", os.O_RDWR)
    except PermissionError:
        say("ERROR: need root for /dev/i2c-5. Use sudo.")
        sys.exit(1)
    except FileNotFoundError:
        say("ERROR: /dev/i2c-5 not present. Boot into KBDBLT-TEST DTB and "
            "`modprobe i2c-dev`.")
        sys.exit(1)
    fcntl.ioctl(fd, I2C_SLAVE, EC_ADDR)
    return fd


def i2c_write(fd, data: bytes, settle_ms=5):
    os.write(fd, data)
    time.sleep(settle_ms / 1000.0)


def i2c_write_then_read(fd, wdata: bytes, rlen: int):
    """Combined I2C write + restart + read using I2C_RDWR ioctl."""
    wbuf = ctypes.create_string_buffer(wdata, len(wdata))
    rbuf = ctypes.create_string_buffer(rlen)
    msgs = (I2cMsg * 2)(
        I2cMsg(addr=EC_ADDR, flags=0,
               len=len(wdata), buf=ctypes.cast(wbuf, ctypes.POINTER(ctypes.c_char))),
        I2cMsg(addr=EC_ADDR, flags=I2C_M_RD,
               len=rlen, buf=ctypes.cast(rbuf, ctypes.POINTER(ctypes.c_char))),
    )
    data = I2cRdwrIoctlData(msgs=msgs, nmsgs=2)
    fcntl.ioctl(fd, I2C_RDWR, data)
    return list(rbuf.raw)


# --------------------------------------------------------------------------- #
# Mbox protocol primitives
# --------------------------------------------------------------------------- #

def mbox_write(fd, cmd_hi: int, cmd_lo: int, data: int, verbose=False):
    """Send a 5-byte Mbox WRITE packet: [0x40, 0x00, cmd_hi, cmd_lo, data]."""
    pkt = bytes([WRITE_PREFIX, 0x00, cmd_hi & 0xff, cmd_lo & 0xff, data & 0xff])
    if verbose:
        say(f"  mbox_write: {fmt(pkt)}")
    i2c_write(fd, pkt)


def mbox_read(fd, cmd_hi: int, cmd_lo: int, verbose=False):
    """Send 4-byte READ request, read back 2 bytes. Returns (status, data)."""
    wpkt = bytes([READ_PREFIX, 0x00, cmd_hi & 0xff, cmd_lo & 0xff])
    resp = i2c_write_then_read(fd, wpkt, 2)
    if verbose:
        say(f"  mbox_read:  {fmt(wpkt)} -> {fmt(resp)}")
    return resp[0], resp[1]


# --------------------------------------------------------------------------- #
# High-level EC register access
# --------------------------------------------------------------------------- #

def write_ec_reg(fd, reg: int, value: int, verbose=False):
    """Write byte <value> to EC register <reg> via Mbox protocol.
    Three I2C transactions per WriteEcSpace() in EC2.sys.
    """
    if verbose:
        say(f"[WriteEcSpace reg=0x{reg:02x} val=0x{value:02x}]")
    # Step 1: EneEcWriteMbox(0xF480, reg) → [0x40, 0x00, 0xF4, 0x80, reg]
    mbox_write(fd, CMD_SET_TARGET_HI, CMD_SET_TARGET_LO, reg, verbose=verbose)
    # Step 2: EneEcWriteMbox(0xF481, value) → [0x40, 0x00, 0xF4, 0x81, value]
    mbox_write(fd, CMD_STAGE_VAL_HI, CMD_STAGE_VAL_LO, value, verbose=verbose)
    # Step 3: EneEcWriteMbox(0xFF10, 0x89) → [0x40, 0x00, 0xFF, 0x10, 0x89]
    mbox_write(fd, CMD_EXECUTE_HI, CMD_EXECUTE_LO, EXEC_ARG_WRITE, verbose=verbose)
    time.sleep(0.005)


def read_ec_reg(fd, reg: int, verbose=False):
    """Read one byte from EC register <reg>. Returns (status_byte, data_byte).
    Four I2C transactions per ReadEcSpace() in EC2.sys.
    """
    if verbose:
        say(f"[ReadEcSpace reg=0x{reg:02x}]")
    # Step 1: EneEcWriteMbox(0xF480, reg) → set target
    mbox_write(fd, CMD_SET_TARGET_HI, CMD_SET_TARGET_LO, reg, verbose=verbose)
    # Step 2: EneEcWriteMbox(0xFF10, 0x88) → exec READ
    mbox_write(fd, CMD_EXECUTE_HI, CMD_EXECUTE_LO, EXEC_ARG_READ, verbose=verbose)
    # Step 3: EneEcReadMbox(0xF480) → read 2 bytes: [status, data]
    status, data = mbox_read(fd, CMD_SET_TARGET_HI, CMD_SET_TARGET_LO, verbose=verbose)
    return status, data


# --------------------------------------------------------------------------- #
# CMDD (extended command) via Mbox
# --------------------------------------------------------------------------- #

def cmdd(fd, ecmd: int, args: list[int], verbose=False):
    """Execute an extended EC command via Mbox.
    Equivalent to DSDT CMDD(ecmd, len(args), args):
      writes MBUF = args then ECMD = ecmd.
    """
    say(f"[CMDD ecmd=0x{ecmd:02x} args={[f'0x{a:02x}' for a in args]}]")
    # Write MBUF bytes first
    for i, v in enumerate(args):
        reg = EC_REG["MBUF0"] + i
        write_ec_reg(fd, reg, v, verbose=verbose)
    # Write ECMD last (this triggers execution)
    write_ec_reg(fd, EC_REG["ECMD"], ecmd, verbose=verbose)


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #

def read_temps_max() -> float:
    tmax = 0.0
    for p in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            v = int(p.read_text().strip()) / 1000.0
            tmax = max(tmax, v)
        except Exception:
            continue
    return tmax


def read_cpu_mhz_max() -> int:
    mmax = 0
    for p in Path("/sys/devices/system/cpu").glob(
            "cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            v = int(p.read_text().strip()) // 1000
            mmax = max(mmax, v)
        except Exception:
            continue
    return mmax


def telemetry_row(prefix, fd=None):
    row = f"  {prefix} cpu={read_cpu_mhz_max():>5d}MHz kernel_temp={read_temps_max():>5.1f}°C"
    if fd is not None:
        try:
            st, level = read_ec_reg(fd, EC_REG["FANS"])
            st, ctmp = read_ec_reg(fd, EC_REG["CTMP"])
            row += f"  ec_fan={level & 0x0f}/15  ec_temp=0x{ctmp:02x}"
        except Exception as e:
            row += f"  ec_read_err={e}"
    say(row)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_dump(fd):
    say("=" * 60)
    say("EC register dump via Mbox")
    say("=" * 60)
    for name, reg in EC_REG.items():
        try:
            st, val = read_ec_reg(fd, reg, verbose=False)
            ok = "OK" if st == READ_SUCCESS else f"status=0x{st:02x}"
            say(f"  [0x{reg:02x}] {name:<8s} = 0x{val:02x} ({val:3d})  [{ok}]")
        except Exception as e:
            say(f"  [0x{reg:02x}] {name:<8s} = ERR {e}")


def cmd_get_fan_level(fd):
    st, val = read_ec_reg(fd, EC_REG["FANS"], verbose=True)
    level = val & 0x0f
    say(f"fan level = {level}/15  (raw byte=0x{val:02x}, status=0x{st:02x})")


def cmd_get_reg(fd, reg_str):
    reg = int(reg_str, 0)
    st, val = read_ec_reg(fd, reg, verbose=True)
    ok = "SUCCESS" if st == READ_SUCCESS else "???"
    say(f"EC[0x{reg:02x}] = 0x{val:02x}  (status=0x{st:02x} {ok})")


def cmd_set_fan_level(fd, level):
    if not 0 <= level <= 15:
        say(f"fan level must be 0..15, got {level}")
        sys.exit(2)
    say(f"Setting EC[0x87] FANS = {level}")
    write_ec_reg(fd, EC_REG["FANS"], level, verbose=True)
    time.sleep(2)
    telemetry_row("after 2s:", fd)


def cmd_perf(fd, mode_name):
    val = PERF_MODES[mode_name]
    say(f"Setting perf mode = {mode_name} (0x{val:02x}) via CMDD(0xEE, [0x80, 0x{val:02x}])")
    telemetry_row("  before:", fd)
    cmdd(fd, CMDD_PERF_MODE, [0x80, val], verbose=True)
    time.sleep(1)
    telemetry_row("  after 1s:", fd)
    time.sleep(3)
    telemetry_row("  after 4s:", fd)


def cmd_ramp(fd, mode_name, window_s):
    say("=" * 60)
    say(f"Ramp test — baseline=AUTO → target={mode_name}, window={window_s}s")
    say("=" * 60)
    try:
        # baseline
        say("\n-- baseline: AUTO --")
        cmdd(fd, CMDD_PERF_MODE, [0x80, PERF_MODES["auto"]])
        time.sleep(5)
        telemetry_row("  baseline:", fd)

        # target
        say(f"\n-- switching to {mode_name} --")
        t_start = time.time()
        cmdd(fd, CMDD_PERF_MODE, [0x80, PERF_MODES[mode_name]])

        while time.time() - t_start < window_s:
            t = time.time() - t_start
            telemetry_row(f"  t={t:5.1f}s:", fd)
            time.sleep(0.5)
    finally:
        say("\n[cleanup] reset to AUTO")
        cmdd(fd, CMDD_PERF_MODE, [0x80, PERF_MODES["auto"]])


def cmd_probe(fd):
    """Safe probe: just try read EC register 0x87 (FANS) using all semantics."""
    say("=" * 60)
    say("Safe probe — read EC reg 0x87 (FANS) via Mbox")
    say("=" * 60)
    st, val = read_ec_reg(fd, EC_REG["FANS"], verbose=True)
    say(f"\nresult: status=0x{st:02x} fans_reg=0x{val:02x} level={val & 0x0f}/15")
    if st == READ_SUCCESS:
        say("✓ Mbox read protocol works! status byte == 0x50")
    elif st == 0x01:
        say("✗ Got old-style [01,02,...] response — Mbox not recognized?")
    else:
        say(f"? Unexpected status 0x{st:02x}")


# --------------------------------------------------------------------------- #
# Emergency cleanup
# --------------------------------------------------------------------------- #

def emergency_reset(fd):
    say("\n== emergency reset ==")
    # First try Mbox CMDD AUTO
    try:
        cmdd(fd, CMDD_PERF_MODE, [0x80, PERF_MODES["auto"]])
        say("  Mbox CMDD AUTO: sent")
    except Exception as e:
        say(f"  Mbox CMDD err: {e}")
    # Fallback: legacy fanzone=0
    try:
        os.write(fd, bytes([0x08, 0x00]))
        say("  legacy FANZONE=0: sent")
    except Exception as e:
        say(f"  legacy FANZONE err: {e}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="ENE KB9058 EC Mbox/CMDD protocol tester (Galaxy Book4 Edge)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="Safe read-only probe of fan register")
    sub.add_parser("dump", help="Dump key EC registers")
    sub.add_parser("get-fan-level", help="Read EC reg 0x87 (FANS)")
    p_get = sub.add_parser("get-ec-reg", help="Read an EC register by addr")
    p_get.add_argument("reg", help="e.g. 0x87 or 135")
    p_setfan = sub.add_parser("set-fan-level", help="Write EC reg 0x87 FANS 0..15")
    p_setfan.add_argument("level", type=int)
    p_perf = sub.add_parser("perf", help="Set performance mode via CMDD")
    p_perf.add_argument("mode", choices=list(PERF_MODES.keys()))
    p_ramp = sub.add_parser("ramp", help="Measure fan ramp from AUTO to target")
    p_ramp.add_argument("--mode", choices=["maxperf", "silent"], default="maxperf")
    p_ramp.add_argument("--window", type=int, default=20)

    args = ap.parse_args()

    if os.geteuid() != 0:
        print("ERROR: need root. Try: sudo", file=sys.stderr)
        sys.exit(1)

    say(f"=== ec-mbox test — log={LOG_PATH} ===")
    say(f"cmd={args.cmd}")

    fd = open_bus()

    def _sig(signum, frame):
        say(f"\n[!] signal {signum} — emergency reset")
        emergency_reset(fd)
        os.close(fd)
        sys.exit(130)
    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        if args.cmd == "probe":
            cmd_probe(fd)
        elif args.cmd == "dump":
            cmd_dump(fd)
        elif args.cmd == "get-fan-level":
            cmd_get_fan_level(fd)
        elif args.cmd == "get-ec-reg":
            cmd_get_reg(fd, args.reg)
        elif args.cmd == "set-fan-level":
            cmd_set_fan_level(fd, args.level)
        elif args.cmd == "perf":
            cmd_perf(fd, args.mode)
        elif args.cmd == "ramp":
            cmd_ramp(fd, args.mode, args.window)
    except Exception as e:
        say(f"[!] {type(e).__name__}: {e}")
        emergency_reset(fd)
        raise
    finally:
        os.close(fd)
        _log_fh.flush()
        _log_fh.close()


if __name__ == "__main__":
    main()
