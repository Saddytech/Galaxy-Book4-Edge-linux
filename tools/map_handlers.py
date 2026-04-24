#!/usr/bin/env python3
"""
Walk the IOCTL dispatcher (0x140003d20 .. 0x140004c80) and for every
`mov w19, #imm` found, identify:
  - The immediate (EC opcode)
  - The nearest preceding or following `adrp/add` that loads a log string
  - That string's text (so we know which IOCTL this opcode is for)

This gives us the FULL mapping of EC opcode bytes to IOCTLs.
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

text, tva = sec[".text"]

def read_string_at(va, maxlen=80):
    off, s = va_to_off(va)
    if off is None: return ""
    data = sec[s][0]
    end = off
    while end < min(off+maxlen, len(data)) and 32 <= data[end] <= 126:
        end += 1
    return data[off:end].decode("ascii", errors="replace")

cs = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
cs.detail = True

def insn_at(va):
    off, s = va_to_off(va)
    if off is None or off + 4 > len(sec[s][0]):
        return None
    for i in cs.disasm(sec[s][0][off:off+4], va):
        return i
    return None

# Walk the dispatcher. For each 4-byte slot, check if it's a 'mov w19, #imm'
# or 'movz w19, #imm'.
start, end = 0x140003d20, 0x140004d00

movw19_hits = []  # list of (addr, imm)
for va in range(start, end, 4):
    i = insn_at(va)
    if i is None: continue
    mn = i.mnemonic
    op = i.op_str
    if mn in ("mov", "movz") and op.startswith("w19, "):
        # Parse the immediate
        try:
            val = int(op.split(",")[1].strip().replace("#",""), 0)
        except Exception:
            continue
        movw19_hits.append((va, val))

print(f"Found {len(movw19_hits)} 'mov w19, #imm' sites:")
for addr, imm in movw19_hits:
    print(f"  0x{addr:x}: mov w19, 0x{imm:x} ({imm} dec)")

# Now for each hit, look ±80 bytes for an adrp+add that loads a log string
# at 0x14000a.... (string region).
print("\n=== Mapping each opcode to its log string ===")
for addr, imm in movw19_hits:
    # Search within 80 bytes before-after for `adrp x8, 0x14000a000` + `add x0, x8, #X`
    label = None
    for sva in range(addr-80, addr+80, 4):
        ins1 = insn_at(sva)
        ins2 = insn_at(sva+4)
        if ins1 and ins2 and ins1.mnemonic == "adrp" and ins2.mnemonic == "add":
            # Decode base + imm
            try:
                base = ins1.operands[1].imm
                if base != 0x14000a000: continue
                off12 = ins2.operands[2].imm
                target = base + off12
                s = read_string_at(target)
                if s and ("IOCTL" in s or "KBDBLT" in s or "FAN" in s or "CAPS" in s or "ECLOG" in s or "SABI" in s):
                    label = f"0x{target:x} -> '{s}'"
                    break
            except Exception:
                continue
    print(f"  opcode 0x{imm:02x} (at 0x{addr:x}):  {label or '???'}")
