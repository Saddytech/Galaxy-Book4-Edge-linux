# samsung-galaxybook-battery
Kernel driver that makes the battery visible on the Samsung Galaxy Book4 Edge
(Snapdragon X1E) under Linux.

The Qualcomm DSDT exposes battery state through an ACPI OperationRegion of
type `0xA1`, which has no in-kernel handler, so `qcom-battmgr-bat` comes up
empty. This driver bypasses ACPI and speaks directly to the Samsung ENE
KB9058 EC over its Mbox protocol on `i2c-2 @ 0x64`.

After it loads you get a proper `power_supply` device that every normal
Linux tool honours: the GNOME tray icon, GNOME / KDE system settings,
`upower`, `acpi`, `cat /sys/class/power_supply/*/capacity`, waybar, etc.

## Build
```
make
```
Requires `linux-headers-$(uname -r)` and `gcc`.

## One-shot test (temporary)
```
sudo insmod ./samsung_galaxybook_battery.ko
echo "sgbook-battery 0x64" | sudo tee /sys/bus/i2c/devices/i2c-2/new_device
```
Then `upower -i $(upower -e | grep samsung)` or open GNOME Settings → Power.

## Persistent install
```
sudo make install                                     # copy module + depmod
sudo cp 90-samsung-galaxybook-battery.rules /etc/udev/rules.d/
sudo cp samsung-galaxybook-battery.service /etc/systemd/system/
sudo udevadm control --reload
sudo systemctl daemon-reload
sudo systemctl enable --now samsung-galaxybook-battery.service
```

## Uninstall
```
sudo systemctl disable --now samsung-galaxybook-battery.service
sudo rm /etc/systemd/system/samsung-galaxybook-battery.service
sudo rm /etc/udev/rules.d/90-samsung-galaxybook-battery.rules
sudo rmmod samsung_galaxybook_battery
sudo make uninstall
```

## EC register map
From the DSDT `_SB.ECTC` ECR OperationRegion (type `0xA1`):
```
0x80 bit0 = B1EX   battery 1 present
0x80 bit2 = ACEX   AC adapter online
0x84      = B1ST   state (bit0 discharge, bit1 charge, bit3 full)
0xA0..A3  = B1RR   remaining capacity (mAh, BE upper word)
0xA4..A7  = B1PV   voltage (BE upper) + signed current (BE lower)
0xB0..B3  = B1AF   design mAh (BE upper) + full-charge mAh (BE lower)
0xB4..B7  = B1VL   design voltage mV (BE lower word)
```

## License
GPL v2.
