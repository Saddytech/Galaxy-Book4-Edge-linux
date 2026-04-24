#!/usr/bin/env python3
"""
Samsung Galaxy Book4 Edge — Battery status reader via EC Mbox (i2c-2 @ 0x64).

Decodes the same battery registers that the DSDT _BST/_BIX methods use
(ECR OperationRegion 0xA1 in _SB.ECTC), but accesses them directly
via the EC Mbox protocol that we reverse-engineered from EC2.sys.

EC RAM layout (from DSDT):
    0x80 bit0 = B1EX (battery 1 present)
    0x80 bit2 = ACEX (AC adapter present)
    0x82 bit0 = WLST (wireless state)
    0x84     = B1ST  (battery state: bit0=discharge, bit1=charge, bit3=full)
    0x85     = FANS/LDOS
    0xA0     = B1RR  (32-bit battery remaining capacity, big-endian words)
    0xA4     = B1PV  (32-bit battery present voltage + current)
    0xB0     = B1AF  (32-bit)
    0xB4     = B1VL  (32-bit)
    0xC0     = CTMP  (CPU temp 8-bit)
"""
import ctypes
import fcntl
import os
import sys
import time
from pathlib import Path

# ---- Mbox primitives (same I2C_RDWR method as test_ec_mbox.py) ----------
I2C_BUS   = 2          # Linux i2c-2 (MMIO 0xB80000 = ACPI I2C1)
I2C_ADDR  = 0x64       # Samsung EC Mbox endpoint
I2C_SLAVE = 0x0703
I2C_RDWR  = 0x0707
I2C_M_RD  = 0x0001

class I2cMsg(ctypes.Structure):
    _fields_ = [("addr",  ctypes.c_uint16),
                ("flags", ctypes.c_uint16),
                ("len",   ctypes.c_uint16),
                ("buf",   ctypes.POINTER(ctypes.c_char))]

class I2cRdwrIoctlData(ctypes.Structure):
    _fields_ = [("msgs",  ctypes.POINTER(I2cMsg)),
                ("nmsgs", ctypes.c_uint32)]

def _open_bus():
    fd = os.open(f"/dev/i2c-{I2C_BUS}", os.O_RDWR)
    fcntl.ioctl(fd, I2C_SLAVE, I2C_ADDR)
    return fd

def _i2c_write(fd, data, settle_ms=5):
    os.write(fd, data)
    time.sleep(settle_ms / 1000.0)

def _i2c_write_then_read(fd, wdata, rlen):
    wbuf = ctypes.create_string_buffer(wdata, len(wdata))
    rbuf = ctypes.create_string_buffer(rlen)
    msgs = (I2cMsg * 2)(
        I2cMsg(addr=I2C_ADDR, flags=0, len=len(wdata),
               buf=ctypes.cast(wbuf, ctypes.POINTER(ctypes.c_char))),
        I2cMsg(addr=I2C_ADDR, flags=I2C_M_RD, len=rlen,
               buf=ctypes.cast(rbuf, ctypes.POINTER(ctypes.c_char))),
    )
    req = I2cRdwrIoctlData(msgs=msgs, nmsgs=2)
    fcntl.ioctl(fd, I2C_RDWR, req)
    return list(rbuf.raw)

def mbox_write(fd, cmd_hi, cmd_lo, data):
    _i2c_write(fd, bytes([0x40, 0x00, cmd_hi, cmd_lo, data]))

def mbox_read(fd, cmd_hi, cmd_lo):
    resp = _i2c_write_then_read(fd, bytes([0x30, 0x00, cmd_hi, cmd_lo]), 2)
    status, data = resp[0], resp[1]
    if status != 0x50:
        raise IOError(f"mbox_read status=0x{status:02x} (expected 0x50)")
    return data

def ec_read(fd, reg):
    mbox_write(fd, 0xF4, 0x80, reg)     # latch target reg
    mbox_write(fd, 0xFF, 0x10, 0x88)    # exec READ
    return mbox_read(fd, 0xF4, 0x80)

def ec_read_bytes(regs):
    """Read a list of EC registers, returns dict reg->value.
    Holds a single fd open across all reads (the EC rejects rapid re-opens)."""
    fd = _open_bus()
    try:
        out = {}
        for r in regs:
            out[r] = ec_read(fd, r)
            time.sleep(0.002)
        return out
    finally:
        os.close(fd)

def byteswap16(v):
    return ((v & 0xFF) << 8) | ((v >> 8) & 0xFF)

def _u32_le(regs, base):
    return regs[base] | (regs[base+1]<<8) | (regs[base+2]<<16) | (regs[base+3]<<24)

def _upper_word_be(val32):
    """Return byteswapped upper 16 bits (matches DSDT ByteSwap16(val>>16))."""
    return byteswap16((val32 >> 16) & 0xFFFF)

def _lower_word_be(val32):
    """Return byteswapped lower 16 bits (matches DSDT ByteSwap16(val & 0xFFFF))."""
    return byteswap16(val32 & 0xFFFF)

# ---- Decoding per DSDT _BST --------------------------------------------
def decode_battery(regs):
    """Decode raw EC-register dict into a battery-status info dict.
    EC reports capacity in mAh (not mWh as the DSDT variable names suggest)
    and voltage in mV. B1AF holds design+full, B1VL holds the nominal voltage."""
    b80 = regs[0x80]
    b1ex = bool(b80 & 0x01)
    acex = bool(b80 & 0x04)
    b1st = regs[0x84]
    discharge = bool(b1st & 0x01)
    charge    = bool(b1st & 0x02)
    full      = bool(b1st & 0x08)

    # B1RR @ 0xA0: upper word BE = remaining mAh; lower word unused (usually 0x6100 sentinel)
    b1rr = _u32_le(regs, 0xA0)
    remaining_mah = _upper_word_be(b1rr)
    if remaining_mah == 0xFFFF:
        remaining_mah = None

    # B1PV @ 0xA4: upper=voltage mV, lower=current mA (signed)
    b1pv = _u32_le(regs, 0xA4)
    volt_mv = _upper_word_be(b1pv)
    cur_raw = _lower_word_be(b1pv)
    if cur_raw >= 0x8000:
        cur_raw -= 0x10000
    current_ma = cur_raw

    # B1AF @ 0xB0: upper=design mAh, lower=full-charge mAh (after wear)
    b1af = _u32_le(regs, 0xB0)
    design_mah = _upper_word_be(b1af)
    fullchg_mah = _lower_word_be(b1af)
    if design_mah in (0, 0xFFFF):
        design_mah = None
    if fullchg_mah in (0, 0xFFFF):
        fullchg_mah = None

    # B1VL @ 0xB4: lower=design voltage mV, upper=? (battery temp?)
    b1vl = _u32_le(regs, 0xB4)
    design_mv = _lower_word_be(b1vl)
    b1vl_upper = _upper_word_be(b1vl)

    # Percentage: prefer full-charge capacity, fall back to design
    pct = None
    if remaining_mah is not None:
        denom = fullchg_mah or design_mah
        if denom:
            pct = min(100.0, remaining_mah * 100.0 / denom)

    state = (
        "FULL" if full else
        "CHARGING" if charge else
        "DISCHARGING" if discharge else
        ("IDLE (AC, charged)" if acex and remaining_mah and fullchg_mah
             and remaining_mah >= fullchg_mah * 0.95
         else "IDLE")
    )

    return {
        "b1ex": b1ex, "acex": acex, "state": state, "b1st_raw": b1st,
        "remaining_mah": remaining_mah,
        "design_mah": design_mah, "fullchg_mah": fullchg_mah,
        "voltage_mv": volt_mv, "current_ma": current_ma,
        "design_mv": design_mv, "b1vl_upper": b1vl_upper,
        "percent": pct,
        "b80_raw": b80,
        "b1rr_raw": f"0x{b1rr:08x}", "b1pv_raw": f"0x{b1pv:08x}",
        "b1af_raw": f"0x{b1af:08x}", "b1vl_raw": f"0x{b1vl:08x}",
    }

# ---- Design capacity: try ACPI _BIX first --------------------------------
def read_design_capacity_mwh():
    """Try to read design capacity from /sys/class/power_supply first,
       else return None."""
    for psy in Path("/sys/class/power_supply").glob("*"):
        for fname in ("energy_full_design", "energy_full", "charge_full_design"):
            f = psy / fname
            if f.exists():
                try:
                    v = int(f.read_text().strip())
                    if v > 0:
                        return v // 1000  # µWh → mWh
                except (ValueError, OSError):
                    pass
    return None

def format_report(info):
    L = []
    L.append(f"AC adapter : {'plugged in' if info['acex'] else 'UNPLUGGED'}")
    L.append(f"Battery    : {'present' if info['b1ex'] else 'ABSENT'}")
    L.append(f"State      : {info['state']} (B1ST=0x{info['b1st_raw']:02x})")
    if info['percent'] is not None:
        # big, obvious percentage line
        L.append(f"Charge     : {info['percent']:5.1f}%")
    L.append(f"Voltage    : {info['voltage_mv']/1000:.3f} V now  /  "
             f"{info['design_mv']/1000:.2f} V design")
    flow = ('charging' if info['current_ma']>0 else
            'discharging' if info['current_ma']<0 else 'idle')
    L.append(f"Current    : {info['current_ma']:+d} mA  ({flow})")
    if info['remaining_mah'] is not None:
        design_wh  = (info['design_mah']  or 0) * info['design_mv'] / 1e6
        full_wh    = (info['fullchg_mah'] or 0) * info['design_mv'] / 1e6
        remain_wh  = info['remaining_mah']      * info['design_mv'] / 1e6
        L.append(f"Capacity   : {info['remaining_mah']} / {info['fullchg_mah']} mAh "
                 f"(design {info['design_mah']} mAh)")
        L.append(f"Energy     : {remain_wh:.2f} Wh now / {full_wh:.2f} Wh full / "
                 f"{design_wh:.2f} Wh design")
        if info['fullchg_mah'] and info['design_mah']:
            health = info['fullchg_mah'] * 100.0 / info['design_mah']
            L.append(f"Health     : {health:.1f}% (full-charge vs design)")
    else:
        L.append("Capacity   : UNKNOWN (0xFFFF sentinel)")
    L.append("")
    L.append(f"Raw        : B1RR={info['b1rr_raw']}  B1PV={info['b1pv_raw']}")
    L.append(f"             B1AF={info['b1af_raw']}  B1VL={info['b1vl_raw']}")
    L.append(f"             ECR[0x80]=0x{info['b80_raw']:02x}  B1VL.upper=0x{info['b1vl_upper']:04x}")
    return "\n".join(L)

WANT = [0x80, 0x82, 0x84,
        0xA0, 0xA1, 0xA2, 0xA3,
        0xA4, 0xA5, 0xA6, 0xA7,
        0xB0, 0xB1, 0xB2, 0xB3,
        0xB4, 0xB5, 0xB6, 0xB7]

def read_once():
    return decode_battery(ec_read_bytes(WANT))

def main():
    import argparse, json
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[2])
    ap.add_argument("--watch", "-w", type=float, nargs="?", const=2.0, default=None,
                    metavar="SEC", help="refresh every SEC seconds (default 2)")
    ap.add_argument("--json",  action="store_true", help="emit a single JSON line")
    ap.add_argument("--percent", "-p", action="store_true",
                    help="print just the integer charge percentage")
    args = ap.parse_args()

    def emit(info):
        if args.percent:
            print(int(round(info['percent'])) if info['percent'] is not None else "?")
        elif args.json:
            print(json.dumps(info, default=str))
        else:
            print(format_report(info))

    if args.watch is None:
        emit(read_once())
        return

    print(f"Polling every {args.watch}s — Ctrl-C to stop")
    try:
        while True:
            info = read_once()
            ts = time.strftime("%H:%M:%S")
            pct = f"{info['percent']:5.1f}%" if info['percent'] is not None else "  ?  %"
            flow = ('chg' if info['current_ma']>0 else
                    'dis' if info['current_ma']<0 else 'idl')
            print(f"[{ts}] {pct}  {info['voltage_mv']/1000:.2f}V  "
                  f"{info['current_ma']:+5d}mA {flow}  "
                  f"{info['remaining_mah']}/{info['fullchg_mah']}mAh  "
                  f"AC={'Y' if info['acex'] else 'N'}  B1ST=0x{info['b1st_raw']:02x}",
                  flush=True)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    try:
        main()
    except PermissionError:
        print("ERROR: must run as root (needs /dev/i2c-2).", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
