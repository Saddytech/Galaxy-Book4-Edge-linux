# Samsung Galaxy Book4 Edge — EC Protocol (Reverse-Engineered)

**Target**: ENE KB9058 on I2C slave `0x62`, bus `i2c-5`.

## IOCTL handler map (complete)

| Opcode | Label | Payload len | Input bytes read |
|---|---|---|---|
| `0x01` | IOCTL_TEST_START | None | n/a |
| `0x00` | ++++++++++++++++++++++++++++++ | None | [0] |
| `0x0f` | IOCTL_SET_CAPSLED | None | n/a |
| `0x0c` | IOCTL_SET_ECLOG | None | [0] |
| `0x08` | fanzone to set = 0x%x | None | [0] |
| `0x10` | IOCTL_SET_KBDBLT | None | [0, 1] |
| `0x17` | ioTargetState = 0x%x | None | [0, 1, 2, 3] |
| `0x13` | IOCTL_SET_SVCLED_FLAG | None | [0, 1, 2] |
| `0x12` | IOCTL_SVCLED_SABI | None | [0, 1, 2] |
| `0x11` | IOCTL_GET_KBDBLT | None | [1] |

## Handler addresses and common branches

- opcode 0x01 — handler at 0x14000406c, branches to 0x1400049f0
- opcode 0x00 — handler at 0x140004134, branches to 0x140003f5c
- opcode 0x0f — handler at 0x140004278, branches to 0x1400049f0
- opcode 0x0c — handler at 0x140004358, branches to 0x140004a08
- opcode 0x08 — handler at 0x140004430, branches to 0x140004a04
- opcode 0x10 — handler at 0x140004664, branches to 0x140004384
- opcode 0x17 — handler at 0x140004788, branches to 0x140004a08
- opcode 0x13 — handler at 0x140004810, branches to 0x140004a08
- opcode 0x12 — handler at 0x1400048a0, branches to 0x140004830
- opcode 0x11 — handler at 0x1400049ec, branches to ?

## EC read wrapper disassembly (0x1400052d0..0x140005380)

```
0x1400052d0: cmp w20, w25
0x1400052d4: b.lo #0x1400052b4
0x1400052d8: ldp x24, x23, [sp, #0x38]
0x1400052dc: mov w0, w21
0x1400052e0: bl #0x140006370
0x1400052e4: uxtb w8, w0
0x1400052e8: mov w0, w21
0x1400052ec: cbz w8, #0x140005354
0x1400052f0: bl #0x140006280
0x1400052f4: bl #0x140006418
0x1400052f8: mov w20, w0
0x1400052fc: tbz w20, #0x1f, #0x140005244
0x140005300: ldr x8, [x19]
0x140005304: cmp x8, x19
0x140005308: b.eq #0x140005330
0x14000530c: mov w3, #0x19
0x140005310: adrp x8, #0x14000c000
0x140005314: ldr x8, [x8, #0x98]
0x140005318: adrp x9, #0x14000a000
0x14000531c: add x5, x9, #0xd60
0x140005320: mov w6, w20
0x140005324: ldr x0, [x8, #0x40]
0x140005328: mov w2, #5
0x14000532c: bl #0x140005b20
0x140005330: mov w21, w20
0x140005334: ldr x0, [x23, #0x98]
0x140005338: bl #0x140003ae8
0x14000533c: adrp x25, #0x14000c000
0x140005340: ldr x8, [x25, #0xa08]
0x140005344: adrp x9, #0x14000c000
0x140005348: ldr x0, [x9, #0xa10]
0x14000534c: ldr x2, [sp, #0xb8]
0x140005350: b #0x140004c00
0x140005354: bl #0x140006280
0x140005358: mov w21, #0
0x14000535c: b #0x140005334
0x140005360: cmp w20, #0
0x140005364: csel w20, w20, wzr, lt
0x140005368: csdb 
0x14000536c: mov w0, w21
0x140005370: bl #0x140006280
0x140005374: ldr x8, [x19]
0x140005378: cmp x8, x19
0x14000537c: b.eq #0x140005330
```
