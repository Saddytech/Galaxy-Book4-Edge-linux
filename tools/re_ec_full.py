#!/usr/bin/env python3
"""
Comprehensive reverse-engineering of EC2.sys.

Produces a full protocol document by:
  1. Walking the IOCTL dispatcher 0x140003d20 .. 0x140004d00
  2. For every handler block, finding:
      - The EC opcode (from `mov w19, #imm`)
      - The payload length (from `mov w8, #imm; strb w8, [sp, 0xc]`)
      - The input-buffer reads (`ldrb wN, [x8]`, `ldrb wN, [x8, #N]`)
      - The log-string label (from `adrp/add` targeting 0x14000a000)
  3. Also identifies:
      - The common I2C WRITE path (after `b 0x140004a04/a08`)
      - The EC READ path (0x140005318 wrapper)

Output is saved to ec_protocol.md for the user to read.
"""
import lief, re
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
    if off is None: return None
    return int.from_bytes(sec[s][0][off:off+4], "little")

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
    if off is None or off+4 > len(sec[s][0]): return None
    for i in cs.disasm(sec[s][0][off:off+4], va):
        return i
    return None

# ---------------------------------------------------------------------------
# Pass 1: find every block in the dispatcher with `mov w19, #imm` and the
# surrounding context.  A "block" = the code from (mov w19) back to nearest
# `bl 0x140002390` (log call), and forward to the nearest `b 0x140004a0X`
# which is the common I2C-write jump.
# ---------------------------------------------------------------------------

DISPATCH_START, DISPATCH_END = 0x140003d20, 0x140004d00

# Locate all `mov/movz w19, #imm` with imm in plausible-opcode range
opcodes = []
for va in range(DISPATCH_START, DISPATCH_END, 4):
    ins = insn_at(va)
    if not ins: continue
    if ins.mnemonic in ("mov", "movz") and ins.op_str.startswith("w19, "):
        try:
            imm = int(ins.op_str.split(",")[1].strip().replace("#",""), 0)
        except: continue
        opcodes.append((va, imm))

# For each opcode, analyse its block
out = []
for op_addr, opcode in opcodes:
    info = {"opcode": opcode, "addr": op_addr}

    # Walk BACKWARDS up to 160 bytes looking for an adrp+add loading a log
    # string at 0x14000a.... to label this handler.
    label = None
    for sva in range(op_addr, max(DISPATCH_START, op_addr-160), -4):
        ins = insn_at(sva)
        if ins and ins.mnemonic == "bl" and ins.op_str.replace("#","") == "0x140002390":
            # The BL just before is the log call.  The adrp+add right before
            # it loads the log string.
            ins1 = insn_at(sva-8); ins2 = insn_at(sva-4)
            if ins1 and ins2 and ins1.mnemonic == "adrp" and ins2.mnemonic == "add":
                try:
                    base = ins1.operands[1].imm
                    off12 = ins2.operands[2].imm
                    target = base + off12
                    s = read_string_at(target)
                    if s:
                        label = s.strip()
                        break
                except: pass
    info["label"] = label

    # Look FORWARD for `mov wN, #imm` + `strb wN, [sp, 0xc]` — this is the
    # payload length.
    payload_len = None
    for sva in range(op_addr, op_addr+80, 4):
        ins = insn_at(sva)
        if not ins: continue
        # Look for strb wX, [sp, 0xc]  preceded by mov wX, #imm
        if ins.mnemonic == "strb" and "[sp, 0xc]" in ins.op_str:
            prev = insn_at(sva-4)
            if prev and prev.mnemonic in ("mov","movz"):
                try:
                    payload_len = int(prev.op_str.split(",")[1].strip().replace("#",""),0)
                    break
                except: pass
    info["payload_len"] = payload_len

    # Count ldrb loads from input buffer (x8) between op_addr and forward branch
    in_bytes = []
    for sva in range(op_addr-40, op_addr+80, 4):
        ins = insn_at(sva)
        if not ins: continue
        if ins.mnemonic == "ldrb":
            m = re.search(r"\[x(\d+)(?:, #?(0x[0-9a-f]+|\d+))?\]", ins.op_str)
            if m:
                off = 0
                if m.group(2):
                    try: off = int(m.group(2),0)
                    except: off = 0
                in_bytes.append(off)
    info["input_bytes"] = sorted(set(in_bytes))

    # Find the branch target (the common write helper)
    for sva in range(op_addr, op_addr+100, 4):
        ins = insn_at(sva)
        if ins and ins.mnemonic == "b":
            info["branch"] = ins.op_str.replace("#","")
            break

    out.append(info)

# ---------------------------------------------------------------------------
# Pass 2: analyse GET_DATA_FROM_EC path (the read wrapper at 0x140005318)
# and decode how an EC read is structured.
# ---------------------------------------------------------------------------
read_wrapper_dump = []
for va in range(0x1400052d0, 0x140005380, 4):
    ins = insn_at(va)
    if ins:
        read_wrapper_dump.append(f"0x{ins.address:x}: {ins.mnemonic} {ins.op_str}")
    else:
        v = read_u32(va)
        read_wrapper_dump.append(f"0x{va:x}: .word 0x{v:08x}" if v else "")

# ---------------------------------------------------------------------------
# Output markdown
# ---------------------------------------------------------------------------
with open("ec_protocol.md","w") as f:
    f.write("# Samsung Galaxy Book4 Edge — EC Protocol (Reverse-Engineered)\n\n")
    f.write("**Target**: ENE KB9058 on I2C slave `0x62`, bus `i2c-5`.\n\n")
    f.write("## IOCTL handler map (complete)\n\n")
    f.write("| Opcode | Label | Payload len | Input bytes read |\n")
    f.write("|---|---|---|---|\n")
    for i in out:
        f.write(f"| `0x{i['opcode']:02x}` | {i.get('label') or '???'} | "
                f"{i['payload_len']} | {i.get('input_bytes') or 'n/a'} |\n")
    f.write("\n## Handler addresses and common branches\n\n")
    for i in out:
        f.write(f"- opcode 0x{i['opcode']:02x} — handler at 0x{i['addr']:x}, "
                f"branches to {i.get('branch','?')}\n")
    f.write("\n## EC read wrapper disassembly (0x1400052d0..0x140005380)\n\n```\n")
    f.write("\n".join(read_wrapper_dump))
    f.write("\n```\n")

print("Wrote ec_protocol.md")
print()
print("## IOCTL handler map")
print(f"{'Opcode':8} {'Payload':8} {'InputOfs':16} Label")
for i in out:
    print(f"0x{i['opcode']:02x}    {str(i['payload_len']):8} {str(i.get('input_bytes','n/a')):16} {i.get('label') or '???'}")
