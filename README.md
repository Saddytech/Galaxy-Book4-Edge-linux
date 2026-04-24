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
