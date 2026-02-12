"""
OpenClaw Display Manager

Provides access to the screen via Linux framebuffer (/dev/fb0) and
DRM subsystem for video memory access.
"""

import os
import struct
import mmap
import fcntl
from typing import Optional


# Linux framebuffer ioctl constants
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602


class FramebufferInfo:
    """Parsed framebuffer screen information."""

    def __init__(self):
        self.width = 0
        self.height = 0
        self.bits_per_pixel = 0
        self.line_length = 0
        self.mem_length = 0
        self.red_offset = 0
        self.green_offset = 0
        self.blue_offset = 0

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "bpp": self.bits_per_pixel,
            "line_length": self.line_length,
            "mem_length": self.mem_length,
        }


class DisplayManager:
    """
    Manages screen access via Linux framebuffer and DRM.

    Provides:
    - Framebuffer read/write for pixel-level screen access
    - Screen resolution and color depth info
    - Video memory mapping
    - DRM device enumeration
    """

    def __init__(self, config: dict):
        self.config = config
        self.fb_path = config.get("framebuffer", "/dev/fb0")
        self.fb_fd: Optional[int] = None
        self.fb_mmap: Optional[mmap.mmap] = None
        self.info = FramebufferInfo()
        self._initialized = False

    def initialize(self):
        """Initialize framebuffer access."""
        if not os.path.exists(self.fb_path):
            # No framebuffer available - work in headless mode
            self._initialized = False
            return

        try:
            self.fb_fd = os.open(self.fb_path, os.O_RDWR)
            self._read_screen_info()
            self._map_framebuffer()
            self._initialized = True
        except (OSError, PermissionError) as e:
            self._initialized = False
            raise RuntimeError(f"Failed to initialize framebuffer: {e}") from e

    def _read_screen_info(self):
        """Read framebuffer variable screen info via ioctl."""
        if self.fb_fd is None:
            return

        # FBIOGET_VSCREENINFO returns struct fb_var_screeninfo
        # First 8 fields are u32: xres, yres, xres_virtual, yres_virtual,
        #                          xoffset, yoffset, bits_per_pixel, grayscale
        try:
            buf = bytearray(160)  # fb_var_screeninfo is ~160 bytes
            fcntl.ioctl(self.fb_fd, FBIOGET_VSCREENINFO, buf)
            (xres, yres, _, _, _, _, bpp, _) = struct.unpack_from("8I", buf, 0)
            self.info.width = xres
            self.info.height = yres
            self.info.bits_per_pixel = bpp

            # Color offsets (at offset 32 in the struct)
            # Each bitfield is {offset, length, msb_right} = 3 * u32
            self.info.red_offset = struct.unpack_from("I", buf, 32)[0]
            self.info.green_offset = struct.unpack_from("I", buf, 44)[0]
            self.info.blue_offset = struct.unpack_from("I", buf, 56)[0]
        except (OSError, struct.error):
            pass

        # FBIOGET_FSCREENINFO for line_length and memory size
        try:
            buf = bytearray(168)  # fb_fix_screeninfo
            fcntl.ioctl(self.fb_fd, FBIOGET_FSCREENINFO, buf)
            # line_length is at offset 48, smem_len at offset 8
            self.info.mem_length = struct.unpack_from("I", buf, 8)[0]
            self.info.line_length = struct.unpack_from("I", buf, 48)[0]
        except (OSError, struct.error):
            # Estimate
            self.info.line_length = self.info.width * (self.info.bits_per_pixel // 8)
            self.info.mem_length = self.info.line_length * self.info.height

    def _map_framebuffer(self):
        """Memory-map the framebuffer for direct pixel access."""
        if self.fb_fd is None or self.info.mem_length == 0:
            return

        try:
            self.fb_mmap = mmap.mmap(
                self.fb_fd,
                self.info.mem_length,
                mmap.MAP_SHARED,
                mmap.PROT_READ | mmap.PROT_WRITE,
            )
        except (OSError, ValueError):
            self.fb_mmap = None

    def get_state(self) -> dict:
        """Return current display state."""
        state = {
            "initialized": self._initialized,
            "framebuffer": self.fb_path,
            "resolution": f"{self.info.width}x{self.info.height}",
            "bpp": self.info.bits_per_pixel,
            "mapped": self.fb_mmap is not None,
        }

        # Check for DRM devices
        drm_devices = []
        drm_dir = "/dev/dri"
        if os.path.isdir(drm_dir):
            for entry in os.listdir(drm_dir):
                drm_devices.append(os.path.join(drm_dir, entry))
        state["drm_devices"] = drm_devices

        return state

    def read_pixel(self, x: int, y: int) -> tuple:
        """Read a pixel at (x, y) as (R, G, B)."""
        if not self.fb_mmap:
            return (0, 0, 0)

        bpp_bytes = self.info.bits_per_pixel // 8
        offset = y * self.info.line_length + x * bpp_bytes

        if offset + bpp_bytes > self.info.mem_length:
            return (0, 0, 0)

        self.fb_mmap.seek(offset)
        pixel_data = self.fb_mmap.read(bpp_bytes)

        if bpp_bytes == 4:  # 32-bit BGRA
            b, g, r, _ = struct.unpack("BBBB", pixel_data)
            return (r, g, b)
        elif bpp_bytes == 3:  # 24-bit BGR
            b, g, r = struct.unpack("BBB", pixel_data)
            return (r, g, b)
        elif bpp_bytes == 2:  # 16-bit RGB565
            val = struct.unpack("H", pixel_data)[0]
            r = ((val >> 11) & 0x1F) << 3
            g = ((val >> 5) & 0x3F) << 2
            b = (val & 0x1F) << 3
            return (r, g, b)

        return (0, 0, 0)

    def write_pixel(self, x: int, y: int, r: int, g: int, b: int):
        """Write a pixel at (x, y) with color (R, G, B)."""
        if not self.fb_mmap:
            return

        bpp_bytes = self.info.bits_per_pixel // 8
        offset = y * self.info.line_length + x * bpp_bytes

        if offset + bpp_bytes > self.info.mem_length:
            return

        if bpp_bytes == 4:
            data = struct.pack("BBBB", b, g, r, 255)
        elif bpp_bytes == 3:
            data = struct.pack("BBB", b, g, r)
        elif bpp_bytes == 2:
            val = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            data = struct.pack("H", val)
        else:
            return

        self.fb_mmap.seek(offset)
        self.fb_mmap.write(data)

    def read_region(self, x: int, y: int, width: int, height: int) -> bytes:
        """Read a rectangular region of the framebuffer."""
        if not self.fb_mmap:
            return b""

        bpp_bytes = self.info.bits_per_pixel // 8
        result = bytearray()

        for row in range(y, min(y + height, self.info.height)):
            offset = row * self.info.line_length + x * bpp_bytes
            length = min(width * bpp_bytes,
                         self.info.mem_length - offset)
            if length <= 0:
                break
            self.fb_mmap.seek(offset)
            result.extend(self.fb_mmap.read(length))

        return bytes(result)

    def get_video_memory_info(self) -> dict:
        """Get video memory information from /dev/mem and DRM."""
        info = {"framebuffer_size": self.info.mem_length}

        # Check /dev/mem for video memory region
        if os.path.exists("/dev/mem"):
            info["dev_mem_available"] = True
            try:
                info["dev_mem_readable"] = os.access("/dev/mem", os.R_OK)
                info["dev_mem_writable"] = os.access("/dev/mem", os.W_OK)
            except OSError:
                pass

        # Check DRM for GPU memory info
        drm_dir = "/sys/class/drm"
        if os.path.isdir(drm_dir):
            for card in os.listdir(drm_dir):
                mem_path = os.path.join(drm_dir, card, "device", "mem_info_vram_total")
                if os.path.exists(mem_path):
                    try:
                        with open(mem_path, "r") as f:
                            info[f"{card}_vram"] = int(f.read().strip())
                    except (ValueError, OSError):
                        pass

        return info

    def cleanup(self):
        """Release framebuffer resources."""
        if self.fb_mmap:
            self.fb_mmap.close()
            self.fb_mmap = None
        if self.fb_fd is not None:
            os.close(self.fb_fd)
            self.fb_fd = None
        self._initialized = False
