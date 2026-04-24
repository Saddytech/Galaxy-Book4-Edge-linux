# SPDX-License-Identifier: GPL-2.0
#
# Out-of-tree kbuild Makefile for samsung_galaxybook_battery

obj-m := samsung_galaxybook_battery.o

KERNEL_VERSION ?= $(shell uname -r)
KDIR           ?= /lib/modules/$(KERNEL_VERSION)/build
PWD            := $(shell pwd)

.PHONY: all clean install uninstall modules modules_install

all: modules

modules:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

modules_install install:
	$(MAKE) -C $(KDIR) M=$(PWD) modules_install
	depmod -a $(KERNEL_VERSION)

uninstall:
	rm -f /lib/modules/$(KERNEL_VERSION)/extra/samsung_galaxybook_battery.ko
	depmod -a $(KERNEL_VERSION)
