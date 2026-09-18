"""
Jarvis Storage Manager

Provides access to block devices (HDD, SSD, NVMe) via Linux
block device layer and sysfs.
"""

import os
import glob
import subprocess
from typing import Optional


class BlockDevice:
    """Represents a block storage device."""

    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path  # e.g., /dev/sda
        self.size_bytes = 0
        self.model = ""
        self.removable = False
        self.readonly = False
        self.partitions: list[dict] = []
        self._fd: Optional[int] = None

    def probe(self):
        """Read device information from sysfs."""
        sysfs_base = f"/sys/block/{self.name}"

        # Size (in 512-byte sectors)
        size_path = os.path.join(sysfs_base, "size")
        if os.path.exists(size_path):
            try:
                with open(size_path, "r") as f:
                    sectors = int(f.read().strip())
                self.size_bytes = sectors * 512
            except (ValueError, OSError):
                pass

        # Model
        model_path = os.path.join(sysfs_base, "device", "model")
        if os.path.exists(model_path):
            try:
                with open(model_path, "r") as f:
                    self.model = f.read().strip()
            except OSError:
                pass

        # Removable
        removable_path = os.path.join(sysfs_base, "removable")
        if os.path.exists(removable_path):
            try:
                with open(removable_path, "r") as f:
                    self.removable = f.read().strip() == "1"
            except OSError:
                pass

        # Read-only
        ro_path = os.path.join(sysfs_base, "ro")
        if os.path.exists(ro_path):
            try:
                with open(ro_path, "r") as f:
                    self.readonly = f.read().strip() == "1"
            except OSError:
                pass

        # Partitions
        self.partitions = []
        for entry in sorted(glob.glob(os.path.join(sysfs_base, f"{self.name}*"))):
            part_name = os.path.basename(entry)
            if part_name == self.name:
                continue
            part_info = {"name": part_name, "path": f"/dev/{part_name}"}
            part_size_path = os.path.join(entry, "size")
            if os.path.exists(part_size_path):
                try:
                    with open(part_size_path, "r") as f:
                        part_info["size_bytes"] = int(f.read().strip()) * 512
                except (ValueError, OSError):
                    pass
            self.partitions.append(part_info)

    def open(self, readonly: bool = False):
        """Open the block device for I/O."""
        flags = os.O_RDONLY if readonly else os.O_RDWR
        self._fd = os.open(self.path, flags)

    def read_sectors(self, start_sector: int, count: int) -> bytes:
        """Read sectors from the device."""
        if self._fd is None:
            raise IOError("Device not open")
        sector_size = 512
        os.lseek(self._fd, start_sector * sector_size, os.SEEK_SET)
        return os.read(self._fd, count * sector_size)

    def write_sectors(self, start_sector: int, data: bytes):
        """Write sectors to the device."""
        if self._fd is None:
            raise IOError("Device not open")
        if self.readonly:
            raise IOError("Device is read-only")
        sector_size = 512
        if len(data) % sector_size != 0:
            raise ValueError("Data must be aligned to sector size (512 bytes)")
        os.lseek(self._fd, start_sector * sector_size, os.SEEK_SET)
        os.write(self._fd, data)

    def close(self):
        """Close the device."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def to_dict(self) -> dict:
        size_gb = round(self.size_bytes / (1024**3), 2) if self.size_bytes else 0
        return {
            "name": self.name,
            "path": self.path,
            "model": self.model,
            "size_bytes": self.size_bytes,
            "size_gb": size_gb,
            "removable": self.removable,
            "readonly": self.readonly,
            "partitions": self.partitions,
            "open": self._fd is not None,
        }


class StorageManager:
    """
    Manages block storage devices.

    Discovers and provides access to all block devices (HDD, SSD, NVMe, USB)
    via Linux sysfs and direct device I/O.
    """

    def __init__(self, config: dict):
        self.config = config
        self.enforce_readonly = config.get("readonly", False)
        self.devices: list[BlockDevice] = []
        self._initialized = False

    def initialize(self):
        """Discover all block devices."""
        self.devices.clear()

        # Scan /sys/block for block devices
        block_dir = "/sys/block"
        if os.path.isdir(block_dir):
            for entry in sorted(os.listdir(block_dir)):
                # Skip loop, ram, and dm devices
                if entry.startswith(("loop", "ram", "dm-")):
                    continue

                dev_path = f"/dev/{entry}"
                if os.path.exists(dev_path):
                    dev = BlockDevice(entry, dev_path)
                    dev.probe()
                    if self.enforce_readonly:
                        dev.readonly = True
                    self.devices.append(dev)

        self._initialized = True

    def get_devices(self) -> list[dict]:
        """List all discovered storage devices."""
        return [d.to_dict() for d in self.devices]

    def get_device(self, name: str) -> Optional[BlockDevice]:
        """Get a specific device by name (e.g., 'sda')."""
        for dev in self.devices:
            if dev.name == name:
                return dev
        return None

    def get_state(self) -> dict:
        """Return current storage manager state."""
        total_bytes = sum(d.size_bytes for d in self.devices)
        return {
            "initialized": self._initialized,
            "device_count": len(self.devices),
            "total_storage_gb": round(total_bytes / (1024**3), 2),
            "enforce_readonly": self.enforce_readonly,
            "devices": self.get_devices(),
        }

    def get_mount_info(self) -> list[dict]:
        """Read current mount information from /proc/mounts."""
        mounts = []
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 4:
                        mounts.append({
                            "device": parts[0],
                            "mountpoint": parts[1],
                            "fstype": parts[2],
                            "options": parts[3],
                        })
        except FileNotFoundError:
            pass
        return mounts

    def get_disk_usage(self) -> list[dict]:
        """Get disk usage for mounted filesystems."""
        usage = []
        try:
            result = subprocess.run(
                ["df", "-B1", "--output=source,target,size,used,avail,pcent"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                for line in lines[1:]:  # Skip header
                    parts = line.split()
                    if len(parts) >= 6:
                        usage.append({
                            "device": parts[0],
                            "mountpoint": parts[1],
                            "size": int(parts[2]) if parts[2].isdigit() else 0,
                            "used": int(parts[3]) if parts[3].isdigit() else 0,
                            "available": int(parts[4]) if parts[4].isdigit() else 0,
                            "use_percent": parts[5],
                        })
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return usage

    def cleanup(self):
        """Close all open devices."""
        for dev in self.devices:
            dev.close()
        self.devices.clear()
        self._initialized = False
