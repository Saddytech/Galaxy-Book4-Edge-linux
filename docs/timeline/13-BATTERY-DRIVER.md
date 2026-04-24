# Chapter 13 — The Battery Driver

## 13.1 The user's ask

> **"i want read the status like in any normal linux distro, so in system settings, icon tray and terminal with the normal commands everyone use to control it"**

Translation: `acpi -b` must work, `upower -i battery_...` must work, GNOME's system tray must show a battery icon, and KDE/Cinnamon/Xfce must all Just Work™. That can only happen if the battery appears under `/sys/class/power_supply/`. The Python userspace reader from Chapter 12 gave us the *data* but not the *integration*.

## 13.2 Decoding the battery registers in practice

Chapter 12 mapped the EC register offsets but not the actual encoding. A session of reading the registers and decoding revealed:

```text path=null start=null
EC register 0x80 = 0x05
  → bit 0 B1EX = 1 (battery present)
  → bit 2 ACEX = 1 (AC connected)

EC register 0x84 = 0x00
  → all state bits clear (idle at full charge)

EC registers 0xA0-0xA3 = 00 61 0F 28
  → upper 16 bits big-endian: 0x0F28 = 3880
  → unit: **mAh** (NOT mWh as DSDT field name "mWh" suggested)
  → interpretation: remaining capacity 3 880 mAh

EC registers 0xA4-0xA7 = 00 00 44 68
  → upper 16 bits BE: 0x4468 = 17 512 → **17.512 V** present voltage
  → lower 16 bits BE: 0x0000 = 0 → **0 mA** (idle)

EC registers 0xB0-0xB3 = 0F A0 0F 3C
  → upper 16 bits BE: 0x0FA0 = 4 000 → **design capacity 4 000 mAh**
  → lower 16 bits BE: 0x0F3C = 3 900 → **full-charge capacity 3 900 mAh** (wear indicator)

EC registers 0xB4-0xB7 = 3C A0 00 29
  → lower 16 bits BE: 0x3CA0 = 15 520 → **design voltage 15.52 V**
  → upper 16 bits: possibly temperature
```

**Cross-validation**: 4 000 mAh × 15.52 V = **62.08 Wh**, matching the Book4 Edge 14" spec (61.8 Wh nameplate). **The decoding is correct.**

Derived metrics for power_supply_class:

| Field | Value | EC source |
|---|---|---|
| `STATUS` | Full / Charging / Discharging | bit derivation from `0x84` + `0x80` |
| `PRESENT` | 1 if B1EX bit set | `0x80` bit 0 |
| `TECHNOLOGY` | `Li-ion` | constant (design) |
| `VOLTAGE_MIN_DESIGN` | 15 520 000 µV | `0xB4-B7` lower BE × 1000 |
| `VOLTAGE_NOW` | 17 512 000 µV | `0xA4-A7` upper BE × 1000 |
| `CURRENT_NOW` | 0 µA | `0xA4-A7` lower BE × 1000 |
| `CHARGE_FULL_DESIGN` | 4 000 000 µAh | `0xB0-B3` upper BE × 1000 |
| `CHARGE_FULL` | 3 900 000 µAh | `0xB0-B3` lower BE × 1000 |
| `CHARGE_NOW` | 3 880 000 µAh | `0xA0-A3` upper BE × 1000 |
| `CAPACITY` | 99 | `CHARGE_NOW * 100 / CHARGE_FULL` |
| `MODEL_NAME` | `Galaxy Book4 Edge Battery` | constant |
| `MANUFACTURER` | `Samsung` | constant |
| `SCOPE` | `System` | constant |

The 99.5% charge comes out to 99% when rounded for `CAPACITY`. Health = 3 900 / 4 000 = 97.5% wear-preserved.

## 13.3 Kernel module scaffold

Kernel headers were already installed for DKMS. Directory:

```
driver/
├── samsung_galaxybook_battery.c      (417 lines)
├── Makefile
├── dkms.conf
├── samsung-galaxybook-battery.service
├── 90-samsung-galaxybook-battery.rules
├── 90-ignore-qcom-battmgr-bat.rules
└── README.md
```

The module is an **I²C driver** that binds to the EC at bus `i2c-2` slave `0x64`. It registers a `power_supply_class` device and polls every 5 s.

## 13.4 Why slave `0x64`, not `0x62`

Chapter 12 found the EC on two I²C buses. The mainline `qcom-battmgr` driver was already claiming `0x62` via the SCAI/pmic_glink binding, so to avoid a conflict, the new battery driver went on the second bus — `/dev/i2c-2` slave `0x64` — which happens to also expose the Mbox register interface.

```c path=null start=null
static const struct i2c_device_id sam_gbook_bat_i2c_id[] = {
    { "sam-gbook-bat", 0 },     // name capped at 20 chars!
    { }
};
MODULE_DEVICE_TABLE(i2c, sam_gbook_bat_i2c_id);

static const struct of_device_id sam_gbook_bat_of_match[] = {
    { .compatible = "samsung,galaxybook-battery" },
    { }
};
MODULE_DEVICE_TABLE(of, sam_gbook_bat_of_match);
```

Since there's no DT node declaring `samsung,galaxybook-battery`, we instantiate the I²C client manually:

```bash path=null start=null
echo sam-gbook-bat 0x64 | sudo tee /sys/bus/i2c/devices/i2c-2/new_device
```

Encapsulated in a systemd unit so it runs on boot.

## 13.5 The Mbox reader (kernel C)

```c path=null start=null
static int ene_mbox_write(struct i2c_client *client, u16 cmd, u8 data)
{
    u8 pkt[5] = { 0x40, 0x00, (cmd >> 8) & 0xFF, cmd & 0xFF, data };
    int ret;
    
    ret = i2c_master_send(client, pkt, 5);
    return (ret == 5) ? 0 : -EIO;
}

static int ene_ec_read_byte(struct i2c_client *client, u8 reg)
{
    int ret;
    u8 trigger[5] = { 0x40, 0x00, 0xFF, 0x11, 0x88 };
    u8 resp[8] = { 0 };
    
    struct i2c_msg msgs[2] = {
        { .addr = client->addr, .flags = 0,        .len = 5, .buf = trigger },
        { .addr = client->addr, .flags = I2C_M_RD, .len = 8, .buf = resp },
    };
    
    mutex_lock(&mbox_lock);
    ret = ene_mbox_write(client, 0xF480, reg);
    if (ret)
        goto out;
    
    ret = i2c_transfer(client->adapter, msgs, 2);
    if (ret != 2) {
        ret = -EIO;
        goto out;
    }
    ret = resp[2];
out:
    mutex_unlock(&mbox_lock);
    return ret;
}
```

Exact port of the Python helper from Chapter 12.

## 13.6 Polling worker

```c path=null start=null
struct sam_gbook_bat_priv {
    struct i2c_client *client;
    struct power_supply *psy;
    struct delayed_work poll_work;
    struct mutex data_lock;
    
    /* Cached values */
    int status;
    int present;
    int voltage_uv;
    int current_ua;
    int charge_now_uah;
    int charge_full_uah;
    int charge_full_design_uah;
    int capacity_percent;
};

static void sam_gbook_bat_poll(struct work_struct *work)
{
    struct sam_gbook_bat_priv *priv = container_of(work, struct sam_gbook_bat_priv, poll_work.work);
    u8 b80, b84, bA0, bA1, bA2, bA3, bA4, bA5, bA6, bA7, bB0, bB1, bB2, bB3;
    u16 rem_mah, design_mah, full_mah, voltage_mv, current_ma;
    
    mutex_lock(&priv->data_lock);
    
    b80 = ene_ec_read_byte(priv->client, 0x80);
    b84 = ene_ec_read_byte(priv->client, 0x84);
    bA0 = ene_ec_read_byte(priv->client, 0xA0);
    bA1 = ene_ec_read_byte(priv->client, 0xA1);
    /* ... */
    
    rem_mah     = (bA2 << 8) | bA3;          /* upper word BE */
    voltage_mv  = (bA6 << 8) | bA7;
    current_ma  = (bA4 << 8) | bA5;          /* lower word; sign when discharging? */
    design_mah  = (bB0 << 8) | bB1;
    full_mah    = (bB2 << 8) | bB3;
    
    priv->voltage_uv           = voltage_mv * 1000;
    priv->current_ua           = current_ma * 1000;
    priv->charge_now_uah       = rem_mah    * 1000;
    priv->charge_full_uah      = full_mah   * 1000;
    priv->charge_full_design_uah = design_mah * 1000;
    
    if (full_mah > 0)
        priv->capacity_percent = (rem_mah * 100) / full_mah;
    
    priv->present = !!(b80 & 0x01);
    
    if (b84 & 0x01)       priv->status = POWER_SUPPLY_STATUS_DISCHARGING;
    else if (b84 & 0x02)  priv->status = POWER_SUPPLY_STATUS_CHARGING;
    else if (b80 & 0x04)  priv->status = POWER_SUPPLY_STATUS_FULL;
    else                  priv->status = POWER_SUPPLY_STATUS_UNKNOWN;
    
    mutex_unlock(&priv->data_lock);
    
    power_supply_changed(priv->psy);
    schedule_delayed_work(&priv->poll_work, msecs_to_jiffies(5000));
}
```

## 13.7 power_supply get_property callback

```c path=null start=null
static int sam_gbook_bat_get_property(struct power_supply *psy,
                                      enum power_supply_property psp,
                                      union power_supply_propval *val)
{
    struct sam_gbook_bat_priv *priv = power_supply_get_drvdata(psy);
    int ret = 0;
    
    mutex_lock(&priv->data_lock);
    switch (psp) {
    case POWER_SUPPLY_PROP_STATUS:
        val->intval = priv->status;
        break;
    case POWER_SUPPLY_PROP_PRESENT:
        val->intval = priv->present;
        break;
    case POWER_SUPPLY_PROP_TECHNOLOGY:
        val->intval = POWER_SUPPLY_TECHNOLOGY_LION;
        break;
    case POWER_SUPPLY_PROP_VOLTAGE_MIN_DESIGN:
        val->intval = 15520000;
        break;
    case POWER_SUPPLY_PROP_VOLTAGE_NOW:
        val->intval = priv->voltage_uv;
        break;
    case POWER_SUPPLY_PROP_CURRENT_NOW:
        val->intval = priv->current_ua;
        break;
    case POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN:
        val->intval = priv->charge_full_design_uah;
        break;
    case POWER_SUPPLY_PROP_CHARGE_FULL:
        val->intval = priv->charge_full_uah;
        break;
    case POWER_SUPPLY_PROP_CHARGE_NOW:
        val->intval = priv->charge_now_uah;
        break;
    case POWER_SUPPLY_PROP_CAPACITY:
        val->intval = priv->capacity_percent;
        break;
    case POWER_SUPPLY_PROP_MODEL_NAME:
        val->strval = "Galaxy Book4 Edge Battery";
        break;
    case POWER_SUPPLY_PROP_MANUFACTURER:
        val->strval = "Samsung";
        break;
    case POWER_SUPPLY_PROP_SCOPE:
        val->intval = POWER_SUPPLY_SCOPE_SYSTEM;
        break;
    default:
        ret = -EINVAL;
    }
    mutex_unlock(&priv->data_lock);
    return ret;
}
```

## 13.8 probe() + remove()

```c path=null start=null
static int sam_gbook_bat_probe(struct i2c_client *client)
{
    struct sam_gbook_bat_priv *priv;
    struct power_supply_config cfg = { 0 };
    
    priv = devm_kzalloc(&client->dev, sizeof(*priv), GFP_KERNEL);
    if (!priv) return -ENOMEM;
    
    priv->client = client;
    mutex_init(&priv->data_lock);
    INIT_DELAYED_WORK(&priv->poll_work, sam_gbook_bat_poll);
    
    cfg.drv_data = priv;
    cfg.fwnode = dev_fwnode(&client->dev);    // kernel 7.x: fwnode, not of_node
    
    priv->psy = devm_power_supply_register(&client->dev,
                                            &sam_gbook_bat_desc, &cfg);
    if (IS_ERR(priv->psy))
        return PTR_ERR(priv->psy);
    
    schedule_delayed_work(&priv->poll_work, 0);
    i2c_set_clientdata(client, priv);
    return 0;
}

static void sam_gbook_bat_remove(struct i2c_client *client)
{
    struct sam_gbook_bat_priv *priv = i2c_get_clientdata(client);
    cancel_delayed_work_sync(&priv->poll_work);
}
```

## 13.9 Build errors encountered

First build attempt failed twice:

**Error 1**:
```
error: 'struct power_supply_config' has no member named 'of_node'
```

Kernel 7.x renamed `cfg.of_node` to `cfg.fwnode`. Fix: replace across the driver.

**Error 2**:
```
error: i2c_device_id name length exceeds 20
```

`"samsung-galaxybook-battery-i2c"` (29 chars) > the 20-char limit in `struct i2c_device_id.name[20]`. Shortened to `"sam-gbook-bat"` (13 chars).

After both fixes: `make` succeeded, signed module at `samsung_galaxybook_battery.ko.zst` ready to install.

## 13.10 Installing + activating

```bash path=null start=null
# Install module
sudo make install       # goes to /lib/modules/7.0.0-22-qcom-x1e/updates/

# Install udev + systemd + acpi CLI
sudo cp 90-samsung-galaxybook-battery.rules /etc/udev/rules.d/
sudo cp samsung-galaxybook-battery.service /etc/systemd/system/
sudo udevadm control --reload
sudo systemctl daemon-reload
sudo systemctl enable --now samsung-galaxybook-battery.service

# Install the standard acpi CLI tool
sudo apt install -y acpi
```

The systemd unit:

```ini
[Unit]
Description=Instantiate Samsung Galaxy Book battery i2c client
After=local-fs.target systemd-modules-load.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'echo sam-gbook-bat 0x64 > /sys/bus/i2c/devices/i2c-2/new_device'
ExecStop=/bin/sh -c 'echo 0x64 > /sys/bus/i2c/devices/i2c-2/delete_device'

[Install]
WantedBy=multi-user.target
```

## 13.11 Verification round-trip

```text path=null start=null
$ acpi -b
Battery 1: Full, 99%

$ acpi -i
Battery 1: design capacity 4000 mAh, last full capacity 3900 mAh = 97%

$ cat /sys/class/power_supply/samsung-galaxybook-bat/capacity
99

$ cat /sys/class/power_supply/samsung-galaxybook-bat/uevent
POWER_SUPPLY_NAME=samsung-galaxybook-bat
POWER_SUPPLY_TYPE=Battery
POWER_SUPPLY_STATUS=Full
POWER_SUPPLY_PRESENT=1
POWER_SUPPLY_TECHNOLOGY=Li-ion
POWER_SUPPLY_VOLTAGE_MIN_DESIGN=15520000
POWER_SUPPLY_VOLTAGE_NOW=17510000
POWER_SUPPLY_CURRENT_NOW=0
POWER_SUPPLY_CHARGE_FULL_DESIGN=4000000
POWER_SUPPLY_CHARGE_FULL=3900000
POWER_SUPPLY_CHARGE_NOW=3880000
POWER_SUPPLY_CAPACITY=99
POWER_SUPPLY_SCOPE=System
POWER_SUPPLY_MODEL_NAME=Galaxy Book4 Edge Battery
POWER_SUPPLY_MANUFACTURER=Samsung

$ upower -i /org/freedesktop/UPower/devices/battery_samsung_galaxybook_bat
native-path:          samsung-galaxybook-bat
vendor:               Samsung
model:                Galaxy Book4 Edge Battery
power supply:         yes
battery
  present:             yes
  rechargeable:        yes
  state:               fully-charged
  energy:              60.22 Wh
  energy-empty:        0 Wh
  energy-full:         60.53 Wh
  energy-full-design:  62.08 Wh
  energy-rate:         0 W
  voltage:             17.51 V
  percentage:          99%
  capacity:            97.5%
  technology:          lithium-ion
  icon-name:          'battery-full-charged-symbolic'
  warning-level:      'none'
```

GNOME Shell's status bar immediately started showing the battery icon. Full desktop integration.

## 13.12 Handling the stale `qcom-battmgr-bat`

The old `qcom-battmgr-bat` node (empty, returns `EAGAIN` on every read) was still present and confusing UPower's DisplayDevice aggregation. An udev rule hides it from UPower:

```
# /etc/udev/rules.d/90-ignore-qcom-battmgr-bat.rules
SUBSYSTEM=="power_supply", ATTR{name}=="qcom-battmgr-bat", ENV{UPOWER_IGNORE}="1"
SUBSYSTEM=="power_supply", ATTR{name}=="qcom-battmgr-usb", ENV{UPOWER_IGNORE}="1"
SUBSYSTEM=="power_supply", ATTR{name}=="qcom-battmgr-wls", ENV{UPOWER_IGNORE}="1"
```

After next boot the user's display shows only the working battery.

## 13.13 DKMS for kernel upgrades

So the driver rebuilds automatically when Canonical pushes a new kernel:

```bash path=null start=null
sudo apt install -y dkms
sudo mkdir -p /usr/src/samsung-galaxybook-battery-1.0.0
sudo cp samsung_galaxybook_battery.c Makefile dkms.conf \
    /usr/src/samsung-galaxybook-battery-1.0.0/
sudo dkms add -m samsung-galaxybook-battery -v 1.0.0
sudo dkms build -m samsung-galaxybook-battery -v 1.0.0
sudo dkms install -m samsung-galaxybook-battery -v 1.0.0 --force

$ dkms status
samsung-galaxybook-battery/1.0.0, 7.0.0-22-qcom-x1e, aarch64: installed
```

`dkms.conf`:

```
PACKAGE_NAME="samsung-galaxybook-battery"
PACKAGE_VERSION="1.0.0"
BUILT_MODULE_NAME[0]="samsung_galaxybook_battery"
DEST_MODULE_LOCATION[0]="/updates"
AUTOINSTALL="yes"
```

## 13.14 Driver signed via MOK

DKMS signed the module using `/var/lib/shim-signed/mok/MOK.priv`:

```
Signing module /var/lib/dkms/samsung-galaxybook-battery/1.0.0/build/samsung_galaxybook_battery.ko
Found pre-existing /lib/modules/7.0.0-22-qcom-x1e/updates/samsung_galaxybook_battery.ko.zst, archiving for uninstallation
Installing /lib/modules/7.0.0-22-qcom-x1e/updates/dkms/samsung_galaxybook_battery.ko.zst
```

Signed module survives Secure Boot (if enabled) and future kernel upgrades.

## 13.15 Reboot survivability test

```bash path=null start=null
# Cold reboot
sudo reboot

# After reboot:
$ acpi -b
Battery 1: Full, 99%
```

Driver auto-loaded by DKMS → systemd unit → `new_device` → probe → `power_supply_class` registered. **End-to-end automatic.**

## 13.16 Lines of code

- `samsung_galaxybook_battery.c` — 417 lines (driver)
- `Makefile` — 12 lines
- `dkms.conf` — 7 lines
- `samsung-galaxybook-battery.service` — 14 lines
- `90-samsung-galaxybook-battery.rules` — 4 lines
- `90-ignore-qcom-battmgr-bat.rules` — 6 lines
- `README.md` — 180 lines

Total: ~640 lines to go from "no battery reporting" to "first-class citizen in the Linux power stack."

## 13.17 What remains open from Chapter 13

- **Current sign**: charging vs discharging current — the EC's lower-word BE seems to use a signed 16-bit; verify with discharge test
- **Fan tach**: still no live RPM read (deferred to a future reverse-engineering pass)
- **Performance mode**: unresolved from Chapter 11/12

## 13.18 End of Chapter 13

Outputs:

- `driver/` — kernel module source, Makefile, DKMS config, systemd unit, udev rules
- `/lib/modules/7.0.0-22-qcom-x1e/updates/dkms/samsung_galaxybook_battery.ko.zst` — signed module
- `/etc/systemd/system/samsung-galaxybook-battery.service` — auto-instantiation on boot
- Full `acpi -b` / `upower` / GNOME tray integration

The user's original request — *"i want read the status like in any normal linux distro"* — was fully satisfied. The project now had a functional native Linux install on Samsung Galaxy Book4 Edge 14" with working battery reporting.

Co-Authored-By: Oz <oz-agent@warp.dev>
