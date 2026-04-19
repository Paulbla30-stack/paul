"""
OpenClaw Console UI

Text-based console interface for interacting with the agent.
Provides a command prompt, status display, and live dashboard.
"""

import sys
import time
import os


class ConsoleUI:
    """
    Interactive console interface for the OpenClaw agent.

    Provides a command-line interface for controlling the agent,
    viewing status, and issuing commands.
    """

    BANNER = r"""
  ___                    ____ _
 / _ \ _ __   ___ _ __  / ___| | __ ___      __
| | | | '_ \ / _ \ '_ \| |   | |/ _` \ \ /\ / /
| |_| | |_) |  __/ | | | |___| | (_| |\ V  V /
 \___/| .__/ \___|_| |_|\____|_|\__,_| \_/\_/
      |_|
    Agentic Agent Environment v1.0.0
"""

    def __init__(self, agent):
        self.agent = agent
        self.running = False
        self.commands = {
            "help": (self._cmd_help, "Show available commands"),
            "status": (self._cmd_status, "Show agent status"),
            "hardware": (self._cmd_hardware, "Show hardware status"),
            "memory": (self._cmd_memory, "Show agent memory summary"),
            "scan": (self._cmd_scan, "Run security scan"),
            "harden": (self._cmd_harden, "Apply security hardening"),
            "goal": (self._cmd_goal, "Add a goal for the agent"),
            "cycle": (self._cmd_cycle, "Run one agent cycle"),
            "run": (self._cmd_run, "Run agent continuously"),
            "stop": (self._cmd_stop, "Stop continuous execution"),
            "tasks": (self._cmd_tasks, "Show pending tasks"),
            "history": (self._cmd_history, "Show task history"),
            "devices": (self._cmd_devices, "List hardware devices"),
            "display": (self._cmd_display, "Show display info"),
            "storage": (self._cmd_storage, "Show storage devices"),
            "clear": (self._cmd_clear, "Clear screen"),
            "exit": (self._cmd_exit, "Exit OpenClaw"),
            "quit": (self._cmd_exit, "Exit OpenClaw"),
        }

    def run(self):
        """Start the interactive console."""
        self.running = True
        print(self.BANNER)
        self._show_system_summary()
        print("\nType 'help' for available commands.\n")

        while self.running:
            try:
                prompt = f"\033[1;36m[openclaw]\033[0m $ "
                line = input(prompt).strip()
                if not line:
                    continue

                parts = line.split(None, 1)
                cmd = parts[0].lower()
                args = parts[1] if len(parts) > 1 else ""

                if cmd in self.commands:
                    handler, _ = self.commands[cmd]
                    handler(args)
                else:
                    print(f"Unknown command: {cmd}")
                    print("Type 'help' for available commands.")

            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print("\nUse 'exit' to quit.")

    def _show_system_summary(self):
        """Show a brief system summary on startup."""
        print("System Summary:")
        status = self.agent.get_status()

        hw = status.get("hardware", {})
        print(f"  Hardware: ", end="")
        hw_list = [k for k, v in hw.items() if v]
        print(", ".join(hw_list) if hw_list else "none available")

        # Memory info
        if self.agent.hardware.get("memory"):
            try:
                stats = self.agent.hardware["memory"].get_stats()
                total = stats.get("total_mb", "?")
                used = stats.get("used_mb", "?")
                print(f"  RAM: {used}MB used / {total}MB total")
            except Exception:
                pass

        # Storage
        if self.agent.hardware.get("storage"):
            try:
                devices = self.agent.hardware["storage"].get_devices()
                total_gb = sum(d.get("size_gb", 0) for d in devices)
                print(f"  Storage: {len(devices)} devices, {total_gb:.1f} GB total")
            except Exception:
                pass

    def _cmd_help(self, args):
        print("\nAvailable commands:")
        print("-" * 50)
        for cmd, (_, desc) in sorted(self.commands.items()):
            print(f"  {cmd:<12s}  {desc}")
        print()

    def _cmd_status(self, args):
        status = self.agent.get_status()
        print(f"\nAgent: {status['name']}")
        print(f"  Running:         {status['running']}")
        print(f"  Cycle count:     {status['cycle_count']}")
        print(f"  Pending tasks:   {status['pending_tasks']}")
        print(f"  Completed tasks: {status['completed_tasks']}")
        print(f"  Memory entries:  {status['memory_entries']}")
        current = status.get("current_task")
        if current:
            print(f"  Current task:    {current['description']}")
        print()

    def _cmd_hardware(self, args):
        status = self.agent.get_status()
        hw = status.get("hardware", {})
        print("\nHardware Status:")
        for name, available in hw.items():
            state = "ACTIVE" if available else "NOT AVAILABLE"
            print(f"  {name:<12s}: {state}")
        print()

    def _cmd_memory(self, args):
        summary = self.agent.memory.get_summary()
        print(f"\nAgent Memory:")
        print(f"  Entries: {summary['total_entries']} / {summary['max_entries']}")
        print(f"  Categories:")
        for cat, count in summary.get("categories", {}).items():
            print(f"    {cat}: {count}")
        print()

    def _cmd_scan(self, args):
        print("Running security scan...")
        from openclaw.security.scanner import SecurityScanner
        scanner = SecurityScanner()
        report = scanner.full_scan()
        scanner.print_report(report)

    def _cmd_harden(self, args):
        print("Applying security hardening...")
        from openclaw.security.hardening import SystemHardener
        hardener = SystemHardener()
        results = hardener.apply_all()
        for r in results:
            status = "OK" if r["applied"] else f"FAILED: {r.get('error', '?')}"
            print(f"  {r['name']}: {status}")
        print()

    def _cmd_goal(self, args):
        if not args:
            print("Usage: goal <description>")
            return
        self.agent.add_goal(args)
        print(f"Goal added: {args}")

    def _cmd_cycle(self, args):
        print("Running agent cycle...")
        result = self.agent.run_cycle()
        print(f"  Cycle #{result['cycle']}")
        action = result.get("action", "idle")
        print(f"  Action: {action}")
        if action != "idle":
            task_result = result.get("result", {})
            success = task_result.get("success", "?")
            print(f"  Success: {success}")
        print()

    def _cmd_run(self, args):
        """Run agent cycles continuously."""
        max_cycles = int(args) if args.isdigit() else 10
        print(f"Running {max_cycles} agent cycles...")
        self.agent.running = True

        for i in range(max_cycles):
            if not self.agent.running:
                break
            result = self.agent.run_cycle()
            action = result.get("action", "idle")
            if action != "idle":
                print(f"  Cycle {result['cycle']}: {action}")
            time.sleep(0.1)

        print(f"Completed {self.agent.cycle_count} total cycles.")

    def _cmd_stop(self, args):
        self.agent.running = False
        print("Agent stopped.")

    def _cmd_tasks(self, args):
        tasks = self.agent.planner.pending_tasks
        if not tasks:
            print("No pending tasks.")
            return
        print(f"\nPending tasks ({len(tasks)}):")
        for t in sorted(tasks):
            print(f"  [P{t.priority}] {t.description} ({t.task_type.value})")
        print()

    def _cmd_history(self, args):
        history = self.agent.task_history
        if not history:
            print("No task history.")
            return
        limit = int(args) if args.isdigit() else 10
        print(f"\nRecent tasks (last {limit}):")
        for entry in history[-limit:]:
            task = entry["task"]
            result = entry["result"]
            status = "OK" if result.get("success") else "FAIL"
            print(f"  [{status}] {task['description']}")
        print()

    def _cmd_devices(self, args):
        if self.agent.hardware.get("input"):
            devices = self.agent.hardware["input"].list_devices()
            print(f"\nInput devices ({len(devices)}):")
            for d in devices:
                print(f"  {d['path']}: {d['name']} ({d['type']})")
        else:
            print("Input manager not available.")
        print()

    def _cmd_display(self, args):
        if self.agent.hardware.get("display"):
            state = self.agent.hardware["display"].get_state()
            print(f"\nDisplay:")
            for k, v in state.items():
                print(f"  {k}: {v}")
            vmem = self.agent.hardware["display"].get_video_memory_info()
            print("Video Memory:")
            for k, v in vmem.items():
                print(f"  {k}: {v}")
        else:
            print("Display manager not available.")
        print()

    def _cmd_storage(self, args):
        if self.agent.hardware.get("storage"):
            devices = self.agent.hardware["storage"].get_devices()
            print(f"\nStorage devices ({len(devices)}):")
            for d in devices:
                print(f"  {d['path']}: {d['model'] or 'Unknown'}")
                print(f"    Size: {d['size_gb']} GB, "
                      f"RO: {d['readonly']}, "
                      f"Removable: {d['removable']}")
                for p in d.get("partitions", []):
                    size = p.get("size_bytes", 0)
                    size_mb = round(size / (1024 * 1024), 1) if size else "?"
                    print(f"      {p['name']}: {size_mb} MB")

            # Mount info
            mounts = self.agent.hardware["storage"].get_mount_info()
            if mounts:
                print(f"\nMount points ({len(mounts)}):")
                for m in mounts:
                    print(f"  {m['device']} -> {m['mountpoint']} ({m['fstype']})")
        else:
            print("Storage manager not available.")
        print()

    def _cmd_clear(self, args):
        os.system("clear" if os.name != "nt" else "cls")

    def _cmd_exit(self, args):
        print("Shutting down OpenClaw...")
        self.running = False
        self.agent.shutdown()
