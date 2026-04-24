# Chapter 10 — Ubuntu Installed + USB-C Recovery + BOOTKIT

## 10.1 Decision to install

With a proven PXE + minimal-initramfs chain, and with a working Ubuntu live session running via the dock Ethernet, the user made the consequential call:

> **"in the installation I don't have the option to install Ubuntu alongside windows, is it safe to lose windows and erase all the disk?"**

The agent laid out the consequences (loss of Samsung Recovery, loss of Samsung firmware update path via Windows Update). User accepted the tradeoffs on the understanding that the 202 MB Windows backup from Chapter 2 preserved the essentials.

## 10.2 Subiquity errors, round 1: missing `minimal.en.squashfs`

Running the installer from the live session:

```
Error: '///cdrom/casper/minimal.en.squashfs' doesn't exist or is invalid
```

But the file **did** exist on the USB. `df` reported 3.9 GB used, `ls /cdrom/casper/` returned… nothing. **The USB's FAT32 directory entries had corrupted.** `fsck.vfat -v /dev/sdb1` showed 50+ FAT-fs errors accumulating in real time. The Kingston SV300 (a 2014-era SATA SSD bridged over USB) was physically failing.

## 10.3 Bind-mount workaround

The mounted ISO on the **build host** was fine. SCP'd the casper files from the host to the laptop's `/tmp` (RAM-backed tmpfs, 7.6 GB), then bind-mounted `/tmp/new-casper` over the corrupted `/cdrom/casper`:

```bash path=null start=null
# On laptop
sudo mkdir -p /tmp/new-casper
sudo mount --bind /tmp/new-casper /cdrom/casper
sudo systemctl restart subiquity-server.service
```

Installer advanced further, then died on `grub-install`.

## 10.4 Subiquity errors, round 2: `efibootmgr` can't write EFI variables

```
curtin: grub-install: failed with code 3
  → efibootmgr: EFI variables are not supported on this system.
```

Curtin's chroot environment didn't bind-mount `/sys/firmware/efi/efivars`, so `efibootmgr` had nothing to write to. The entire rest of the install had completed (rootfs unpacked, kernel + initrd installed, filesystem committed). Only the boot-entry registration failed.

**Manual completion via SSH:**

```bash path=null start=null
# On laptop, from live session with /target still mounted by curtin
sudo mount --bind /sys/firmware/efi/efivars \
    /target/sys/firmware/efi/efivars

sudo arch-chroot /target /bin/bash <<'EOF'
# Use --removable so UEFI's BOOTAA64.EFI fallback works without efibootmgr
grub-install --target=arm64-efi --efi-directory=/boot/efi --removable --no-uefi-secure-boot
update-grub
EOF
```

## 10.5 The file-copy Plymouth crash

Third install attempt (after switching USBs to a fresh 58.6 GB stick that was physically sound):

```
[installer] Something went wrong.
OSError: [Errno 5] Input/output error: 'localectl'
```

The live rootfs's `loop0` backing file (`/cdrom/casper/minimal.squashfs`) was on the new USB, and even that new USB was stuttering under the installer's random-read load. The squashfs pages were getting evicted from the page cache and re-reads were hitting uncached blocks that the USB couldn't serve fast enough.

Workaround: pre-cache the whole squashfs in RAM with a single `cat minimal.squashfs > /dev/null`:

```bash path=null start=null
sudo cat /cdrom/casper/minimal.squashfs > /dev/null
# ~3 GB file, reads at 14 GB/s on second pass (all cached)
```

## 10.6 Install finally completes

The combination of:

1. Bind-mounting a fresh casper tree from `/tmp` over the corrupt `/cdrom/casper`
2. Pre-caching the squashfs into RAM
3. Manual `grub-install --removable` + `update-grub`
4. Manual EFI variable write from the live session (not chroot)

...got Ubuntu 24.04 **installed** onto the laptop's internal UFS `/dev/sda2`. ESP at `/dev/sda1` (260 MB FAT32). `GRUB_DEVICE_TREE=/boot/x1e80100-samsung-galaxy-book4-edge-14.dtb` baked into `/etc/default/grub`. Kernel `7.0.0-22-qcom-x1e` installed from the Canonical Concept-X1E PPA.

## 10.7 Cold boot from internal UFS

User powered off, unplugged the USB, powered on. **Ubuntu booted natively from internal UFS.** Display came up. SSH was reachable (after the live session had pre-registered the boot entry via `efibootmgr` from a session that *could* write efivars).

Running on the installed system:

```text path=null start=null
ubuntu@user:~$ df -h /
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda2       476G   15G  444G   4% /

ubuntu@user:~$ cat /proc/device-tree/model
Samsung Galaxy Book4 Edge (14 inch)

ubuntu@user:~$ uname -r
7.0.0-22-qcom-x1e

ubuntu@user:~$ cat /sys/firmware/efi/efivars/BootCurrent-*
BOOT0001  ubuntu
```

A full native Ubuntu install, booting from internal UFS, no USB required. **Major milestone.**

## 10.8 Samsung firmware + `.jsn` files installed

The 38 MB Linux-pathed firmware from Chapter 4 was rsync'd into `/lib/firmware/` on the installed system. The five `.jsn` files found later in the Samsung backup were placed in `/lib/firmware/qcom/x1e80100/SAMSUNG/galaxy-book4-edge/`:

- `adspr.jsn` — ADSP root domain
- `adsps.jsn` — ADSP secondary
- `adspua.jsn` — ADSP user agent
- `battmgr.jsn` — Battery Manager (later replaced with a Lenovo 21N1 version)
- `cdspr.jsn` — CDSP root domain

After reboot, `qcom-q6v5-pas` loaded both DSPs, fastrpc came up, and the audio codec (WCD938x) bound via SoundWire.

## 10.9 Wi-Fi on the 14": finding the patched `board-2.bin`

The upstream linux-firmware `board-2.bin` didn't let ath12k find a calibration match for the 14" PCI subsystem IDs. After searching zensanp's issue #3 thread for days, the community-patched file surfaced: `board-2.bin.zensanp-14inch` with sha1 starting `22fcfb9f…`. Copied into `/lib/firmware/ath12k/WCN7850/hw2.0/board-2.bin` and reloaded `ath12k_pci`:

```bash path=null start=null
sudo modprobe -r ath12k_pci
sudo modprobe ath12k_pci
dmesg | tail -20
# ath12k_pci 0004:01:00.0: board-2.bin matched, loaded WCN7850 calibration
# wlan0: link becomes ready
```

Wi-Fi up. First time.

## 10.10 The USB-C dock regression and recovery

Several days into work on the installed system, while doing unrelated battery investigation, the user reported:

> **"dock don't come up even after the reboot. obviously you touch something when you give the command that panic the laptop and make it reboot"**

Investigating, the agent identified that during the battery session it had:

1. Stopped `pd-mapper.service`
2. Unloaded + reloaded `qcom_pd_mapper`
3. Unbound + rebound `qcom_battmgr`
4. Copied a `RADS.bin` file into `/lib/firmware/qcom/x1e80100/SAMSUNG/galaxy-book4-edge/`
5. Run a risky recursive grep into `/sys/kernel/debug/` that caused the kernel to panic and auto-reboot

After the panic-reboot, USB-C wasn't enumerating. Chain of diagnosis:

```bash path=null start=null
# Check current kernel state
lsmod | grep -E 'qcom_pd|ucsi|typec'
# All loaded ✓

systemctl is-active pd-mapper.service
# active ✓

ls /sys/bus/auxiliary/devices/pmic_glink.*/driver -la
# pmic_glink.altmode.0 → pmic_glink_altmode.pmic_glink_altmode
# pmic_glink.ucsi.0 → ucsi_glink.pmic_glink_ucsi
# pmic_glink.power-supply.0 → qcom_battmgr.pmic_glink_power_supply
# All bound ✓

lsusb -t
# Bus 006: no devices
```

Software looked right, but the dock wasn't enumerating. `dmesg | grep -i ucsi` showed:

```
ucsi_glink.pmic_glink_ucsi pmic_glink.ucsi.0: UCSI version unknown
```

**UCSI stuck in "version unknown" state** — the layer that tells the kernel "a device connected/disconnected on port X" wasn't communicating with the PD controller.

## 10.11 The altmode rebind recovery

The fix was an unbind + rebind cycle for both altmode and UCSI:

```bash path=null start=null
# Rebind altmode
echo pmic_glink.altmode.0 | sudo tee \
    /sys/bus/auxiliary/drivers/pmic_glink_altmode.pmic_glink_altmode/unbind
sleep 1
echo pmic_glink.altmode.0 | sudo tee \
    /sys/bus/auxiliary/drivers/pmic_glink_altmode.pmic_glink_altmode/bind

# Rebind UCSI
echo pmic_glink.ucsi.0 | sudo tee \
    /sys/bus/auxiliary/drivers/ucsi_glink.pmic_glink_ucsi/unbind
sleep 1
echo pmic_glink.ucsi.0 | sudo tee \
    /sys/bus/auxiliary/drivers/ucsi_glink.pmic_glink_ucsi/bind

# Then unplug + replug the dock's USB-C cable
```

User unplugged + replugged. Dock enumerated. RTL8153 appeared as `enxXXXXXXXXXXXX` with `LOWER_UP`. **USB-C restored.**

Encapsulated into a helper script `/usr/local/sbin/book4-usbc-recover.sh` for future use.

## 10.12 The RADS.bin revert

Removed the `RADS.bin` added during the battery investigation (only one file that had been added since the known-good state):

```bash path=null start=null
sudo cp /lib/firmware/qcom/x1e80100/SAMSUNG/galaxy-book4-edge/RADS.bin \
    ~/RADS.bin.battinvest-backup
sudo rm /lib/firmware/qcom/x1e80100/SAMSUNG/galaxy-book4-edge/RADS.bin
sudo systemctl restart pd-mapper.service
```

## 10.13 The BOOTKIT — distilling the working state

To never lose the working state again, the agent built a **BOOTKIT**: a self-contained, portable archive of every file and config that represented "Samsung Galaxy Book4 Edge 14" + Ubuntu 26.04 booting correctly with USB-C + Wi-Fi."

### BOOTKIT inventory

```
BOOTKIT-20260421-223459-USBC-OK/
├── VERSION
├── README.md
├── INSTALL.sh              (idempotent installer with DMI check)
├── CHECKSUMS.sha256
├── firmware/
│   └── lib/firmware/qcom/x1e80100/SAMSUNG/galaxy-book4-edge/
│       ├── qcadsp8380.mbn            (19.9 MB)
│       ├── qccdsp8380.mbn            (3.1 MB)
│       ├── qcdxkmsuc8380.mbn
│       ├── wlanfw20.mbn
│       ├── adsp_dtbs.elf
│       ├── cdsp_dtbs.elf
│       ├── adspr.jsn, adsps.jsn, adspua.jsn
│       ├── cdspr.jsn
│       └── battmgr.jsn               (Lenovo 21N1)
├── wifi/
│   └── lib/firmware/ath12k/WCN7850/hw2.0/
│       ├── amss.bin
│       ├── board-2.bin               (zensanp 14")
│       └── m3.bin
├── dtb/
│   ├── x1e80100-samsung-galaxy-book4-edge-14.dtb        (GOLDEN)
│   ├── x1e80100-samsung-galaxy-book4-edge-14.dtb.NOBT
│   └── x1e80100-samsung-galaxy-book4-edge-14.dtb.PRE-TPADFIX
├── configs/
│   ├── modules-load.d/book4-pd-mapper.conf
│   ├── udev/90-book4edge-keyboard.rules
│   ├── grub/book4-dtb-pickup.cfg
│   └── grub/book4-rescue-menu.cfg
├── scripts/
│   ├── book4-restore-golden.sh
│   ├── book4-full-rollback.sh
│   ├── book4-revert-touchpad-patch.sh
│   ├── book4-revert-wifi-fw.sh
│   ├── book4-fix-grubcfg.sh
│   └── book4-usbc-recover.sh
└── alsa/
    └── var/lib/alsa/asound.state
```

### The INSTALL.sh

Idempotent. Checks DMI (`/sys/devices/virtual/dmi/id/product_name`) to refuse running on a non-Book4. Copies firmware, DTB, configs. Runs `update-initramfs -u` and `update-grub`. Restores ALSA state. Validates with `modinfo` + `lsmod` probe.

### Triple-copy locations

```
<HOME>/book4-snapshot/BOOTKIT-20260421-223459-USBC-OK.tar.gz
/boot/BOOTKIT-20260421-223459-USBC-OK.tar.gz
/boot/efi/book4-golden/BOOTKIT-20260421-223459-USBC-OK.tar.gz
```

All three with matching SHA256 `3815fe2469a793cd…`. The `/boot/efi/book4-golden/` copy survives on the ESP and is readable even from a pre-install live USB, so you can recover without having the rootfs mounted.

## 10.14 The full remaster ISO

The user asked for a distributable ISO:

> **"option B for sure so I can upload it online to help who is in my position"**

Download Canonical's `resolute-desktop-arm64+x1e-20260326.iso` (3.8 GB). SHA256: `d0cbef7b48f5806093c2f4d8ea6d372249e86ace0051217c76ce92d60274078d`.

Extract → modify → repack workflow planned:

```bash path=null start=null
sudo apt-get install -y xorriso squashfs-tools genisoimage rsync

mkdir -p <HOME>/Galaxy-Book4-Edge-linux/{base,extract,squashfs-root,output}

# Extract ISO via xorriso (preserves El Torito boot)
xorriso -osirrox on -indev resolute-desktop-arm64+x1e-20260326.iso \
    -extract / <HOME>/Galaxy-Book4-Edge-linux/extract/

# Unpack minimal.standard.squashfs (the main rootfs layer)
sudo unsquashfs -d <HOME>/Galaxy-Book4-Edge-linux/squashfs-root \
    <HOME>/Galaxy-Book4-Edge-linux/extract/casper/minimal.standard.squashfs

# Overlay our BOOTKIT files
sudo rsync -aAXH \
    <HOME>/book4-snapshot/BOOTKIT-*/firmware/ \
    <HOME>/Galaxy-Book4-Edge-linux/squashfs-root/

# Repack with zstd, update metadata, regenerate ISO
sudo mksquashfs <HOME>/Galaxy-Book4-Edge-linux/squashfs-root \
    <HOME>/Galaxy-Book4-Edge-linux/extract/casper/minimal.standard.squashfs \
    -comp zstd -Xcompression-level 22 -noappend

xorriso -as mkisofs -iso-level 3 \
    -volid "Ubuntu 26.04 Book4 Edge" \
    -o <HOME>/Galaxy-Book4-Edge-linux/output/book4edge-ubuntu-26.04.iso \
    <HOME>/Galaxy-Book4-Edge-linux/extract/
```

## 10.15 End of Chapter 10

By end of Chapter 10:

- **Ubuntu 26.04 booting natively from internal UFS** ✅
- **USB-C dock + RTL8153 Ethernet** ✅ (via altmode rebind recovery pattern)
- **Wi-Fi 7 (WCN7850 via ath12k)** ✅ (with zensanp-patched 14" board-2.bin)
- **ADSP + CDSP remoteprocs** ✅
- **Audio codec WCD938x bound** ✅
- **BOOTKIT** triple-copied and SHA-verified ✅
- **Full remaster ISO** workspace set up in `<HOME>/Galaxy-Book4-Edge-linux/`

Still open:
- Native I²C-HID keyboard events don't reach GNOME
- Built-in speakers (WSA884x) don't attach
- Battery reporting is empty (qcom-battmgr nodes populate with no data)
- Camera/fingerprint unsupported

Battery became the next obsession — the user couldn't use a laptop without knowing its charge level. That triggered the journey into fan and EC research (Chapter 11) and ultimately the custom battery driver (Chapter 12).
