#!/usr/bin/env python3
"""Colab Boost Console — Detached VM-side cost-guard watchdog (stdlib only).

Runs inside the Colab VM independently of the local console. Enforces:
  1. Absolute auto-stop deadline (``deadline_epoch``) with optional wind-down hooks.
  2. Idle auto-stop (``idle_timeout_s``): triggers only when BOTH the recipe
     heartbeat file has not been updated AND GPU utilization stayed < 5%.

Releases the VM directly via ``POST http://$TBE_RUNTIME_ADDR/unassign`` so
billing stops even when the user's computer is offline or the console is closed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

GPU_BUSY_THRESHOLD_PCT = 5.0
UNASSIGN_RETRIES = 3


class VmWatchdog:
    def __init__(self, config_path: str, hooks: list[dict] | None = None, unassign_url_override: str | None = None):
        self.config_path = os.path.abspath(config_path)
        self.base_dir = os.path.dirname(self.config_path)
        self.winddown_marker = os.path.join(self.base_dir, "winddown.json")
        self._initial_hooks = list(hooks or [])
        self.hooks = list(self._initial_hooks)
        self.unassign_url_override = unassign_url_override
        self.cfg: dict = {}
        self.started_at = time.time()
        self.last_gpu_active_at = 0.0
        self.winddown_executed_for: float | None = None
        self.winddown_hooks_ran: int = 0

    def log(self, msg: str) -> None:
        log_file = self.cfg.get("log_file") or os.path.join(self.base_dir, "watchdog.log")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(f"{stamp} [watchdog] {msg}\n")
        except OSError:
            pass

    def reload_config(self) -> None:
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.cfg = loaded
        except (OSError, ValueError) as exc:
            self.log(f"config read warning ({type(exc).__name__}); keeping cached config")
        started = self.cfg.get("started_at")
        if isinstance(started, (int, float)) and started > 0:
            self.started_at = float(started)
        cfg_hooks = self.cfg.get("hooks")
        merged = list(self._initial_hooks)
        if isinstance(cfg_hooks, list):
            for h in cfg_hooks:
                if isinstance(h, dict) and h not in merged:
                    merged.append(h)
        self.hooks = merged

    def sample_gpu_utilization(self) -> float | None:
        gpu_cmd = self.cfg.get("gpu_probe_cmd") or [
            "nvidia-smi",
            "--query-gpu=utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
        try:
            proc = subprocess.run(
                gpu_cmd,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
        except Exception:
            return None
        samples: list[float] = []
        for line in proc.stdout.splitlines():
            try:
                samples.append(float(line.strip()))
            except ValueError:
                continue
        return max(samples) if samples else None

    def resolve_runtime_addr(self) -> str | None:
        addr = self.cfg.get("runtime_addr") or os.environ.get("TBE_RUNTIME_ADDR")
        if addr:
            return str(addr)
        if os.path.isdir("/proc"):
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/environ", "rb") as fh:
                        for item in fh.read().split(b"\0"):
                            if item.startswith(b"TBE_RUNTIME_ADDR="):
                                return item.split(b"=", 1)[1].decode("utf-8", errors="replace")
                except OSError:
                    continue
        return None

    def release_vm(self, reason: str) -> bool:
        target_url = self.unassign_url_override
        if not target_url:
            addr = self.resolve_runtime_addr()
            if not addr:
                self.log(f"cannot unassign ({reason}): TBE_RUNTIME_ADDR missing")
                return False
            target_url = addr if addr.startswith("http") else f"http://{addr}/unassign"

        for attempt in range(1, UNASSIGN_RETRIES + 1):
            try:
                req = urllib.request.Request(target_url, data=b"", method="POST")
                with urllib.request.urlopen(req, timeout=20) as resp:
                    code = resp.status
                if 200 <= code < 300:
                    self.log(f"runtime unassigned ({reason})")
                    return True
                self.log(f"unassign HTTP {code} on attempt {attempt}")
            except Exception as exc:
                self.log(f"unassign error {type(exc).__name__} on attempt {attempt}")
            if attempt < UNASSIGN_RETRIES:
                time.sleep(2)
        return False

    def compute_idle_seconds(self, now: float) -> float:
        last_activity = self.started_at
        hb_path = self.cfg.get("heartbeat_file")
        if hb_path:
            try:
                last_activity = max(last_activity, os.path.getmtime(hb_path))
            except OSError:
                pass
        gpu_util = self.sample_gpu_utilization()
        if gpu_util is not None and gpu_util >= GPU_BUSY_THRESHOLD_PCT:
            self.last_gpu_active_at = now
        return max(0.0, now - max(last_activity, self.last_gpu_active_at))

    def clear_winddown(self) -> None:
        self.winddown_executed_for = None
        self.winddown_hooks_ran = 0
        try:
            os.remove(self.winddown_marker)
        except OSError:
            pass

    def execute_winddown(self, deadline: float, *, final_pass: bool = False) -> None:
        self.winddown_executed_for = deadline
        self.winddown_hooks_ran = len(self.hooks)
        try:
            with open(self.winddown_marker, "w", encoding="utf-8") as fh:
                json.dump(
                    {"deadline_epoch": deadline, "triggered_at": time.time(), "final_pass": final_pass},
                    fh,
                )
        except OSError:
            pass
        phase = "final" if final_pass else "grace"
        self.log(f"wind-down ({phase}) entered ({len(self.hooks)} hook(s))")
        for idx, hook in enumerate(self.hooks):
            argv = hook.get("argv") if isinstance(hook, dict) else None
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                continue
            env = dict(os.environ)
            for k, v in (hook.get("env") or {}).items():
                env[str(k)] = str(v)
            hook_limit = float(hook.get("timeout_s") or 60.0)
            if final_pass:
                timeout = max(1.0, min(300.0, hook_limit))
            else:
                remaining = max(5.0, deadline - time.time())
                timeout = max(1.0, min(remaining, hook_limit))
            try:
                res = subprocess.run(
                    argv,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                )
                self.log(f"hook[{idx}] exited {res.returncode}")
            except Exception as exc:
                self.log(f"hook[{idx}] failed ({type(exc).__name__})")

    def serve_forever(self) -> int:
        self.reload_config()
        self.log(f"started pid={os.getpid()}")
        while True:
            self.reload_config()
            if self.cfg.get("cancelled") is True:
                self.clear_winddown()
                self.log("cancelled flag set; exiting cleanly without unassign")
                return 0

            now = time.time()
            deadline = self.cfg.get("deadline_epoch")
            if isinstance(deadline, (int, float)) and deadline > 0:
                grace = self.cfg.get("grace_s", 180)
                raw_grace_s = float(grace) if isinstance(grace, (int, float)) and grace >= 0 else 180.0
                total_span = max(0.0, float(deadline) - self.started_at)
                grace_s = (
                    min(raw_grace_s, total_span * 0.5)
                    if total_span > 0 and raw_grace_s >= total_span
                    else raw_grace_s
                )
                if (
                    self.winddown_executed_for is not None
                    and deadline != self.winddown_executed_for
                    and now < deadline - grace_s
                ):
                    self.log("deadline extended; wind-down cleared")
                    self.clear_winddown()
                if (
                    (self.winddown_executed_for is None or len(self.hooks) > self.winddown_hooks_ran)
                    and (deadline - grace_s) <= now < deadline
                ):
                    self.execute_winddown(float(deadline), final_pass=False)
                    continue
                if now >= deadline:
                    if self.hooks:
                        self.execute_winddown(float(deadline), final_pass=True)
                    if self.release_vm("deadline_reached"):
                        return 0
            elif self.winddown_executed_for is not None:
                self.clear_winddown()

            idle_limit = self.cfg.get("idle_timeout_s")
            if isinstance(idle_limit, (int, float)) and idle_limit > 0:
                idle_s = self.compute_idle_seconds(now)
                if idle_s >= float(idle_limit):
                    if self.hooks:
                        self.execute_winddown(now + 60.0, final_pass=True)
                    if self.release_vm(f"idle_{int(idle_s)}s"):
                        return 0

            poll_s = self.cfg.get("poll_s", 15)
            sleep_for = float(poll_s) if isinstance(poll_s, (int, float)) else 15.0
            time.sleep(max(0.1, min(60.0, sleep_for)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Colab Boost VM Watchdog")
    parser.add_argument("--config", required=True, help="Path to watchdog.json")
    parser.add_argument("--hooks-stdin", action="store_true", help="Read wind-down hooks JSON list on stdin")
    parser.add_argument("--unassign-url", default=None, help="Optional override URL for testing")
    args = parser.parse_args()

    hooks: list[dict] = []
    if args.hooks_stdin:
        try:
            parsed = json.loads(sys.stdin.read() or "[]")
            if isinstance(parsed, list):
                hooks = parsed
        except ValueError:
            hooks = []

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    pid_file = os.path.join(os.path.dirname(os.path.abspath(args.config)), "watchdog.pid")
    try:
        with open(pid_file, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass

    return VmWatchdog(args.config, hooks=hooks, unassign_url_override=args.unassign_url).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
