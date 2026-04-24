# Chapter 5 — Rootfs Build and the First USB

## 5.1 Arch Linux ARM as the base

`mkarchiso` is x86_64-only, so the live-USB's root filesystem had to be hand-rolled from the Arch Linux ARM aarch64 tarball:

```bash path=null start=null
cd ~/Documents/Galaxy-Book4-Edge-linux/rootfs/
wget http://os.archlinuxarm.org/os/ArchLinuxARM-aarch64-latest.tar.gz
sudo bsdtar -xpf ArchLinuxARM-aarch64-latest.tar.gz -C root/
du -sh root/    # ~2.2 GB
```

The tarball was pre-populated with a default `alarm` user, basic tools, and a minimal systemd. Everything else needed to be added via chroot.

## 5.2 Enter the `qemu-user-static` rabbit hole

On x86_64 → aarch64 cross-bootstrapping, the trick is to copy `qemu-aarch64-static` into the chroot so binfmt_misc transparently executes ARM binaries through QEMU. The `qemu-user-static-binfmt` package registers the necessary binfmt entries.

```bash path=null start=null
sudo cp /usr/bin/qemu-aarch64-static root/usr/bin/
# Verify binfmt is registered
cat /proc/sys/fs/binfmt_misc/qemu-aarch64
```

## 5.3 arch-chroot reality check

`arch-chroot` mounts `/dev`, `/proc`, `/sys`, `/run`, and **`/tmp` as a fresh tmpfs**. The last one bit us immediately: any setup script placed in `/tmp` on the host vanished inside the chroot.

The fix: place the setup script in `/root/` (which is preserved), and invoke it with an explicit path.

```bash path=null start=null
sudo cp setup.sh root/root/setup.sh
sudo chmod +x root/root/setup.sh
sudo arch-chroot root /root/setup.sh
```

## 5.4 pacman 7.0's Landlock sandbox surprise

With the chroot set up, the first `pacman -Syu` inside it failed:

```
error: failed to synchronize all databases (could not initialize sandbox)
error: failed to commit transaction (could not determine cachedir mount point /var/cache/pacman/pkg)
```

pacman 7.0 added a **Landlock-based sandbox** that requires:
1. Landlock support in the **host kernel** (not the one inside the chroot — the real one QEMU is running on)
2. The cache directory `/var/cache/pacman/pkg` to be a **mount point** (not just a regular directory)
3. The root `/` to also be a mount point

Three issues, three fixes, applied in sequence:

### 5.4.1 Disable the sandbox

```bash path=null start=null
# Inside the chroot
sed -i 's/^#DisableSandbox/DisableSandbox/' /etc/pacman.conf
```

This alone wasn't enough — the checks fire before sandbox init.

### 5.4.2 Bind-mount the cache dir

```bash path=null start=null
mkdir -p /var/cache/pacman/pkg
mount --bind /var/cache/pacman/pkg /var/cache/pacman/pkg
```

### 5.4.3 Bind-mount root over itself

```bash path=null start=null
mount --bind / /
# And disable the disk-space check entirely
sed -i 's/^CheckSpace/#CheckSpace/' /etc/pacman.conf
```

After all three, the 205-package install (live-iso environment + tools) ran clean.

## 5.5 Packages installed in the chroot

```
base-devel base openssh sudo dosfstools e2fsprogs 
squashfs-tools vim nano git wget curl networkmanager
mkinitcpio-archiso (doesn't actually work on aarch64 — see 5.6)
firmware-linux-nonfree (general firmware base)
ath12k-firmware (replaced later with our Windows extract)
```

Total: 205 packages, 2.5 GB rootfs after install.

## 5.6 Custom `book4live` initramfs hook

`mkinitcpio-archiso` is the standard Arch live-boot hook but it's x86_64-centric and makes assumptions that don't hold on aarch64 (no BIOS fallback path, different label probe). A custom hook was written:

**`/etc/initcpio/hooks/book4live`**:
```bash path=null start=null
run_hook() {
    # Probe for USB with label BOOK4E_ARCH
    for i in 1 2 3 4 5; do
        if [ -e /dev/disk/by-label/BOOK4E_ARCH ]; then
            break
        fi
        sleep 1
    done
    
    # Loop-mount squashfs
    mkdir -p /run/rootfs /run/squashfs
    mount -o loop /dev/disk/by-label/BOOK4E_ARCH /run/squashfs
    mount -o ro,loop /run/squashfs/airootfs.sfs /run/rootfs
    
    # Overlay tmpfs
    mkdir -p /run/overlay/upper /run/overlay/work
    mount -t tmpfs tmpfs /run/overlay
    mkdir -p /run/overlay/upper /run/overlay/work
    mount -t overlay overlay -o lowerdir=/run/rootfs,upperdir=/run/overlay/upper,workdir=/run/overlay/work /new_root
    
    exit_hook() {
        :
    }
}
```

The companion `install` script told mkinitcpio to include this hook in the initramfs.

## 5.7 Firmware merge

```bash path=null start=null
sudo rsync -aAHX \
    ~/Documents/Galaxy-Book4-Edge-linux/firmware-stage/lib/firmware/ \
    ~/Documents/Galaxy-Book4-Edge-linux/rootfs/root/lib/firmware/
sudo rsync -aAHX \
    ~/Documents/Galaxy-Book4-Edge-linux/build/modroot/lib/modules/6.17.0-rc4-g708b2aeff3e9/ \
    ~/Documents/Galaxy-Book4-Edge-linux/rootfs/root/lib/modules/6.17.0-rc4-g708b2aeff3e9/
```

## 5.8 Rootfs overlays (first-boot helpers)

Three scripts were placed in the rootfs per Chapter 3's audit:

1. **`/etc/udev/rules.d/99-book4edge-keyboard.rules`** — forces the ELAN I²C-HID keyboard into keyboard (not tabletpad) classification
2. **`/usr/local/bin/book4edge-bt-mac`** — one-shot that runs `btmgmt -i hci0 public-addr XX:XX:XX:XX:XX:XX`
3. **`/usr/local/bin/book4edge-extract-firmware`** — was originally needed before we extracted from Windows; kept as a safety net

Each had a corresponding systemd unit in `/etc/systemd/system/` marked `enabled` via symlinks in `multi-user.target.wants/`.

## 5.9 Generating the initramfs inside the chroot

```bash path=null start=null
sudo arch-chroot root
# inside chroot:
mkinitcpio -k 6.17.0-rc4-g708b2aeff3e9 \
           -c /etc/mkinitcpio.conf \
           -g /boot/initramfs-book4edge.img
exit
```

Output: `initramfs-book4edge.img` (~30 MB compressed with zstd).

## 5.10 Squashfs build

```bash path=null start=null
sudo mksquashfs rootfs/root/ airootfs.sfs -comp zstd -Xcompression-level 22
ls -sh airootfs.sfs    # ~1.2 GB
```

Moved into `iso/BOOK4E_ARCH/`.

## 5.11 GRUB aarch64 EFI bootloader

```bash path=null start=null
sudo grub-mkstandalone --format=arm64-efi --output=iso/EFI/BOOT/BOOTAA64.EFI \
    --modules="part_gpt part_msdos fat iso9660 normal chain boot configfile linux search echo" \
    --fonts="unicode" "boot/grub/grub.cfg=grub.cfg"
```

Custom `grub.cfg` entries:

```
menuentry "Book4 Edge LIVE" {
    search --set=root --label BOOK4E_ARCH
    linux /Image root=LABEL=BOOK4E_ARCH rw quiet splash
    devicetree /book4edge.dtb
    initrd /initramfs-book4edge.img
}
```

Two additional entries supplied an `earlycon=efifb` verbose fallback and a `nomodeset` emergency entry.

## 5.12 Final ISO assembly

```bash path=null start=null
xorriso -as mkisofs \
    -iso-level 3 \
    -volid BOOK4E_ARCH \
    -e EFI/BOOT/BOOTAA64.EFI \
    -no-emul-boot \
    -isohybrid-gpt-basdat \
    -o book4edge-arch-live.iso \
    iso/
```

Final ISO size: ~1.4 GB.

## 5.13 First USB flash

```bash path=null start=null
# /dev/sda identified as a 111.8 GB Kingston SV300 (2014-era SATA SSD with USB adapter)
sudo dd if=book4edge-arch-live.iso of=/dev/sda bs=4M status=progress conv=fsync
sudo sync
```

Duration: ~3 minutes. Everything looked fine.

## 5.14 The moment of truth

The USB went into the Book4 Edge. UEFI picked it up. GRUB showed the menu. Kernel started loading. **Screen went black about 12 seconds later.** No TTY. No network.

This is where the project's hardest chapters began.

## 5.15 End of Chapter 5

Outputs:

- `~/Documents/Galaxy-Book4-Edge-linux/rootfs/root/` — full 2.5 GB ALARM rootfs with our kernel modules + firmware + helpers
- `~/Documents/Galaxy-Book4-Edge-linux/iso/` — staged tree with `Image`, DTB, initramfs, GRUB EFI, squashfs
- `~/Documents/Galaxy-Book4-Edge-linux/book4edge-arch-live.iso` — 1.4 GB hybrid UEFI ISO
- A freshly flashed Kingston SV300 USB

And a laptop that **refused to give us any visible sign of life** past 12 seconds of boot.

The next chapter is about why — and why the answer turned out to be a broken USB controller, not broken software.
