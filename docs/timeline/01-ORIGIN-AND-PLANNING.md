# Chapter 1 — Origin and Initial Planning

## 1.1 The opening request

The very first user message in this journey:

> **"build a bootable archlinux iso for Galaxy Book4 Edge starting from this project https://github.com/zensanp/linux-book4-edge"**

One sentence. A single external URL. No device information, no SSH credentials, no firmware. That was the entire spec.

## 1.2 What we knew (and didn't know) at t=0

The `zensanp/linux-book4-edge` repository is a community kernel fork that carries patches to support the Samsung Galaxy Book4 Edge on top of mainline Linux. Its README quickly told us the target SoC was **Qualcomm Snapdragon X1E80100**, ARM64, and that getting any Linux distro onto it would involve:

- Cross-compiling a custom kernel (mainline lags behind zensanp's fork by several kernel cycles)
- Applying a DTS (device-tree source) patch for Wi-Fi (`regulator-always-on` on `VREG_WCN_3P3`)
- Getting proprietary Qualcomm firmware blobs for ADSP, CDSP, Adreno GPU, and Wi-Fi — none of which are redistributable
- Supplying a valid `board-2.bin` for the WCN7850 Wi-Fi chip

**What made Arch specifically hard**:
1. Arch Linux proper has **no aarch64 edition**. The target would have to be **Arch Linux ARM (ALARM)**.
2. `archiso`/`mkarchiso` (the standard Arch Galaxy-Book4-Edge-linuxer) is x86_64-only.
3. The hybrid UEFI/BIOS ISO that Arch normally produces has no ARM equivalent; we'd be hand-rolling with GRUB + `xorriso`.

## 1.3 The initial plan (before any contact with the device)

The agent drafted a seven-phase plan in the `create_plan` tool:

1. **Toolchain**: `aarch64-linux-gnu-gcc`, `qemu-user-static`, `qemu-user-static-binfmt`, `pacman-key`
2. **Kernel**: clone zensanp, patch the WCN3P3 regulator, build an aarch64 `Image` + device-tree blob
3. **Firmware**: ship a first-boot helper `qcom-firmware-extract` that would mount the Windows partition via `ntfs-3g` and pull `.mbn` blobs from `C:\Windows\System32\DriverStore\FileRepository`
4. **Rootfs**: extract the ALARM `archlinux-aarch64-latest.tar.gz` tarball, `arch-chroot` via qemu-user-static, install live-iso packages
5. **Initramfs**: a custom `book4live` hook (not `mkinitcpio-archiso`, which is x86-centric) that does *label-probe → loop-mount squashfs → overlay tmpfs → switch_root*
6. **GRUB**: aarch64 EFI bootloader with `earlycon=efifb` on the kernel cmdline (no UART on the Book4, so framebuffer is the only early debug channel)
7. **ISO packing**: build the squashfs + EFI ESP, wrap with xorriso into a hybrid UEFI-only ISO

Key user-visible warnings from the plan:
- Qualcomm DSP/GPU/Wi-Fi firmware isn't freely redistributable
- Secure Boot must be disabled
- USB must be plugged into the **left** USB-C port (per zensanp's README)

## 1.4 The first plot twist: SSH access is available

The user's second message completely reshaped the approach:

> **"the galaxy book4 edge is on ssh at ip 10.x.x.x if we need any info we get it from there"**

This was fantastic news: we could interrogate the exact device, not guess at a generic model. The agent asked for the SSH username. The user clarified:

> **"it's windows so i supposed my windows account?"**

Another plot twist — the Book4 Edge was **still running Windows 11 ARM**. That meant:

- SSH would land in PowerShell, not bash
- The Qualcomm firmware could be extracted **directly from Windows' DriverStore** instead of relying on a first-boot helper
- A live-USB approach (boot-only, no install) made immediate sense because we could preserve Windows

The user provided the username `user` and confirmed password authentication.

## 1.5 Deciding live-USB vs install

At this point the agent pivoted the first milestone from "installable ISO" to **"live USB that boots and runs in RAM, leaving Windows untouched"**. Rationale:

- The device had only ~51 GB free on C: (later corrected to 142 GB)
- Shrinking Windows reliably on ARM64 is fraught (Fast Startup state, BitLocker possibility)
- The user had not yet consented to losing Windows
- A live USB proves the kernel + DTB + firmware stack works before any disk surgery

## 1.6 Technical decisions locked in before the first line of code

| Choice | Decision | Why |
|---|---|---|
| Base OS | Arch Linux ARM (aarch64) | User asked for Arch; ALARM is the closest viable target |
| Kernel | zensanp `x1e80100-book4e-6.17-rc4`, pinned to a specific commit SHA | Reproducibility |
| DTB | `x1e80100-samsung-galaxy-book4-edge.dtb` | Matches the SKU we later confirmed |
| Build host | User's Arch system at `<HOME>/Documents/Galaxy-Book4-Edge-linux/` | Existing toolchain |
| Init framework | Custom `book4live` mkinitcpio hook | `mkinitcpio-archiso` is x86_64-only |
| Boot debug | `earlycon=efifb` | No UART header on Book4 |
| Firmware strategy | Extract from Windows over SSH now, bundle in rootfs | Fastest path + preserves originals |
| Wi-Fi board file | Debian `firmware-atheros 20251111-1` | Known working on 16" per zensanp issue #3 |

## 1.7 What the user expected vs what they'd get

User's mental model: *"build a bootable archlinux iso"*
Engineering reality: *"hand-craft a bespoke kernel+initramfs+firmware-staging+GRUB-boot-chain ARM64 live-USB because the community tooling for this device doesn't exist yet"*

The gap between those two would become a running theme for the next five days.

## 1.8 Foreshadowing

Two risks were explicitly flagged but not yet felt:

1. **ADSP VBUS blip** — loading the audio DSP firmware on X1E can cause a USB-C voltage glitch that disconnects anything plugged into the left USB-C. Mentioned in the plan. Not respected. Paid for in blood later (Chapters 6 and 10).

2. **Touchscreen and audio** — explicitly marked "not supported upstream" in the plan. Still a limitation at the end of the project.

## 1.9 Initial commands run (this chapter only)

```bash path=null start=null
# Verified that all toolchain packages exist in Arch's official repos
pacman -Si aarch64-linux-gnu-gcc qemu-user-static qemu-user-static-binfmt

# Installed them
sudo pacman -S --noconfirm aarch64-linux-gnu-gcc aarch64-linux-gnu-glibc-headers \
    qemu-user-static qemu-user-static-binfmt xz zstd squashfs-tools \
    arch-install-scripts

# Workspace
mkdir -p ~/Documents/Galaxy-Book4-Edge-linux/{build,src,rootfs,firmware-stage,iso,logs}

# Kernel source
cd ~/Documents/Galaxy-Book4-Edge-linux/src
git clone --depth=1 --branch x1e80100-book4e-6.17-rc4 \
    https://github.com/zensanp/linux-book4-edge.git linux
cd linux
git rev-parse HEAD > ../../build/KERNEL_SHA   # 708b2aeff3e9e014aaf6ec36e3de0e43b7c23aa5
```

The workspace was structured so that every build artefact (kernel `Image`, DTB, initramfs, firmware stage, logs) lived under one roof that could later be archived as a reproducibility bundle.

## 1.10 End of Chapter 1

At the end of the planning phase we had:

- A clear multi-phase plan recorded in the Warp plan artefact
- An Arch build host with aarch64 cross-tools installed
- The zensanp kernel source cloned and SHA-pinned
- A confirmed SSH target (Windows 11 ARM) on the LAN
- No actual device information yet — that's Chapter 2

The next move was to interrogate the laptop over SSH and turn "we think it's a Galaxy Book4 Edge with an X1E80100" into a full device fingerprint.
