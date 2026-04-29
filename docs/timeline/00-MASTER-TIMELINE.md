# Samsung Galaxy Book4 Edge — The Complete Linux Journey
## Master Timeline (April 2026)

This document chronicles a multi-day reverse-engineering, firmware-spelunking and kernel-hacking marathon that took a brand-new **Samsung Galaxy Book4 Edge 14" (Snapdragon X1E-80-100)** from a locked-down Windows 11 ARM device to a **fully-booted Ubuntu 26.04 (resolute) installation** with a native kernel battery driver, PXE boot infrastructure, and a distributable BOOTKIT.

The journey spans **five major phases** and thirteen chapters. Each chapter is in its own file in this folder so the whole story can be printed or bound as a ~70-page PDF.

---

## Top-level timeline

| Phase | Duration | Chapters | Outcome |
|---|---|---|---|
| **I. Planning & Recon** | Day 1 morning | 01, 02 | Target identified: X1E80100 Book4 Edge 14", firmware inventoried via SSH to Windows |
| **II. Arch Linux ARM attempt** | Day 1 afternoon | 03, 04, 05 | Custom 6.17-rc4 kernel cross-compiled, firmware extracted, ALARM rootfs built, ISO assembled |
| **III. USB boot wall** | Day 1 night → Day 2 | 06, 07 | a800000 USB controller confirmed dead in Linux; pivoted to jglathe's Ubuntu X1E image |
| **IV. PXE + minimal initramfs** | Day 2 → Day 3 | 08, 09 | Network boot established; 7 MB busybox initramfs finally brought up a real shell on screen |
| **V. Functional Ubuntu + Research** | Day 3 → Day 5 | 10, 11, 12, 13 | Ubuntu installed to internal UFS; EC reverse-engineered; battery driver written; distributable BOOTKIT produced |

---

## The cast of hardware

- **Laptop**: Samsung Galaxy Book4 Edge 14" (model NP960XMA-KB1IT, SKU `GALAXY A5A5-PAKX`)
- **SoC**: Qualcomm Snapdragon **X1E80100** (12-core ARM64)
- **Storage**: 512 GB KIOXIA UFS 4.0 (`T2T85`)
- **Panel**: Samsung Display `ATNA60CL07-02` AMOLED 2880×1800
- **Wi-Fi/BT**: Qualcomm WCN7850 (Wi-Fi 7, ath12k)
- **EC**: **ENE KB9058** (at I²C `b94000`, slave `0x62` and `0x64`)
- **BIOS**: `P00AKX.061` → later `P08AKX.061.250718.MY.1010`
- **MAC (Bluetooth)**: `XX:XX:XX:XX:XX:XX`
- **MAC (Wi-Fi)**: `XX:XX:XX:XX:XX:XX`
- **Battery**: 4000 mAh / 15.52 V design (~62 Wh)

## The cast of software

- **Kernel (attempt 1)**: `zensanp/linux-book4-edge` @ `x1e80100-book4e-6.17-rc4`, pinned SHA `708b2aeff3e9…`
- **Base distro (final)**: Ubuntu 26.04 "resolute" with Canonical's `7.0.0-22-qcom-x1e` kernel
- **jglathe image**: `Ubuntu_Desktop_24.04_x1e_6.11rc_v7.img` (intermediate stepping stone)
- **Build host**: Arch Linux (user `user`) at `10.x.x.x`, then Ubuntu (user `user`)
- **PXE infrastructure**: dnsmasq + TFTP + NFS on interface `enp4s0` at `192.168.x.x/24`

## What was built, and what works at the end

| Capability | Status | Where it lives |
|---|---|---|
| Boot from internal UFS | ✅ | GRUB `BOOTAA64.EFI` registered via efibootmgr |
| Native I2C-HID keyboard (ELAN 0CF2:9050) | ⚠️ events at kernel level; desktop layer tuning WIP | DTB + udev override |
| Touchpad | ⚠️ DT node patched to address `0xd1` | `x1e80100-samsung-galaxy-book4-edge-14.dtb` |
| UFS storage | ✅ | mainline `scsi-ufs-qcom` |
| USB-C dock (RTL8153 Ethernet) | ✅ with altmode rebind workaround | `pd-mapper.service` + Samsung `.jsn` files |
| Wi-Fi (WCN7850 via ath12k) | ✅ with zensanp `board-2.bin` | `/lib/firmware/ath12k/WCN7850/hw2.0/` |
| Bluetooth | ⚠️ firmware loads, MAC set via `btmgmt` | `book4edge-bt-mac.service` |
| ADSP / CDSP remoteprocs | ✅ | extracted from Windows DriverStore |
| Audio codec (WCD938x) | ✅ bound via SoundWire | stock |
| Built-in speakers (WSA884x) | ❌ UNATTACHED | known DT pinctrl gap |
| Display (eDP panel) | ✅ | mainline DRM/MSM + link-rate fallback patch (Jesse Ahn/@moolwalk) |
| Adreno GPU | ❌ intentionally off (needs Mesa ≥ 25.3.3) | DTS `status = "disabled"` |
| Battery percentage | ✅ native `/sys/class/power_supply/` integration | custom kernel module (Chapter 12) |
| Fan control | ⚠️ basic zone + RPM works; ramp-speed research ongoing | userspace daemon + EC research |
| Performance modes (SABI) | ❌ dead-end (opcodes 0x12/0x13/0xEE all echo) | Chapter 11 |
| Camera | ❌ no upstream support yet | N/A |
| Fingerprint | ❌ no upstream support | N/A |
| Hibernate | ❌ untested | N/A |

## Artefacts produced

| Path | Contents |
|---|---|
| `BOOTKIT-20260421-223459-USBC-OK.tar.gz` | 19 MB portable recovery bundle, triple-copied to `/boot/` and `/boot/efi/book4-golden/` |
| `tools/` | 22 MB of cached upstream tools (`x1e-ec-tool`, `it8987-qcom-tool`, Samsung Galaxy Book Extras DSDT dumps, mainline drivers) |
| `driver/` | C kernel module (417 lines) + Makefile + DKMS config + systemd unit + udev rules + README |
| `Galaxy-Book4-Edge-linux/` | Workspace for full remastered Ubuntu 26.04 `resolute-desktop-arm64+x1e` installer ISO |
| `book4edge-iso/` | Original Arch Linux ARM build tree (kernel `Image`, initramfs, firmware stage, Windows backup) |

---

## How to read the rest of this folder

Each chapter file is self-contained and can be read in isolation, but for the full narrative arc follow them in order:

- **01-ORIGIN-AND-PLANNING.md** — The zensanp request and initial Arch ARM ISO plan
- **02-DEVICE-RECONNAISSANCE.md** — SSH into Windows 11 ARM, PowerShell inventory, the 14"-vs-16" question
- **03-KERNEL-CROSS-COMPILE.md** — Building the 6.17-rc4 kernel for aarch64 on an x86_64 host
- **04-FIRMWARE-EXTRACTION.md** — 128 Qualcomm blobs, 201 MB Windows backup, ACPI tables, MAC addresses
- **05-ROOTFS-FIRST-USB.md** — Arch Linux ARM rootfs, pacman landlock workarounds, first ISO burn
- **06-USB-BOOT-FAILURES.md** — a800000.usb controller dies, dock won't enumerate, dongles rejected
- **07-PIVOT-TO-UBUNTU.md** — jglathe's pre-built image, UFS timeout warnings, SSH baked into the rootfs
- **08-PXE-BOOT-SETUP.md** — One-cable dream: dnsmasq, proxy-DHCP, direct-wire subnet, router races
- **09-MINIMAL-INITRAMFS-BREAKTHROUGH.md** — Custom 7 MB busybox init that finally rendered a shell
- **10-UBUNTU-INSTALLED-AND-USB-C.md** — Full install, BOOTKIT snapshot, USB-C altmode rebind recovery
- **11-FAN-CONTROL-RESEARCH.md** — Upstream survey, SABI v4 protocol decoding, opcode 0xEE false lead
- **12-EC-REVERSE-ENGINEERING.md** — radare2 on EC2.sys, ENE KB9058 confirmation, Mbox wire protocol
- **13-BATTERY-DRIVER.md** — Writing a proper `power_supply_class` kernel module, DKMS, upower integration
- **14-LESSONS-AND-OPEN-QUESTIONS.md** — What worked, what didn't, and where the next person can pick up

---

## One-paragraph summary for anyone Googling this later

The Samsung Galaxy Book4 Edge (X1E80100, SAM0430/SAM060B ACPI IDs, ENE KB9058 EC) can be made to boot Ubuntu 26.04 arm64 from internal UFS by (1) taking the mainline `x1e80100-samsung-galaxy-book4-edge-14.dtb` and patching the touchpad I²C address to `0xd1` plus the WCN7850 VREG regulator-always-on fix, (2) extracting Samsung's ADSP/CDSP/WLAN firmware from `C:\Windows\System32\DriverStore\FileRepository` (no other source ships it), (3) enabling `qcom_pd_mapper` auto-load plus Samsung's five `.jsn` domain files so USB-C altmode actually negotiates, (4) flashing the custom-kernel image to a good USB (NOT a flaky Kingston SV300 from 2014), (5) installing with `grub-install --removable` because `efibootmgr` can't write EFI variables when the DTB is GRUB-overridden, (6) after install, registering the EFI entry from the live session which CAN write efivars, and (7) maintaining a "golden" snapshot of firmware + configs so you can return to the known-working state after any kernel experiment. Battery reporting requires a custom I²C `power_supply_class` driver that talks to the EC Mbox protocol at `0x62` and decodes registers at offsets `0x80/0x84/0xA0/0xA4/0xB0/0xB4`. Fan control works via EC opcode `0x08` (zone) and `0x17` (RPM target) but Samsung's performance-mode SABI path at opcode `0xEE` is NOT on I²C — it goes through a different code path in `EC2.sys` and is not yet reverse-engineered.

Co-Authored-By: Oz <oz-agent@warp.dev>
