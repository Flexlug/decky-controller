"""Shared test doubles: a composable fake ``/sys`` tree, small file helpers, display fakes."""
import os
import socket

import _path  # noqa: F401

from deckgadget.platform.display.base import ScreenMethod
from deckgadget.platform.display.compositor import CommandResult


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(text, (bytes, bytearray)) else "w"
    with open(path, mode) as f:
        f.write(text)


def read(path):
    with open(path) as f:
        return f.read()


def make_socket(path):
    """Create a real unix socket at ``path`` (the returned object must stay alive)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    return sock


class FakeSysfs:
    """Fake ``/sys`` + ``/dev`` + configfs under ``root``; populate with the ``add_*`` methods (chainable).

    Layouts mirror what was read on a Steam Deck OLED: Neptune at bus 3 / device 3 with interfaces 1.0–1.4,
    ``amdgpu_bl0`` backlight, ``ACAD`` power supply, ``steamdeck_hwmon`` with the PD-contract channels,
    ``steamdeck-extcon`` and the ``dwc3.1.auto`` UDC.
    """

    NEPTUNE_INTERFACES = {
        0: (3, 0, 2, [(0x81, 3, 64)]),
        1: (3, 1, 1, [(0x82, 3, 64)]),
        2: (3, 0, 0, [(0x83, 3, 64)]),
        3: (2, 2, 1, [(0x84, 3, 16)]),
        4: (10, 0, 0, [(0x85, 2, 64), (0x05, 2, 64)]),
    }

    def __init__(self, root):
        self.root = root
        self.sys = os.path.join(root, "sys")
        self.dev = os.path.join(root, "dev")
        self.configfs = os.path.join(root, "configfs")
        self.devices = os.path.join(self.sys, "bus", "usb", "devices")
        self.usbhid = os.path.join(self.sys, "bus", "usb", "drivers", "usbhid")
        self.state_file = os.path.join(root, "run", "brightness")
        self.backlight = None
        self.hwmon = None
        self.udc = None
        os.makedirs(self.sys, exist_ok=True)
        os.makedirs(self.dev, exist_ok=True)

    # --- built-in controller ("Neptune", 28de:1205) --------------------------------------------
    def add_neptune(self, bus=3, devnum=3):
        name = f"{bus}-{devnum}"
        self.neptune_name = name
        os.makedirs(self.usbhid, exist_ok=True)
        write(os.path.join(self.usbhid, "bind"), "")
        write(os.path.join(self.usbhid, "unbind"), "")
        write(os.path.join(self.sys, "bus", "usb", "drivers_probe"), "")
        device = os.path.join(self.devices, name)
        write(os.path.join(device, "idVendor"), "28de\n")
        write(os.path.join(device, "idProduct"), "1205\n")
        write(os.path.join(device, "busnum"), f"{bus}\n")
        write(os.path.join(device, "devnum"), f"{devnum}\n")
        write(os.path.join(device, "product"), "Steam Controller\n")
        write(os.path.join(self.devices, "1-1", "idVendor"), "05e3\n")
        write(os.path.join(self.devices, "1-1", "idProduct"), "0610\n")
        write(os.path.join(self.devices, "usb1", "idVendor"), "1d6b\n")
        write(os.path.join(self.devices, "usb1", "idProduct"), "0002\n")
        for number, (usb_class, subclass, protocol, endpoints) in self.NEPTUNE_INTERFACES.items():
            interface = self.interface(number)
            write(os.path.join(interface, "bInterfaceNumber"), f"{number:02x}\n")
            write(os.path.join(interface, "bInterfaceClass"), f"{usb_class:02x}\n")
            write(os.path.join(interface, "bInterfaceSubClass"), f"{subclass:02x}\n")
            write(os.path.join(interface, "bInterfaceProtocol"), f"{protocol:02x}\n")
            for address, attributes, max_packet in endpoints:
                endpoint = os.path.join(interface, f"ep_{address:02x}")
                write(os.path.join(endpoint, "bEndpointAddress"), f"{address:02x}\n")
                write(os.path.join(endpoint, "bmAttributes"), f"{attributes:02x}\n")
                write(os.path.join(endpoint, "wMaxPacketSize"), f"{max_packet:04x}\n")
                write(os.path.join(endpoint, "bInterval"), "04\n")
                write(os.path.join(endpoint, "direction"), "in\n" if address & 0x80 else "out\n")
            os.symlink(interface, os.path.join(self.devices, f"{name}:1.{number}"))
            self.bind(number, "usbhid" if number < 3 else "cdc_acm")
        return self

    def interface(self, number):
        return os.path.join(self.devices, self.neptune_name, f"{self.neptune_name}:1.{number}")

    def bind(self, number, driver):
        link = os.path.join(self.interface(number), "driver")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(os.path.join(self.sys, "bus", "usb", "drivers", driver), link)

    def unbind(self, number):
        link = os.path.join(self.interface(number), "driver")
        if os.path.lexists(link):
            os.unlink(link)

    # --- display -------------------------------------------------------------------------------
    def add_backlight(self, brightness=120, max_brightness=255, name="amdgpu_bl0"):
        self.backlight = os.path.join(self.sys, "class", "backlight", name)
        write(os.path.join(self.backlight, "brightness"), f"{brightness}\n")
        write(os.path.join(self.backlight, "max_brightness"), f"{max_brightness}\n")
        return self

    def add_input_device(self, event, name):
        write(os.path.join(self.sys, "class", "input", event, "device", "name"), f"{name}\n")
        return self

    # --- USB-C port: power, PD contract, extcon, UDC --------------------------------------------
    def add_power_supply(self, acad_online=1, with_acad=True):
        supplies = os.path.join(self.sys, "class", "power_supply")
        write(os.path.join(supplies, "BAT1", "type"), "Battery\n")
        write(os.path.join(supplies, "BAT1", "online"), "1\n")
        if with_acad:
            write(os.path.join(supplies, "ACAD", "type"), "Mains\n")
            write(os.path.join(supplies, "ACAD", "online"), f"{acad_online}\n")
        return self

    def add_hwmon(self, pd_mv=5000, pd_ma=1500, with_steamdeck=True, with_labels=True):
        hwmon = os.path.join(self.sys, "class", "hwmon")
        write(os.path.join(hwmon, "hwmon0", "name"), "amdgpu\n")
        write(os.path.join(hwmon, "hwmon0", "in0_input"), "20000\n")
        write(os.path.join(hwmon, "hwmon0", "in0_label"), "vddgfx\n")
        write(os.path.join(hwmon, "hwmon0", "curr1_input"), "999\n")
        os.makedirs(os.path.join(hwmon, "hwmon1"), exist_ok=True)
        if with_steamdeck:
            self.hwmon = os.path.join(hwmon, "hwmon3")
            write(os.path.join(self.hwmon, "name"), "steamdeck_hwmon\n")
            if with_labels:
                write(os.path.join(self.hwmon, "in0_label"), "PD Contract Voltage\n")
                write(os.path.join(self.hwmon, "curr1_label"), "PD Contract Current\n")
                write(os.path.join(self.hwmon, "temp1_label"), "Battery Temp\n")
                write(os.path.join(self.hwmon, "temp1_input"), "30000\n")
            write(os.path.join(self.hwmon, "in0_input"), f"{pd_mv}\n")
            write(os.path.join(self.hwmon, "curr1_input"), f"{pd_ma}\n")
        return self

    def add_extcon(self, usb=0, usb_host=0):
        extcon = os.path.join(self.sys, "class", "extcon", "extcon0")
        write(os.path.join(extcon, "name"), "steamdeck-extcon\n")
        write(os.path.join(extcon, "state"), f"USB={usb}\nUSB-HOST={usb_host}\nSDP=0\nCDP=0\nDCP=0\nACA=0\n")
        return self

    def add_udc(self, name="dwc3.1.auto", state="not attached", speed="UNKNOWN"):
        self.udc = os.path.join(self.sys, "class", "udc", name)
        write(os.path.join(self.udc, "state"), f"{state}\n")
        write(os.path.join(self.udc, "current_speed"), f"{speed}\n")
        write(os.path.join(self.udc, "function"), "\n")
        return self

    def set_udc_state(self, state):
        write(os.path.join(self.udc, "state"), f"{state}\n")

    def add_pci_bus(self):
        os.makedirs(os.path.join(self.sys, "bus", "pci", "devices"), exist_ok=True)
        return self

    # --- configfs gadgets ------------------------------------------------------------------------
    def add_gadget(self, name, udc="dwc3.1.auto"):
        gadget = os.path.join(self.configfs, "usb_gadget", name)
        write(os.path.join(gadget, "UDC"), f"{udc}\n")
        write(os.path.join(gadget, "idVendor"), "0x1d6b\n")
        os.makedirs(os.path.join(gadget, "strings", "0x409"))
        os.makedirs(os.path.join(gadget, "configs", "c.1", "strings", "0x409"))
        write(os.path.join(gadget, "configs", "c.1", "MaxPower"), "250\n")
        os.makedirs(os.path.join(gadget, "functions", "hid.usb0"))
        os.symlink(os.path.join(gadget, "functions", "hid.usb0"), os.path.join(gadget, "configs", "c.1", "hid.usb0"))
        return gadget



class FakeRunner:
    """Injected command runner for the compositor methods: records (argv, env, timeout, user), returns canned results."""

    def __init__(self, rc=0, stdout="", stderr="", error=None):
        self.calls = []
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.raise_exc = None

    def __call__(self, argv, env, timeout, user=None):
        self.calls.append({"argv": list(argv), "env": dict(env), "timeout": timeout, "user": user})
        if self.raise_exc is not None:
            raise self.raise_exc
        return CommandResult(self.rc, self.stdout, self.stderr, self.error)

    @property
    def argvs(self):
        return [call["argv"] for call in self.calls]


class FakeScreenMethod(ScreenMethod):
    """Scriptable screen-off strategy (records its calls) for controller / guard tests."""

    def __init__(self, name, available=True, sleep_ok=True, wake_ok=True):
        self.name = name
        self._available = available
        self.sleep_ok = sleep_ok
        self.wake_ok = wake_ok
        self.calls = []

    def available(self):
        self.calls.append("available")
        return self._available

    def sleep(self):
        self.calls.append("sleep")
        return self.sleep_ok

    def wake(self):
        self.calls.append("wake")
        return self.wake_ok

    def release(self):
        self.calls.append("release")
        return self.wake_ok
