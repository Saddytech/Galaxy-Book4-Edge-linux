# Chapter 9 — The Minimal Initramfs Breakthrough

## 9.1 Strategy pivot

After days of chasing NFS-root failures, kernel panics, and blank screens, the agent proposed a complete strategy change:

> **"let me just build a tiny 984 KB initramfs that contains busybox, a static `/init` script, and nothing else. If the kernel can get to that init, at least we have a shell on the screen. Then we add hardware drivers one module at a time."**

The key insight: **stop trying to boot a full Ubuntu rootfs**. Instead, boot into a single `/init` script running in RAM, prove basic kernel/userspace contract works, and only then add complexity.

This was the **turning point** of the entire project.

## 9.2 Ingredients

For a RAM-only initramfs:

1. `busybox-static` — a single ARM64 binary implementing ~250 UNIX commands (shell, mount, ls, cat, etc.)
2. A minimal `/init` script as PID 1
3. `/dev`, `/proc`, `/sys` mount points
4. Just enough kernel modules to bring up USB and HID so a keyboard works
5. Optional: a telnetd for network-based shell access

## 9.3 Building busybox-static

The build host's package manager had a pre-built static busybox for aarch64. On Ubuntu:

```bash path=null start=null
sudo apt-get install -y busybox-static
# /bin/busybox → small statically-linked ARM64 binary
file /bin/busybox
# ELF 64-bit LSB executable, ARM aarch64, statically linked
```

For the laptop's actual arch (aarch64), the build host used Canonical's `busybox-static` package from `ports.ubuntu.com`.

## 9.4 The `/init` script (v1 — absolutely minimal)

```sh path=null start=null
#!/bin/busybox sh

# Mount pseudo-filesystems
/bin/busybox --install -s /bin
mkdir -p /proc /sys /dev /tmp
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev

# Bring up loopback
ip link set lo up

# Try DHCP on any interface, fall back to static
for iface in $(ls /sys/class/net/ | grep -v lo); do
    udhcpc -i $iface -t 5 -n -q && break
done
if ! ip addr show | grep -q "inet "; then
    ip addr add 192.168.x.x/24 dev eth0 2>/dev/null
fi

# Start services
telnetd -l /bin/sh -p 23 &
httpd -p 8080 -h /tmp &

# Drop to shell on tty0
setsid sh < /dev/tty0 > /dev/tty0 2>&1
```

## 9.5 Packing it up

```bash path=null start=null
mkdir -p /tmp/mini-init/{bin,sbin,etc,proc,sys,dev,tmp,newroot}
cp /usr/aarch64-linux-gnu/bin/busybox /tmp/mini-init/bin/busybox

# Install script
cat > /tmp/mini-init/init <<'EOF'
... (content from 9.4) ...
EOF
chmod +x /tmp/mini-init/init

# Pack as cpio.gz
cd /tmp/mini-init
find . | cpio -H newc -o | gzip -9 > <HOME>/pxe/tftp/initrd-mini.cpio.gz
ls -lh <HOME>/pxe/tftp/initrd-mini.cpio.gz
# 984 K — 250x smaller than the 258 MB Ubuntu initrd
```

## 9.6 Adding a new GRUB menu entry

```grub
menuentry "*** MINIMAL DEBUG ***" {
    linux (tftp)/Image \
        rdinit=/init \
        clk_ignore_unused pd_ignore_unused regulator_ignore_unused \
        earlycon=efifb console=tty0 loglevel=8
    devicetree (tftp)/x1e80100-samsung-galaxy-book4-edge-14.dtb
    initrd (tftp)/initrd-mini.cpio.gz
}

set default=7   # the 8th entry, 0-indexed
```

## 9.7 First boot — major win

User rebooted, UEFI PXE'd, GRUB auto-selected MINIMAL DEBUG, and **a busybox shell rendered on the laptop's internal display**. A photo captured:

```
~ #
~ # uname -r
6.11.0-061100-x1e-generic
~ # ls /sys/class/net
lo
~ # lsmod
Module                  Size  Used by
~ #
```

**First time in the project a shell was visible on the laptop's screen.** Progress! But also clear: only `lo` was up, no `eth0`, no keyboard driver loaded. The agent's analysis:

> **"MAJOR WIN — our init ran! We have a shell on screen (busybox ash). But as you noted: only loopback interface (`lo`) — no eth0: our initramfs has no kernel modules, so `r8152` (USB NIC driver) can't load. Internal keyboard dead: `i2c-hid-of` driver is also a module, not loaded."**

## 9.8 Adding kernel modules

Needed:
- `r8152.ko` — RTL8153 Ethernet on the dock (USB)
- `usbhid.ko`, `hid.ko`, `hid-generic.ko` — USB HID for plain keyboards/mice
- `i2c-hid.ko`, `i2c-hid-of.ko` — internal I²C-HID keyboard
- `dwc3.ko`, `dwc3-qcom.ko`, `xhci-plat-hcd.ko` — USB host controller stack
- `phy-qcom-qmp-usb.ko`, `phy-qcom-qmp-combo.ko`, `phy-qcom-eusb2-repeater.ko` — USB PHYs

Modules were extracted from jglathe's squashfs:

```bash path=null start=null
# Mount the squashfs that contains /lib/modules
sudo mount -t squashfs /mnt/jesp/casper/minimal.squashfs /mnt/sqfs
ls /mnt/sqfs/lib/modules/
# 6.19.0-3-generic (jglathe's kernel)

# Copy only the critical driver subdirs into our initramfs
mkdir -p /tmp/mini-init/lib/modules/6.19.0-3-generic/kernel/drivers/{net/usb,hid,i2c,usb,phy/qualcomm}
cp -r /mnt/sqfs/lib/modules/6.19.0-3-generic/kernel/drivers/net/usb/* \
     /tmp/mini-init/lib/modules/6.19.0-3-generic/kernel/drivers/net/usb/
# ...repeat for hid, i2c, usb, phy
cp /mnt/sqfs/lib/modules/6.19.0-3-generic/modules.{dep,alias,symbols} \
    /tmp/mini-init/lib/modules/6.19.0-3-generic/
```

Total module tree size inside the initramfs: **11 MB**.

## 9.9 The zstd-compression snag

Rebuilt the initramfs (now 7 MB compressed). Booted. The screen showed:

```
[init] loading r8152...
modprobe: can't load module r8152 (kernel/drivers/net/usb/r8152.ko.zst):
    No such file or directory
[init] Loaded modules: (empty)
```

User inferred:

> **"i think the module format is the problem"**

Correct — Ubuntu ships modules as `.ko.zst` (zstd-compressed), and busybox's built-in `modprobe` doesn't decompress on the fly.

## 9.10 Decompressing all 478 modules

```bash path=null start=null
# One pass to decompress .ko.zst → .ko
cd /tmp/mini-init/lib/modules/6.19.0-3-generic
find . -name '*.ko.zst' | while read f; do
    out="${f%.zst}"
    zstd -d -f -q -o "$out" "$f" && rm "$f"
done

# Fix modules.dep references (it lists .ko.zst paths)
sed -i 's/\.ko\.zst/\.ko/g' modules.dep modules.alias
```

This worked. All 478 modules were now uncompressed.

Re-packed initramfs: **30 MB** (larger but still tiny vs 258 MB).

## 9.11 Second boot — modules load but nothing binds

Photos from the boot showed:

```
[init] Phase 1: USB PHYs + DWC3 + xHCI...
insmod phy-qcom-qmp-combo.ko
insmod dwc3.ko
insmod dwc3-qcom.ko
insmod xhci-plat-hcd.ko

[init] Phase 2: USB clients...
insmod usbhid.ko
insmod r8152.ko

[init] Loaded modules:
Module                  Size  Used by
r8152                 135168  0           ← zero users
hid_generic            12288  0           ← zero users
i2c_hid_of             12288  0           ← zero users
dwc3_qcom              28672  0           ← driver loaded but not bound
xhci_plat_hcd          24576  0           ← same
usbhid                 86016  0           ← same
```

**All modules loaded, but `Used by` was `0` for every single device driver.** Drivers were in memory but hadn't bound to hardware — which meant the **bus controllers** had never produced children.

## 9.12 The SCMI dead-end

Scrolling dmesg revealed:

```
[ 14.820] geni_se_qup 8c0000.geniqup: Err getting clks -517    (EPROBE_DEFER)
[ 14.820] geni_se_qup ac0000.geniqup: Err getting clks -517
[ 14.822] geni_se_qup bc0000.geniqup: Err getting clks -517
[ 14.823] arm-smmu 3da0000.iommu: probe with driver arm-smmu failed with error -110
[ 14.824] arm-scmi arm-scmi.0.auto: failed to setup channel for protocol:0x10
```

**Root cause identified:** SCMI (System Control & Management Interface) protocol 0x10 (Power Domain) doesn't come up → clock providers aren't registered → SMMU can't probe → USB controllers can't allocate IOMMU domains → DWC3 never binds → USB devices never enumerate.

This is a **platform-level firmware issue**, not something a kernel module load can fix.

## 9.13 The EL2 DTB experiment

zensanp's fork shipped an alternative device-tree variant `x1e80100-samsung-galaxy-book4-edge-14-el2.dtb` that enables SMMU-500 at `0x15000000` (normally reserved by firmware) and adds iommu-map entries.

The theory: under hypervisor-level execution (EL2), the OS could directly own the SMMU and bypass SCMI for power-domain calls.

```grub
menuentry "*** MINIMAL DEBUG EL2 ***" {
    linux (tftp)/Image rdinit=/init ...
    devicetree (tftp)/x1e80100-samsung-galaxy-book4-edge-14-el2.dtb
    initrd (tftp)/initrd-mini.cpio.gz
}
set default=8
```

User rebooted. Result:

> **"el2 variant stuck immediately with a white cursor on top left and nothing else"**

Complete hang before any kernel output. Diagnosis: UEFI hands off at EL1 on Samsung firmware, so the EL2 DTB's assumption of direct SMMU ownership is wrong — the SMMU is still firmware-owned and enabling it at the wrong EL locks the system.

Reverted to the standard DTB + MINIMAL DEBUG entry.

## 9.14 The Windows-side consult

The user suggested:

> **"i don't know if it can be useful but we can still log in windows and get info from there if we can get something useful"**

Plan: boot Windows (still installed), run a PowerShell enumeration script to compare how Windows talks to the same hardware. Specifically the goal was to identify:

- Exact Qualcomm firmware files loaded by Windows
- USB topology (which DWC3 instance the USB-A port uses)
- Hypervisor state (does Windows itself run at EL1 under a Samsung-provided hypervisor?)

The PowerShell recon ran, yielded `Book4Edge-Info.txt` (~80 KB), and was shipped back over SSH. The key finding: Windows 11 ARM on this laptop runs **at EL1 under Samsung's firmware hypervisor**, which explained why the EL2 DTB variant couldn't boot — Linux, same as Windows, isn't given EL2 by the firmware.

## 9.15 The SSH-from-Windows lifeline

Because the Windows session had SSH server enabled and the laptop was cabled directly to the build host's `enp4s0` (the same PXE cable), the user made a key observation:

> **"ok, windows is logged and i have ssh open, connected directly via ethernet to this pc as for the pxe"**

For the rest of the project, the laptop could dual-purpose: when Windows-booted, it was an **SSH client into the build host** and a target for PowerShell recon; when Linux-booted via PXE, it was **controlled by the build host** over TFTP/NFS. Either way, no USB pulling required.

## 9.16 End of Chapter 9

Outputs:

- `<HOME>/pxe/tftp/initrd-mini.cpio.gz` — 30 MB initramfs with busybox + 478 uncompressed kernel modules
- Two GRUB entries in `grub.cfg`: MINIMAL DEBUG (standard DTB) and MINIMAL DEBUG EL2 (failed)
- A **visible shell on the laptop's screen** for the first time in the project
- Confirmation that the USB + keyboard chain fails due to **SCMI / SMMU firmware-ownership** issues, not missing drivers
- Windows SSH channel for PowerShell recon

This was enough proof-of-life to pivot the project to a completely new goal: **actually install Ubuntu to the internal UFS disk**, boot from there natively (where UEFI is cooperative with the SMMU), and then iterate on hardware support from a stable installed system.

That's Chapter 10.
