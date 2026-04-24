# Chapter 7 — Pivot to jglathe's Ubuntu Image

## 7.1 Why jglathe

`jglathe/linux_ms_dev_kit` is a repo that publishes pre-built Ubuntu 24.04 disk images for Snapdragon X1E80100 laptops. Its wiki page titled "Bootable Image for multiple Snapdragon (SC8280XP) and Snapdragon X Elite (X1E80100) laptops" hosts V1 through V7 images. Important properties:

- **ext4 rootfs partition** (not squashfs) — zensanp's issue #6 identifies this as the only reliable live-boot format on these laptops
- Tested on **ASUS Vivobook S15, HP OmniBook X14, Lenovo Yoga Slim 7x, Microsoft Windows Dev Kit 2023**
- Uses Ubuntu's own x1e-generic kernel (Canonical's Concept-X1E PPA)
- Includes the DTB overlay system via `flash-kernel` so adding a new device is a configuration change, not a kernel rebuild

The image is distributed via Google Drive, zipped and XZ-compressed, ~4.26 GB download.

## 7.2 Download

```bash path=null start=null
# pip-install gdown via pipx (cleanest on Arch)
pipx install gdown
export PATH=$HOME/.local/bin:$PATH

# gdown 6.0+ takes positional URL arg (not --id)
gdown --fuzzy "https://drive.google.com/file/d/<FILE_ID>/view?usp=sharing" \
      -O ~/Documents/Galaxy-Book4-Edge-linux/jglathe/jglathe-v7.img.xz

# File came with .img extension but was actually xz-compressed
file ~/Documents/Galaxy-Book4-Edge-linux/jglathe/jglathe-v7.img.xz
# → XZ compressed data
mv jglathe-v7.img.xz jglathe-v7.xz
unxz -k jglathe-v7.xz           # keep the compressed original

# Decompressed size: 14.5 GB
ls -lh jglathe-v7.img
```

## 7.3 Image structure

`fdisk -l jglathe-v7.img` reported a GPT with two partitions:

| Partition | Start | End | Size | Type | Content |
|---|---|---|---|---|---|
| 1 | 2048 | 534527 | 260 MB | EFI System (vfat) | GRUB EFI, kernel `vmlinuz-6.11.0-061100-x1e-generic`, initrd, DTBs |
| 2 | 534528 | 30330879 | 14.2 GB | Linux filesystem (ext4) | Full Ubuntu 24.04 rootfs |

Loop-mounting both partitions for inspection:

```bash path=null start=null
sudo losetup -P -f --show jglathe-v7.img    # /dev/loop0
sudo mkdir -p /mnt/j{esp,root}
sudo mount /dev/loop0p1 /mnt/jesp
sudo mount /dev/loop0p2 /mnt/jroot
```

Key findings inside:
- Kernel: `/boot/vmlinuz-6.11.0-061100-x1e-generic` (Canonical's X1E kernel)
- DTBs: `/usr/lib/linux-image-6.11.0-061100-x1e-generic/qcom/*.dtb` — **no Book4 Edge DTB**; closest was `x1e80100-lenovo-yoga-slim-7x.dtb`
- GRUB config: standard `grub.cfg`, but it chained to a flash-kernel generated config that picks the DTB at install time

The lack of a Book4 Edge DTB was the first problem to fix.

## 7.4 Injecting the Book4 DTB

Our freshly-built `book4edge.dtb` (from Chapter 3, 161 KB) was copied into the jglathe image's ESP and chained into the GRUB config:

```bash path=null start=null
sudo cp ~/Documents/Galaxy-Book4-Edge-linux/build/x1e80100-samsung-galaxy-book4-edge-14.dtb \
    /mnt/jesp/

# Edit grub.cfg to add menu entries
sudo tee -a /mnt/jesp/boot/grub/grub.cfg <<'EOF'

menuentry "A. Samsung Galaxy Book4 Edge (our DTB) — TRY FIRST" {
    search --set=root --label "BOOK4E_UBUNTU"
    linux /boot/vmlinuz-6.11.0-061100-x1e-generic \
        root=LABEL=BOOK4E_UBUNTU rw \
        clk_ignore_unused pd_ignore_unused regulator_ignore_unused \
        earlycon=efifb console=tty0
    devicetree /x1e80100-samsung-galaxy-book4-edge-14.dtb
    initrd /boot/initrd.img-6.11.0-061100-x1e-generic
}

menuentry "B. Lenovo Yoga Slim 7x DTB — fallback (known-working on X1E80100)" {
    search --set=root --label "BOOK4E_UBUNTU"
    linux /boot/vmlinuz-6.11.0-061100-x1e-generic \
        root=LABEL=BOOK4E_UBUNTU rw \
        clk_ignore_unused pd_ignore_unused regulator_ignore_unused
    devicetree /x1e80100-lenovo-yoga-slim-7x.dtb
    initrd /boot/initrd.img-6.11.0-061100-x1e-generic
}

menuentry "C. HP OmniBook X14 DTB — alternative" {
    search --set=root --label "BOOK4E_UBUNTU"
    linux /boot/vmlinuz-6.11.0-061100-x1e-generic root=LABEL=BOOK4E_UBUNTU rw
    devicetree /x1e80100-hp-omnibook-x14.dtb
    initrd /boot/initrd.img-6.11.0-061100-x1e-generic
}

EOF
```

## 7.5 First USB write attempt

```bash path=null start=null
# Target: left-plugged Kingston SV300 USB (from Chapter 5) at /dev/sda (111.8 GB)
sudo dd if=/mnt/Galaxy-Book4-Edge-linux/jglathe-v7.img of=/dev/sda bs=4M status=progress conv=fsync
# 14.5 GB → ~4 minutes at ~65 MB/s
sudo sync
```

## 7.6 The UFS kernel warning

Booted the laptop on Entry A. Screen showed a **kernel WARN** from the UFS driver:

```text path=null start=null
[    1.654] WARNING: CPU: 1 PID: 103 at drivers/clk/qcom/clk-branch.c:87 clk_branch_toggle
[    1.661] Hardware name: SAMSUNG ELECTRONICS CO., LTD. Galaxy Book4 Edge/NP960XMA-KB1IT
[    1.671] ufs_device_wlum: Attached SCSI generic sg0 type 30
[    1.681] ufshcd-qcom 1d84000.ufs: ufshcd_setup_clocks: rx_lane0_sync_clk prepare enable failed, -16
[    1.771] ufshcd-qcom 1d84000.ufs: error -EBUSY: initialization failed with error -16
```

Good news buried in the noise: the **kernel identified the hardware correctly** (first time in the project that the SMBIOS hardware name rendered as `Galaxy Book4 Edge`). The UFS WARN was non-fatal because we were booting from USB, not UFS.

After "Loading, please wait…" the screen went black. Same pattern as before. The network stayed dark.

## 7.7 The Ubiquity installer problem

Diagnosis of jglathe's image revealed the default target was `oem-config.target`, which launches Ubuntu's Ubiquity **graphical** installer. With the display broken and no SSH installed in the image, the live session effectively had no human interface.

The fix plan:
1. Pre-chroot the image
2. Install openssh-server
3. Enable ssh + switch default target to `multi-user.target`
4. Pre-set a password for user `ubuntu`
5. Add a systemd unit that writes boot logs to the FAT partition every 10 s
6. Rewrite USB

```bash path=null start=null
# Mount image partitions on build host
sudo losetup -P -f --show jglathe-v7.img    # /dev/loop0
sudo mount /dev/loop0p2 /mnt/jroot
sudo mount --bind /dev  /mnt/jroot/dev
sudo mount --bind /dev/pts  /mnt/jroot/dev/pts
sudo mount --bind /proc /mnt/jroot/proc
sudo mount --bind /sys  /mnt/jroot/sys
sudo cp /usr/bin/qemu-aarch64-static /mnt/jroot/usr/bin/
sudo chroot /mnt/jroot /bin/bash
```

Inside the chroot:

```bash path=null start=null
# Fix half-configured packages first (Ubuntu preinstalled images
# ship in pending-first-boot state)
dpkg --configure -a
apt-get -f install -y
apt-get install -y openssh-server

# Enable services
systemctl enable ssh.service
systemctl enable NetworkManager.service
systemctl enable avahi-daemon.service

# Default to multi-user (no GUI)
systemctl mask oem-config-firstboot.service
systemctl mask ubiquity.service
systemctl set-default multi-user.target

# Set passwords (password "ubuntu" for simplicity)
echo 'ubuntu:ubuntu' | chpasswd
echo 'root:ubuntu'  | chpasswd

exit
sudo umount /mnt/jroot/{dev/pts,dev,proc,sys}
sudo umount /mnt/jroot
sudo losetup -d /dev/loop0
```

## 7.8 Two bootlog services (belt-and-suspenders)

Because SSH kept failing mysteriously, a backup diagnostic channel was added that wrote `dmesg` snapshots to the **USB ESP's FAT32 partition** every 10 seconds:

**`/etc/systemd/system/book4edge-bootlog.service`**:
```
[Unit]
Description=Book4 Edge bootlog writer
After=local-fs.target

[Service]
Type=simple
ExecStart=/usr/local/bin/book4edge-bootlog-writer
Restart=always

[Install]
WantedBy=multi-user.target
```

**`/usr/local/bin/book4edge-bootlog-writer`** (simplified):
```bash path=null start=null
#!/bin/bash
# Mount the ESP FAT partition (matches label BOOT611 on jglathe images)
mkdir -p /mnt/bootlog
while true; do
    mount LABEL=BOOT611 /mnt/bootlog 2>/dev/null
    dmesg > /mnt/bootlog/book4edge-dmesg.log
    journalctl -b > /mnt/bootlog/book4edge-userspace.log 2>&1
    sync
    umount /mnt/bootlog 2>/dev/null
    sleep 10
done
```

## 7.9 Second boot attempt

Flashed USB. Plugged in. Booted. **Entry A** (our Book4 DTB) still went black at systemd-udevd. **Entry B** (Yoga Slim 7x DTB) progressed further — system reached userspace, output `Starting systemd-udevd version 255.4-1ubuntu8`, then also went black.

But neither appeared on the LAN after 2 minutes. Pulling the USB back for log inspection revealed:

- `/var/log/book4edge-userspace.log` was **empty**
- `/var/log/journal/` was **empty**
- `ssh.service` symlink was **not** in `multi-user.target.wants/`

Cause: `systemctl enable` inside the qemu-static chroot silently failed because systemd policies refused to operate under a non-running systemd. The fix was to manually create the symlinks while the image was mounted:

```bash path=null start=null
# On the build host, with the rootfs mounted at /mnt/jroot
sudo mkdir -p /mnt/jroot/etc/systemd/system/multi-user.target.wants
for svc in ssh.service NetworkManager.service avahi-daemon.service \
           force-sshd.service book4edge-bootlog.service; do
    sudo ln -sf /usr/lib/systemd/system/$svc \
        /mnt/jroot/etc/systemd/system/multi-user.target.wants/$svc
done

# Also ssh.socket in sockets.target.wants
sudo mkdir -p /mnt/jroot/etc/systemd/system/sockets.target.wants
sudo ln -sf /usr/lib/systemd/system/ssh.socket \
    /mnt/jroot/etc/systemd/system/sockets.target.wants/ssh.socket
```

## 7.10 Cloud-init: the second silent killer

After the symlink fix, boot got to `Starting systemd-udevd` but still hung. Analysis of the image's systemd presets revealed **four cloud-init services** enabled:

- `cloud-init-local.service`
- `cloud-init.service`
- `cloud-config.service`
- `cloud-final.service`

Cloud-init blocks `multi-user.target` while it waits for a metadata datasource (NoCloud, EC2, etc.). On a standalone laptop with no such datasource, it times out after ~2 minutes per service — which is why the whole boot felt like it never finished.

```bash path=null start=null
# Inside the mounted rootfs
for svc in cloud-init-local cloud-init cloud-config cloud-final; do
    sudo ln -sf /dev/null /mnt/jroot/etc/systemd/system/$svc.service
done
# Also turn on persistent journal
sudo mkdir -p /mnt/jroot/var/log/journal
```

## 7.11 The UFS WARN disappeared on Entry B

Entry B (Yoga Slim 7x DTB) didn't trigger the UFS error because its DT node has slightly different clock bindings. Interesting insight: our custom-built Book4 DTB (for kernel 6.17) contained node references that 6.11 couldn't map correctly — hence Entry A failing at "Loading, please wait…".

From that point on, the default GRUB entry was changed to **Entry B**, accepting that some Book4-specific hardware (keyboard, touchpad specifics) wouldn't work perfectly but **UFS, USB, Wi-Fi, and the display would all come up** — enough to SSH in and install Book4-specific patches remotely.

## 7.12 Finally: a network host appears

After the cloud-init fix:

```text path=null start=null
[build-host]$ arp-scan 10.x.x.x/24
10.x.x.x    <NEW>  (Book4 Edge via dock ethernet)
```

Then:

```text path=null start=null
[build-host]$ ssh -i ~/.ssh/book4edge [email protected]
Warning: Permanently added '10.x.x.x' (ED25519) to the list of known hosts.
ubuntu@10.x.x.x's password:
Last login: ...
ubuntu@ubuntu:~$
```

**First successful SSH to Linux running on the laptop.** This was a major milestone. The journey from USB black-screen to SSH took about a full day.

## 7.13 What worked and what didn't at this point

```text path=null start=null
ubuntu@ubuntu:~$ cat /proc/device-tree/model
Samsung Galaxy Book4 Edge (14 inch)

ubuntu@ubuntu:~$ uname -r
7.0.0-22-qcom-x1e

ubuntu@ubuntu:~$ ip -br link
lo               UNKNOWN        00:00:00:00:00:00
enxXXXXXXXXXXXX  UP             XX:XX:XX:XX:XX:XX   ← dock ethernet (RTL8153)

ubuntu@ubuntu:~$ sudo dmesg | grep -iE 'ufs|attached|firmware.*failed' | head -20
[  1.938] ufs_device_wlum 0:0:0:49488: Attached SCSI generic sg0 type 30
[  1.272] remoteproc remoteproc0: Direct firmware load for \
    qcom/x1e80100/SAMSUNG/galaxy-book4-edge/qcadsp8380.mbn failed with error -2
[  1.275] remoteproc remoteproc0: Direct firmware load for \
    qcom/x1e80100/SAMSUNG/galaxy-book4-edge/qccdsp8380.mbn failed with error -2
[  2.035] msm_dpu ae01000.display-controller: Direct firmware load for \
    qcom/gen70500_sqe.fw failed with error -2
```

Good news:
- **UFS** properly detected and attached ✅
- **Native DTB** loaded correctly ✅
- **Kernel** is Canonical's 7.0.0-22-qcom-x1e (matches the PPA) ✅
- **Dock ethernet** is up with DHCP ✅

Missing firmware (easy to fix — we already extracted it in Chapter 4):
- Samsung ADSP/CDSP blobs (kernel looks in `/SAMSUNG/galaxy-book4-edge/`, not `/samsung/galaxy-book4-edge/` — note uppercase S)
- Adreno GPU `gen70500_sqe.fw`

## 7.14 Pushing the extracted firmware via scp

```bash path=null start=null
# Build host → laptop
cd ~/Documents/Galaxy-Book4-Edge-linux/firmware-stage/lib/firmware
tar -cf - qcom ath12k qca | ssh ubuntu@10.x.x.x \
    "sudo tar -xf - -C /lib/firmware"

# Also create the uppercase SAMSUNG symlink
ssh ubuntu@10.x.x.x \
    "sudo ln -sf /lib/firmware/qcom/x1e80100/samsung \
        /lib/firmware/qcom/x1e80100/SAMSUNG"
```

Then, on the laptop:

```bash path=null start=null
sudo systemctl restart qcom-q6v5-pas.service
sudo dmesg | tail -30
```

**ADSP and CDSP came up.** fastrpc compute devices registered. APR/GPR audio framework initialised. The SSH session was now a fully-functional Linux box on Book4 Edge hardware.

## 7.15 The native keyboard/touchpad puzzle

The user reported:

> **"native keyboard and touchpad not working. the keyboard that it's working is the usb GXT trust keyboard and the USB mouse"**

`cat /proc/interrupts | grep hid-over-i2c` showed the ELAN keyboard was firing **73 IRQs** during a 5-second test — hardware was alive.

`sudo evtest --grab /dev/input/event1` (with `--grab` to prevent GNOME from consuming the events) captured real keystrokes on the native keyboard. So **kernel IRQ → event device → evtest** was working.

The broken link was between **evdev → libinput → GNOME Shell**. Likely a Wayland focus/routing problem on the 14" specifically (since the 16" had been tested extensively by zensanp and worked there). No clean fix was landed in this chapter; the workaround was to use the USB keyboard + mouse for interactive use and SSH for everything else.

## 7.16 End of Chapter 7

By end of Chapter 7:

- jglathe's Ubuntu 24.04 image was running on the Book4 Edge ✅
- SSH access via dock Ethernet ✅
- UFS, display, audio codec, ADSP, CDSP, dock USB, dock Ethernet all working ✅
- Wi-Fi detected but missing board calibration (Chapter 4's known 14"-board-2.bin problem)
- Native keyboard/touchpad visible in kernel but not reaching GNOME (open issue)

But the pull-replug-USB dance was still the bottleneck for any kernel-level change. The user explicitly asked for a better workflow:

> **"i'm tired of pulling usb in and pulling usb, can we use a method where we do all via pxe boot, the image injection, the log sent here or in the NAS specific, i don't care"**

Next chapter: building a PXE boot infrastructure so the USB never needs to be pulled again.
