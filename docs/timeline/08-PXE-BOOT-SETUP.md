# Chapter 8 — PXE Boot Infrastructure

## 8.1 The goal

Replace the "pull USB → mount → inspect → edit → replug → reboot" loop with a **network-boot workflow** where:

- The laptop boots via UEFI IPv4 PXE
- Kernel, initramfs, DTB come from TFTP on the build host
- Rootfs comes via NFS over the same cable
- Editing the rootfs on the build host + rebooting the laptop ≈ "apply change"

Iteration time target: **under 1 minute per round trip**, entirely headless.

## 8.2 What already existed

`ls <HOME>/pxe/` on the build host revealed pre-existing PXE infrastructure from an earlier experiment:

```
<HOME>/pxe/
├── dnsmasq.conf
├── http/           (empty)
├── tftp/
│   ├── grub/
│   │   └── grub.cfg
│   ├── Image                (Custom 6.17-rc4 kernel, backup from earlier build)
│   ├── initrd               (258 MB — Fedora-style)
│   ├── initrd-backup-*.img
│   └── x1e80100-samsung-galaxy-book4-edge-14.dtb
└── logs/
    └── boot-monitor.log
```

The config pointed at `10.x.x.x` (an older IP of the build host) and was set to boot a Fedora-flavoured rootfs via HTTP on port 8080 — which was empty. The machine now lived at `10.x.x.x`, network interface changed from `enp4s0` to `eno1`.

Rather than rebuild from scratch, the agent **patched the existing config** to match current reality.

## 8.3 Installing dnsmasq + NFS

On the build host (Arch, later Ubuntu):

```bash path=null start=null
sudo pacman -S --noconfirm dnsmasq nfs-utils tftp-hpa
sudo systemctl enable --now nfs-server
```

Initial firewall rules were a no-op (iptables INPUT/OUTPUT both ACCEPT). Good.

## 8.4 The `dnsmasq.conf` (first version — regular DHCP mode)

```conf path=null start=null
interface=eno1
bind-interfaces

# DHCP pool for the LAN, avoiding router's range
dhcp-range=10.x.x.x,10.x.x.x,12h
dhcp-authoritative

# Static lease for the dock's RTL8153 Ethernet
dhcp-host=XX:XX:XX:XX:XX:XX,10.x.x.x,book4edge

# Static lease for the ASUS 2.5GbE dongle
dhcp-host=YY:YY:YY:YY:YY:YY,10.x.x.x,book4edge-dongle

# PXE boot parameters per client arch
dhcp-match=set:aarch64-uefi,option:client-arch,11
dhcp-boot=tag:aarch64-uefi,grub/grubnetaa64.efi

# TFTP server
enable-tftp
tftp-root=<HOME>/pxe/tftp

# Logging
log-dhcp
log-facility=<HOME>/pxe/dnsmasq.log
```

## 8.5 GRUB netboot EFI

The laptop's UEFI PXE downloads `grubnetaa64.efi` over TFTP, then the EFI runs `grub.cfg` from the same TFTP tree.

```bash path=null start=null
grub-mknetdir --net-directory=<HOME>/pxe/tftp --subdir=grub
```

`grub.cfg` (excerpt):

```grub
set default=0
set timeout=10

menuentry "Book4 Edge — 6.17 kernel + Ubuntu NFS rootfs" {
    linux (tftp)/Image \
        root=/dev/nfs \
        nfsroot=192.168.x.x:/srv/nfs/book4-rootfs,vers=4,tcp \
        ip=dhcp \
        rw \
        earlycon=efifb console=tty0 \
        clk_ignore_unused pd_ignore_unused regulator_ignore_unused
    devicetree (tftp)/x1e80100-samsung-galaxy-book4-edge-14.dtb
    initrd (tftp)/initrd
}

menuentry "Book4 Edge — Ubuntu 6.11 kernel (fallback)" {
    linux (tftp)/vmlinuz-6.11.0-061100-x1e-generic \
        root=/dev/nfs nfsroot=192.168.x.x:/srv/nfs/book4-rootfs,vers=4,tcp \
        ip=dhcp rw
    devicetree (tftp)/x1e80100-samsung-galaxy-book4-edge-14.dtb
    initrd (tftp)/initrd.img-6.11.0-061100-x1e-generic
}
```

## 8.6 NFS export

```bash path=null start=null
# Copy the Ubuntu rootfs from our earlier jglathe loop-mount
sudo mkdir -p /srv/nfs/book4-rootfs
sudo rsync -aAXH --numeric-ids \
    /mnt/jroot/ /srv/nfs/book4-rootfs/

# Export with NFSv4
echo "/srv/nfs/book4-rootfs *(rw,sync,no_root_squash,no_subtree_check,fsid=0)" | \
    sudo tee -a /etc/exports
sudo exportfs -rav
sudo systemctl restart nfs-server
```

## 8.7 First PXE attempt: `PXE boot doesn't find anything`

User clicked "IPv4 PXE Boot" from the UEFI boot menu. Agent watched:

```bash path=null start=null
sudo tcpdump -i eno1 -nn port 67 or port 68 or port 69
# [timeout] no packets
```

Zero DHCP discover packets reaching dnsmasq. Yet the laptop's PXE banner scrolled on the screen (user verified).

## 8.8 Router DHCP winning the race

Tentative diagnosis: the laptop's PXE client broadcasts DHCPDISCOVER; **both** the LAN router (on `10.x.x.x`) and our dnsmasq receive it; the router replies first with a lease but without PXE boot options; laptop accepts that lease and has no way to fetch the bootfile.

Attempt 1 — switch dnsmasq to **proxy-DHCP** mode:

```conf path=null start=null
dhcp-range=10.x.x.x,proxy
pxe-service=aarch64-uefi,"Book4 Edge PXE",grub/grubnetaa64.efi
```

Restarted dnsmasq. Retried PXE. tcpdump still silent on DHCPDISCOVER specifically. Proxy mode only responds to packets with vendor-class `PXEClient` — Samsung's UEFI apparently doesn't set that correctly.

## 8.9 Evidence from the wire

Adding a wider tcpdump filter revealed traffic the summaries had been hiding:

```text path=null start=null
15:51:36.123 IP 0.0.0.0.68 > 255.255.255.255.67: BOOTP/DHCP, Request length 347
15:51:40.456 IP 0.0.0.0.68 > 255.255.255.255.67: BOOTP/DHCP, Request length 347
```

Those 347-byte packets from `0.0.0.0:68` were **real PXE DHCPDISCOVERs** from the dock's MAC (`XX:XX:XX:XX:XX:XX…`). dnsmasq wasn't answering because proxy-DHCP without a correctly-tagged request is a no-op.

Also noticed: ARP and NetBIOS chatter from `10.x.x.x` (the dock's "Windows leased" IP from when the laptop was booted into Windows on the dock earlier). Not relevant, just noise.

## 8.10 Going back to authoritative regular DHCP

```conf path=null start=null
# Remove proxy directive
# dhcp-range=10.x.x.x,proxy

# Restore regular DHCP
dhcp-range=10.x.x.x,10.x.x.x,12h
dhcp-authoritative
```

Restarted. tcpdump immediately showed `DHCPOFFER` replies. But laptop booting went further, then dropped to Windows (Boot Priority fallback).

## 8.11 The `MSFT 5.0` finding

dnsmasq's log showed:

```
dhcp-client: BOOK-7F3TQMDS4A vendor-class=MSFT 5.0 requested boot file
```

Wait — `MSFT 5.0`? That's **Windows**, not a PXE client. Hypothesis: after PXE timed out on IPv4, the laptop tried IPv6 (also timed out), then fell back to the next boot priority entry — Windows Boot Manager. Windows booted, renewed its DHCP lease from the router (not us), and that's what our tcpdump was capturing.

User confirmed:

> **"the problem is much more simpler, it tried to boot via pxe boot but nothing arrived, so it cycled to ipv6 and then to windows. you only capture packet when it goes on windows"**

Exactly right. The PXE DHCPDISCOVER was happening in a narrow 10-second window before the laptop timed out; dnsmasq's restart had cleared its response before a fresh attempt.

## 8.12 Diagnosing the router race

With dnsmasq now responsive and authoritative, a fresh PXE attempt:

```
dnsmasq-dhcp: DHCPDISCOVER(eno1) XX:XX:XX:XX:XX:XX
dnsmasq-dhcp: DHCPOFFER(eno1) 10.x.x.x XX:XX:XX:XX:XX:XX
dnsmasq-dhcp: DHCPDISCOVER(eno1) XX:XX:XX:XX:XX:XX
dnsmasq-dhcp: DHCPOFFER(eno1) 10.x.x.x XX:XX:XX:XX:XX:XX
```

Four OFFERs, zero REQUESTs. The laptop **accepted the router's offer** (arriving first) and never sent a DHCPREQUEST to us — so TFTP never started.

## 8.13 User bypasses the problem

User cut through the debate by physically moving the Ethernet cable:

> **"i'm done, i'm fucking connected the ethernet to this machine directly"**

The dock's RJ45 was now plugged **directly** into the build host's second Ethernet port `enp4s0`, and from the build host's `enp4s0` an Ethernet cable ran to the Book4 Edge's dock. **No router in the middle** = no race, no rogue DHCP offers.

Perfect isolated subnet.

## 8.14 Creating an isolated `192.168.x.x/24` subnet

```bash path=null start=null
# Assign static IP to enp4s0
sudo ip addr flush dev enp4s0
sudo ip addr add 192.168.x.x/24 dev enp4s0
sudo ip link set enp4s0 up

# Permanent config (systemd-networkd):
sudo tee /etc/systemd/network/10-book4edge.network <<EOF
[Match]
Name=enp4s0

[Network]
Address=192.168.x.x/24
EOF
```

## 8.15 NetworkManager interference

NetworkManager immediately tried to DHCP on `enp4s0` because it saw a fresh Ethernet cable:

```text path=null start=null
nmcli device status
DEVICE    TYPE      STATE                                  CONNECTION
enp4s0    ethernet  connecting (getting IP configuration)  Wired connection 2
```

That DHCP client was **wiping our static IP** every few seconds. Fix:

```bash path=null start=null
# Mark enp4s0 as unmanaged for NetworkManager
sudo tee /etc/NetworkManager/conf.d/10-book4edge-unmanage.conf <<EOF
[keyfile]
unmanaged-devices=interface-name:enp4s0
EOF
sudo systemctl restart NetworkManager
```

## 8.16 dnsmasq on the isolated subnet

Updated `dnsmasq.conf`:

```conf path=null start=null
interface=enp4s0
bind-dynamic             # modern equivalent of bind-interfaces; keeps raw socket
port=0                   # disable DNS (don't compete with system resolver)
dhcp-range=192.168.x.x,192.168.x.x,12h
dhcp-authoritative
dhcp-option=option:router,192.168.x.x
dhcp-host=XX:XX:XX:XX:XX:XX,192.168.x.x,book4edge
dhcp-match=set:aarch64-uefi,option:client-arch,11
dhcp-boot=tag:aarch64-uefi,grub/grubnetaa64.efi
enable-tftp
tftp-root=<HOME>/pxe/tftp
log-dhcp
log-facility=<HOME>/pxe/dnsmasq.log
```

## 8.17 The `dnsmasq isn't responding` mystery

Restart dnsmasq. User clicks PXE. tcpdump sees DHCPDISCOVER. **dnsmasq logs nothing, sends nothing.**

Investigation:

```bash path=null start=null
sudo ss -tulpn | grep dnsmasq
# UDP 0.0.0.0:67, UDP 0.0.0.0:69 — bound but not responding
```

Root cause: `bind-interfaces` prevents dnsmasq from using the **raw socket** it needs to reply to broadcast DHCP packets. Must remove `bind-interfaces` (use only `bind-dynamic` OR nothing at all).

After removing the directive and restarting:

```text path=null start=null
dnsmasq-dhcp: DHCPDISCOVER(enp4s0) XX:XX:XX:XX:XX:XX 
dnsmasq-dhcp: DHCPOFFER(enp4s0) 192.168.x.x XX:XX:XX:XX:XX:XX book4edge
dnsmasq-dhcp: DHCPREQUEST(enp4s0) 192.168.x.x XX:XX:XX:XX:XX:XX book4edge
dnsmasq-dhcp: DHCPACK(enp4s0) 192.168.x.x XX:XX:XX:XX:XX:XX book4edge
dnsmasq-tftp: sent <HOME>/pxe/tftp/grub/grubnetaa64.efi to 192.168.x.x
```

**PXE is working.** TFTP served grubnetaa64.efi. A few seconds later, kernel + initrd + DTB transfers followed.

## 8.18 Permissions speed bump

`<HOME>/` has mode `0700`, so dnsmasq's user (`dnsmasq`, non-root) couldn't read the TFTP tree. User flagged:

> **"kill it and fix permission!"**

Quickest fix: run dnsmasq as root via `user=root` in the config. This is a localhost-only dev setup, not Internet-facing, so the security impact is tolerable:

```conf path=null start=null
user=root
group=root
```

Long-term fix (not adopted in this project): move TFTP tree to `/srv/tftp/` with mode 0755.

## 8.19 End-to-end PXE handshake working

Final successful boot sequence captured in dnsmasq.log:

```
18:45:12 dhcp-discover eno1 XX:XX:XX:XX:XX:XX (aarch64-uefi)
18:45:12 dhcp-offer    enp4s0 192.168.x.x book4edge
18:45:12 dhcp-request  enp4s0 XX:XX:XX:XX:XX:XX
18:45:12 dhcp-ack      enp4s0 192.168.x.x book4edge
18:45:13 tftp sent grub/grubnetaa64.efi to 192.168.x.x
18:45:15 tftp sent grub/grub.cfg to 192.168.x.x
18:45:16 tftp sent grub/x86_64-efi/... to 192.168.x.x   (ignored; we're aarch64)
18:45:17 tftp sent Image to 192.168.x.x           (22 MB, 0.9 s)
18:45:18 tftp sent x1e80100-samsung-galaxy-book4-edge-14.dtb to 192.168.x.x
18:45:19 tftp sent initrd to 192.168.x.x          (258 MB, 12 s)
18:45:32 <kernel log silent; boot proceeding>
```

But the laptop still went dark and never SSH'd back in.

The PXE **download** was working; the NFS **rootfs** was failing to mount. That puzzle led directly into Chapter 9.

## 8.20 Screen captured via phone photo (first time)

Since the network channel wasn't yielding logs, the user started **photographing the laptop screen with their Samsung phone** and sending the images. A typical picture showed:

```
[    1.037] NFS-ROOT: mount failed: no such device
[    1.115] VFS: Unable to mount root fs via NFS
[    1.120] Kernel panic - not syncing: VFS: Unable to mount root fs on 0:ff
```

Clear evidence: the initramfs was missing NFS-root support (`nfs_root.ko` not built in, `CONFIG_ROOT_NFS` not enabled, or rpcbind missing inside initramfs).

## 8.21 NFS-root retrofit

Quick fix attempted — inject NFS into Ubuntu's existing initramfs:

```bash path=null start=null
sudo mkdir -p /srv/nfs/book4-rootfs/etc/initramfs-tools/modules.d
echo -e "nfs\nnfsv4\nrpcsec_gss_krb5" | sudo tee \
    /srv/nfs/book4-rootfs/etc/initramfs-tools/modules.d/nfs.conf

# Inside NFS rootfs (mounted locally on build host)
sudo chroot /srv/nfs/book4-rootfs /usr/sbin/update-initramfs -u
```

The `update-initramfs` script wasn't in the default PATH inside chroot. Full path `/usr/sbin/update-initramfs` worked after reinstalling `initramfs-tools`:

```bash path=null start=null
sudo chroot /srv/nfs/book4-rootfs apt-get install --reinstall -y initramfs-tools
sudo chroot /srv/nfs/book4-rootfs update-initramfs -u -k 6.11.0-061100-x1e-generic
```

## 8.22 The full-rootfs-over-NFS rabbit hole

NFS-root with a large rootfs (14.2 GB) boots slowly over a gigabit cable (reading everything the kernel asks for takes 2+ minutes just for the first read burst). Worse, Ubuntu's initramfs was fragile — any missing module in the NFS stack caused immediate kernel panic.

This is what motivated the **minimal RAM-only initramfs approach** in Chapter 9 — boot something tiny that can establish a known-good shell, and skip NFS-root entirely for initial debugging.

## 8.23 End of Chapter 8

By end of Chapter 8:

- A working PXE server on `enp4s0` at `192.168.x.x/24`
- dnsmasq serving DHCP, TFTP, authoritative responses only on the isolated subnet
- Static lease for the dock's MAC `XX:XX:XX:XX:XX:XX…` → `192.168.x.x`
- TFTP tree at `<HOME>/pxe/tftp/` with kernel, initrd, DTB, grub EFI
- NFS export of the modified Ubuntu rootfs at `/srv/nfs/book4-rootfs`
- **NetworkManager taught to leave `enp4s0` alone**
- `bind-dynamic` in dnsmasq (not `bind-interfaces`) for raw-socket DHCP replies

Still not booting to userspace reliably — NFS-root + big initramfs was fragile. But the USB was now out of the equation. Next chapter: the minimal initramfs breakthrough that finally got a visible shell on the laptop's screen.
