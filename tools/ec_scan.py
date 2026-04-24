#!/usr/bin/env python3
"""
ec_scan.py — dump all EC registers 0x00..0xFF via Mbox; run before/during/after
load to find registers that correlate with fan #1 activity.

Usage:
  sudo ./ec_scan.py > /tmp/ec_idle.txt
  # then with stress-ng --cpu 12 --timeout 30 running:
  sudo ./ec_scan.py > /tmp/ec_load.txt
  diff /tmp/ec_idle.txt /tmp/ec_load.txt
"""
import ctypes, fcntl, os, sys, time

MBOX_BUS, MBOX_ADDR = 2, 0x64
I2C_SLAVE, I2C_RDWR, I2C_M_RD = 0x0703, 0x0707, 0x0001

class I2cMsg(ctypes.Structure):
    _fields_=[("addr",ctypes.c_uint16),("flags",ctypes.c_uint16),
              ("len",ctypes.c_uint16),("buf",ctypes.POINTER(ctypes.c_char))]
class I2cRdwrIoctlData(ctypes.Structure):
    _fields_=[("msgs",ctypes.POINTER(I2cMsg)),("nmsgs",ctypes.c_uint32)]

def open_bus():
    fd=os.open(f"/dev/i2c-{MBOX_BUS}",os.O_RDWR); fcntl.ioctl(fd,I2C_SLAVE,MBOX_ADDR); return fd

def mbox_w(fd,h,l,d):
    os.write(fd,bytes([0x40,0,h&0xff,l&0xff,d&0xff])); time.sleep(0.002)

def ec_read(fd,reg):
    try:
        mbox_w(fd,0xF4,0x80,reg); mbox_w(fd,0xFF,0x10,0x88)
        wb=ctypes.create_string_buffer(bytes([0x30,0,0xF4,0x80]),4); rb=ctypes.create_string_buffer(2)
        msgs=(I2cMsg*2)(
            I2cMsg(addr=MBOX_ADDR,flags=0,len=4,buf=ctypes.cast(wb,ctypes.POINTER(ctypes.c_char))),
            I2cMsg(addr=MBOX_ADDR,flags=I2C_M_RD,len=2,buf=ctypes.cast(rb,ctypes.POINTER(ctypes.c_char))))
        fcntl.ioctl(fd,I2C_RDWR,I2cRdwrIoctlData(msgs=msgs,nmsgs=2))
        r=list(rb.raw); return r[1] if r[0]==0x50 else -1
    except OSError: return -1

def main():
    if os.geteuid()!=0: sys.exit("need root")
    fd=open_bus()
    try:
        for reg in range(0x00,0x100):
            v=ec_read(fd,reg)
            if v<0: print(f"0x{reg:02x}: ERR")
            else:   print(f"0x{reg:02x}: 0x{v:02x} ({v})")
    finally:
        os.close(fd)

if __name__=="__main__": main()
