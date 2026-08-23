"""EP0 (control endpoint) handling for the raw-gadget transport.

Standard device requests (descriptors, configuration, status, features) and the interface / endpoint
recipients are answered here; everything else — interface GET_DESCRIPTOR for HID, class and vendor
requests — is the profile's business.

A half-answered EP0 request makes raw-gadget answer every further setup packet with -EBUSY while
``ep0_in_pending`` / ``ep0_out_pending`` is set, so any failure inside control handling must end in
EP0_STALL (the transport's event loop does that).
"""
from __future__ import annotations

from typing import Callable

from deckgadget.platform.rawgadget.device import RawGadgetDevice
from deckgadget.profiles.base import (
    USB_DT_CONFIG, USB_DT_DEVICE, USB_DT_DEVICE_QUALIFIER, USB_DT_OTHER_SPEED_CONFIG, USB_DT_STRING,
    USB_RECIP_DEVICE, USB_REQ_CLEAR_FEATURE, USB_REQ_GET_CONFIGURATION, USB_REQ_GET_DESCRIPTOR,
    USB_REQ_GET_INTERFACE, USB_REQ_GET_STATUS, USB_REQ_SET_CONFIGURATION, USB_REQ_SET_FEATURE,
    USB_REQ_SET_INTERFACE, USB_TYPE_STANDARD, GadgetDescriptors, Profile, SetupPacket,
)
from deckgadget.util.log import get_logger

log = get_logger("raw_gadget")

INTERFACE_AND_ENDPOINT_REQUESTS = (USB_REQ_SET_INTERFACE, USB_REQ_GET_INTERFACE, USB_REQ_GET_STATUS,
                                   USB_REQ_CLEAR_FEATURE, USB_REQ_SET_FEATURE)


class Ep0Request:
    """One setup packet plus the three ways to finish it on the device."""

    def __init__(self, device: RawGadgetDevice, setup: SetupPacket) -> None:
        self.device = device
        self.setup = setup

    def reply(self, data: bytes) -> None:
        """IN data stage. raw-gadget marks an IN request with wLength == 0 as OUT-pending (``gadget_setup``),
        so EP0_WRITE would fail with EBUSY: finish it with the zero-length status read instead."""
        if self.setup.wLength == 0:
            self.device.ep0_read(0)
        else:
            self.device.ep0_write(bytes(data[:self.setup.wLength]))

    def ack(self) -> None:
        self.device.ep0_read(0)

    def stall(self) -> None:
        self.device.ep0_stall()

    def read_data(self) -> bytes:
        return self.device.ep0_read(self.setup.wLength) if self.setup.wLength else b""


class ControlHandler:
    def __init__(self, descriptors: GadgetDescriptors, profile: Profile, high_speed: bool,
                 configured: Callable[[], bool], set_configuration: Callable[[int], None],
                 log_requests: bool = True) -> None:
        self.descriptors = descriptors
        self.profile = profile
        self.high_speed = high_speed
        self.configured = configured
        self.set_configuration = set_configuration
        self.log_requests = log_requests

    def handle(self, device: RawGadgetDevice, raw: bytes) -> None:
        setup = SetupPacket.unpack(raw)
        if self.log_requests:
            log.debug("ep0: %s", setup.describe())
        request = Ep0Request(device, setup)
        if setup.req_type == USB_TYPE_STANDARD and setup.recipient == USB_RECIP_DEVICE:
            self._standard_device_request(request)
        elif setup.req_type == USB_TYPE_STANDARD and setup.bRequest in INTERFACE_AND_ENDPOINT_REQUESTS:
            self._interface_or_endpoint_request(request)
        else:
            self._delegate_to_profile(request)

    def _standard_device_request(self, request: Ep0Request) -> None:
        setup = request.setup
        if setup.bRequest == USB_REQ_GET_DESCRIPTOR and setup.dir_in:
            self._get_descriptor(request)
        elif setup.bRequest == USB_REQ_SET_CONFIGURATION and not setup.dir_in:
            self.set_configuration(setup.wValue & 0xFF)
            request.ack()
        elif setup.bRequest == USB_REQ_GET_CONFIGURATION and setup.dir_in:
            request.reply(bytes([1 if self.configured() else 0]))
        elif setup.bRequest == USB_REQ_GET_STATUS and setup.dir_in:
            request.reply(b"\x00\x00")
        elif setup.bRequest in (USB_REQ_CLEAR_FEATURE, USB_REQ_SET_FEATURE) and not setup.dir_in:
            request.ack()
        else:
            request.stall()

    def _get_descriptor(self, request: Ep0Request) -> None:
        descriptor_type, descriptor_index = request.setup.wValue >> 8, request.setup.wValue & 0xFF
        descriptors = self.descriptors
        if descriptor_type == USB_DT_DEVICE:
            request.reply(descriptors.device_descriptor())
        elif descriptor_type == USB_DT_CONFIG:
            request.reply(descriptors.config_descriptor(USB_DT_CONFIG))
        elif descriptor_type == USB_DT_STRING:
            string_descriptor = descriptors.string(descriptor_index)
            request.reply(string_descriptor) if string_descriptor else request.stall()
        elif descriptor_type == USB_DT_DEVICE_QUALIFIER and descriptors.high_speed and self.high_speed:
            request.reply(descriptors.qualifier_descriptor())
        elif descriptor_type == USB_DT_OTHER_SPEED_CONFIG and descriptors.high_speed and self.high_speed:
            request.reply(descriptors.config_descriptor(USB_DT_OTHER_SPEED_CONFIG))
        else:
            request.stall()  # BOS, MS OS 0xEE, qualifier at full speed, …: STALL is accepted by Linux and Windows

    @staticmethod
    def _interface_or_endpoint_request(request: Ep0Request) -> None:
        setup = request.setup
        if setup.bRequest == USB_REQ_GET_INTERFACE and setup.dir_in:
            request.reply(b"\x00")
        elif setup.bRequest == USB_REQ_GET_STATUS and setup.dir_in:
            request.reply(b"\x00\x00")
        elif not setup.dir_in:
            request.ack()
        else:
            request.stall()

    def _delegate_to_profile(self, request: Ep0Request) -> None:
        result = self.profile.handle_control(request.setup, request.read_data)
        if result is None:
            request.stall()
        elif request.setup.dir_in:
            request.reply(result)
        elif request.setup.wLength == 0:
            request.ack()  # OUT without data: the profile had nothing to consume, finish the status stage
        # OUT with data: the profile consumed the data stage via read_data(), which is also the status stage
