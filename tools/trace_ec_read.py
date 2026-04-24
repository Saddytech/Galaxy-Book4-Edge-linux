#!/usr/bin/env python3
"""
Trace the EC-read call path:
   OpRegion handler
      -> BMOP/EMOP field read
         -> function at 0x14000532c (calls SendExCmdDataReadData via 0x140005b20)
            -> actual I2C transaction

We want to find the immediate bytes passed in as command bytes / offsets
so we can reproduce the read from Linux.

Approach:
  1. Dump 0x140005300..0x140005500 (the "read field" wrapper) with call resolution
  2. Dump the function called (looks like 0x140005b20) for I2C op codes
  3. Look for:
      - `mov wN, #imm` setting small immediates (candidate command bytes)
      - `i2ctransfer`/"hid"-like function calls
      - any WDF I2C request calls
"""
import lief
from capstone import *
from capstone.arm64 import *
import os

PATH = os.environ.get("EC2_SYS_PATH", "EC2.sys")
pe = lief.parse(PATH)
imbase = pe.optional_header.imagebase

sec = {}
for s in pe.sections:
    va = imbase + s.virtual_address
    sec[s.name.rstrip('\x00')] = (bytes(s.content), va)

def va_to_off(va):
    for s in pe.sections:
        base = imbase + s.virtual_address
        if base <= va < base + len(bytes(s.content)):
            return va - base, s.name.rstrip('\x00')
    return None, None

def read_u32(va):
    off, s = va_to_off(va)
    if off is None:
        return None
    return int.from_bytes(sec[s][0][off:off+4], "little")

cs = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
cs.detail = True

text, tva = sec[".text"]
def insn_at(va):
    off, s = va_to_off(va)
    if off is None or off + 4 > len(sec[s][0]):
        return None
    for i in cs.disasm(sec[s][0][off:off+4], va):
        return i
    return None

# known string labels to resolve
LABELS = {
    0x14000a648: "str.IOCTL_I2CRead_TEST",
    0x14000a6a8: "str.IOCTL_I2CWrite_TEST",
    0x14000ad28: "SecEne9058KbcSendExCmd",
    0x14000ad40: "SecEne9058KbcSendExCmdData",
    0x14000ad60: "SecEne9058KbcSendExCmdDataReadData",
    0x14000ad88: "SecEne9058KbcSendExCmdFull",
    0x14000ada8: "SecEne9058KbcWriteEcSpace",
    0x140002390: "DbgPrintEx",
    0x140005b20: "I2C_op (lowlevel)",
    0x1400017b0: "???_helper",
}

def dump(start, count, label=None):
    if label:
        print(f"\n=== {label} (start=0x{start:x}) ===")
    for i in range(count):
        va = start + i*4
        ins = insn_at(va)
        if ins is None:
            v = read_u32(va)
            print(f"   0x{va:x}: .word 0x{v:08x}" if v else f"   0x{va:x}: ???")
            continue
        extra = ""
        # Resolve BL targets into named labels
        if ins.mnemonic == "bl":
            try:
                t = int(ins.op_str.replace("#",""), 16)
                if t in LABELS:
                    extra = f"   ; -> {LABELS[t]}"
                else:
                    extra = f"   ; -> fcn.{t:x}"
            except Exception:
                pass
        # Resolve ADRP loads into "page= ..."
        if ins.mnemonic == "adrp":
            try:
                t = ins.operands[1].imm
                extra = f"   ; page 0x{t:x}"
            except Exception:
                pass
        print(f"   0x{ins.address:x}: {ins.mnemonic:10s} {ins.op_str}{extra}")

# 1) Dump the function around 0x140005318 (the read helper "wrapper")
dump(0x1400052a0, 120, "Read wrapper around 0x140005318")

# 2) Dump 0x140005b20 — the likely low-level I2C function
dump(0x140005b00, 120, "Low-level function around 0x140005b20")
