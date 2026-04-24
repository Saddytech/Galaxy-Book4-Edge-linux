# Chapter 4 — Firmware Extraction from Windows

## 4.1 Why this step existed at all

Qualcomm's ADSP (`qcadsp8380.mbn`), CDSP (`qccdsp8380.mbn`), WLAN firmware (`wlanfw20.mbn`), ath12k `board-2.bin`, and the Bluetooth `hmtbtfw20.tlv` / `hmtnv20.bin` are **not in linux-firmware** for the X1E80100 Galaxy Book4 Edge combination. They're not redistributable, and without them:

- ADSP failing to load → USB-C altmode state machine never initialises → dock disconnects
- CDSP failing to load → several drivers that depend on the compute DSP fail silently
- No WLAN firmware → Wi-Fi chip shows up but never brings up a network interface
- No `board-2.bin` with the right Samsung subsystem ID → even with firmware, ath12k fails to find a matching calibration profile

Windows has **working drivers** for this exact silicon. So the shortest path is to extract the blobs from `C:\Windows\System32\DriverStore\FileRepository` and translate Windows filenames to the paths Linux expects.

## 4.2 Staging setup

SSH + SCP with key auth (from Chapter 2) made this essentially one command:

```bash path=null start=null
# Create a tight zip on the Windows side with only the blobs we care about
ssh -i ~/.ssh/book4edge user@10.x.x.x 'powershell -Command "
    $src = \"C:\\Windows\\System32\\DriverStore\\FileRepository\";
    $dst = \"C:\\Users\\user\\Desktop\\qc-fw.zip\";
    $files = Get-ChildItem -Path $src -Recurse -Include *.mbn,*.bin,*.tlv \
             -ErrorAction SilentlyContinue | Where-Object { 
                 $_.DirectoryName -like \"*qc*\" 
             };
    Compress-Archive -Path $files -DestinationPath $dst -Force;
    (Get-Item $dst).Length
"'

# Pull it back
scp -i ~/.ssh/book4edge user@10.x.x.x:C:/Users/user/Desktop/qc-fw.zip \
    ~/Documents/Galaxy-Book4-Edge-linux/firmware-raw/

# Unpack
cd ~/Documents/Galaxy-Book4-Edge-linux/firmware-raw/
unzip qc-fw.zip -d extracted/
```

Result: **128 firmware files**, 38 MB compressed, sitting in `firmware-raw/extracted/` with Windows-style paths.

## 4.3 File inventory (what matters)

Of the 128 files, these were the ones that mattered for first boot:

| Windows name | Size | Role |
|---|---|---|
| `qcadsp8380.mbn` | 19.9 MB | ADSP remoteproc firmware |
| `qccdsp8380.mbn` | 3.1 MB | CDSP remoteproc firmware |
| `qcwlanhmt8380.wlanfw20.mbn` | 6.0 MB | WCN7850 WLAN firmware (ath12k) |
| `qcwlanhmt8380.regdb.bin` | 24 KB | Wi-Fi regulatory domain |
| `qcdxkmsuc8380.mbn` | 12 KB | Direct-X kernel-mode (GPU micro-code, not usable on Linux) |
| `hmtbtfw20.tlv` | 281 KB | Bluetooth firmware (native Linux filename!) |
| `hmtnv20.bin` | 9.6 KB | Bluetooth NVM calibration (native Linux filename!) |
| `wpss.mbn` | 7.3 MB | WLAN subsystem processor firmware |
| `qcdxkmbase8380_*.bin` | varies | Windows-format GPU firmware — **not usable** by Linux's Adreno driver |

The GPU firmware naming mismatch was the only blocker for accelerated graphics: Linux's MSM driver expects `a750_sqe.fw` / `a750_gmu.bin`, not `qcdxkmbase8380_*.bin`. Since the zensanp README says to leave the GPU disabled until Mesa ≥ 25.3.3 anyway, this was a non-issue — but flagged in `DEVICE-INFO.md` for later work.

## 4.4 Path translation

The Linux firmware tree expects a very specific hierarchy:

```
/lib/firmware/
├── ath12k/
│   └── WCN7850/
│       └── hw2.0/
│           ├── amss.bin       ← from wlanfw20.mbn (renamed)
│           ├── board-2.bin    ← from upstream linux-firmware.git
│           └── regdb.bin      ← from regdb.bin (Windows)
├── qca/
│   ├── hmtbtfw20.tlv          ← direct copy
│   └── hmtnv20.bin            ← direct copy
└── qcom/
    └── x1e80100/
        └── samsung/
            └── galaxy-book4-edge/
                ├── adsp.mbn   ← renamed from qcadsp8380.mbn
                ├── cdsp.mbn   ← renamed from qccdsp8380.mbn
                └── wpss.mbn   ← renamed from wpss.mbn
```

A short staging script performed the rename + mkdir dance:

```bash path=null start=null
STAGE=~/Documents/Galaxy-Book4-Edge-linux/firmware-stage/lib/firmware
mkdir -p $STAGE/{ath12k/WCN7850/hw2.0,qca,qcom/x1e80100/samsung/galaxy-book4-edge}

# Qualcomm remoteproc
cp firmware-raw/extracted/**/qcadsp8380.mbn  $STAGE/qcom/x1e80100/samsung/galaxy-book4-edge/adsp.mbn
cp firmware-raw/extracted/**/qccdsp8380.mbn  $STAGE/qcom/x1e80100/samsung/galaxy-book4-edge/cdsp.mbn
cp firmware-raw/extracted/**/wpss.mbn        $STAGE/qcom/x1e80100/samsung/galaxy-book4-edge/wpss.mbn

# Wi-Fi
cp firmware-raw/extracted/**/wlanfw20.mbn    $STAGE/ath12k/WCN7850/hw2.0/amss.bin
cp firmware-raw/extracted/**/regdb.bin       $STAGE/ath12k/WCN7850/hw2.0/regdb.bin

# Bluetooth (native names)
cp firmware-raw/extracted/**/hmtbtfw20.tlv   $STAGE/qca/hmtbtfw20.tlv
cp firmware-raw/extracted/**/hmtnv20.bin     $STAGE/qca/hmtnv20.bin

du -sh $STAGE    # 38M
```

## 4.5 The missing `board-2.bin`

ath12k needs a per-board calibration file (`board-2.bin`) that matches the PCI subsystem IDs of the Wi-Fi chip. Windows doesn't ship this in the Linux format.

First attempt: Debian's `firmware-atheros 20251111-1` package (mentioned in zensanp issue #3 as working for the 16"). The download URL 404'd — package name had changed. Next option:

```bash path=null start=null
# Pull directly from upstream linux-firmware.git
cd $STAGE/ath12k/WCN7850/hw2.0/
wget https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git/plain/ath12k/WCN7850/hw2.0/board-2.bin
```

SHA1: `22fcfb9f…` — recorded. This file would later become the object of a hunt when it turned out **the 14" variant of the Book4 Edge needs a different board-2.bin** than the 16".

## 4.6 The 14" board-2.bin saga (foreshadowing)

zensanp issue #3 was a long discussion where the 14" user (zensanp himself) could never get Wi-Fi working even with the upstream `board-2.bin`, while a 16" user (`@edwardak`) succeeded with the Debian `firmware-atheros 20251111-1` build (sha1 `b75e8a31…`).

The eventual solution was to use a **custom patched board-2.bin** that `zensanp` built with the Samsung 14" PCI IDs inserted. That file lived in the final BOOTKIT under the name `board-2.bin.zensanp-14inch`. This wasn't found in this chapter — it took weeks of searching the issue tracker to locate.

## 4.7 The PD-mapper `.jsn` discovery (later)

Much later (Chapter 10), when USB-C altmode was failing, the agent discovered that the Samsung Windows backup contained five **`.jsn` domain-mapping files** that weren't in either Windows DriverStore's firmware subdirectory or in linux-firmware:

- `adspr.jsn`  — ADSP root domain
- `adsps.jsn`  — ADSP secondary
- `adspua.jsn` — ADSP user agent
- `battmgr.jsn` — Battery Manager (later replaced with a Lenovo 21N1 one)
- `cdspr.jsn`  — CDSP root domain

These had to be placed in `/lib/firmware/qcom/x1e80100/SAMSUNG/galaxy-book4-edge/` (note the uppercase SAMSUNG — case-sensitive path). Without them, `pd-mapper` couldn't route the PD Mapper service's QMI messages and USB-C altmode stalled.

These weren't in the initial extraction because we didn't know we needed them. They were recovered later by searching the Lenovo Slim 7x bootkit that the community had published.

## 4.8 Final firmware stage size

After all passes:

| Path | Size | Source |
|---|---|---|
| `ath12k/WCN7850/hw2.0/amss.bin` | 6 MB | Windows `wlanfw20.mbn` |
| `ath12k/WCN7850/hw2.0/regdb.bin` | 24 KB | Windows |
| `ath12k/WCN7850/hw2.0/board-2.bin` | 2.2 MB | upstream linux-firmware.git |
| `qca/hmtbtfw20.tlv` | 281 KB | Windows |
| `qca/hmtnv20.bin` | 9.6 KB | Windows |
| `qcom/x1e80100/samsung/galaxy-book4-edge/adsp.mbn` | 19.9 MB | Windows |
| `qcom/x1e80100/samsung/galaxy-book4-edge/cdsp.mbn` | 3.1 MB | Windows |
| `qcom/x1e80100/samsung/galaxy-book4-edge/wpss.mbn` | 7.3 MB | Windows |
| **TOTAL** | **38 MB** | |

## 4.9 Why this matters for distribution

Any "install script" or "ISO" produced from this project has to decide how to handle proprietary firmware:

1. **Bundle** the blobs directly — fastest user experience but legally gray (Samsung/Qualcomm never licensed these for redistribution)
2. **Provide an extraction helper** that mounts the user's Windows partition or their own downloaded driver package and pulls the blobs — slower but legally clean

For the personal BOOTKIT the user bundled directly. For a hypothetical public release, the agent's final recommendation was a two-tier approach: a "starter ISO" that omits the blobs and a "manual install script" that extracts from the user's existing Windows installation.

## 4.10 Commands reference

```bash path=null start=null
# One-shot Windows backup, from Chapter 2
# Produces ~/Documents/Galaxy-Book4-Edge-linux/windows-backup/extracted/ (342 MB)

# Path-translated firmware stage (this chapter)
mkdir -p ~/Documents/Galaxy-Book4-Edge-linux/firmware-stage/lib/firmware/{ath12k/WCN7850/hw2.0,qca,qcom/x1e80100/samsung/galaxy-book4-edge}

# Populate staging
cp ~/Documents/Galaxy-Book4-Edge-linux/windows-backup/extracted/DriverStore/**/qcadsp8380.mbn \
   ~/Documents/Galaxy-Book4-Edge-linux/firmware-stage/lib/firmware/qcom/x1e80100/samsung/galaxy-book4-edge/adsp.mbn
# ... repeat per the table

# Verify staging
du -sh ~/Documents/Galaxy-Book4-Edge-linux/firmware-stage/    # 38M
find ~/Documents/Galaxy-Book4-Edge-linux/firmware-stage/ -type f | wc -l    # 8
```

## 4.11 End of Chapter 4

By the end of this chapter:

- 38 MB of Linux-pathed firmware was ready to merge into the rootfs
- The 201 MB Windows backup was permanent at `~/Documents/Galaxy-Book4-Edge-linux/windows-backup/`
- The BT MAC (`XX:XX:XX:XX:XX:XX`) was recorded for later use in `btmgmt`
- The Wi-Fi MAC (`XX:XX:XX:XX:XX:XX`) was also recorded
- Windows product key was preserved in `DEVICE-INFO.md`

Critical observation: the agent's decision to do a **comprehensive Windows backup before any kernel boot** was repeatedly validated over the next weeks. Several files (`.jsn`, BT NVM, regulatory DB) wouldn't have been easily re-acquirable after Windows was wiped.

Next chapter: building the actual Arch Linux ARM rootfs, integrating the kernel modules + firmware, and producing the first USB.
