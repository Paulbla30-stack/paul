"""Tests for Jarvis Hardware Access Layer."""

import unittest
import os
import tempfile

from jarvis.hardware.display import DisplayManager, FramebufferInfo
from jarvis.hardware.input_devices import InputManager, InputDevice
from jarvis.hardware.mem import MemoryManager, MemoryRegion
from jarvis.hardware.storage import StorageManager, BlockDevice


class TestFramebufferInfo(unittest.TestCase):
    """Tests for FramebufferInfo."""

    def test_defaults(self):
        info = FramebufferInfo()
        self.assertEqual(info.width, 0)
        self.assertEqual(info.height, 0)
        self.assertEqual(info.bits_per_pixel, 0)

    def test_to_dict(self):
        info = FramebufferInfo()
        info.width = 1920
        info.height = 1080
        info.bits_per_pixel = 32
        d = info.to_dict()
        self.assertEqual(d["width"], 1920)
        self.assertEqual(d["height"], 1080)
        self.assertEqual(d["bpp"], 32)


class TestDisplayManager(unittest.TestCase):
    """Tests for DisplayManager."""

    def test_init(self):
        config = {"framebuffer": "/dev/fb_nonexistent"}
        dm = DisplayManager(config)
        self.assertFalse(dm._initialized)

    def test_initialize_no_fb(self):
        config = {"framebuffer": "/dev/fb_nonexistent"}
        dm = DisplayManager(config)
        dm.initialize()
        self.assertFalse(dm._initialized)

    def test_get_state_not_initialized(self):
        config = {"framebuffer": "/dev/fb_nonexistent"}
        dm = DisplayManager(config)
        state = dm.get_state()
        self.assertFalse(state["initialized"])
        self.assertFalse(state["mapped"])

    def test_read_pixel_no_mmap(self):
        config = {"framebuffer": "/dev/fb_nonexistent"}
        dm = DisplayManager(config)
        self.assertEqual(dm.read_pixel(0, 0), (0, 0, 0))

    def test_read_region_no_mmap(self):
        config = {"framebuffer": "/dev/fb_nonexistent"}
        dm = DisplayManager(config)
        self.assertEqual(dm.read_region(0, 0, 10, 10), b"")

    def test_cleanup(self):
        config = {"framebuffer": "/dev/fb_nonexistent"}
        dm = DisplayManager(config)
        dm.cleanup()  # Should not raise
        self.assertIsNone(dm.fb_fd)
        self.assertIsNone(dm.fb_mmap)

    def test_video_memory_info(self):
        config = {"framebuffer": "/dev/fb_nonexistent"}
        dm = DisplayManager(config)
        info = dm.get_video_memory_info()
        self.assertIn("framebuffer_size", info)


class TestInputDevice(unittest.TestCase):
    """Tests for InputDevice."""

    def test_init(self):
        dev = InputDevice("/dev/input/event99")
        self.assertEqual(dev.path, "/dev/input/event99")
        self.assertEqual(dev.name, "unknown")
        self.assertIsNone(dev.fd)

    def test_read_events_not_open(self):
        dev = InputDevice("/dev/input/event99")
        self.assertEqual(dev.read_events(), [])

    def test_to_dict(self):
        dev = InputDevice("/dev/input/event0")
        dev.name = "Test Keyboard"
        dev.device_type = "keyboard"
        d = dev.to_dict()
        self.assertEqual(d["name"], "Test Keyboard")
        self.assertEqual(d["type"], "keyboard")
        self.assertFalse(d["open"])


class TestInputManager(unittest.TestCase):
    """Tests for InputManager."""

    def test_init(self):
        mgr = InputManager({"grab_exclusive": False})
        self.assertFalse(mgr._initialized)
        self.assertEqual(len(mgr.devices), 0)

    def test_poll_events_no_devices(self):
        mgr = InputManager({"grab_exclusive": False})
        events = mgr.poll_events()
        self.assertEqual(events, [])

    def test_list_devices_empty(self):
        mgr = InputManager({"grab_exclusive": False})
        self.assertEqual(mgr.list_devices(), [])

    def test_get_state(self):
        mgr = InputManager({"grab_exclusive": False})
        state = mgr.get_state()
        self.assertFalse(state["initialized"])
        self.assertEqual(state["device_count"], 0)

    def test_cleanup(self):
        mgr = InputManager({"grab_exclusive": False})
        mgr.cleanup()
        self.assertFalse(mgr._initialized)


class TestMemoryManager(unittest.TestCase):
    """Tests for MemoryManager."""

    def test_init(self):
        mgr = MemoryManager({"max_map_mb": 128})
        self.assertEqual(mgr.max_map_mb, 128)
        self.assertFalse(mgr._initialized)

    def test_initialize(self):
        mgr = MemoryManager({"max_map_mb": 128})
        mgr.initialize()
        self.assertTrue(mgr._initialized)

    def test_get_stats(self):
        mgr = MemoryManager({"max_map_mb": 128})
        stats = mgr.get_stats()
        # Should have some content (either real data or error)
        self.assertIsInstance(stats, dict)

    def test_get_stats_has_total(self):
        mgr = MemoryManager({"max_map_mb": 128})
        stats = mgr.get_stats()
        # On Linux, should have MemTotal
        if "error" not in stats:
            self.assertIn("MemTotal", stats)

    def test_allocate(self):
        mgr = MemoryManager({"max_map_mb": 128})
        region = mgr.allocate(4096)
        self.assertEqual(region.size, 4096)
        region.close()

    def test_allocate_read_write(self):
        mgr = MemoryManager({"max_map_mb": 128})
        region = mgr.allocate(4096)
        region.write(0, b"Hello")
        data = region.read(0, 5)
        self.assertEqual(data, b"Hello")
        region.close()

    def test_allocate_exceeds_limit(self):
        mgr = MemoryManager({"max_map_mb": 1})
        # 1MB limit, try to allocate 2MB
        with self.assertRaises(MemoryError):
            mgr.allocate(2 * 1024 * 1024)

    def test_get_state(self):
        mgr = MemoryManager({"max_map_mb": 128})
        mgr.initialize()
        state = mgr.get_state()
        self.assertTrue(state["initialized"])
        self.assertEqual(state["max_map_mb"], 128)

    def test_cleanup(self):
        mgr = MemoryManager({"max_map_mb": 128})
        mgr.initialize()
        mgr.allocate(4096)
        mgr.cleanup()
        self.assertFalse(mgr._initialized)
        self.assertEqual(len(mgr._regions), 0)

    def test_get_iomem_map(self):
        mgr = MemoryManager({"max_map_mb": 128})
        regions = mgr.get_iomem_map()
        self.assertIsInstance(regions, list)


class TestBlockDevice(unittest.TestCase):
    """Tests for BlockDevice."""

    def test_init(self):
        dev = BlockDevice("sda", "/dev/sda")
        self.assertEqual(dev.name, "sda")
        self.assertEqual(dev.path, "/dev/sda")
        self.assertEqual(dev.size_bytes, 0)

    def test_to_dict(self):
        dev = BlockDevice("sda", "/dev/sda")
        dev.model = "Test Disk"
        dev.size_bytes = 1024 * 1024 * 1024  # 1GB
        d = dev.to_dict()
        self.assertEqual(d["name"], "sda")
        self.assertEqual(d["model"], "Test Disk")
        self.assertAlmostEqual(d["size_gb"], 1.0)


class TestStorageManager(unittest.TestCase):
    """Tests for StorageManager."""

    def test_init(self):
        mgr = StorageManager({"readonly": False})
        self.assertFalse(mgr._initialized)

    def test_initialize(self):
        mgr = StorageManager({"readonly": False})
        mgr.initialize()
        self.assertTrue(mgr._initialized)

    def test_get_devices(self):
        mgr = StorageManager({"readonly": False})
        mgr.initialize()
        devices = mgr.get_devices()
        self.assertIsInstance(devices, list)

    def test_get_mount_info(self):
        mgr = StorageManager({"readonly": False})
        mounts = mgr.get_mount_info()
        self.assertIsInstance(mounts, list)

    def test_get_state(self):
        mgr = StorageManager({"readonly": False})
        mgr.initialize()
        state = mgr.get_state()
        self.assertTrue(state["initialized"])
        self.assertIn("device_count", state)

    def test_enforce_readonly(self):
        mgr = StorageManager({"readonly": True})
        mgr.initialize()
        for dev in mgr.devices:
            self.assertTrue(dev.readonly)

    def test_cleanup(self):
        mgr = StorageManager({"readonly": False})
        mgr.initialize()
        mgr.cleanup()
        self.assertFalse(mgr._initialized)
        self.assertEqual(len(mgr.devices), 0)


if __name__ == "__main__":
    unittest.main()
