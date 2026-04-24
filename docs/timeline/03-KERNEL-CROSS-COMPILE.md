# Chapter 3 — Cross-Compiling the Kernel

## 3.1 Workspace layout

After recon, the build host (`user@archlinux`) had the following tree ready:

```
~/Documents/Galaxy-Book4-Edge-linux/
├── build/                      ← outputs land here
│   └── KERNEL_SHA              ← 708b2aeff3e9e014aaf6ec36e3de0e43b7c23aa5
├── src/
│   └── linux/                  ← zensanp clone, branch x1e80100-book4e-6.17-rc4
├── rootfs/                     ← later: ALARM base + modules + firmware
├── firmware-stage/             ← Linux-pathed firmware for packaging
├── iso/                        ← ISO assembly root
└── logs/
    └── kernel-build.log        ← full build output
```

All tooling paths were set in a single env file so the build was reproducible:

```bash path=null start=null
export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-
export KERNEL_SRC=~/Documents/Galaxy-Book4-Edge-linux/src/linux
export OUT=~/Documents/Galaxy-Book4-Edge-linux/build
```

## 3.2 DTS sanity check (the patch that wasn't needed)

The plan called for hand-patching `x1e80100-samsung-galaxy-book4-edge.dts` to add `regulator-always-on;` to the `VREG_WCN_3P3` node — this is the well-known Wi-Fi fix on this board.

Before blindly applying a patch, the agent inspected the DTS first:

```bash path=null start=null
grep -n 'VREG_WCN_3P3' $KERNEL_SRC/arch/arm64/boot/dts/qcom/*.dts*
sed -n '390,400p' $KERNEL_SRC/arch/arm64/boot/dts/qcom/x1e80100-samsung-galaxy-book4-edge.dts
```

Line 397 already contained `regulator-always-on;`. **zensanp had merged the fix upstream into the branch we were on.** The patch step became a no-op. Saved us from applying a duplicate chunk that would either fail or silently reintroduce whitespace diffs.

This is a recurring theme in the project: the zensanp fork is actively maintained and many community workarounds have already been integrated, so we have to check before patching.

## 3.3 Kernel config

The starting point was `defconfig` for aarch64, which gives mainline + Qualcomm base. From there we flipped a curated set of switches:

```bash path=null start=null
cd $KERNEL_SRC
make O=$OUT ARCH=arm64 defconfig

# Turn on must-haves
scripts/config --file $OUT/.config \
    -e CONFIG_SCSI_UFS_QCOM \
    -e CONFIG_SCSI_UFSHCD_PLATFORM \
    -e CONFIG_PHY_QCOM_QMP_UFS \
    -e CONFIG_PHY_QCOM_EUSB2_REPEATER \
    -e CONFIG_PHY_QCOM_QMP_COMBO \
    -e CONFIG_COMMON_CLK_QCOM \
    -e CONFIG_QCOM_RPMHPD \
    -e CONFIG_QCOM_RPMH \
    -e CONFIG_QCOM_PDR_HELPERS \
    -e CONFIG_INTERCONNECT_QCOM \
    -e CONFIG_REMOTEPROC \
    -e CONFIG_QCOM_Q6V5_PAS \
    -e CONFIG_PINCTRL_X1E80100 \
    -m CONFIG_DRM_MSM \
    -e CONFIG_DRM_MSM_DPU \
    -e CONFIG_ATH12K \
    -e CONFIG_NTFS3_FS

# Make sure everything needed for initial rootfs access is BUILT-IN not module
make O=$OUT olddefconfig
```

### 3.3.1 Why UFS had to be built-in

UFS is the laptop's internal storage controller. If `SCSI_UFS_QCOM` were a module, the root filesystem (on `/dev/sdaY`) would be unreachable before the initramfs could load that module — chicken/egg. Built-in (`=y`) guarantees UFS is alive from the moment the kernel takes over the framebuffer.

### 3.3.2 Why MSM/DRM stayed a module

The opposite problem: loading `drm_msm` too early in the boot path (before the EFI framebuffer hand-off) causes the internal panel to go black and never come back. Keeping it as a module lets GRUB → EFI → framebuffer → panel-on chain work, then userspace loads DRM once we can SSH in and recover if anything breaks.

### 3.3.3 Why NTFS3 was built-in

The first-boot firmware-extract helper was supposed to mount `C:` via `ntfs-3g`. For that to run without networking or module dependencies, `NTFS3_FS=y` was required.

## 3.4 The actual build

```bash path=null start=null
# 8-thread build (was over-provisioned; 4 threads is safer on 8GB RAM)
time make O=$OUT ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
    -j$(nproc) Image dtbs modules 2>&1 | tee ~/Documents/Galaxy-Book4-Edge-linux/logs/kernel-build.log
```

Wall-clock: the build completed in well under an hour on the user's Arch host.

Output artefacts:

| File | Size | Notes |
|---|---|---|
| `$OUT/arch/arm64/boot/Image` | **40.8 MB** | Raw aarch64 kernel binary |
| `$OUT/arch/arm64/boot/dts/qcom/x1e80100-samsung-galaxy-book4-edge.dtb` | **161 KB** | Device Tree Blob v17 |
| `$OUT/modroot/lib/modules/6.17.0-rc4-g708b2aeff3e9/` | **78 MB** | 1 410 `.ko` modules, stripped |

`file Image` reported:
```
Image: Linux kernel ARM64 boot executable Image, little-endian, 4K pages
```

KVER string: `6.17.0-rc4-g708b2aeff3e9` — used for mkinitcpio later.

## 3.5 Module install to staging dir

```bash path=null start=null
sudo make O=$OUT ARCH=arm64 INSTALL_MOD_PATH=$OUT/modroot modules_install
sudo make O=$OUT ARCH=arm64 INSTALL_MOD_STRIP=1 INSTALL_MOD_PATH=$OUT/modroot modules_install

cp $OUT/arch/arm64/boot/Image $OUT/
cp $OUT/arch/arm64/boot/dts/qcom/x1e80100-samsung-galaxy-book4-edge.dtb $OUT/book4edge.dtb
```

`INSTALL_MOD_STRIP=1` halved the total module size.

## 3.6 Pause and cross-check

At this point the agent explicitly stopped and showed the user:

```
| File                    | Size    | Details                                     |
|-------------------------|---------|---------------------------------------------|
| Image                   | 40.8 MB | ARM64 boot executable                       |
| book4edge.dtb           | 161 KB  | Device Tree Blob v17, Samsung Galaxy Book4 |
| modroot/lib/modules/... | 78 MB   | 1 410 .ko (stripped)                        |
| KERNEL_SHA              | —       | 708b2aeff3e9...                             |
| kernel-build.log        | —       | full transcript                             |
```

The user's response: *"inspect anything, make double check of what the repo said we should do and verify we are doing all good and not missing anything"* — the cross-validation pass from Chapter 2, §2.4.

## 3.7 Second pass: the 14"/touchpad discovery

When the audit pass resolved the device as 14", the agent switched branches:

```bash path=null start=null
cd $KERNEL_SRC
git fetch --depth=1 origin x1e80100-book4e-14-temp-6.17-rc4
git checkout x1e80100-book4e-14-temp-6.17-rc4
git rev-parse HEAD > $OUT/KERNEL_SHA_14
```

The 14" branch carries a touchpad-specific DTS change. The DTB name changed from `x1e80100-samsung-galaxy-book4-edge.dtb` to `x1e80100-samsung-galaxy-book4-edge-14.dtb`, which was reflected in later GRUB configs.

## 3.8 Display fallback patch confirmation

zensanp's fork already carries the DP link-rate fallback from Launchpad comment #99:

```bash path=null start=null
grep -A5 'max_dp_link_rate' $KERNEL_SRC/drivers/gpu/drm/msm/dp/dp_panel.c
```

Line 194-195 contained the exact `msm_dp_panel->link_info.rate = msm_dp_panel->max_dp_link_rate;` fallback inside an `#if 0 / #else` block. This is the fix that prevents the internal OLED from staying dark when it reports illegal DPCD link rates. Good — no patch needed.

## 3.9 Audit table after build

| README item | Status |
|---|---|
| Keyboard | ✅ in-kernel I²C-HID driver present |
| Touchpad (14") | ✅ specific DTS on 14-temp branch |
| USB-C | ✅ |
| UFS storage | ✅ built-in `SCSI_UFS_QCOM=y` |
| HDMI right-side | ✅ |
| Built-in display (link-rate fallback) | ✅ already merged |
| GPU Adreno | ⚠️ intentionally `status="disabled"` in DTS |
| Touchscreen | ❌ no upstream support |
| Wi-Fi (14") | ✅ ath12k built-in, DTS VREG fix already in branch |
| Bluetooth | ✅ btmgmt helper deferred to rootfs |
| ADSP / CDSP | ✅ firmware available from Windows |
| Audio | ❌ upstream not ready |
| DP altmode | ❌ untested |
| Sleep | ✅ works (doesn't save power) |
| Hibernate | ❌ untested |

## 3.10 Notes for the next attempt

Two things learned here paid dividends in later chapters:

1. **Always `grep` before patching.** The zensanp fork moves fast; half the "apply this patch" instructions in community guides are already merged.
2. **Kernel version strings matter.** `6.17.0-rc4-g708b2aeff3e9` appears in module paths, initramfs hooks, and DKMS later. Pin the SHA in `KERNEL_SHA` and never lose it.

## 3.11 End of Chapter 3

Outputs by end of chapter:

- `~/Documents/Galaxy-Book4-Edge-linux/build/Image` (40.8 MB)
- `~/Documents/Galaxy-Book4-Edge-linux/build/book4edge.dtb` (161 KB)
- `~/Documents/Galaxy-Book4-Edge-linux/build/modroot/lib/modules/6.17.0-rc4-g708b2aeff3e9/` (78 MB, 1 410 .ko)
- `~/Documents/Galaxy-Book4-Edge-linux/build/KERNEL_SHA` (pinned)
- `~/Documents/Galaxy-Book4-Edge-linux/logs/kernel-build.log` (full log for post-mortem)

Next chapter: turning the 342 MB Windows extract into Linux-pathed firmware directories that the kernel can actually find at boot.
