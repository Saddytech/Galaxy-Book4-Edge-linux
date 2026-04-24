#!/usr/bin/env python3
"""
Samsung Galaxy Book4 Edge — EC (ENE KB9058) control library.

Target:  I2C slave 0x62 on bus i2c-5 (the 0xB94000 GENI controller we enabled
         via the KBDBLT DTB).

All protocol details in this module were reverse-engineered from Samsung's
Windows driver EC2.sys.  See ec_protocol.md for raw details.

SAFE USE:
  - Every send_* method explicitly passes a known opcode; we do NOT write
    arbitrary bytes to the EC.
  - Reads don't alter state on the EC (worst case we get stale buffer).
  - Helpers for setting fan back to AUTO are provided at the top in case
    anything gets stuck.

Usage:
  sudo python3 book4_ec.py status
  sudo python3 book4_ec.py fan-auto
  sudo python3 book4_ec.py fan-rpm 50
  sudo python3 book4_ec.py kbd-backlight 3
  sudo python3 book4_ec.py read-raw 8
"""
from __future__ import annotations
import os, sys, fcntl, struct, argparse, time

# ---- Protocol constants (reverse engineered) --------------------------------
I2C_BUS        = 5
EC_ADDR        = 0x62

# Decoded opcodes: {opcode: (name, write_len, safe_defaults)}
OPCODES = {
    0x00: "TEST_POWERBUTTON",
    0x01: "TEST_START",
    0x08: "SET_FANRPM",           # 2-byte write: [0x08, value]; value=0 → auto
    0x0C: "SET_ECLOG",
    0x0F: "SET_CAPSLED",
    0x10: "SET_KBD_BACKLIGHT",    # 3-byte: [0x10, timeout, level]
    0x11: "GET_KBD_BACKLIGHT",    # 1-byte send then read
    0x12: "SVCLED_SABI",          # 4-byte
    0x13: "SET_SVCLED_FLAG",      # 4-byte
    0x17: "SET_FANRPM2",          # 5-byte, reversed: [0x17, in[3], in[2], in[1], in[0]]
}

# ---- Low-level I2C primitives ------------------------------------------------
I2C_SLAVE = 0x0703

def _open_bus():
    fd = os.open(f"/dev/i2c-{I2C_BUS}", os.O_RDWR)
    fcntl.ioctl(fd, I2C_SLAVE, EC_ADDR)
    return fd

def ec_write(payload: bytes):
    """Write raw `payload` bytes to the EC."""
    fd = _open_bus()
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)

def ec_read(n: int = 8) -> bytes:
    """Read `n` bytes from the EC response buffer.
    Format observed: [0x07, status, data0, data1, ...]"""
    fd = _open_bus()
    try:
        return os.read(fd, n)
    finally:
        os.close(fd)

def ec_command(opcode: int, *data: int, read_back: int = 8) -> bytes:
    """Send [opcode, *data] and read back the response buffer."""
    if opcode not in OPCODES:
        raise ValueError(f"Refusing to send unknown opcode 0x{opcode:02x}. "
                         f"Known: {[hex(o) for o in OPCODES]}")
    payload = bytes([opcode & 0xff] + [b & 0xff for b in data])
    ec_write(payload)
    time.sleep(0.01)                # give EC time to respond
    return ec_read(read_back)

# ---- High-level helpers ------------------------------------------------------
def fan_auto():
    """Put fan back under EC auto-thermal control (restores from 'stuck' state)."""
    return ec_command(0x08, 0x00)

def fan_set_rpm(value: int):
    """Set fan RPM percent 0..100.  0 = auto, 100 = max."""
    if not 0 <= value <= 100:
        raise ValueError("fan value 0..100")
    return ec_command(0x08, value)

def fan_mode2(b0: int, b1: int, b2: int, b3: int):
    """FANRPM2 — 4-byte mode command.  Byte order on wire is reversed (b3,b2,b1,b0).
    Exact semantics still TBD; use only for experimentation."""
    return ec_command(0x17, b3, b2, b1, b0, read_back=8)

def kbd_backlight(level: int, timeout: int = 0):
    """Set keyboard backlight level 0..3 with optional timeout seconds."""
    if not 0 <= level <= 3:
        raise ValueError("level 0..3")
    return ec_command(0x10, timeout & 0xff, level & 0xff)

def kbd_backlight_get():
    """Read current kbd backlight level."""
    r = ec_command(0x11, read_back=8)
    return r

def capslock_led(on: bool):
    """Toggle the Samsung-controlled CapsLock LED (separate from HID CapsLock)."""
    return ec_command(0x0F, 0x01 if on else 0x00)

def read_raw(op: int, *data: int, n: int = 8) -> bytes:
    """Raw KNOWN-opcode command + read response."""
    return ec_command(op, *data, read_back=n)

def status():
    """Print everything we can read about the EC right now."""
    print(f"EC: slave 0x{EC_ADDR:x} on /dev/i2c-{I2C_BUS}")
    print()
    print("=== Last response buffer (5 reads in succession) ===")
    for _ in range(5):
        r = ec_read(8)
        print(" ", " ".join(f"{b:02x}" for b in r))
        time.sleep(0.05)

# ---- CLI ---------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    try:
        if cmd == "status":
            status()
        elif cmd == "fan-auto":
            r = fan_auto(); print("resp:", " ".join(f"{b:02x}" for b in r))
        elif cmd == "fan-rpm":
            v = int(sys.argv[2]); r = fan_set_rpm(v); print("resp:", " ".join(f"{b:02x}" for b in r))
        elif cmd == "kbd-backlight":
            lvl = int(sys.argv[2]); t = int(sys.argv[3]) if len(sys.argv)>3 else 0
            r = kbd_backlight(lvl, t); print("resp:", " ".join(f"{b:02x}" for b in r))
        elif cmd == "kbd-get":
            r = kbd_backlight_get(); print("resp:", " ".join(f"{b:02x}" for b in r))
        elif cmd == "caps":
            r = capslock_led(sys.argv[2] in ("on","1","true"))
            print("resp:", " ".join(f"{b:02x}" for b in r))
        elif cmd == "read-raw":
            op = int(sys.argv[2],0)
            data = [int(x,0) for x in sys.argv[3:]]
            r = read_raw(op, *data)
            print("resp:", " ".join(f"{b:02x}" for b in r))
        else:
            print(__doc__); sys.exit(1)
    except PermissionError:
        print("Run as root (sudo)"); sys.exit(1)

if __name__ == "__main__":
    main()
