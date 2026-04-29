# Chapter 14 — Lessons Learned and Open Questions

## 14.1 What worked (end state)

### Fully functional

- **Boot from internal UFS** — Ubuntu 26.04 (Canonical kernel `7.0.0-22-qcom-x1e`)
- **Wi-Fi 7** via ath12k + zensanp's 14" board-2.bin
- **USB-C dock** (RTL8153 Ethernet) — with the `pmic_glink.altmode.0` / `ucsi.0` rebind recovery pattern
- **ADSP + CDSP remoteprocs** — Samsung firmware from DriverStore
- **Audio codec** (WCD938x) bound via SoundWire
- **Display** — internal OLED panel works with DP link-rate fallback (already in zensanp)
- **Battery reporting** — custom `samsung_galaxybook_battery` kernel module + DKMS + udev + systemd
- **SSH over dock Ethernet** — survives reboot
- **Remote kernel iteration via PXE** — `enp4s0` direct wire, dnsmasq + TFTP + NFS
- **Bootable BOOTKIT** — triple-copied, SHA-verified, idempotent `INSTALL.sh`

### Partially working

- **Native I²C-HID keyboard (ELAN 0CF2:9050)** — IRQs reach the kernel (evtest grabs real keys), but events don't route to GNOME Wayland session. Works when `evtest` is grabbed directly.
- **Touchpad** — DT node patched to I²C address `0xd1`; needs further DTS polish
- **Bluetooth** — firmware loads; MAC must be manually set via `btmgmt -i hci0 public-addr XX:XX:XX:XX:XX:XX` on first boot
- **Fan control** — basic `0x08` zone + `0x17` RPM target works; ramp-speed remains slow; performance modes (SABI) unresolved

### Non-working / not yet attempted

- **Built-in speakers** (WSA884x SoundWire) — UNATTACHED status; needs DTS pinctrl patches
- **Adreno GPU** — intentionally disabled in DTS; needs Mesa ≥ 25.3.3 and Linux-format firmware
- **Camera** — no upstream CAMSS support for this SKU yet
- **Fingerprint reader** — no upstream driver
- **Hibernate** — untested
- **Sleep** — "works" in that the kernel goes through the motions but power draw is not reduced meaningfully

## 14.2 Ten durable technical lessons

### 1. Always grep before patching

zensanp's kernel fork actively integrates community fixes. Several patches the project tried to apply manually were **already in the branch**. The habit of `grep`-ing for the thing-being-patched before writing the patch saved hours.

### 2. Preserve the Windows side first

The 201 MB Windows backup captured in Chapter 2 proved essential again and again. Specifically, the `.jsn` pd-mapper files, the Bluetooth NVM, the Windows product key, and the ACPI tables would have been **unrecoverable after install**.

### 3. "Pull the USB to read logs" is a productivity trap

The single biggest bottleneck in the project was the physical USB dance. Once we pivoted to PXE + the minimal initramfs with screen capture via phone photos, iteration time dropped from 10 minutes per cycle to under a minute.

### 4. `bind-dynamic`, not `bind-interfaces`, in dnsmasq

If dnsmasq needs to reply to broadcast DHCPDISCOVER packets (and it does, for PXE), it must keep the **raw socket** path. `bind-interfaces` breaks that; `bind-dynamic` is the modern equivalent that preserves it.

### 5. NFS-root is fragile; ramfs is king

For early boot debugging, a tiny custom initramfs with busybox and no network-filesystem dependency is dramatically more reliable than NFS-root. Start there, add complexity only once a known-good shell is proven.

### 6. zstd-compressed modules need decompression for busybox modprobe

Ubuntu ships `.ko.zst` kernel modules. busybox's built-in `modprobe` doesn't decompress on the fly. If you're shipping modules inside a busybox initramfs, decompress them first (see Chapter 9, §9.10).

### 7. `I2C_RDWR` ioctl for atomic mailbox protocols

The ENE KB9058 mailbox protocol requires a write + repeated-start + read **in one transaction**. Separate `write()` and `read()` syscalls insert a STOP condition between them, which makes the EC forget its mailbox state. Always use `I2C_RDWR` for multi-phase EC protocols.

### 8. `grub-install --removable` sidesteps efibootmgr chroot bugs

Subiquity's chroot can't write EFI variables. Use `grub-install --removable` to place `BOOTAA64.EFI` at the fallback path, then register the EFI entry **from the live session** (which can write efivars). Post-install, the registered entry handles everything.

### 9. Kernel 7.x renamed `of_node` → `fwnode`

For any out-of-tree driver being built against kernel 7.x series, the `power_supply_config.of_node` field is now `.fwnode`. Recognise that compile error instantly.

### 10. `i2c_device_id.name[20]` is 20 chars, no more

Short name. If your driver identifier is longer, you get a build error that looks unrelated. Don't waste time — just shorten.

## 14.3 Root cause summary: why the Book4 Edge 14" is hard

The laptop sits at the intersection of three separate "brand new Linux territory" problems:

1. **Snapdragon X1E80100** — bleeding-edge ARM64 SoC; mainline kernel support is arriving in waves; SCMI / SMMU / pmic_glink all need Samsung-specific tuning
2. **Samsung's closed firmware interface** — SABI v4 on ARM is implemented purely in Windows-side driver code, not ACPI; there's no open specification
3. **ENE KB9058** — a keyboard-controller-class EC with no public datasheet; we had to reverse-engineer the Mbox wire protocol from `EC2.sys`

Each of these alone is a multi-week effort. Combined, they make this laptop one of the hardest "just boot Linux on my new laptop" targets in 2026.

The flip side: because Samsung inherits much of its platform from Qualcomm's X1E reference design, **most progress here is directly reusable on other X1E laptops** (ASUS Vivobook, HP OmniBook, Lenovo Yoga Slim 7x, Microsoft Surface). The BOOTKIT structure and rebuild scripts generalise with small tweaks.

## 14.4 Open research questions for future work

### Fan control: where does SABI SET perf-mode actually go on the wire?

Three candidates remain untested:

- **I²C1 slave `0x64`** (the second EC bus declared in DSDT but untouched)
- **A different mailbox-style command through opcode `0x11 EXECUTE_MBOX` on `0x62`** with a different framing than what we tried
- **A GPIO-mediated sideband** (some Samsung laptops use an extra GPIO toggle to wake the EC into SABI mode first)

Needed: a logic analyser on the I²C SCL/SDA lines, or someone willing to run a full dynamic trace of `EC2.sys` inside a Windows kernel debugger.

### Fan RPM read

No Mbox register or opcode returns live fan RPM. Windows' Samsung Settings app does display it, so there must be a path — possibly through an ACPI method we haven't RE'd.

### Native keyboard → GNOME Wayland routing

`evtest --grab` on `/dev/input/event1` captures real key events. `libinput list-devices` sees the device. But GNOME Shell under Wayland doesn't route the events to focused applications. Suspect: a subtle Wayland compositor focus bug that only affects the 14" Book4 because of some quirk in the udev-assigned name/properties.

### WSA884x speakers

Declared in the DTB but fail to attach. Needs pinctrl + clock updates specific to the 14" chassis. Community hasn't figured this out yet.

### Adreno GPU firmware translation

`qcdxkmbase8380_*.bin` (Windows GPU firmware) probably contains the same microcode as the Linux driver's expected `a750_sqe.fw` / `a750_gmu.bin` — just in a different container. An offline converter would unblock the GPU on Mesa ≥ 25.3.3.

### Upstream the Book4 Edge DTB to mainline

Mainline Linux currently does not have a Book4 Edge 14" DTS. zensanp's fork does, but upstreaming it would make future kernel updates not require a fork.

## 14.5 Numbers

| Metric | Value |
|---|---|
| **Days of active work** | ~5 |
| **Lines in the raw event log** | 5 063 |
| **Distinct commands executed** | ~800 |
| **USB pulls** | ~25 (including the bad Kingston cycles) |
| **Reboots** | ~60 |
| **Kernel builds** | 2 (full) + several module-only |
| **Ubuntu live ISOs flashed** | 4 (1 Arch, 3 Ubuntu-based) |
| **Firmware files extracted from Windows** | 128 |
| **Size of Windows backup** | 201 MB zip / 342 MB extracted |
| **Size of final firmware stage** | 38 MB |
| **Lines of code written** | ~640 (battery driver + helpers) |
| **Lines of research notes cached** | 941 + 22 MB of git clones |
| **PR-able upstream patches produced** | 0 (all work in private BOOTKIT; upstream is a future milestone) |

## 14.6 Gratitude

Three people / groups deserve explicit mention:

1. **`zensanp`** — for maintaining the active kernel fork, writing honest caveats in issues #3 and #6, and shipping both the main branch and the `14-temp` variant
2. **`jglathe`** — for publishing pre-built X1E80100 Ubuntu images that proved the concept was possible and served as a stepping stone away from the squashfs-live-boot dead end
3. **`icecream95` and `Maccraft123`** — for reverse-engineering the ITE IT8987 EC on ASUS/Lenovo X1E laptops; their tools provided the ASUS-kick-start pattern that informed our own fan daemon work
4. **`Jesse Ahn (@moolwalk)`** — for his link-rate fallback patch which fixed the display on X1E Samsung panels and prevented them from going dark after initramfs.

## 14.7 The agent's own self-assessment

Failure modes that hurt the most:

- **Speculating without evidence** in the USB-controller debug (Chapter 6) — burned through 2+ hours of user patience before reading logs
- **Running a recursive `grep /sys/kernel/debug/`** that caused a kernel panic and USB-C regression (Chapter 10, §10.10)
- **Assuming `0x12`/`0x13` were the SABI opcodes** based on a string label rather than verifying the dispatch table first (Chapter 11)
- **Rebuilding the remaster ISO workflow before fully understanding Ubuntu's new modular squashfs layout** (Chapter 10, §10.14) — wasted effort later revisited

Successes worth keeping:

- **Insisting on the Windows backup** before any disk operation
- **Pivoting to the minimal initramfs** instead of fighting NFS-root
- **Using r2 string-xref tracing** to name stripped ARM64 PE functions — surprisingly effective for understanding `EC2.sys`
- **Triple-copying the BOOTKIT** with SHA verification, including one copy on the ESP itself

## 14.8 The user's journey

In the user's own words:

> **"we make something incredible!"**

One sentence that captures six days of back-and-forth across USB crashes, kernel panics, frustrated reboots, and finally a Galaxy Book4 Edge 14" running Linux with a native battery icon in the GNOME tray.

The fact that this is — to the best of our research — **the first publicly documented end-to-end Linux install on a Samsung Galaxy Book4 Edge 14" with working USB-C + Wi-Fi + battery** makes the effort worthwhile even before any of it is upstreamed.

## 14.9 Quick-reference recovery runbook

If the working state breaks in future:

```bash path=null start=null
# 1. Re-apply BOOTKIT
tar xzf /boot/efi/book4-golden/BOOTKIT-20260421-223459-USBC-OK.tar.gz -C /tmp/
cd /tmp/BOOTKIT-20260421-223459-USBC-OK
sudo ./INSTALL.sh

# 2. USB-C recovery
sudo /usr/local/sbin/book4-usbc-recover.sh

# 3. Set Bluetooth MAC
sudo btmgmt -i hci0 public-addr XX:XX:XX:XX:XX:XX

# 4. Battery driver re-install
cd driver
sudo make clean && sudo make && sudo make install
sudo systemctl restart samsung-galaxybook-battery.service

# 5. Verify
acpi -b
lsusb -t
ip -br link
sudo dmesg | grep -E 'firmware.*failed|ERROR' | head -20
```

## 14.10 End of the Timeline

This is where the documented journey ends — Ubuntu booting from internal UFS, battery showing 99%, Wi-Fi up, dock Ethernet working, and a clear list of what's still open for anyone who picks up the project next.

Thirteen chapters, ~70 pages when rendered as PDF, ~5 000 events in the raw log summarised into a coherent narrative. The BOOTKIT lives at `book4-snapshot/` and can be rebuilt from first principles using these chapters as a blueprint.

> *"it was a long journey but we make something incredible"* — user

Co-Authored-By: Oz <oz-agent@warp.dev>
