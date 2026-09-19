"""
Jarvis Memory Manager

Provides access to system RAM via /proc/meminfo, /dev/mem,
and mmap for direct memory operations.
"""

import os
import mmap
import struct
from typing import Optional


class MemoryRegion:
    """Represents a mapped memory region."""

    def __init__(self, start: int, size: int, mm: mmap.mmap):
        self.start = start
        self.size = size
        self.mmap = mm

    def read(self, offset: int, length: int) -> bytes:
        """Read bytes from the mapped region."""
        if offset + length > self.size:
            length = self.size - offset
        self.mmap.seek(offset)
        return self.mmap.read(length)

    def write(self, offset: int, data: bytes):
        """Write bytes to the mapped region."""
        if offset + len(data) > self.size:
            raise ValueError("Write exceeds region bounds")
        self.mmap.seek(offset)
        self.mmap.write(data)

    def close(self):
        """Unmap the memory region."""
        self.mmap.close()


class MemoryManager:
    """
    Manages system RAM access.

    Provides:
    - Memory statistics from /proc/meminfo
    - Direct memory mapping via /dev/mem (when available)
    - Anonymous memory allocation via mmap
    - NUMA topology information
    """

    def __init__(self, config: dict):
        self.config = config
        self.max_map_mb = config.get("max_map_mb", 256)
        self._regions: list[MemoryRegion] = []
        self._dev_mem_fd: Optional[int] = None
        self._initialized = False

    def initialize(self):
        """Initialize memory management."""
        # Try to open /dev/mem for physical memory access
        if os.path.exists("/dev/mem"):
            try:
                self._dev_mem_fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
            except (OSError, PermissionError):
                self._dev_mem_fd = None

        self._initialized = True

    def get_stats(self) -> dict:
        """Read memory statistics from /proc/meminfo."""
        stats = {}
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val_parts = parts[1].strip().split()
                        try:
                            value_kb = int(val_parts[0])
                            stats[key] = value_kb
                        except (ValueError, IndexError):
                            stats[key] = parts[1].strip()
        except FileNotFoundError:
            return {"error": "/proc/meminfo not available"}

        # Compute derived stats
        total = stats.get("MemTotal", 0)
        available = stats.get("MemAvailable", stats.get("MemFree", 0))
        if total > 0:
            used = total - available
            stats["used_kb"] = used
            stats["used_percent"] = round(used / total * 100, 1)
            stats["total_mb"] = round(total / 1024, 1)
            stats["available_mb"] = round(available / 1024, 1)
            stats["used_mb"] = round(used / 1024, 1)

        return stats

    def get_state(self) -> dict:
        """Return current memory manager state."""
        return {
            "initialized": self._initialized,
            "dev_mem_available": self._dev_mem_fd is not None,
            "mapped_regions": len(self._regions),
            "max_map_mb": self.max_map_mb,
            "stats": self.get_stats(),
        }

    def map_physical(self, address: int, size: int) -> Optional[MemoryRegion]:
        """
        Map a physical memory region via /dev/mem.

        This provides direct access to physical RAM, including video memory
        regions. Requires /dev/mem access.
        """
        if self._dev_mem_fd is None:
            return None

        # Enforce size limit
        max_bytes = self.max_map_mb * 1024 * 1024
        total_mapped = sum(r.size for r in self._regions)
        if total_mapped + size > max_bytes:
            raise MemoryError(
                f"Would exceed max mapping limit of {self.max_map_mb}MB"
            )

        try:
            # Align to page boundary
            page_size = os.sysconf("SC_PAGE_SIZE")
            aligned_addr = address & ~(page_size - 1)
            offset_in_page = address - aligned_addr
            aligned_size = size + offset_in_page

            mm = mmap.mmap(
                self._dev_mem_fd,
                aligned_size,
                mmap.MAP_SHARED,
                mmap.PROT_READ | mmap.PROT_WRITE,
                offset=aligned_addr,
            )

            region = MemoryRegion(address, size, mm)
            self._regions.append(region)
            return region

        except (OSError, mmap.error) as e:
            raise MemoryError(f"Failed to map physical memory: {e}") from e

    def allocate(self, size: int) -> MemoryRegion:
        """Allocate anonymous mapped memory."""
        max_bytes = self.max_map_mb * 1024 * 1024
        total_mapped = sum(r.size for r in self._regions)
        if total_mapped + size > max_bytes:
            raise MemoryError(
                f"Would exceed max mapping limit of {self.max_map_mb}MB"
            )

        mm = mmap.mmap(-1, size, mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                        mmap.PROT_READ | mmap.PROT_WRITE)
        region = MemoryRegion(0, size, mm)
        self._regions.append(region)
        return region

    def get_numa_info(self) -> dict:
        """Read NUMA topology from sysfs."""
        numa = {"nodes": []}
        node_dir = "/sys/devices/system/node"

        if not os.path.isdir(node_dir):
            return {"available": False}

        try:
            for entry in sorted(os.listdir(node_dir)):
                if not entry.startswith("node"):
                    continue
                node_path = os.path.join(node_dir, entry)
                node_info = {"name": entry}

                # Memory info
                meminfo_path = os.path.join(node_path, "meminfo")
                if os.path.exists(meminfo_path):
                    with open(meminfo_path, "r") as f:
                        node_info["meminfo"] = f.read()[:500]

                numa["nodes"].append(node_info)
        except OSError:
            pass

        numa["available"] = len(numa["nodes"]) > 0
        return numa

    def get_iomem_map(self) -> list[dict]:
        """Read physical memory map from /proc/iomem."""
        regions = []
        try:
            with open("/proc/iomem", "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        addr_range = parts[0].strip()
                        name = parts[1].strip()
                        regions.append({
                            "range": addr_range,
                            "name": name,
                        })
        except (FileNotFoundError, PermissionError):
            pass
        return regions

    def cleanup(self):
        """Release all mapped memory regions."""
        for region in self._regions:
            try:
                region.close()
            except Exception:
                pass
        self._regions.clear()

        if self._dev_mem_fd is not None:
            os.close(self._dev_mem_fd)
            self._dev_mem_fd = None

        self._initialized = False
