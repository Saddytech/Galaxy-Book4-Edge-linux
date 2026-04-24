# Chapter 2 — Device Reconnaissance

## 2.1 Building a recon payload

SSH from an Arch bash shell into Windows 11 OpenSSH lands in either `cmd.exe` or PowerShell, depending on the default shell. Running a multi-line PowerShell script across that boundary quickly turns into a quoting nightmare — nested single quotes, backticks for escape, and bash's own expansion all fight each other.

The agent's solution: write the PowerShell script locally, **base64-encode it UTF-16LE** (as PowerShell's `-EncodedCommand` expects), and invoke it with a single flag.

```bash path=null start=null
# Compose the PowerShell recon script
cat > recon.ps1 <<'PSEOF'
Get-CimInstance Win32_ComputerSystem | Format-List
Get-CimInstance Win32_Processor | Format-List
Get-Disk | Format-List Number, FriendlyName, SerialNumber, Size
Get-Partition | Format-List DiskNumber, PartitionNumber, Type, Size, DriveLetter
bcdedit /enum firmware
Get-ChildItem -Path "C:\Windows\System32\DriverStore\FileRepository" -Recurse -Include *.mbn,*.bin,*.tlv -ErrorAction SilentlyContinue | Measure-Object
PSEOF

# UTF-16LE encode + base64
ENCCMD=$(iconv -f UTF-8 -t UTF-16LE recon.ps1 | base64 -w0)

# Send through ssh (interactive for password prompt)
ssh user@10.x.x.x "powershell -NoProfile -EncodedCommand $ENCCMD"
```

## 2.2 First-pass results

This one SSH session returned a gold mine:

| Fact | Value |
|---|---|
| Product | **Samsung Galaxy Book4 Edge** |
| Version | **2.1** |
| SKU | **`GALAXY A5A5-PAKX`** |
| CPU | Snapdragon **X1E80100** (12 cores) |
| OS | Windows 11 ARM Insider build 29558 |
| BIOS | `P00AKX.061.250718.WY.1810` (July 2025) |
| Storage | 512 GB **KIOXIA UFS** (model `T2T85`) |
| Partition layout | GPT, 5 partitions (ESP 260 MB, MSR, C: 457 GB NTFS, Windows RE 850 MB, Samsung Recovery 18.6 GB) |
| Free on C: | Initially reported as 51 GB (wrong), later corrected to **142 GB** |
| Unallocated space | **0** — no room for a dual-boot partition without shrinking |

**Firmware inventory** (first pass, under DriverStore):
- 102 `.bin` files
- 29 `.mbn` files (incl. `qcadsp8380.mbn` at **19 MB**, the full ADSP blob)
- `qcwlanhmt8380*` — ath12k/WCN7850 driver stack

The agent confirmed the DTB target as `x1e80100-samsung-galaxy-book4-edge.dtb`.

## 2.3 The 14"-vs-16" question (part 1)

The zensanp README distinguishes between Galaxy Book4 Edge 14" and 16" models. They share the SoC but have different touchpad addresses, different panels, and the 14" needs a different Wi-Fi board file. The SKU `GALAXY A5A5-PAKX` alone didn't resolve this.

The agent attempted another SSH recon to query screen resolution and product details, but the connection kept dropping before the user could enter the password (Windows OpenSSH `LoginGraceTime` timing out while waiting for human input in the agent's interactive tool).

After several attempts, the agent just asked the user directly:

> **"Is your device 14-inch or 16-inch?"**

> **"14 inch"** (eventually — took some back-and-forth)

This resolved the ambiguity and led to a re-verification of the DTB choice. The agent later discovered a deeper issue: zensanp's fork had an `x1e80100-book4e-14-temp-6.17-rc4` branch specifically for the 14" variant with a touchpad fix. The agent switched to that branch for a later rebuild.

## 2.4 Sanity-audit pass

The user issued a critical challenge:

> **"inspect anything, make double check of what the repo said we should do and verify we are doing all good and not missing anything"**

This triggered a thorough README re-read that surfaced more requirements than the initial plan had captured:

| README item | Status in plan |
|---|---|
| Keyboard udev quirk (hid-over-i2c 0CF2:9050) | ❌ missing — added to rootfs |
| Touchpad (16" only on main branch) | ❌ wrong branch for 14" — switched later |
| Built-in display patch (Launchpad comment #99) | ✅ already in zensanp |
| GPU Adreno | ✅ disabled in DTS per README |
| Wi-Fi regulator-always-on patch | ✅ already in zensanp |
| Bluetooth MAC manual setup | ❌ missing — added helper service |
| ADSP/CDSP firmware | ✅ extracted |
| Audio | ❌ upstream not ready |
| Touchscreen | ❌ no upstream support |
| Sleep | ⚠️ works but doesn't save power |

The audit resulted in three new rootfs deliverables:

1. `/etc/udev/rules.d/99-book4edge-keyboard.rules` — forces keyboard classification
2. `/usr/local/bin/book4edge-extract-firmware` — already planned; reinforced
3. `/usr/local/bin/book4edge-bt-mac` + `book4edge-bt-mac.service` — runs `btmgmt -i hci0 public-addr <MAC>` at first boot

## 2.5 Deep recon: the 201 MB Windows backup

At one point the user pushed back hard on firmware extraction:

> **"since we are going to install linux on this machine and then we could not have access to windows anymore, do you take everything we need from this fucking laptop??? include all firmware we need or other file!?!?!"**

Fair point. The agent escalated from "just the blobs" to a comprehensive one-shot Windows backup. A larger PowerShell script (uploaded via `scp` because the base64 `-EncodedCommand` path hit Windows' command-line length limit) captured:

- **Full DriverStore**: 131 directories of firmware and driver metadata
- **Registry hives**: BTHPORT, CurrentControlSet, HARDWARE\DEVICEMAP, Qualcomm-specific keys
- **ACPI tables**: DSDT, FADT, RSDT, FACS (exposed via registry)
- **EDID**: full panel information
- **Wi-Fi profiles**: 8 SSIDs with saved credentials
- **Windows product key**: `XXXXX-XXXXX-XXXXX-XXXXX-XXXXX`
- **Bluetooth MAC** (after additional hunting — see 2.6)
- **Samsung configs**: from `C:\Program Files\Samsung` and `C:\ProgramData\Samsung`
- **Systeminfo output**: for hypervisor state and BIOS version

**Size**: 201 MB compressed, 202 MB zip archive, **342 MB** extracted. Stored at `~/Documents/Galaxy-Book4-Edge-linux/windows-backup/`.

## 2.6 The Bluetooth MAC hunt

On Qualcomm Windows drivers the local BT adapter's MAC is read at runtime from the BT chip's NVM — it's **not** stored in any normally-accessible Windows registry key. The first recon pass returned only paired-peripheral MACs (Sony WF-1000XM5, TOZO, Bose) — useless.

Multiple techniques were tried:
- `Get-PnpDevice` by device ID — returned generic Qualcomm entries
- Registry `BTHPORT\Parameters\Local\00000000` — only UUIDs
- PowerShell ADSI/WMI queries — blocked by Qualcomm driver

**What finally worked**: `ipconfig /all` + `Get-NetAdapter` together listed the adapter with its runtime MAC:

```
Physical Address: XX-XX-XX-XX-XX-XX
```

That's the BT MAC we'd later feed into `btmgmt -i hci0 public-addr`. The Wi-Fi MAC was sequentially one higher at `XX:XX:XX:XX:XX:XX`, the typical pattern for combo adapters.

## 2.7 Consolidated device inventory

The agent wrote everything learned into a permanent reference file:

**`~/Documents/Galaxy-Book4-Edge-linux/DEVICE-INFO.md`** (3 KB):
- Product string, SKU, version
- SoC, BIOS, OS
- Storage layout
- Panel (Samsung Display `ATNA60CL07-02`, 2880×1800 AMOLED)
- MAC addresses (both BT and Wi-Fi)
- Windows product key
- ACPI tables extracted

**`~/Documents/Galaxy-Book4-Edge-linux/rootfs-overlay/etc/book4edge/device.conf`** (~300 B):
- Machine-readable values for first-boot helpers

## 2.8 Explicit warnings to the user

Before any destructive operation, the agent flagged:

> ⚠️ **Installing Linux will wipe the `SAMSUNG_REC2` factory-recovery partition (18.6 GB).** That's your only way to restore the Book4 Edge to factory Windows state without reimaging from Samsung.

The user's response: they'd accept that risk **but only later**. First priority remained a **live USB** that didn't touch the internal disk.

## 2.9 Updated plan after recon

The plan was re-edited with the real device facts:

1. DTB confirmed: `x1e80100-samsung-galaxy-book4-edge.dtb`
2. Kernel config must include NTFS3 (so the first-boot helper can mount C:)
3. Kernel config must include ATH12K (so Wi-Fi can probe)
4. Kernel must build UFS drivers as `=y` (built-in), not as modules — otherwise root disk unreachable at boot
5. Kernel must build MSM DRM as module (not `=y`) — otherwise black screen after GRUB
6. Live USB first; install later (separate milestone)

## 2.10 Commands run in this chapter

```bash path=null start=null
# First recon pass
scp recon.ps1 user@10.x.x.x:C:/Users/user/Desktop/
ssh user@10.x.x.x "powershell -File C:/Users/user/Desktop/recon.ps1 > \
    C:/Users/user/Desktop/recon-out.txt"
scp user@10.x.x.x:C:/Users/user/Desktop/recon-out.txt ~/Documents/Galaxy-Book4-Edge-linux/logs/

# Comprehensive backup
scp comprehensive-backup.ps1 user@10.x.x.x:C:/Users/user/Desktop/
ssh user@10.x.x.x "powershell -File C:/Users/user/Desktop/comprehensive-backup.ps1"
scp user@10.x.x.x:C:/Users/user/Desktop/book4edge-complete.zip \
    ~/Documents/Galaxy-Book4-Edge-linux/windows-backup/

# Decompress + verify
cd ~/Documents/Galaxy-Book4-Edge-linux/windows-backup/
unzip book4edge-complete.zip -d extracted/
find extracted/ -name '*.mbn' | wc -l    # 29
du -sh extracted/                         # 342M
```

## 2.11 The SSH-key detour

Interactive password SSH sessions kept timing out in the agent's environment. The fix:

```bash path=null start=null
# Generate an ed25519 keypair on the Arch host
ssh-keygen -t ed25519 -f ~/.ssh/book4edge -N "" -C "book4edge-recon"

# Install pubkey on the Windows target (one-time password prompt)
PUBKEY=$(cat ~/.ssh/book4edge.pub)
ssh user@10.x.x.x "powershell -Command \"\
    \$p='C:/Users/user/.ssh/authorized_keys'; \
    New-Item -Path (Split-Path \$p) -ItemType Directory -Force; \
    Add-Content -Path \$p -Value '${PUBKEY}'\""

# Test
ssh -i ~/.ssh/book4edge user@10.x.x.x "whoami"
```

After that, all subsequent recon + extraction ran password-free. Critical for batching firmware extraction and for later when we needed repeated fast turn-around.

## 2.12 End of Chapter 2

By the end of reconnaissance we had:

- A **locked-in DTB target** matching the physical device
- A **201 MB backup** of everything that existed on the Windows side and might not be recoverable after wipe
- **Two MAC addresses** we'd need for BT and Wi-Fi post-install
- **Windows product key** preserved (for possible future recovery via Samsung's tools)
- **Key-based SSH** eliminating the password-prompt time-outs

The next chapter covers the actual kernel build — where the recon data finally got turned into a bootable `Image` + DTB.
