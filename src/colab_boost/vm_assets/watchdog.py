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
import math
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

GPU_BUSY_THRESHOLD_PCT = 5.0
UNASSIGN_RETRIES = 3


def _as_float(val: object) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    num = float(val)
    if not math.isfinite(num):
        return None
    return num


def _normalize_unassign_url(addr: str) -> str:
    raw = str(addr or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urllib.parse.urlparse(raw)
    if not parsed.netloc:
        return ""
    path = parsed.path or ""
    if not path or path == "/":
        parsed = parsed._replace(path="/unassign")
    return urllib.parse.urlunparse(parsed)


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
        self.started_mono = time.monotonic()
        self.last_gpu_active_at = 0.0
        self.winddown_executed_for: object = None
        self.winddown_hooks_ran: int = 0
        self.final_winddown_done: bool = False
        self.final_winddown_done_for: object = None
        self.idle_winddown_done: bool = False
        self._remaining_anchor_mono: float | None = None
        self._remaining_anchor_val: float | None = None
        self._remaining_cfg_mtime_ns: int | None = None

    def log(self, msg: str) -> None:
        log_file = self.cfg.get("log_file") or os.path.join(self.base_dir, "watchdog.log")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(f"{stamp} [watchdog] {msg}\n")
        except OSError:
            pass

    def reload_config(self) -> None:
        cfg_mtime_ns: int | None = None
        try:
            st = os.stat(self.config_path)
            cfg_mtime_ns = st.st_mtime_ns
            with open(self.config_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.cfg = loaded
        except (OSError, ValueError) as exc:
            self.log(f"config read warning ({type(exc).__name__}); keeping cached config")

        started = _as_float(self.cfg.get("started_at"))
        if started is not None and started > 0:
            self.started_at = started

        rem_val = _as_float(self.cfg.get("remaining_s"))
        if rem_val is not None and rem_val > 0:
            if (
                self._remaining_anchor_mono is None
                or self._remaining_anchor_val != rem_val
                or (cfg_mtime_ns is not None and cfg_mtime_ns != self._remaining_cfg_mtime_ns)
            ):
                self._remaining_anchor_mono = time.monotonic()
                self._remaining_anchor_val = rem_val
                self._remaining_cfg_mtime_ns = cfg_mtime_ns
        else:
            self._remaining_anchor_mono = None
            self._remaining_anchor_val = None
            self._remaining_cfg_mtime_ns = cfg_mtime_ns

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
        cfg_addr = self.cfg.get("runtime_addr")
        if isinstance(cfg_addr, str) and cfg_addr.strip():
            return cfg_addr.strip()
        env_addr = (os.environ.get("TBE_RUNTIME_ADDR") or "").strip()
        if env_addr:
            return env_addr
        if os.path.isdir("/proc"):
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/environ", "rb") as fh:
                        for item in fh.read().split(b"\0"):
                            if item.startswith(b"TBE_RUNTIME_ADDR="):
                                val = item.split(b"=", 1)[1].decode("utf-8", errors="replace").strip()
                                if val:
                                    return val
                except OSError:
                    continue
        return None

    def release_vm(self, reason: str) -> bool:
        raw_target = self.unassign_url_override
        if not raw_target:
            addr = self.resolve_runtime_addr()
            if not addr:
                self.log(f"cannot unassign ({reason}): TBE_RUNTIME_ADDR missing")
                return False
            raw_target = addr
        target_url = _normalize_unassign_url(raw_target)
        if not target_url:
            self.log(f"cannot unassign ({reason}): invalid target URL")
            return False

        # Bypass HTTP_PROXY / HTTPS_PROXY env vars for internal Colab runtime unassign calls.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(1, UNASSIGN_RETRIES + 1):
            try:
                req = urllib.request.Request(target_url, data=b"", method="POST")
                with opener.open(req, timeout=20) as resp:
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
        if isinstance(hb_path, str) and hb_path:
            try:
                last_activity = max(last_activity, os.path.getmtime(hb_path))
            except OSError:
                pass
        gpu_util = self.sample_gpu_utilization()
        if gpu_util is not None and gpu_util >= GPU_BUSY_THRESHOLD_PCT:
            self.last_gpu_active_at = now
        return max(0.0, now - max(last_activity, self.last_gpu_active_at))

    def clear_winddown(self, *, clear_idle: bool = True) -> None:
        self.winddown_executed_for = None
        self.winddown_hooks_ran = 0
        self.final_winddown_done = False
        self.final_winddown_done_for = None
        if clear_idle:
            self.idle_winddown_done = False
        try:
            os.remove(self.winddown_marker)
        except OSError:
            pass

    def execute_winddown(
        self,
        deadline: float,
        *,
        final_pass: bool = False,
        deadline_key: object = None,
        for_idle: bool = False,
    ) -> None:
        key = deadline_key if deadline_key is not None else deadline
        if for_idle:
            self.idle_winddown_done = True
            if final_pass:
                self.final_winddown_done = True
        else:
            self.winddown_executed_for = key
            self.winddown_hooks_ran = len(self.hooks)
            if final_pass:
                self.final_winddown_done = True
                self.final_winddown_done_for = key
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
            hook_env = hook.get("env")
            if isinstance(hook_env, dict):
                for k, v in hook_env.items():
                    env[str(k)] = str(v)
            raw_limit = _as_float(hook.get("timeout_s"))
            hook_limit = raw_limit if (raw_limit is not None and raw_limit > 0) else 60.0
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

    def _resolve_effective_deadline(
        self, now_wall: float, now_mono: float
    ) -> tuple[object, float, float, float] | None:
        """Resolves the tightest active deadline across wall-clock and monotonic relative timers.

        Returns ``(deadline_key, remaining_s, total_span_s, deadline_epoch_approx)`` or ``None``.
        """
        candidates: list[tuple[object, float, float, float]] = []

        deadline_epoch = _as_float(self.cfg.get("deadline_epoch"))
        if deadline_epoch is not None and deadline_epoch > 0:
            rem = deadline_epoch - now_wall
            span = max(0.0, deadline_epoch - self.started_at)
            candidates.append((("epoch", deadline_epoch), rem, span, deadline_epoch))

        max_runtime_s = _as_float(self.cfg.get("max_runtime_s"))
        if max_runtime_s is not None and max_runtime_s > 0:
            rem = (self.started_mono + max_runtime_s) - now_mono
            candidates.append((("max_runtime_s", max_runtime_s), rem, max_runtime_s, now_wall + max(0.0, rem)))

        if self._remaining_anchor_mono is not None and self._remaining_anchor_val is not None:
            rem = (self._remaining_anchor_mono + self._remaining_anchor_val) - now_mono
            key = ("remaining_s", self._remaining_anchor_mono, self._remaining_anchor_val)
            candidates.append((key, rem, self._remaining_anchor_val, now_wall + max(0.0, rem)))

        if not candidates:
            return None
        return min(candidates, key=lambda item: item[1])

    def _sleep_interval(self) -> float:
        poll_val = _as_float(self.cfg.get("poll_s", 15))
        sleep_for = poll_val if poll_val is not None else 15.0
        return max(0.1, min(60.0, sleep_for))

    def serve_forever(self) -> int:
        self.reload_config()
        self.log(f"started pid={os.getpid()}")
        while True:
            try:
                self.reload_config()
                if self.cfg.get("cancelled") is True:
                    self.clear_winddown()
                    self.log("cancelled flag set; exiting cleanly without unassign")
                    return 0

                now = time.time()
                now_mono = time.monotonic()
                effective = self._resolve_effective_deadline(now, now_mono)
                if effective is not None:
                    deadline_key, remaining_s, total_span, deadline_ts = effective
                    grace = _as_float(self.cfg.get("grace_s", 180))
                    raw_grace_s = grace if (grace is not None and grace >= 0) else 180.0
                    grace_s = (
                        min(raw_grace_s, total_span * 0.5)
                        if total_span > 0 and raw_grace_s >= total_span
                        else raw_grace_s
                    )
                    if (
                        (self.winddown_executed_for is not None or self.final_winddown_done_for is not None)
                        and deadline_key != self.winddown_executed_for
                        and deadline_key != self.final_winddown_done_for
                        and remaining_s > grace_s
                    ):
                        self.log("deadline extended; wind-down cleared")
                        self.clear_winddown()
                    if (
                        (self.winddown_executed_for != deadline_key or len(self.hooks) > self.winddown_hooks_ran)
                        and (0.0 < remaining_s <= grace_s)
                    ):
                        self.execute_winddown(deadline_ts, final_pass=False, deadline_key=deadline_key)
                        continue
                    if remaining_s <= 0.0:
                        if self.hooks and self.final_winddown_done_for != deadline_key:
                            self.execute_winddown(deadline_ts, final_pass=True, deadline_key=deadline_key)
                        if self.release_vm("deadline_reached"):
                            return 0
                elif self.winddown_executed_for is not None or self.final_winddown_done_for is not None:
                    self.clear_winddown(clear_idle=False)

                idle_limit = _as_float(self.cfg.get("idle_timeout_s"))
                if idle_limit is not None and idle_limit > 0:
                    idle_s = self.compute_idle_seconds(now)
                    if idle_s >= idle_limit:
                        if self.hooks and not self.idle_winddown_done:
                            self.execute_winddown(now + 60.0, final_pass=True, for_idle=True)
                        if self.release_vm(f"idle_{int(idle_s)}s"):
                            return 0
                    elif self.idle_winddown_done:
                        self.idle_winddown_done = False
            except Exception as exc:
                self.log(f"watchdog loop error ({type(exc).__name__}: {exc})")

            time.sleep(self._sleep_interval())


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
