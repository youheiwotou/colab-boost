"""Real subprocess tests for the detached VM-side watchdog (``vm_assets/watchdog.py``)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from colab_boost.config import project_root


def _watchdog_script() -> Path:
    return project_root() / "src" / "colab_boost" / "vm_assets" / "watchdog.py"


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
    try:
        gpu_val_file = tmp_path / "gpu_util.txt"
        gpu_val_file.write_text("45.0\n", encoding="utf-8")
        hb_file = tmp_path / "heartbeat"
        hb_file.write_text("", encoding="utf-8")

        cfg_file = tmp_path / "watchdog_idle.json"
        cfg = {
            "deadline_epoch": None,
            "idle_timeout_s": 0.6,
            "cancelled": False,
            "heartbeat_file": str(hb_file),
            "poll_s": 0.1,
            "started_at": time.time(),
            "gpu_probe_cmd": [sys.executable, "-c", f"print(open({str(gpu_val_file)!r}).read())"],
        }
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(_watchdog_script()), "--config", str(cfg_file), "--unassign-url", rec.url]
        )
        # Keep GPU busy past the 0.6s idle window -> must NOT unassign while busy!
        time.sleep(0.85)
        assert proc.poll() is None
        assert rec.calls == 0

        # Now GPU drops to 0% -> watchdog should unassign within ~1.2s
        gpu_val_file.write_text("0.0\n", encoding="utf-8")
        rc = proc.wait(timeout=5)
        assert rc == 0
        assert rec.calls == 1
    finally:
        rec.close()


def test_watchdog_reloads_dynamic_hooks_from_config_before_idle_unassign(tmp_path: Path) -> None:
    """Proves watchdog picks up [[keep]] wind-down hooks added to watchdog.json after startup."""
    rec = _UnassignRecorder()
    try:
        gpu_val_file = tmp_path / "gpu_util_dyn.txt"
        gpu_val_file.write_text("60.0\n", encoding="utf-8")
        kept_marker = tmp_path / "kept_on_idle.txt"

        cfg_file = tmp_path / "watchdog_dyn.json"
        cfg = {
            "deadline_epoch": None,
            "idle_timeout_s": 0.5,
            "cancelled": False,
            "poll_s": 0.1,
            "started_at": time.time(),
            "gpu_probe_cmd": [sys.executable, "-c", f"print(open({str(gpu_val_file)!r}).read())"],
            "hooks": [],
        }
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(_watchdog_script()), "--config", str(cfg_file), "--unassign-url", rec.url]
        )
        time.sleep(0.25)
        assert proc.poll() is None

        # Dynamically inject wind-down hook into watchdog.json (simulating deploying a [[keep]] recipe onto an existing VM)
        cfg["hooks"] = [
            {
                "argv": [sys.executable, "-c", f"open({str(kept_marker)!r}, 'w', encoding='utf-8').write('archived_before_idle')"],
                "timeout_s": 5,
            }
        ]
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

        # Let GPU go idle -> watchdog must run the dynamically added hook before calling /unassign
        gpu_val_file.write_text("0.0\n", encoding="utf-8")
        rc = proc.wait(timeout=6)
        assert rc == 0
        assert kept_marker.read_text(encoding="utf-8") == "archived_before_idle"
        assert rec.calls == 1
    finally:
        rec.close()


def test_watchdog_final_winddown_captures_late_outputs_and_short_deadlines(tmp_path: Path) -> None:
    """Proves short deadlines (grace_s > total_span) and late renders during grace window are captured before /unassign."""
    rec = _UnassignRecorder()
    try:
        src_file = tmp_path / "late_render.txt"
        src_file.write_text("initial", encoding="utf-8")
        archived_file = tmp_path / "archived_snapshot.txt"
        cfg_file = tmp_path / "watchdog_short.json"
        now = time.time()
        # grace_s=180 exceeds total_span=0.9s; previously this triggered wind-down at T=0s and missed late writes!
        cfg = {
            "deadline_epoch": now + 0.9,
            "idle_timeout_s": None,
            "cancelled": False,
            "poll_s": 0.1,
            "grace_s": 180.0,
            "started_at": now,
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
        # Simulate ComfyUI finishing a new render right before deadline (inside the grace window)
        time.sleep(0.55)
        src_file.write_text("final_late_render_v2", encoding="utf-8")

        rc = proc.wait(timeout=8)
        assert rc == 0
        assert rec.calls == 1
        assert archived_file.read_text(encoding="utf-8") == "final_late_render_v2"
    finally:
        rec.close()
