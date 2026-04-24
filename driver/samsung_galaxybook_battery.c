// SPDX-License-Identifier: GPL-2.0
/*
 * Samsung Galaxy Book4 Edge battery driver.
 *
 * Reads battery state from the Samsung ENE KB9058 EC via its Mbox
 * protocol on I2C. The Qualcomm DSDT on this laptop exposes the same
 * data through an ACPI OperationRegion of type 0xA1, but there is no
 * in-kernel handler for that region type, so the qcom-battmgr power
 * supply comes up empty. This driver bypasses ACPI entirely and talks
 * to the EC directly.
 *
 * EC register layout (from DSDT _SB.ECTC ECR region):
 *   0x80 bit0 = B1EX   (battery 1 present)
 *   0x80 bit2 = ACEX   (AC adapter online)
 *   0x84      = B1ST   (state: bit0 discharge, bit1 charge, bit3 full)
 *   0xA0..A3  = B1RR   (remaining capacity; upper word big-endian = mAh)
 *   0xA4..A7  = B1PV   (upper word BE = voltage mV, lower BE = signed mA)
 *   0xB0..B3  = B1AF   (upper BE = design mAh, lower BE = full-charge mAh)
 *   0xB4..B7  = B1VL   (lower BE = nominal design voltage mV)
 *
 * Author: Galaxy Book4 Edge Linux research (reverse engineered from EC2.sys)
 */

#include <linux/delay.h>
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/power_supply.h>
#include <linux/workqueue.h>

#define DRV_NAME		"samsung_galaxybook_battery"
#define POLL_INTERVAL_S		5

/* Mbox framing */
#define MBOX_WRITE_PREFIX	0x40
#define MBOX_READ_PREFIX	0x30
#define MBOX_READ_SUCCESS	0x50

#define CMD_TARGET_HI		0xF4
#define CMD_TARGET_LO		0x80
#define CMD_EXEC_HI		0xFF
#define CMD_EXEC_LO		0x10
#define EXEC_READ		0x88

/* EC registers we care about */
#define EC_REG_FLAGS		0x80	/* B1EX + ACEX */
#define EC_REG_B1ST		0x84	/* state */
#define EC_REG_B1RR		0xA0	/* remaining mAh (BE upper word) */
#define EC_REG_B1PV		0xA4	/* voltage mV + signed current mA */
#define EC_REG_B1AF		0xB0	/* design mAh + full-charge mAh */
#define EC_REG_B1VL		0xB4	/* design voltage mV */

#define B1ST_DISCHARGE		BIT(0)
#define B1ST_CHARGE		BIT(1)
#define B1ST_FULL		BIT(3)

#define FLAG_B1EX		BIT(0)
#define FLAG_ACEX		BIT(2)

struct sgb_bat {
	struct i2c_client	*client;
	struct power_supply	*psy;
	struct delayed_work	poll;
	struct mutex		lock;		/* serialises EC access */

	/* cached readings (µV / µA / µAh, per power_supply class convention) */
	int	voltage_now;
	int	voltage_design;
	int	current_now;
	int	charge_now;
	int	charge_full;
	int	charge_full_design;
	int	status;
	bool	present;
	bool	ac_online;
	bool	have_data;
};

/* ------------------------------------------------------------------ */
/* Mbox primitives, built on raw i2c_transfer() so we don't need the   */
/* i2c-dev character device.                                           */
/* ------------------------------------------------------------------ */

static int sgb_mbox_write(struct i2c_client *c, u8 hi, u8 lo, u8 data)
{
	u8 buf[5] = { MBOX_WRITE_PREFIX, 0x00, hi, lo, data };
	struct i2c_msg msg = {
		.addr  = c->addr,
		.flags = 0,
		.len   = sizeof(buf),
		.buf   = buf,
	};
	int ret = i2c_transfer(c->adapter, &msg, 1);

	if (ret != 1)
		return ret < 0 ? ret : -EIO;
	usleep_range(5000, 6000);
	return 0;
}

static int sgb_mbox_read(struct i2c_client *c, u8 hi, u8 lo, u8 *out)
{
	u8 wbuf[4] = { MBOX_READ_PREFIX, 0x00, hi, lo };
	u8 rbuf[2] = { 0, 0 };
	struct i2c_msg msgs[2] = {
		{ .addr = c->addr, .flags = 0,        .len = 4, .buf = wbuf },
		{ .addr = c->addr, .flags = I2C_M_RD, .len = 2, .buf = rbuf },
	};
	int ret = i2c_transfer(c->adapter, msgs, 2);

	if (ret != 2)
		return ret < 0 ? ret : -EIO;
	if (rbuf[0] != MBOX_READ_SUCCESS)
		return -EIO;
	*out = rbuf[1];
	return 0;
}

static int sgb_ec_read_byte(struct i2c_client *c, u8 reg, u8 *out)
{
	int ret;

	ret = sgb_mbox_write(c, CMD_TARGET_HI, CMD_TARGET_LO, reg);
	if (ret)
		return ret;
	ret = sgb_mbox_write(c, CMD_EXEC_HI, CMD_EXEC_LO, EXEC_READ);
	if (ret)
		return ret;
	return sgb_mbox_read(c, CMD_TARGET_HI, CMD_TARGET_LO, out);
}

static int sgb_ec_read_block(struct i2c_client *c, u8 reg, u8 *buf, int n)
{
	int i, ret;

	for (i = 0; i < n; i++) {
		ret = sgb_ec_read_byte(c, reg + i, &buf[i]);
		if (ret)
			return ret;
	}
	return 0;
}

/* ------------------------------------------------------------------ */
/* Poll work: read EC regs and update cached values                     */
/* ------------------------------------------------------------------ */

static void sgb_poll_work(struct work_struct *w)
{
	struct sgb_bat *b = container_of(to_delayed_work(w), struct sgb_bat, poll);
	u8 flags, b1st, rr[4], pv[4], af[4], vl[4];
	u16 remaining, voltage, design_c, fullchg_c, design_v;
	s16 current_ma;
	int ret;

	mutex_lock(&b->lock);

	ret = sgb_ec_read_byte(b->client, EC_REG_FLAGS, &flags);
	if (ret)
		goto err;
	ret = sgb_ec_read_byte(b->client, EC_REG_B1ST, &b1st);
	if (ret)
		goto err;
	ret = sgb_ec_read_block(b->client, EC_REG_B1RR, rr, 4);
	if (ret)
		goto err;
	ret = sgb_ec_read_block(b->client, EC_REG_B1PV, pv, 4);
	if (ret)
		goto err;
	ret = sgb_ec_read_block(b->client, EC_REG_B1AF, af, 4);
	if (ret)
		goto err;
	ret = sgb_ec_read_block(b->client, EC_REG_B1VL, vl, 4);
	if (ret)
		goto err;

	/*
	 * DSDT _BST does `ByteSwap16(B1RR >> 16)` — that's the big-endian
	 * word at offsets [2][3] of the 4-byte LE field. Likewise the lower
	 * word BE sits at offsets [0][1].
	 */
	remaining  = ((u16)rr[2] << 8) | rr[3];		/* mAh */
	voltage    = ((u16)pv[2] << 8) | pv[3];		/* mV */
	current_ma = (s16)(((u16)pv[0] << 8) | pv[1]);	/* signed mA */
	design_c   = ((u16)af[2] << 8) | af[3];		/* mAh */
	fullchg_c  = ((u16)af[0] << 8) | af[1];		/* mAh */
	design_v   = ((u16)vl[0] << 8) | vl[1];		/* mV */

	/* 0xFFFF == "unknown / uninitialised" per DSDT convention */
	if (remaining == 0xFFFF)
		remaining = 0;
	if (design_c == 0xFFFF)
		design_c = 0;
	if (fullchg_c == 0xFFFF)
		fullchg_c = 0;

	b->present            = !!(flags & FLAG_B1EX);
	b->ac_online          = !!(flags & FLAG_ACEX);
	b->voltage_now        = (int)voltage   * 1000;		/* mV -> µV */
	b->voltage_design     = (int)design_v  * 1000;
	b->current_now        = (int)current_ma * 1000;		/* mA -> µA */
	b->charge_now         = (int)remaining * 1000;		/* mAh -> µAh */
	b->charge_full        = (int)(fullchg_c ? fullchg_c : design_c) * 1000;
	b->charge_full_design = (int)design_c  * 1000;

	if (b1st & B1ST_FULL)
		b->status = POWER_SUPPLY_STATUS_FULL;
	else if (b1st & B1ST_CHARGE)
		b->status = POWER_SUPPLY_STATUS_CHARGING;
	else if (b1st & B1ST_DISCHARGE)
		b->status = POWER_SUPPLY_STATUS_DISCHARGING;
	else if (b->ac_online && fullchg_c &&
		 remaining >= (fullchg_c * 95 / 100))
		b->status = POWER_SUPPLY_STATUS_FULL;
	else if (b->ac_online)
		b->status = POWER_SUPPLY_STATUS_NOT_CHARGING;
	else
		b->status = POWER_SUPPLY_STATUS_DISCHARGING;

	b->have_data = true;
	mutex_unlock(&b->lock);
	power_supply_changed(b->psy);
	goto resched;

err:
	mutex_unlock(&b->lock);
	dev_warn_ratelimited(&b->client->dev,
			     "EC read failed: %d\n", ret);
resched:
	schedule_delayed_work(&b->poll, POLL_INTERVAL_S * HZ);
}

/* ------------------------------------------------------------------ */
/* power_supply properties                                              */
/* ------------------------------------------------------------------ */

static enum power_supply_property sgb_props[] = {
	POWER_SUPPLY_PROP_STATUS,
	POWER_SUPPLY_PROP_PRESENT,
	POWER_SUPPLY_PROP_TECHNOLOGY,
	POWER_SUPPLY_PROP_VOLTAGE_NOW,
	POWER_SUPPLY_PROP_VOLTAGE_MIN_DESIGN,
	POWER_SUPPLY_PROP_CURRENT_NOW,
	POWER_SUPPLY_PROP_CHARGE_NOW,
	POWER_SUPPLY_PROP_CHARGE_FULL,
	POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN,
	POWER_SUPPLY_PROP_CAPACITY,
	POWER_SUPPLY_PROP_MODEL_NAME,
	POWER_SUPPLY_PROP_MANUFACTURER,
	POWER_SUPPLY_PROP_SCOPE,
};

static int sgb_get_prop(struct power_supply *psy,
			enum power_supply_property prop,
			union power_supply_propval *val)
{
	struct sgb_bat *b = power_supply_get_drvdata(psy);
	int ret = 0;

	mutex_lock(&b->lock);

	if (!b->have_data &&
	    prop != POWER_SUPPLY_PROP_MODEL_NAME &&
	    prop != POWER_SUPPLY_PROP_MANUFACTURER &&
	    prop != POWER_SUPPLY_PROP_TECHNOLOGY &&
	    prop != POWER_SUPPLY_PROP_SCOPE) {
		mutex_unlock(&b->lock);
		return -ENODATA;
	}

	switch (prop) {
	case POWER_SUPPLY_PROP_STATUS:
		val->intval = b->status;
		break;
	case POWER_SUPPLY_PROP_PRESENT:
		val->intval = b->present;
		break;
	case POWER_SUPPLY_PROP_TECHNOLOGY:
		val->intval = POWER_SUPPLY_TECHNOLOGY_LION;
		break;
	case POWER_SUPPLY_PROP_VOLTAGE_NOW:
		val->intval = b->voltage_now;
		break;
	case POWER_SUPPLY_PROP_VOLTAGE_MIN_DESIGN:
		val->intval = b->voltage_design;
		break;
	case POWER_SUPPLY_PROP_CURRENT_NOW:
		val->intval = b->current_now;
		break;
	case POWER_SUPPLY_PROP_CHARGE_NOW:
		val->intval = b->charge_now;
		break;
	case POWER_SUPPLY_PROP_CHARGE_FULL:
		val->intval = b->charge_full;
		break;
	case POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN:
		val->intval = b->charge_full_design;
		break;
	case POWER_SUPPLY_PROP_CAPACITY:
		if (b->charge_full > 0)
			val->intval = clamp_t(int,
				(int)((long long)b->charge_now * 100 /
				      b->charge_full),
				0, 100);
		else
			ret = -ENODATA;
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

	mutex_unlock(&b->lock);
	return ret;
}

static const struct power_supply_desc sgb_psy_desc = {
	.name		= "samsung-galaxybook-bat",
	.type		= POWER_SUPPLY_TYPE_BATTERY,
	.properties	= sgb_props,
	.num_properties	= ARRAY_SIZE(sgb_props),
	.get_property	= sgb_get_prop,
};

/* ------------------------------------------------------------------ */
/* i2c probe / remove                                                   */
/* ------------------------------------------------------------------ */

static int sgb_probe(struct i2c_client *client)
{
	struct sgb_bat *b;
	struct power_supply_config cfg = { 0 };
	u8 probe_byte;
	int ret;

	b = devm_kzalloc(&client->dev, sizeof(*b), GFP_KERNEL);
	if (!b)
		return -ENOMEM;

	b->client = client;
	mutex_init(&b->lock);
	INIT_DELAYED_WORK(&b->poll, sgb_poll_work);

	/* Probe the EC with a single-byte read to validate we're talking
	 * to the right thing before registering the power supply. */
	ret = sgb_ec_read_byte(client, EC_REG_FLAGS, &probe_byte);
	if (ret) {
		dev_err(&client->dev,
			"EC probe read failed (%d) — wrong bus/addr?\n", ret);
		return ret;
	}
	dev_info(&client->dev,
		 "EC flags byte = 0x%02x (B1EX=%d, ACEX=%d)\n",
		 probe_byte,
		 !!(probe_byte & FLAG_B1EX),
		 !!(probe_byte & FLAG_ACEX));

	cfg.drv_data = b;
	cfg.fwnode   = dev_fwnode(&client->dev);

	b->psy = devm_power_supply_register(&client->dev,
					    &sgb_psy_desc, &cfg);
	if (IS_ERR(b->psy)) {
		ret = PTR_ERR(b->psy);
		dev_err(&client->dev,
			"power_supply_register failed: %d\n", ret);
		return ret;
	}

	i2c_set_clientdata(client, b);
	schedule_delayed_work(&b->poll, 0);		/* immediate first read */
	dev_info(&client->dev,
		 "Samsung Galaxy Book4 Edge battery driver ready\n");
	return 0;
}

static void sgb_remove(struct i2c_client *client)
{
	struct sgb_bat *b = i2c_get_clientdata(client);

	cancel_delayed_work_sync(&b->poll);
}

/* i2c_device_id.name is limited to 20 chars, so use a shorter id.
 * The user-facing power_supply name is still "samsung-galaxybook-bat". */
static const struct i2c_device_id sgb_id[] = {
	{ "sgbook-battery", 0 },
	{ }
};
MODULE_DEVICE_TABLE(i2c, sgb_id);

static const struct of_device_id sgb_of_match[] = {
	{ .compatible = "samsung,galaxybook-battery" },
	{ }
};
MODULE_DEVICE_TABLE(of, sgb_of_match);

static struct i2c_driver sgb_driver = {
	.driver = {
		.name		= DRV_NAME,
		.of_match_table	= sgb_of_match,
	},
	.probe		= sgb_probe,
	.remove		= sgb_remove,
	.id_table	= sgb_id,
};
module_i2c_driver(sgb_driver);

MODULE_AUTHOR("Galaxy Book4 Edge Linux research");
MODULE_DESCRIPTION("Samsung Galaxy Book4 Edge battery driver (EC Mbox)");
MODULE_LICENSE("GPL v2");
MODULE_ALIAS("i2c:sgbook-battery");
