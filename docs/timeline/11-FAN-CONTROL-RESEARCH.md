# Chapter 11 — Fan Control Research and the SABI Dead-End

## 11.1 The motivating observation

With Ubuntu installed on internal UFS, the laptop ran hot under load. The user ran a fan-ramp test using the existing EC helper (`ec_fan_set`) and observed:

- At RPM target 8 000: quiet
- At RPM target 12 000: louder
- At RPM target 20 000: **same loudness** as 12 000

> **"i think the fan maxes out around 12 000 RPM, but the ramp up takes forever. when i overshoot to 20 000 it's not faster, it just caps."**

Two problems:
1. **Maximum RPM**: ~12 000 — hardware ceiling
2. **Ramp speed**: painfully slow — EC's PID loop has a slew-rate limit that makes thermal emergencies respond too gradually

Windows feels much snappier on the same hardware, so there has to be a way to tell the EC to ramp faster.

## 11.2 Round-1 online search

Installed `git` on the build host:

```bash path=null start=null
sudo apt-get install -y git
```

Targeted searches:

- "ENE KB9058 fan control Linux"
- "Snapdragon X Elite fan control"
- "x1e80100 EC fan"
- "Samsung SABI Linux ARM"

### Discovered projects

| Project | EC chip | Laptops | Relevance to Book4 Edge |
|---|---|---|---|
| `icecream95/x1e-ec-tool` | ITE (I2C `0x5B` + `0x76`) | ASUS Vivobook S 15 | Same I²C bus family; different chip |
| `Maccraft123/it8987-qcom-tool` | IT8987 @ `0x76` | Lenovo Slim 7x, Honor, HP, Medion | Pattern reference for X1E laptops |
| `qcom-x1e-it8987` (kernel patch series) | IT8987 | Most X1E | Mainline template; **doesn't cover Samsung** |
| TUXEDO X1E project notes | — | Cancelled X1E laptop | **Public admission that X1E fan control is an industry-wide gap** |
| `joshuagrisham/samsung-galaxybook-extras` | — (x86) | NP-series Galaxy Books | **Contains SABI v4 protocol docs + 7 DSDT dumps** |
| `Andycodeman/samsung-galaxy-book-linux-fixes` | x86 (Meteor Lake / Lunar Lake) | Galaxy Book 3/4/5 x86 | Doesn't cover ARM |

Samsung Galaxy Book4 Edge uses **ENE KB9058 at I²C `b94000` slaves `0x62` and `0x64`** — a different chip family than the others. So prior art was parallel but not directly applicable.

## 11.3 Setting up a cache

Everything worth preserving was git-cloned to `<HOME>/re-cache/fan-control-research/`:

```bash path=null start=null
mkdir -p <HOME>/re-cache/fan-control-research
cd <HOME>/re-cache/fan-control-research
git clone --depth=1 https://github.com/icecream95/x1e-ec-tool.git
git clone --depth=1 https://github.com/Maccraft123/it8987-qcom-tool.git
git clone --depth=1 https://github.com/joshuagrisham/samsung-galaxybook-extras.git
```

Also pulled the mainline driver sources from Linus' tree:

```bash path=null start=null
mkdir mainline-drivers
cd mainline-drivers
wget https://git.kernel.org/.../platform/x86/samsung-galaxybook.c
wget https://git.kernel.org/.../platform/x86/samsung-laptop.c
```

Final cache size: **22 MB**.

## 11.4 The ASUS kick-start pattern (icecream95)

`x1e-ec-tool/tool.py` revealed a useful trick: before ramping to target RPM, ASUS's ITE EC accepts a **pre-kick** write that bypasses the PID loop and sets direct PWM for a moment:

```python
def set_fan_speed(fan_id, pwm_value):
    # 0x5B is the "direct PWM" register on IT8987
    ec_write(0x5B, fan_id, pwm_value)

# Spin-up routine
set_fan_speed(0, 170)   # medium pulse to overcome stall torque
time.sleep(1)
set_fan_speed(0, 255)   # full bore
```

This means on IT8987: **writing register `0x5B` skips the PID slew limiter**.

On our ENE KB9058 we have opcodes `0x08` (fan zone 0-15) and `0x17` (fan RPM target). Hypothesis: write `0x08 FANZONE=15` for 1.5 s (direct PWM), then switch to `0x17` with the real target. Tested — modest improvement, but not the dramatic ramp Windows exhibits.

## 11.5 The OS→EC temperature feed pattern

The ASUS/IT8987 family pattern also has the OS **push CPU temperature** into an EC register; the EC then uses that to drive its own aggressive curve.

Our KB9058 doesn't expose a documented temperature register. We scanned `EC2.sys` strings for `SET_CPU_TEMP` or `OS_TEMP_FEED` but didn't find matching handlers. Dead end.

## 11.6 The SABI protocol v4 discovery

The mainline `samsung-galaxybook.c` driver (Joshua Grisham) exposed the full **SABI v4** protocol:

| Field | Value |
|---|---|
| SAFN (signature) | `0x5843` = ASCII "CX" |
| **SASB `0x91`** | `GB_SASB_PERFORMANCE_MODE` ⭐ |
| FNCN `0x51` | Performance mode function |
| SUBN `0x01` | LIST |
| SUBN `0x02` | GET |
| SUBN `0x03` | SET |
| Mode `0x0B` | FANOFF |
| Mode `0x0A` | LOWNOISE (silent) |
| Mode `0x02` | OPTIMIZED / AUTO |
| Mode `0x01` | PERFORMANCE |
| Mode `0x15` | PERFORMANCE_V2 |
| Mode `0x16` | **ULTRA** |

On **x86** Galaxy Books, this protocol is invoked via the ACPI method `CSXI` in the DSDT. On **ARM** Galaxy Book4 Edge, there's no ACPI — everything goes through device tree + direct I²C writes.

**Hypothesis**: Samsung's Windows driver on the Book4 Edge sends the equivalent SABI packet **directly over I²C** to the EC. If we could find the opcode (on I²C `0x62`), we could switch performance modes and inherit Samsung's factory-tuned ramp curve.

## 11.7 First guess: opcodes `0x12` and `0x13`

`strings EC2.sys | grep -i sabi` turned up references to `SVCLED_SABI` — the EC's reverse-engineered handlers for opcodes `0x12` and `0x13` had been labelled "SVCLED_SABI" in an internal Samsung log string. The SABI naming made these very tempting candidates.

A test script (`<HOME>/test_sabi_v4.py`) probed three wire-format layouts:

```python
# Layout A — direct translation of struct sawb
ec_write(0x12, [0x91, 0x03, mode, 0x00])    # SASB=0x91, SUBN=0x03 SET, mode

# Layout B — EC infers SASB internally
ec_write(0x12, [0x03, mode, 0x00, 0x00])

# Layout C — reversed byte order, like opcode 0x17 FANRPM2
ec_write(0x12, [0x00, 0x00, mode, 0x03, 0x91])
```

Each write was followed by an 8-byte read. Expected response byte 4 = `0xAA` (RFLG success marker).

Results:

```
sent 0x12 [0x91, 0x03, 0x16, 0x00]
recv [0x01, 0x02, 0x91, 0x03, 0x16, 0x00, 0x22, 0x00]

sent 0x12 [0x03, 0x16, 0x00, 0x00]
recv [0x01, 0x02, 0x03, 0x16, 0x00, 0x00, 0x22, 0x00]
```

The EC was just **echoing** our input inside an envelope `[0x01, 0x02, ...echo..., 0x22, 0x00]`. No `0xAA`, no mode change, no fan response, no CPU-frequency effect.

> **Opcodes `0x12` and `0x13` are NOT the SABI entry points.** The "SVCLED_SABI" string in EC2.sys was a log-message label, not a function name.

## 11.8 The DSDT reveals the truth

The agent asked the user to boot Windows and copy the **full DSDT** from the Samsung driver package (`C:\Samsung\BASW-A4232A01_1063\`). Decompiled with `iasl`:

```
Device (ECTC) {
    Name (_HID, "SAM060B")
    Resource: I2cSerialBus (0x0062, ...) on ^I2C6
    Resource: I2cSerialBus (0x0064, ...) on ^I2C1    ← SECOND I2C bus!
}

Device (SCAI) {
    Name (_HID, "SAM0430")
    Method (CSXI, 1, NotSerialized) {
        ...
        Return (PRF3 (Arg0))
    }
    Method (PRF3, 1, NotSerialized) {
        If (SUBN == 0x03) {     // SET
            // Update thermal zone trip points per mode
            If (mode == 0x0A) { TZ31 = 5500; TZ32 = 7000; ... }
            If (mode == 0x15) { TZ31 = 9500; TZ32 = 11000; ... }
            // Issue EC command
            CMDD (0xEE, 0x02, [0x80, mode])
        }
        ...
    }
}
```

**Two major findings:**

1. The EC is on **TWO I²C buses** (`b94000` slave `0x62` AND `b90000` slave `0x64`). The agent had been writing only to `0x62`. The `0x64` bus was untouched.
2. The **real performance-mode opcode is `0xEE`**, not `0x12`/`0x13`. Wire format: `[0xEE, 0x80, mode]`.

## 11.9 The supported modes on THIS laptop

Further reading of `PRF3`'s SUBN=`0x01` LIST branch showed this model supports exactly **three** modes (not six like x86):

- `0x02` AUTO (balanced) — default
- `0x0A` SILENT
- `0x15` MAXPERF

**Not** `0x16` ULTRA (that's x86-only) and **not** `0x01` PERFORMANCE_V1.

## 11.10 Testing opcode `0xEE`

Updated the test script to use `0xEE`:

```python
ec_write(0xEE, [0x80, 0x15])   # set MAXPERF
response = ec_read(8)
# [0x01, 0x02, 0x80, 0x02, 0x00, 0x00, 0x22, 0x00]
```

**Same echo pattern as `0x12`/`0x13`.** `0xEE` on I²C `0x62` is ALSO just echoing.

## 11.11 The realisation: `0xEE` is not an I²C opcode

The DSDT's `CMDD` macro doesn't directly send `0xEE` over I²C. It writes into an `OperationRegion` at address `0xA2` — a **custom region type** that `EC2.sys` (the Samsung Windows driver) intercepts and translates into *some other I²C framing* we don't yet understand.

The EC at I²C `0x62` has a **dispatch table** for opcodes `0x00, 0x01, 0x08, 0x0C, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x17`. Anything outside that list — including `0xEE` — falls through to a default handler that echoes the input. So `0xEE` **isn't an opcode on that bus at all.**

The real SABI path is:

```
Windows user app
  ↓ CSXI ACPI method
  ↓ PRF3 method
  ↓ CMDD macro
  ↓ writes to OperationRegion 0xA2 (ECMD/MBUF)
  ↓ EC2.sys intercepts the region write
  ↓ translates to SOME I²C framing (unknown to us)
  ↓ sends on EITHER I²C6@0x62 OR I²C1@0x64
  ↓ EC executes the mode change
```

Two possibilities for the real path:
1. The `0x64` I²C slave on I²C1 uses a different protocol (higher-level mailbox?)
2. The `0x62` slave accepts a mailbox-style extended command via one of its existing opcodes (probably `0x10` or `0x11`)

## 11.12 The fallback plan

Since `0xEE` as an I²C opcode is a dead end, three paths remained:

1. **Pragmatic**: Build a user-space fan daemon using the known-good opcodes `0x08` (zone) + `0x17` (RPM target), with the ASUS kick-start pattern. Good enough for most use cases. Don't attempt performance-mode switching.

2. **Exploratory**: Probe `0x64` on the second I²C bus. Risky — unknown protocol could latch the fan at 0 RPM.

3. **Long-term**: Reverse-engineer `EC2.sys` to decode what exactly it puts on the wire for `OperationRegion 0xA2` writes. This would give us the full SABI protocol.

The user chose Option 3 — that's Chapter 12 (EC reverse engineering).

## 11.13 Opcode map (decoded handlers, from RE)

For reference, the opcode dispatch discovered by static + dynamic analysis of EC2.sys:

| Opcode | Samsung name | Description |
|---|---|---|
| `0x00` | `GET_PROTOCOL_VERSION` | Returns major/minor |
| `0x01` | `GET_DEVICE_INFO` | Returns chip string |
| `0x08` | `FANZONE` | Sets fan auto-curve zone 0-15 |
| `0x0C` | `SET_ECLOG` | Configure EC internal logging |
| `0x0F` | `GET_EC_RAM` | Read a register in EC RAM (range 0x00-0xFF) |
| `0x10` | `WRITE_EC_RAM` | Write a byte to EC RAM |
| `0x11` | `EXECUTE_MBOX` | Execute a mailbox-style command (CMDD backend?) |
| `0x12` | `SVCLED_SABI` | Service LED/flag (echoes in our tests) |
| `0x13` | `SET_SVCLED_FLAG` | Same family |
| `0x17` | `FANRPM2` | Set fan 2 target RPM (0-20000) |

Opcode `0x11` became the prime suspect for the real SABI path — it was the **EXECUTE_MBOX** handler. Chapter 12 traced what EC2.sys actually puts in the mailbox.

## 11.14 End of Chapter 11

Outputs by end of chapter:

- `<HOME>/re-cache/fan-control-research/` — 22 MB cache of relevant tools and DSDTs
- `<HOME>/re-cache/fan-control-research/NOTES.md` — research brief summarising findings
- `<HOME>/test_sabi_v4.py` — test harness with three wire-format layouts
- Confirmed dead-ends:
  - Opcodes `0x12`/`0x13`/`0xEE` on I²C `0x62` are **not** the SABI path
  - Samsung 14" Book4 Edge supports only 3 perf modes, not 6 (no Ultra)
- Open leads:
  - I²C1 `0x64` (second EC bus, untouched so far)
  - Opcode `0x11` `EXECUTE_MBOX` on `0x62`
- Cached DSDT showed `OperationRegion 0xA2` with `ECMD` + `MBUF[240]` fields

Co-Authored-By: Oz <oz-agent@warp.dev>
