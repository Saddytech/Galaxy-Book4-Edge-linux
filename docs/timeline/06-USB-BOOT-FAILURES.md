# Chapter 6 — USB Boot Failures

## 6.1 Symptom inventory

First boot attempt on the Book4 Edge with our Arch ISO:

- GRUB menu appears ✓
- Kernel loads (screen shows the normal boot spew)
- After ~12 seconds the screen goes **black**
- No TTY is reachable via Ctrl+Alt+F2/F3
- No device appears on the LAN at all
- sshd (baked into the rootfs) is unreachable
- Power light is on, fan is audible, disk activity LED occasionally blinks

The black screen after systemd-udevd is a well-known X1E80100 gotcha — the MSM DRM driver fails to claim the EFI framebuffer cleanly and neither re-initialises the panel nor gives up gracefully. But even with the display dead, the kernel should be running underneath, and our rootfs has SSH enabled on the `ssh.service` systemd unit.

**So why isn't anything on the network?**

## 6.2 Log capture via USB pull-replug

The only diagnostic channel available was the USB itself. The rootfs had been configured with a systemd unit that wrote `/var/log/book4edge-userspace.log` and `/var/log/book4edge-dmesg.log` every 10 seconds to a RAM-backed path, mirrored to the ESP's FAT32 partition so it survived a hard shutdown.

The agonising workflow that emerged:

1. Force-shutdown the laptop (long-press power)
2. Unplug USB from the laptop
3. Bring USB back to the build PC
4. Mount it read-only
5. Read the logs
6. Form a hypothesis
7. Update something in the rootfs (or the GRUB entry)
8. Unmount, plug into laptop, boot, repeat

Each cycle took 5–10 minutes. Over the course of this chapter and the next, the user performed **at least 15 of these USB dances**, which is what eventually motivated the pivot to PXE boot in Chapter 8.

## 6.3 The smoking gun: `a800000.usb` never probes

After multiple log pulls, the pattern became undeniable. Every boot snapshot showed the same set of errors:

```text path=null start=null
[   10.76] qcom-rpmhpd sync_state pending: 32300000.remoteproc
[   11.24] platform a800000.usb: Fixed dependency cycle(s) with /soc00/phy@fdf000
[   11.28] qcom-qmp-combo-phy fdf000.phy: unable to determine orientation & mode from data-lanes
[   11.30] platform a800000.usb: Fixed dependency cycle(s) with /palc-glink/connector@2
[   14.82] dwc3 a800000.usb: failed to initialize core: -110 (ETIMEDOUT)
```

The X1E80100 has **three USB controllers**:

| Controller | Bus(es) | Status |
|---|---|---|
| `a400000.usb` | 1 / 2 | ✅ working; boot stick enumerated here |
| `a600000.usb` | 3 / 4 | ✅ working; internal peripherals |
| **`a800000.usb`** | 5 / 6 | ❌ **PHY timeout, never probes** |

The `a800000` controller is the one wired to both **left-front USB-C** and (on the 14" SKU) the **right-side USB-A**. Without it, a user only has:

- One working USB-C (the other left port on `a400000`)
- One working USB-A (the other on `a600000`, internal fingerprint/camera)

## 6.4 The dock debacle

The user plugged in a USB-C dock that had an RTL8153 Ethernet chip onboard, hoping to at least get networking to the laptop over the dock's built-in Ethernet. This failed repeatedly:

1. **First attempt**: Dock plugged into left USB-C. Boot USB also left USB-C. Neither enumerated because the user had inserted the dock into the a800000 port.
2. **Second attempt**: User swapped docks. Same result — still the dead port.
3. **Third attempt**: "I've just connected an USB ethernet dongle to this machine, can I use it on the galaxy book? analyze it and tell me." The dongle turned out to be an ASUS 2.5GbE with RTL8156 chipset — kernel has r8152 driver built in. ✅ Driver was fine. ❌ Plugged into right USB-A which is also on a800000. Still dead.

At this point the agent finally got clear evidence that **only one USB port on the laptop works in Linux**: the left USB-C on `a400000`. And that one was currently occupied by the boot stick.

The solution was obvious but took a while to arrive: **use the dock as a hub, plugged into the working port, with the boot USB cascaded through the dock's internal hub**. That way both the boot stick AND the dock's Ethernet share the one working controller.

## 6.5 The ADSP VBUS blip (a second independent problem)

Even on the good port, intermittent USB drop-outs occurred. Correlation with dmesg:

```text path=null start=null
[    5.82] pd-mapper.service: Deactivated … Scheduled restart
[    5.90] remoteproc remoteproc0: attach firmware qcom/x1e80100/samsung/galaxy-book4-edge/adsp.mbn
[    6.05] usb 1-2: USB disconnect, device number 2
[    6.18] usb 1-2: new high-speed USB device number 3 using xhci_hcd
```

When ADSP (`adsp.mbn`) loaded, it caused a **brief VBUS glitch on the working USB-C port**. The boot stick re-enumerated, but sometimes it re-enumerated as `/dev/sdX+1` mid-initramfs, breaking the root= label lookup.

Two strategies emerged:
1. **Delay ADSP load** until after `switch_root` completes (cuts risk of disconnect during critical path)
2. **Use USB label, not block-device name** in `root=` parameter (makes post-renumber recovery possible)

Both were implemented in the GRUB config and the custom `book4live` initramfs hook.

## 6.6 zensanp issues #6 and #3: the turning point

At the height of the frustration, the user sent:

> **"you have to read this https://github.com/zensanp/linux-book4-edge/issues/6 https://github.com/zensanp/linux-book4-edge/issues/3"**

### Issue #6 — zensanp's own warning

A quote from zensanp on Feb 10 (before our project started):

> *"the kernel in the 30-1 release here was not built using Ubuntu's defconfig (just the included defconfig), and probably needs things like squashfs_xz enabled, or else it's not able to mount the fs for Ubuntu's ISOs… I've otherwise only booted successfully via **IMG files that all had ext4 partitions**."*

The author of the kernel fork **himself** hadn't been able to reliably live-boot his own kernel with a squashfs rootfs. Proven-working deployments on Book4 Edge were:
- **jglathe's pre-built Ubuntu 24.04 IMG** — raw disk images with ext4 partitions
- A custom image with an **ext4 rootfs partition** (not squashfs)

We had built exactly the thing zensanp said he couldn't reliably do.

### Issue #3 — Wi-Fi board-2.bin divergence

- `@edwardak` (16" Book4) got Wi-Fi working with `firmware-atheros 20251111-1` sha1 `b75e8a31…`
- We had used the upstream linux-firmware.git file with sha1 `edb8a78f…` — **different file, wouldn't work on 14"**
- zensanp himself (14") couldn't get Wi-Fi working even with the Debian file

## 6.7 The hard decision

At this point the agent laid out the pivot explicitly:

> **Rather than keep fighting squashfs + arch + dock port issues with an unknown stack, let me download jglathe's V7 image** (known working on X1E80100 laptops via USB).

Three reasons:

1. zensanp himself boots that way
2. jglathe's image has been tested on **multiple** X1E80100 laptops
3. It uses ext4 instead of squashfs — one less thing that can go wrong

The user agreed:

> **"they are using ubuntu, can't we simply use what they do then once on ubuntu we pull out all we need?"**

## 6.8 Controllers and ports reference card

This info was distilled into a reference that lived in `DEVICE-INFO.md`:

```text path=null start=null
X1E80100 USB controllers on the Samsung Galaxy Book4 Edge:

  a400000.usb  ──── Bus 1/2  ✅ WORKS  ← left USB-C (back)  ← BOOT STICK HERE
  a600000.usb  ──── Bus 3/4  ✅ WORKS  ← internal peripherals (cam, fp)
  a800000.usb  ──── Bus 5/6  ❌ DEAD   ← left USB-C (front) + right USB-A

Workaround for Ethernet:
  dock's USB-C cable → left USB-C (back) [a400000]
  dock's Ethernet    → router
  dock's USB-A ports → boot stick (cascaded through dock's internal hub)
```

## 6.9 Why the a800000 is dead

At this point we had no root cause for why the third USB controller doesn't probe. Candidate hypotheses:

1. **DTS issue** — the node is declared but some required clock/phy/pwr reference is wrong or missing for the 14" variant
2. **Firmware dependency race** — ADSP isn't up by the time the DWC3 probe tries, leading to EPROBE_DEFER → timeout
3. **Hardware quirk** — Samsung wired this port differently on 14" and the current DTS doesn't match
4. **SCMI** — some clocks routed through Arm SCMI firmware that doesn't answer

The issue would resurface later in Chapter 9 when our minimal initramfs exposed the full failure chain (`arm-smmu: probe failed with error -110` + `arm-scmi ... failed to setup channel for protocol:0x10`). It was never fully solved in this project — the workaround (don't use `a800000`) became the policy.

## 6.10 Psychological toll

The user's messages during this chapter are worth preserving as a tone-check:

> **"pc is on live session, ssh is on"** — relief mid-chaos
> **"i'm done, i'm fucking connected the ethernet to this machine directly"** — breaking point that drove the PXE pivot
> **"i'm tired of pulling usb in and pulling usb"** — explicit plea for a no-USB workflow

The agent's failure modes here were mainly:
- **Speculating without evidence** about which DTB to try next rather than reading logs first
- **Proposing further USB attempts** after it was clear the port was dead
- **Introducing unrelated changes** (firmware tweaks) between USB pulls, making it hard to isolate what actually changed

Lessons etched for the remaining chapters:

1. **Read logs before making changes.**
2. **Change one thing per cycle.**
3. **If USB pulling is the bottleneck, replace USB with a better channel (PXE).**

## 6.11 End of Chapter 6

Outputs:

- A confirmed map of working vs dead USB controllers on this laptop
- A clear decision: pivot away from custom Arch ARM ISO, use jglathe's Ubuntu image
- An updated `DEVICE-INFO.md` with the controller/port mapping
- Multiple failed boot attempts preserved in the Windows backup's `book4edge-userspace.log`

Next: downloading jglathe's V7 image, modifying it to include our DTB, and flashing it to a new USB.
