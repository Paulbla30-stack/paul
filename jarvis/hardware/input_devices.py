"""
Jarvis Input Device Manager

Provides access to keyboard and mouse via Linux input subsystem
(/dev/input/event*, /dev/input/mice).
"""

import os
import struct
import select
import glob
from typing import Optional


# Linux input event format: struct input_event
# time (timeval = 2 * long), type (u16), code (u16), value (s32)
# On 64-bit: 8 + 8 + 2 + 2 + 4 = 24 bytes
INPUT_EVENT_FORMAT = "llHHi"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FORMAT)

# Event types
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02  # Relative movement (mouse)
EV_ABS = 0x03  # Absolute movement (touchscreen/tablet)

# Relative axis codes
REL_X = 0x00
REL_Y = 0x01
REL_WHEEL = 0x08

# Key state values
KEY_RELEASE = 0
KEY_PRESS = 1
KEY_REPEAT = 2

# ioctl for getting device name
EVIOCGNAME = 0x80FF4506  # Approx, varies by arch


class InputDevice:
    """Represents a single Linux input device."""

    def __init__(self, path: str):
        self.path = path
        self.fd: Optional[int] = None
        self.name = "unknown"
        self.device_type = "unknown"  # keyboard, mouse, touchpad, etc.

    def open(self):
        """Open the input device for reading."""
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        self._detect_type()

    def _detect_type(self):
        """Detect device type by reading its name and capabilities."""
        # Try to get device name from sysfs
        basename = os.path.basename(self.path)
        sysfs_name = f"/sys/class/input/{basename}/device/name"
        if os.path.exists(sysfs_name):
            try:
                with open(sysfs_name, "r") as f:
                    self.name = f.read().strip()
            except OSError:
                pass

        # Classify by name heuristics
        name_lower = self.name.lower()
        if any(k in name_lower for k in ("keyboard", "kbd")):
            self.device_type = "keyboard"
        elif any(k in name_lower for k in ("mouse", "mice", "trackpad", "touchpad")):
            self.device_type = "mouse"
        elif "touch" in name_lower:
            self.device_type = "touchscreen"
        else:
            # Check capabilities via sysfs
            cap_path = f"/sys/class/input/{basename}/device/capabilities/ev"
            if os.path.exists(cap_path):
                try:
                    with open(cap_path, "r") as f:
                        caps = int(f.read().strip(), 16)
                    if caps & (1 << EV_KEY) and caps & (1 << EV_REL):
                        self.device_type = "mouse"
                    elif caps & (1 << EV_KEY):
                        self.device_type = "keyboard"
                except (OSError, ValueError):
                    pass

    def read_events(self, max_events: int = 64) -> list[dict]:
        """Read pending input events."""
        if self.fd is None:
            return []

        events = []
        try:
            while len(events) < max_events:
                data = os.read(self.fd, INPUT_EVENT_SIZE)
                if len(data) < INPUT_EVENT_SIZE:
                    break

                tv_sec, tv_usec, ev_type, code, value = struct.unpack(
                    INPUT_EVENT_FORMAT, data
                )

                if ev_type == EV_SYN:
                    continue

                event = {
                    "timestamp": tv_sec + tv_usec / 1000000.0,
                    "device": self.path,
                    "device_type": self.device_type,
                }

                if ev_type == EV_KEY:
                    event["type"] = "key"
                    event["code"] = code
                    event["value"] = value  # 0=release, 1=press, 2=repeat
                    event["state"] = (
                        "press" if value == KEY_PRESS
                        else "release" if value == KEY_RELEASE
                        else "repeat"
                    )
                elif ev_type == EV_REL:
                    event["type"] = "relative"
                    if code == REL_X:
                        event["axis"] = "x"
                    elif code == REL_Y:
                        event["axis"] = "y"
                    elif code == REL_WHEEL:
                        event["axis"] = "wheel"
                    else:
                        event["axis"] = code
                    event["value"] = value
                elif ev_type == EV_ABS:
                    event["type"] = "absolute"
                    event["code"] = code
                    event["value"] = value
                else:
                    event["type"] = f"0x{ev_type:02x}"
                    event["code"] = code
                    event["value"] = value

                events.append(event)

        except BlockingIOError:
            pass  # No more events
        except OSError:
            pass

        return events

    def close(self):
        """Close the device."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "type": self.device_type,
            "open": self.fd is not None,
        }


class InputManager:
    """
    Manages all input devices (keyboards, mice, etc.).

    Discovers and opens Linux input devices, providing a unified
    interface for reading keyboard and mouse events.
    """

    def __init__(self, config: dict):
        self.config = config
        self.grab_exclusive = config.get("grab_exclusive", False)
        self.devices: list[InputDevice] = []
        self._initialized = False

    def initialize(self):
        """Discover and open all input devices."""
        # Find all event devices
        event_paths = sorted(glob.glob("/dev/input/event*"))
        for path in event_paths:
            if os.access(path, os.R_OK):
                dev = InputDevice(path)
                try:
                    dev.open()
                    self.devices.append(dev)
                except OSError:
                    pass

        # Also track the mice aggregator
        mice_path = "/dev/input/mice"
        if os.path.exists(mice_path) and os.access(mice_path, os.R_OK):
            dev = InputDevice(mice_path)
            dev.name = "PS/2 mice aggregator"
            dev.device_type = "mouse"
            self.devices.append(dev)

        self._initialized = True

    def poll_events(self, timeout_ms: int = 0) -> list[dict]:
        """Poll all devices for input events."""
        all_events = []

        # Use select to check which devices have data
        readable_fds = [d.fd for d in self.devices if d.fd is not None]
        if not readable_fds:
            return []

        try:
            timeout_sec = timeout_ms / 1000.0 if timeout_ms > 0 else 0
            ready, _, _ = select.select(readable_fds, [], [], timeout_sec)
        except (OSError, ValueError):
            ready = readable_fds  # Fall back to trying all

        fd_to_device = {d.fd: d for d in self.devices if d.fd is not None}
        for fd in ready:
            dev = fd_to_device.get(fd)
            if dev:
                events = dev.read_events()
                all_events.extend(events)

        return all_events

    def list_devices(self) -> list[dict]:
        """List all discovered input devices."""
        return [d.to_dict() for d in self.devices]

    def get_keyboards(self) -> list[InputDevice]:
        """Get all keyboard devices."""
        return [d for d in self.devices if d.device_type == "keyboard"]

    def get_mice(self) -> list[InputDevice]:
        """Get all mouse devices."""
        return [d for d in self.devices if d.device_type == "mouse"]

    def get_state(self) -> dict:
        """Return current input device state."""
        return {
            "initialized": self._initialized,
            "device_count": len(self.devices),
            "keyboards": len(self.get_keyboards()),
            "mice": len(self.get_mice()),
            "devices": self.list_devices(),
        }

    def cleanup(self):
        """Close all input devices."""
        for dev in self.devices:
            dev.close()
        self.devices.clear()
        self._initialized = False
