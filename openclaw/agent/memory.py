"""
OpenClaw Agent Memory

Persistent and working memory for the agent, supporting categorized
storage, retrieval, and search.
"""

import time
from collections import defaultdict
from typing import Any, Optional


class MemoryEntry:
    """A single memory entry with metadata."""

    __slots__ = ("category", "data", "timestamp", "tags")

    def __init__(self, category: str, data: Any, tags: Optional[list[str]] = None):
        self.category = category
        self.data = data
        self.timestamp = time.time()
        self.tags = tags or []

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "data": self.data,
            "timestamp": self.timestamp,
            "tags": self.tags,
        }


class AgentMemory:
    """
    Agent memory store with categorized entries and bounded size.

    Provides:
    - Categorized storage (task_result, system_check, etc.)
    - Bounded memory with LRU eviction
    - Search by category and tags
    - Summary generation for context
    """

    def __init__(self, max_entries: int = 1000):
        self.max_entries = max_entries
        self._entries: list[MemoryEntry] = []
        self._by_category: dict[str, list[MemoryEntry]] = defaultdict(list)

    def __len__(self) -> int:
        return len(self._entries)

    def store(self, category: str, data: Any, tags: Optional[list[str]] = None):
        """Store a new memory entry."""
        entry = MemoryEntry(category=category, data=data, tags=tags)
        self._entries.append(entry)
        self._by_category[category].append(entry)

        # Evict oldest entries if over limit
        while len(self._entries) > self.max_entries:
            old = self._entries.pop(0)
            cat_list = self._by_category.get(old.category, [])
            if cat_list and cat_list[0] is old:
                cat_list.pop(0)

    def recall(self, category: str, limit: int = 10) -> list[dict]:
        """Recall recent entries from a category."""
        entries = self._by_category.get(category, [])
        return [e.to_dict() for e in entries[-limit:]]

    def recall_all(self, limit: int = 50) -> list[dict]:
        """Recall the most recent entries across all categories."""
        return [e.to_dict() for e in self._entries[-limit:]]

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Search memory entries by string matching in data."""
        results = []
        query_lower = query.lower()
        for entry in reversed(self._entries):
            data_str = str(entry.data).lower()
            if query_lower in data_str:
                results.append(entry.to_dict())
                if len(results) >= limit:
                    break
        return results

    def search_by_tags(self, tags: list[str], limit: int = 10) -> list[dict]:
        """Search memory entries by tags."""
        results = []
        tag_set = set(tags)
        for entry in reversed(self._entries):
            if tag_set.intersection(entry.tags):
                results.append(entry.to_dict())
                if len(results) >= limit:
                    break
        return results

    def get_summary(self) -> dict:
        """Generate a summary of memory contents."""
        categories = {}
        for cat, entries in self._by_category.items():
            categories[cat] = len(entries)

        return {
            "total_entries": len(self._entries),
            "max_entries": self.max_entries,
            "categories": categories,
            "oldest": (self._entries[0].timestamp if self._entries else None),
            "newest": (self._entries[-1].timestamp if self._entries else None),
        }

    def clear(self, category: Optional[str] = None):
        """Clear memory entries, optionally for a specific category only."""
        if category:
            removed = self._by_category.pop(category, [])
            removed_set = set(id(e) for e in removed)
            self._entries = [e for e in self._entries
                            if id(e) not in removed_set]
        else:
            self._entries.clear()
            self._by_category.clear()
