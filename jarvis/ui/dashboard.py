"""
Jarvis Status Dashboard

Provides a continuously updating status display showing
agent state, hardware status, and task progress.
"""

import os
import sys
import time


class Dashboard:
    """
    Live status dashboard for the Jarvis agent.

    Renders a text-based dashboard to the terminal showing
    real-time agent metrics, hardware state, and task queues.
    """

    def __init__(self, agent):
        self.agent = agent
        self.running = False
        self.refresh_interval = 1.0  # seconds

    def render(self) -> str:
        """Render the dashboard to a string."""
        status = self.agent.get_status()
        lines = []

        # Header
        lines.append("=" * 60)
        lines.append("  Jarvis Agent Dashboard")
        lines.append("=" * 60)
        lines.append("")

        # Agent status
        lines.append(f"  Agent: {status['name']}")
        lines.append(f"  Cycle: {status['cycle_count']:>6d}    "
                      f"Tasks: {status['pending_tasks']} pending / "
                      f"{status['completed_tasks']} done")

        current = status.get("current_task")
        if current:
            lines.append(f"  Current: {current['description'][:40]}")
        else:
            lines.append("  Current: idle")
        lines.append("")

        # Hardware
        lines.append("  Hardware:")
        hw = status.get("hardware", {})
        for name, available in hw.items():
            indicator = "[*]" if available else "[ ]"
            lines.append(f"    {indicator} {name}")
        lines.append("")

        # Memory stats
        if self.agent.hardware.get("memory"):
            try:
                mem = self.agent.hardware["memory"].get_stats()
                total = mem.get("total_mb", 0)
                used = mem.get("used_mb", 0)
                pct = mem.get("used_percent", 0)
                bar_len = 30
                filled = int(bar_len * pct / 100)
                bar = "#" * filled + "-" * (bar_len - filled)
                lines.append(f"  RAM: [{bar}] {pct}%")
                lines.append(f"       {used}MB / {total}MB")
            except Exception:
                lines.append("  RAM: unavailable")
        lines.append("")

        # Recent tasks
        lines.append("  Recent Activity:")
        history = self.agent.task_history[-5:]
        if history:
            for entry in reversed(history):
                task = entry["task"]
                result = entry["result"]
                icon = "+" if result.get("success") else "!"
                lines.append(f"    [{icon}] {task['description'][:45]}")
        else:
            lines.append("    (none)")

        lines.append("")
        lines.append("-" * 60)
        lines.append("  Press Ctrl+C to return to command prompt")

        return "\n".join(lines)

    def run(self, duration: float = 0):
        """Run the dashboard until stopped or duration elapsed."""
        self.running = True
        start = time.time()

        try:
            while self.running:
                # Clear screen
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.write(self.render())
                sys.stdout.write("\n")
                sys.stdout.flush()

                # Run an agent cycle
                self.agent.run_cycle()

                # Check duration
                if duration > 0 and (time.time() - start) >= duration:
                    break

                time.sleep(self.refresh_interval)

        except KeyboardInterrupt:
            pass
        finally:
            self.running = False

    def stop(self):
        """Stop the dashboard."""
        self.running = False
