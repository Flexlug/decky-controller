"""raw-gadget ioctl ABI — ``include/uapi/linux/usb/raw_gadget.h`` on x86_64 (struct sizes are the ``_IOC``
size field, so they must match the kernel exactly)."""
from __future__ import annotations

from ...util.ioctl import IO, IOR, IOW, IOWR

UDC_NAME_LENGTH_MAX = 128
SZ_INIT = UDC_NAME_LENGTH_MAX * 2 + 1      # struct usb_raw_init {u8 driver[128]; u8 device[128]; u8 speed}
SZ_EVENT = 8                               # struct usb_raw_event {u32 type; u32 length; u8 data[]}
SZ_EP_IO = 8                               # struct usb_raw_ep_io {u16 ep; u16 flags; u32 length; u8 data[]}
SZ_EP_DESC = 9                             # struct usb_endpoint_descriptor (packed)
SZ_U32 = 4
USB_RAW_EPS_NUM_MAX = 30
SZ_EP_INFO = 32                            # struct usb_raw_ep_info {u8 name[16]; u32 addr; caps u32; limits 8}
SZ_EPS_INFO = USB_RAW_EPS_NUM_MAX * SZ_EP_INFO

USB_RAW_IOCTL_INIT = IOW("U", 0, SZ_INIT)
USB_RAW_IOCTL_RUN = IO("U", 1)
USB_RAW_IOCTL_EVENT_FETCH = IOR("U", 2, SZ_EVENT)
USB_RAW_IOCTL_EP0_WRITE = IOW("U", 3, SZ_EP_IO)
USB_RAW_IOCTL_EP0_READ = IOWR("U", 4, SZ_EP_IO)
USB_RAW_IOCTL_EP_ENABLE = IOW("U", 5, SZ_EP_DESC)
USB_RAW_IOCTL_EP_DISABLE = IOW("U", 6, SZ_U32)
USB_RAW_IOCTL_EP_WRITE = IOW("U", 7, SZ_EP_IO)
USB_RAW_IOCTL_EP_READ = IOWR("U", 8, SZ_EP_IO)
USB_RAW_IOCTL_CONFIGURE = IO("U", 9)
USB_RAW_IOCTL_VBUS_DRAW = IOW("U", 10, SZ_U32)
USB_RAW_IOCTL_EPS_INFO = IOR("U", 11, SZ_EPS_INFO)
USB_RAW_IOCTL_EP0_STALL = IO("U", 12)
USB_RAW_IOCTL_EP_SET_HALT = IOW("U", 13, SZ_U32)
USB_RAW_IOCTL_EP_CLEAR_HALT = IOW("U", 14, SZ_U32)
USB_RAW_IOCTL_EP_SET_WEDGE = IOW("U", 15, SZ_U32)

USB_RAW_EVENT_CONNECT = 1
USB_RAW_EVENT_CONTROL = 2
USB_RAW_EVENT_SUSPEND = 3
USB_RAW_EVENT_RESUME = 4
USB_RAW_EVENT_RESET = 5
USB_RAW_EVENT_DISCONNECT = 6
EVENT_NAMES = {1: "CONNECT", 2: "CONTROL", 3: "SUSPEND", 4: "RESUME", 5: "RESET", 6: "DISCONNECT"}

#: enum usb_device_speed (include/uapi/linux/usb/ch9.h)
USB_SPEED = {"low": 1, "full": 2, "high": 3, "super": 5, "super-plus": 6}
