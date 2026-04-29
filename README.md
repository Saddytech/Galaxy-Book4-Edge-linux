# Samsung Galaxy Book4 Edge - Linux Hardware Support

This repository contains reverse-engineered drivers, tools, and documentation for running Linux on the **Samsung Galaxy Book4 Edge** (Snapdragon X1E). 

Due to the device's proprietary ACPI implementations and reliance on the ENE KB9058 Embedded Controller (EC), many hardware features do not work out-of-the-box on mainline Linux. This repository aims to bridge those gaps.

Watch the video here: https://youtu.be/V2DxY_PqLBg

## Repository Structure

### 🔋 [`driver/`](./driver/)
Contains a custom Linux kernel module (`samsung_galaxybook_battery`) that makes the laptop's battery visible to the OS.
* Bypasses broken ACPI methods and speaks directly to the EC over the Mbox protocol.
* Provides a standard `power_supply` device that works with GNOME, KDE, `upower`, `acpi`, and waybar.

### 🛠️ [`tools/`](./tools/)
A suite of Python scripts used to reverse-engineer and communicate with the ENE KB9058 Embedded Controller over the I2C bus.
* **Fan Control:** Direct control over fan curves and manual RPM overrides.
* **Performance Profiles:** Switch between Silent, Auto, and Max Performance modes.
* **Keyboard Backlight:** Set keyboard brightness and timeouts.
* **Stress Testing:** Automated thermal testing scripts.

### 📚 [`docs/`](./docs/)
Extensive documentation detailing the reverse-engineering journey.
* **`journey/`**: Step-by-step markdown notes covering everything from initial Windows driver reverse-engineering (Ghidra) to DSDT extraction and I2C protocol decoding.
* **`timeline/`**: A chronological timeline of the project, including failures, pivots, and breakthroughs while getting a minimal Ubuntu environment to boot.

## Pre-built Bootkits (ISOs)

If you are looking for ready-to-use bootable Linux images that already include these drivers and tools, please check the **[Releases](../../releases)** page of this GitHub repository. 

> **Note:** Do not clone this repository expecting an ISO file. Large `.tar.gz` bootkits are attached strictly to GitHub Releases to keep the source tree clean and lightweight.

## Disclaimer

This software is experimental and was developed through reverse-engineering. It interacts directly with low-level hardware components (I2C, Embedded Controller, Thermal Management). **Use at your own risk.** We are not responsible for any hardware damage or bricked devices.

## License

* The Battery Driver (`driver/`) is licensed under GPL v2.
* Scripts and tools are provided as-is for research and development purposes.

## Credits & Acknowledgements

A special thank you to the open-source community for laying the foundation for Snapdragon X Elite devices:

* **Max ([zensanp](https://github.com/zensanp/linux-book4-edge))**: Base kernel fork and Device Trees.
* **Wesley Cheng**: Initial X1 Elite minimal kernel.
* **[jglathe](https://github.com/jglathe/linux_ms_dev_kit)**: Pre-built Ubuntu images used for initial booting.
* **Joshua Grisham ([samsung-galaxybook-extras](https://github.com/joshuagrisham/samsung-galaxybook-extras))**: SABI v4 protocol and ACPI/DSDT research.
* **[icecream95](https://github.com/icecream95/xle-ec-tool)** & **[Maccraft123](https://github.com/Maccraft123/it8987-qcom-tool)**: Embedded Controller (EC) research tools and fan control patterns.
* **Jesse Ahn ([@moolwalk](https://github.com/moolwalk))**: Fixed the display for X1E Samsung panels (preventing black screen after initramfs) and contributed fixes for 16" SKUs.
* **Canonical / Ubuntu**: Base 7.0 kernel and official OS image.

### Our Contribution (SaddyTech)

Specific solutions we engineered to make the 16" Galaxy Book4 Edge (NP960XMA-KB1IT) fully usable:

* **Battery Driver**: Reverse-engineered the ENE KB9058 EC Mailbox protocol and wrote the `samsung_galaxybook_battery.c` driver from scratch.
* **PXE Boot**: Built a custom network boot infrastructure to bypass dead USB controllers during installation.
* **DTS & Firmware**: Manually patched the touchpad I2C address (`0xd1`) and extracted Samsung `.jsn` files from Windows for USB-C altmode.
