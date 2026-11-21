"""Real subprocess tests for the detached VM-side watchdog (``vm_assets/watchdog.py``)."""

from __future__ import annotations

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from colab_boost.config import project_root


def _watchdog_script() -> Path:
    return project_root() / "src" / "colab_boost" / "vm_assets" / "watchdog.py"


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
    interval: float = 0.05,
    desc: str = "condition",
) -> None:
    """Polls ``predicate`` with a bounded timeout instead of relying on brittle fixed sleeps."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"Timed out waiting for {desc} after {timeout:.2f}s")
        time.sleep(min(interval, remaining))


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _terminate_proc(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


class _UnassignRecorder:
    def __init__(self) -> None:
        self.calls = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/unassign"

    def _make_handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                if self.path == "/unassign":
                    parent.calls += 1
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *_args):
                return

        return Handler

    def close(self) -> None:
        self.server.shutdown()


def test_watchdog_winddown_hook_and_deadline_unassign(tmp_path: Path) -> None:
    rec = _UnassignRecorder()
    try:
        hook_marker = tmp_path / "hook_ran.txt"
        cfg_file = tmp_path / "watchdog.json"
        now = time.time()
        cfg = {
            "deadline_epoch": now + 0.9,
            "idle_timeout_s": None,
            "cancelled": False,
            "poll_s": 0.1,
            "grace_s": 0.6,
            "started_at": now,
            "log_file": str(tmp_path / "wd.log"),
        }
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

        hooks = [
            {
                "argv": [sys.executable, "-c", f"open({str(hook_marker)!r}, 'w').write('saved')"],
                "timeout_s": 5,
            }
        ]
        proc = subprocess.run(
            [
                sys.executable,
                str(_watchdog_script()),
                "--config",
                str(cfg_file),
                "--hooks-stdin",
                "--unassign-url",
                rec.url,
            ],
            input=json.dumps(hooks),
            text=True,
            timeout=10,
            check=True,
        )
        assert proc.returncode == 0
        assert hook_marker.read_text(encoding="utf-8") == "saved"
        assert rec.calls == 1
    finally:
        rec.close()


def test_watchdog_gpu_busy_delays_idle_then_releases_when_idle(tmp_path: Path) -> None:
    rec = _UnassignRecorder()
    proc: subprocess.Popen | None = None
    try:
        gpu_val_file = tmp_path / "gpu_util.txt"
        gpu_val_file.write_text("45.0\n", encoding="utf-8")
        probe_log = tmp_path / "gpu_probes.log"
        hb_file = tmp_path / "heartbeat"
        hb_file.write_text("", encoding="utf-8")

        probe_script = (
            f"open({str(probe_log)!r}, 'a', encoding='utf-8').write('1\\n');"
            f"print(open({str(gpu_val_file)!r}, encoding='utf-8').read())"
        )
        cfg_file = tmp_path / "watchdog_idle.json"
        cfg = {
            "deadline_epoch": None,
            "idle_timeout_s": 0.4,
            "cancelled": False,
            "heartbeat_file": str(hb_file),
            "poll_s": 0.1,
            "started_at": time.time(),
            "gpu_probe_cmd": [sys.executable, "-c", probe_script],
        }
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(_watchdog_script()), "--config", str(cfg_file), "--unassign-url", rec.url]
        )
        # Wait deterministically for startup & first GPU probe, then verify staying alive past 0.4s idle window
        _wait_until(
            lambda: probe_log.exists() and len(probe_log.read_text(encoding="utf-8").splitlines()) >= 1,
            timeout=5.0,
            desc="initial GPU probe",
        )
        first_probe_at = time.monotonic()
        _wait_until(
            lambda: (
                (time.monotonic() - first_probe_at) >= 0.55
                and len(probe_log.read_text(encoding="utf-8").splitlines()) >= 5
            ),
            timeout=5.0,
            desc="multiple GPU-busy polls past idle_timeout_s",
        )
        assert proc.poll() is None
        assert rec.calls == 0

        # Now GPU drops to 0% -> watchdog should unassign shortly after idle_timeout_s
        gpu_val_file.write_text("0.0\n", encoding="utf-8")
        rc = proc.wait(timeout=5)
        assert rc == 0
        assert rec.calls == 1
    finally:
        _terminate_proc(proc)
        rec.close()


def test_watchdog_reloads_dynamic_hooks_from_config_before_idle_unassign(tmp_path: Path) -> None:
    """Proves watchdog picks up [[keep]] wind-down hooks added to watchdog.json after startup."""
    rec = _UnassignRecorder()
    proc: subprocess.Popen | None = None
    try:
        gpu_val_file = tmp_path / "gpu_util_dyn.txt"
        gpu_val_file.write_text("60.0\n", encoding="utf-8")
        probe_log = tmp_path / "dyn_probes.log"
        kept_marker = tmp_path / "kept_on_idle.txt"

        probe_script = (
            f"open({str(probe_log)!r}, 'a', encoding='utf-8').write('1\\n');"
            f"print(open({str(gpu_val_file)!r}, encoding='utf-8').read())"
        )
        cfg_file = tmp_path / "watchdog_dyn.json"
        cfg = {
            "deadline_epoch": None,
            "idle_timeout_s": 0.4,
            "cancelled": False,
            "poll_s": 0.1,
            "started_at": time.time(),
            "gpu_probe_cmd": [sys.executable, "-c", probe_script],
            "hooks": [],
        }
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(_watchdog_script()), "--config", str(cfg_file), "--unassign-url", rec.url]
        )
        _wait_until(
            lambda: probe_log.exists() and len(probe_log.read_text(encoding="utf-8").splitlines()) >= 1,
            timeout=5.0,
            desc="watchdog startup poll with empty hooks",
        )
        assert proc.poll() is None

        # Dynamically inject wind-down hook into watchdog.json (simulating deploying a [[keep]] recipe onto an existing VM)
        cfg["hooks"] = [
            {
                "argv": [sys.executable, "-c", f"open({str(kept_marker)!r}, 'w', encoding='utf-8').write('archived_before_idle')"],
                "timeout_s": 5,
            }
        ]
        _atomic_write_json(cfg_file, cfg)

        # Let GPU go idle -> watchdog must run the dynamically added hook before calling /unassign
        gpu_val_file.write_text("0.0\n", encoding="utf-8")
        rc = proc.wait(timeout=6)
        assert rc == 0
        assert kept_marker.read_text(encoding="utf-8") == "archived_before_idle"
        assert rec.calls == 1
    finally:
        _terminate_proc(proc)
        rec.close()


def test_watchdog_final_winddown_captures_late_outputs_and_short_deadlines(tmp_path: Path) -> None:
    """Proves short deadlines (grace_s > total_span) and late renders during grace window are captured before /unassign."""
    rec = _UnassignRecorder()
    proc: subprocess.Popen | None = None
    try:
        src_file = tmp_path / "late_render.txt"
        src_file.write_text("initial", encoding="utf-8")
        archived_file = tmp_path / "archived_snapshot.txt"
        cfg_file = tmp_path / "watchdog_short.json"
        log_file = tmp_path / "wd_short.log"
        pid_file = tmp_path / "watchdog.pid"
        winddown_marker = tmp_path / "winddown.json"

        # Start watchdog with no deadline yet so subprocess startup time never races the short deadline window
        cfg: dict = {
            "deadline_epoch": None,
            "idle_timeout_s": None,
            "cancelled": False,
            "poll_s": 0.1,
            "grace_s": 180.0,
            "log_file": str(log_file),
            "hooks": [
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        f"import shutil; shutil.copy2({str(src_file)!r}, {str(archived_file)!r})",
                    ],
                    "timeout_s": 10,
                }
            ],
        }
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(_watchdog_script()), "--config", str(cfg_file), "--unassign-url", rec.url]
        )
        _wait_until(
            lambda: pid_file.exists() and log_file.exists() and "started pid=" in log_file.read_text(encoding="utf-8"),
            timeout=5.0,
            desc="watchdog startup before arming short deadline",
        )

        # Arm a short deadline where grace_s=180 exceeds total_span=1.2s (effective grace = 0.6s)
        now = time.time()
        cfg["started_at"] = now
        cfg["deadline_epoch"] = now + 1.2
        _atomic_write_json(cfg_file, cfg)

        # Wait deterministically for the grace pass (final_pass=False) to complete before writing late render
        _wait_until(
            lambda: (
                winddown_marker.exists()
                and archived_file.exists()
                and archived_file.read_text(encoding="utf-8") == "initial"
            ),
            timeout=5.0,
            desc="grace wind-down pass completion",
        )
        assert json.loads(winddown_marker.read_text(encoding="utf-8"))["final_pass"] is False

        # Simulate ComfyUI finishing a new render right before deadline (inside the grace window)
        src_file.write_text("final_late_render_v2", encoding="utf-8")

        rc = proc.wait(timeout=8)
        assert rc == 0
        assert rec.calls == 1
        assert archived_file.read_text(encoding="utf-8") == "final_late_render_v2"
    finally:
        _terminate_proc(proc)
        rec.close()


def test_watchdog_final_winddown_idempotent_on_release_failure_deadline_and_idle(tmp_path: Path) -> None:
    """When release_vm() fails repeatedly, final_pass hooks must run ONCE (never re-run on every poll)."""
    from colab_boost.vm_assets.watchdog import VmWatchdog

    # 1. Deadline path: release_vm fails twice then succeeds on 3rd poll
    count_file = tmp_path / "deadline_hook_count.txt"
    cfg_file = tmp_path / "wd_deadline.json"
    now = time.time()
    cfg = {
        "deadline_epoch": now - 1.0,
        "idle_timeout_s": None,
        "cancelled": False,
        "poll_s": 0.1,
        "grace_s": 10.0,
        "started_at": now - 20.0,
        "hooks": [
            {
                "argv": [
                    sys.executable,
                    "-c",
                    f"p={str(count_file)!r}; open(p, 'a', encoding='utf-8').write('1\\n')",
                ],
                "timeout_s": 5,
            }
        ],
    }
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

    wd = VmWatchdog(str(cfg_file))
    release_attempts = [0]

    def flaky_release(_reason: str) -> bool:
        release_attempts[0] += 1
        return release_attempts[0] >= 3

    wd.release_vm = flaky_release  # type: ignore[method-assign]
    assert wd.serve_forever() == 0
    assert release_attempts[0] == 3
    assert wd.final_winddown_done is True
    assert len(count_file.read_text(encoding="utf-8").splitlines()) == 1

    # 2. Idle path: release_vm fails twice then succeeds on 3rd poll
    idle_count_file = tmp_path / "idle_hook_count.txt"
    idle_cfg_file = tmp_path / "wd_idle.json"
    idle_cfg = {
        "deadline_epoch": None,
        "idle_timeout_s": 0.1,
        "cancelled": False,
        "poll_s": 0.1,
        "started_at": time.time() - 5.0,
        "gpu_probe_cmd": [sys.executable, "-c", "print('0.0')"],
        "hooks": [
            {
                "argv": [
                    sys.executable,
                    "-c",
                    f"p={str(idle_count_file)!r}; open(p, 'a', encoding='utf-8').write('1\\n')",
                ],
                "timeout_s": 5,
            }
        ],
    }
    idle_cfg_file.write_text(json.dumps(idle_cfg), encoding="utf-8")

    wd_idle = VmWatchdog(str(idle_cfg_file))
    idle_attempts = [0]

    def flaky_idle_release(_reason: str) -> bool:
        idle_attempts[0] += 1
        return idle_attempts[0] >= 3

    wd_idle.release_vm = flaky_idle_release  # type: ignore[method-assign]
    assert wd_idle.serve_forever() == 0
    assert idle_attempts[0] == 3
    assert wd_idle.idle_winddown_done is True
    assert len(idle_count_file.read_text(encoding="utf-8").splitlines()) == 1


def test_watchdog_release_vm_bypasses_proxy_normalizes_url_and_skips_empty_proc_env(
    tmp_path: Path, monkeypatch
) -> None:
    from colab_boost.vm_assets.watchdog import VmWatchdog, _normalize_unassign_url

    assert _normalize_unassign_url("172.28.0.12:9000") == "http://172.28.0.12:9000/unassign"
    assert _normalize_unassign_url("http://172.28.0.12:9000") == "http://172.28.0.12:9000/unassign"
    assert _normalize_unassign_url("http://172.28.0.12:9000/") == "http://172.28.0.12:9000/unassign"
    assert _normalize_unassign_url("http://172.28.0.12:9000/unassign") == "http://172.28.0.12:9000/unassign"

    rec = _UnassignRecorder()
    try:
        host, port = rec.server.server_address
        # Deliberately set broken HTTP/HTTPS proxy env vars and omit '/unassign' from runtime_addr
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.delenv("TBE_RUNTIME_ADDR", raising=False)

        cfg_file = tmp_path / "wd_proxy.json"
        cfg_file.write_text(json.dumps({"runtime_addr": f"http://{host}:{port}"}), encoding="utf-8")
        wd = VmWatchdog(str(cfg_file))
        wd.reload_config()
        assert wd.release_vm("proxy_bypass_test") is True
        assert rec.calls == 1
    finally:
        rec.close()

    # Verify /proc scan skips empty TBE_RUNTIME_ADDR= and keeps scanning to find the non-empty one
    cfg_empty = tmp_path / "wd_proc.json"
    cfg_empty.write_text(json.dumps({}), encoding="utf-8")
    wd_proc = VmWatchdog(str(cfg_empty))
    wd_proc.reload_config()

    monkeypatch.setattr("os.path.isdir", lambda p: p == "/proc")
    monkeypatch.setattr("os.listdir", lambda _p: ["100", "200"])
    fake_env = {
        "/proc/100/environ": b"FOO=bar\x00TBE_RUNTIME_ADDR=\x00",
        "/proc/200/environ": b"TBE_RUNTIME_ADDR=172.28.0.99:9000\x00",
    }
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if path in fake_env:
            import io

            return io.BytesIO(fake_env[path])
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    assert wd_proc.resolve_runtime_addr() == "172.28.0.99:9000"


def test_watchdog_monotonic_relative_timers_max_runtime_and_remaining(tmp_path: Path) -> None:
    # 1. max_runtime_s relative to watchdog monotonic start
    rec1 = _UnassignRecorder()
    try:
        cfg1 = tmp_path / "wd_max_runtime.json"
        cfg1.write_text(
            json.dumps({"max_runtime_s": 0.4, "grace_s": 0.1, "poll_s": 0.1, "cancelled": False}),
            encoding="utf-8",
        )
        proc1 = subprocess.run(
            [sys.executable, str(_watchdog_script()), "--config", str(cfg1), "--unassign-url", rec1.url],
            timeout=8,
            check=True,
        )
        assert proc1.returncode == 0
        assert rec1.calls == 1
    finally:
        rec1.close()

    # 2. remaining_s anchored monotonically across polls (does not reset on reload_config)
    rec2 = _UnassignRecorder()
    try:
        cfg2 = tmp_path / "wd_remaining.json"
        cfg2.write_text(
            json.dumps({"remaining_s": 0.45, "grace_s": 0.1, "poll_s": 0.1, "cancelled": False}),
            encoding="utf-8",
        )
        proc2 = subprocess.run(
            [sys.executable, str(_watchdog_script()), "--config", str(cfg2), "--unassign-url", rec2.url],
            timeout=8,
            check=True,
        )
        assert proc2.returncode == 0
        assert rec2.calls == 1
    finally:
        rec2.close()


def test_watchdog_rejects_bool_timers_and_survives_malformed_hook_timeout(tmp_path: Path) -> None:
    from colab_boost.vm_assets.watchdog import VmWatchdog

    rec = _UnassignRecorder()
    proc: subprocess.Popen | None = None
    try:
        bool_log = tmp_path / "wd_bool.log"
        pid_file = tmp_path / "watchdog.pid"
        cfg_file = tmp_path / "wd_bool.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "deadline_epoch": True,
                    "idle_timeout_s": True,
                    "max_runtime_s": True,
                    "remaining_s": True,
                    "poll_s": True,
                    "cancelled": False,
                    "log_file": str(bool_log),
                    "hooks": [{"argv": [sys.executable, "-c", "pass"], "timeout_s": "not-a-number"}],
                }
            ),
            encoding="utf-8",
        )

        # Deterministic unit verification that boolean timers are rejected & poll_s falls back to 15.0s
        wd_bool = VmWatchdog(str(cfg_file))
        wd_bool.reload_config()
        assert wd_bool._resolve_effective_deadline(time.time(), time.monotonic()) is None
        assert wd_bool._sleep_interval() == 15.0

        proc = subprocess.Popen(
            [sys.executable, str(_watchdog_script()), "--config", str(cfg_file), "--unassign-url", rec.url]
        )
        _wait_until(
            lambda: pid_file.exists() and bool_log.exists() and "started pid=" in bool_log.read_text(encoding="utf-8"),
            timeout=5.0,
            desc="watchdog bool-timer startup",
        )
        # Must NOT treat boolean True as 1.0s deadline/idle and prematurely unassign, and must stay alive
        assert proc.poll() is None
        assert rec.calls == 0

        # Terminate cleanly (poll_s=True fell back to default 15s)
        _terminate_proc(proc)
        proc = None
        assert rec.calls == 0

        # Also verify malformed hook timeout_s never crashes execute_winddown before release_vm
        cfg_hook = tmp_path / "wd_bad_hook.json"
        cfg_hook.write_text(
            json.dumps(
                {
                    "max_runtime_s": 0.2,
                    "poll_s": 0.1,
                    "cancelled": False,
                    "hooks": [{"argv": [sys.executable, "-c", "pass"], "timeout_s": "not-a-number"}],
                }
            ),
            encoding="utf-8",
        )
        proc2 = subprocess.run(
            [sys.executable, str(_watchdog_script()), "--config", str(cfg_hook), "--unassign-url", rec.url],
            timeout=8,
            check=True,
        )
        assert proc2.returncode == 0
        assert rec.calls == 1

        # Verify serve_forever survives an unexpected loop exception and recovers on the next iteration
        cfg_err = tmp_path / "wd_loop_err.json"
        cfg_err.write_text(json.dumps({"idle_timeout_s": 0.1, "poll_s": 0.1, "cancelled": False}), encoding="utf-8")
        wd_err = VmWatchdog(str(cfg_err), unassign_url_override=rec.url)
        calls = [0]

        def flaky_idle(_now: float) -> float:
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("transient unexpected failure")
            return 10.0

        wd_err.compute_idle_seconds = flaky_idle  # type: ignore[method-assign]
        assert wd_err.serve_forever() == 0
        assert calls[0] == 2
        assert rec.calls == 2
    finally:
        _terminate_proc(proc)
        rec.close()

