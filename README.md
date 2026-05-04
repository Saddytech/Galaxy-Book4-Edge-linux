# Samsung Galaxy Book4 Edge — Linux Research & Support

[![License: GPL v2](https://img.shields.io/badge/License-GPL%20v2-blue.svg)](LICENSE)
[![Platform: Snapdragon X Elite](https://img.shields.io/badge/Platform-Snapdragon%20X%20Elite-orange.svg)](https://www.qualcomm.com/products/mobile/snapdragon/pc-processors/snapdragon-x-elite)
[![Status: Experimental](https://img.shields.io/badge/Status-Experimental-red.svg)](#disclaimer)

This repository serves as the central hub for the reverse-engineering efforts and Linux hardware support for the **Samsung Galaxy Book4 Edge** (14" and 16" models). 

By reverse-engineering the **ENE KB9058 Embedded Controller (EC)** and the **SABI v4 protocol**, we have enabled critical features like battery reporting, thermal management, and reliable booting on this bleeding-edge ARM64 platform.

📺 **[Watch the full journey on YouTube](https://youtu.be/V2DxY_PqLBg)**

---

## 📊 Hardware Support Status

| Feature | Status | Notes |
| :--- | :--- | :--- |
| **Boot (UFS)** | ✅ Working | Mainline kernel `7.0.0-22-qcom-x1e` |
| **Battery Reporting** | ✅ Working | Custom `samsung_galaxybook_battery` module |
| **Wi-Fi 7 / BT** | ✅ Working | via `ath12k` + Samsung firmware |
| **Display** | ✅ Working | Internal OLED with DP link-rate fallback |
| **USB-C / AltMode** | ✅ Working | Requires rebind recovery pattern |
| **Audio (Codec)** | ✅ Working | Bound via SoundWire (WCD938x) |
| **Keyboard** | ⚠️ Partial | Native HID events reach kernel; Wayland routing issues |
| **Touchpad** | ⚠️ Partial | Patched I2C address `0xd1`; needs DTS polish |
| **Fan Control** | ⚠️ Partial | Basic zone control works; SABI modes unresolved |
| **Speakers** | ❌ Broken | WSA884x SoundWire needs pinctrl patches |
| **GPU (Adreno)** | ❌ Broken | Needs Mesa ≥ 25.3.3 and firmware translation |
| **Camera/Fingerprint** | ❌ Broken | No upstream driver support yet |

---

## 🚀 Quick Start

### 1. Using Pre-built "Bootkits" (ISOs)
The easiest way to get started is by using our pre-patched Ubuntu ISOs, which include all the drivers and configurations documented here.
*   **[Download Latest Release](../../releases)**
*   **Note:** These images are provided as research snapshots.

### 2. Manual Driver Installation
If you are already running Linux on your Book4 Edge, you can install the battery driver manually:
```bash
cd driver
make
sudo make install
sudo systemctl enable --now samsung-galaxybook-battery.service
```

---

## 📂 Repository Organization

*   **[`driver/`](./driver/)**: The core `samsung_galaxybook_battery` kernel module (GPL v2).
*   **[`tools/`](./tools/)**: Python scripts for EC communication, fan control, and stress testing.
*   **[`docs/`](./docs/)**: 
    *   **[`journey/`](./docs/journey/)**: Deep-dive reverse engineering notes (Ghidra, DSDT, I2C).
    *   **[`timeline/`](./docs/timeline/)**: A day-by-day account of the project's breakthroughs and pivots.

---

## 🛠️ Technical Achievements

*   **ENE KB9058 RE**: Documented the I2C Mailbox wire protocol for the first time.
*   **Battery Driver**: A from-scratch implementation speaking directly to the EC to bypass broken ACPI methods.
*   **PXE Boot Infrastructure**: Custom network-boot setup to iterate kernels without relying on fragile USB-C controllers.

---

## ❤️ Credits & Acknowledgements

This project stands on the shoulders of the open-source community working on Snapdragon X Elite support:

*   **Max ([zensanp](https://github.com/zensanp/linux-book4-edge))**: Base kernel fork and Device Trees.
*   **[moolwalk](https://github.com/moolwalk) (Jesse Ahn)**: Critical display patches and 16" SKU hardware fixes.
*   **[jglathe](https://github.com/jglathe/linux_ms_dev_kit)**: Pre-built Ubuntu images used for initial booting.
*   **Joshua Grisham ([samsung-galaxybook-extras](https://github.com/joshuagrisham/samsung-galaxybook-extras))**: SABI protocol research.
*   **[icecream95](https://github.com/icecream95/xle-ec-tool)** & **[Maccraft123](https://github.com/Maccraft123/it8987-qcom-tool)**: EC research tools foundation.
*   **Wesley Cheng**: Initial X1 Elite kernel work.

---

## ⚖️ Disclaimer & License

**Disclaimer:** This software is experimental. It interacts with low-level hardware (EC, Power Management). Use at your own risk. We are not responsible for bricked devices or hardware damage.

*   **Drivers**: GPL v2
*   **Tools/Docs**: Provided for research purposes under MIT license where applicable.

*Developed with passion by **SaddyTech***
