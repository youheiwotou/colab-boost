"""Unit tests for ssh_bridge process tree cleanup, key security, URL encoding, state machine, and auto-reconnect."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import io
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import pytest

from colab_boost.config import WorkspacePaths
from colab_boost.models import TunnelInfo
import colab_boost.ssh_bridge as ssh_bridge
from colab_boost.ssh_bridge import (
    STDERR_MAX_LINES,
    ActiveTunnelEntry,
    SshTunnelManager,
    _SessionSshRuntime,
    _attach_process_job,
    _terminate_process,
    build_colab_ssh_ws_url,
    ensure_isolated_ed25519_keypair,
    run_websocket_stdio_bridge,
)


def test_build_colab_ssh_ws_url_encodes_proxy_token_and_mode() -> None:
    raw_token = "tok+with/slash=and&amp#frag space"
    url = build_colab_ssh_ws_url("https://ep-1.colab.test:443", raw_token, mode="proxy+v2")
    parsed = urlparse(url)
    assert parsed.scheme == "wss"
    assert parsed.netloc == "ep-1.colab.test:443"
    assert parsed.path == "/colab/ssh"
    assert parsed.fragment == ""  # '#' must be encoded, not treated as URL fragment
    qs = parse_qs(parsed.query)
    assert qs["colab-runtime-proxy-token"] == [raw_token]
    assert qs["colab-client-agent"] == ["colab-cli", "ssh-mode-proxy+v2"]

    ws_http = build_colab_ssh_ws_url("http://127.0.0.1:8888", "a&b=c")
    assert ws_http.startswith("ws://127.0.0.1:8888/colab/ssh?")
    assert parse_qs(urlparse(ws_http).query)["colab-runtime-proxy-token"] == ["a&b=c"]


def test_ensure_isolated_ed25519_keypair_rechecks_existing_key_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(parents=True)
    priv = ssh_dir / "id_ed25519"
    pub = ssh_dir / "id_ed25519.pub"
    priv.write_text("PRIVATE-KEY-CONTENT", encoding="utf-8")
    pub.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest colab-boost\n", encoding="utf-8")

    locked_paths: list[Path] = []
    monkeypatch.setattr(ssh_bridge, "_lock_private_key_permissions", lambda p: locked_paths.append(p))

    out_priv, out_pub = ensure_isolated_ed25519_keypair(ssh_dir)
    assert out_priv == priv
    assert out_pub.startswith("ssh-ed25519 ")
    assert locked_paths == [priv]


def test_temp_key_locked_before_write_deleted_on_ready_and_bounded_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unicode_dir = tmp_path / "中文目录" / "ssh"
    unicode_dir.mkdir(parents=True)
    priv = unicode_dir / "id_ed25519"
    priv.write_bytes(b"SECRET-PRIVATE-KEY-MATERIAL")

    acl_observations: list[tuple[Path, int]] = []
    captured_temp_keys: list[Path] = []

    def spy_lock_acl(p: Path) -> None:
        # Must be called when temp key file is still empty (0 bytes) BEFORE plaintext key is written!
        acl_observations.append((p, p.stat().st_size))
        captured_temp_keys.append(p)

    class FakePopen:
        def __init__(self, cmd: list[str], **_kwargs: Any) -> None:
            self.cmd = cmd
            self.pid = 99991
            self.returncode: Optional[int] = None
            self.stdin = io.StringIO()
            self.stdout = io.StringIO()
            # Emit more than STDERR_MAX_LINES lines to verify bounded deque(maxlen=200)
            self.stderr = io.StringIO("".join(f"line-{i}\n" for i in range(STDERR_MAX_LINES + 75)))
            self._key_arg = Path(cmd[cmd.index("-i") + 1])
            # At spawn time, temp key must exist and contain the copied key bytes
            assert self._key_arg.exists()
            assert self._key_arg.read_bytes() == b"SECRET-PRIVATE-KEY-MATERIAL"

        def poll(self) -> Optional[int]:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: Optional[float] = None) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    monkeypatch.setattr(ssh_bridge.sys, "platform", "win32")
    monkeypatch.setattr(ssh_bridge, "_lock_private_key_permissions", spy_lock_acl)
    monkeypatch.setattr(ssh_bridge, "_attach_process_job", lambda _p: None)
    monkeypatch.setattr(ssh_bridge, "is_local_port_open", lambda _port: True)
    monkeypatch.setattr(ssh_bridge.subprocess, "Popen", FakePopen)

    mgr = SshTunnelManager(WorkspacePaths(tmp_path / "ws"), auto_reconnect=False)
    proc, stderr_lines, _url, _tok = mgr._spawn_multiplexed_ssh(
        session_name="sess-key",
        fence=mgr._get_fence_token("sess-key"),
        ssh_bin="ssh",
        priv_key=priv,
        pubkey="ssh-ed25519 AAAA test",
        runtime_url="https://rt.colab.test",
        proxy_token="tok-abc",
        forwards=[(18001, 8001)],
        allow_429_kill=True,
        on_429_kill_stale_sshd=None,
    )
    assert len(acl_observations) == 1
    temp_key_path, size_at_acl_lock = acl_observations[0]
    assert size_at_acl_lock == 0  # Locked ACL BEFORE writing private key bytes!
    assert not temp_key_path.exists()  # Deleted IMMEDIATELY once tunnel became ready (proc still alive)!
    assert proc.poll() is None

    # Allow collector thread to finish reading FakePopen.stderr and verify bounded maxlen=200
    time.sleep(0.05)
    assert isinstance(stderr_lines, deque)
    assert stderr_lines.maxlen == STDERR_MAX_LINES
    assert len(stderr_lines) == STDERR_MAX_LINES
    assert stderr_lines[-1] == f"line-{STDERR_MAX_LINES + 74}"
    _terminate_process(proc)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object & taskkill tree test")
def test_windows_process_tree_termination_job_object_and_taskkill_fallback(tmp_path: Path) -> None:
    def _is_pid_alive(pid: int) -> bool:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)

    def _spawn_parent_with_grandchild(marker: Path, *, attach_job: bool) -> tuple[subprocess.Popen, int]:
        go_file = marker.with_suffix(".go")
        script = (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"go = Path({str(go_file)!r})\n"
            "while not go.exists():\n"
            "    time.sleep(0.02)\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"Path({str(marker)!r}).write_text(str(child.pid), encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if attach_job:
            job = _attach_process_job(proc)
            assert job is not None
        go_file.write_text("go", encoding="utf-8")
        deadline = time.monotonic() + 8.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "Parent did not record grandchild PID"
        child_pid = int(marker.read_text(encoding="utf-8").strip())
        assert _is_pid_alive(child_pid)
        return proc, child_pid

    # 1. Job Object path (CreateJobObjectW + JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    proc_job, child_pid_job = _spawn_parent_with_grandchild(tmp_path / "child_job.pid", attach_job=True)
    _terminate_process(proc_job)
    assert proc_job.poll() is not None
    assert proc_job.stdin is not None and proc_job.stdin.closed
    assert proc_job.stdout is not None and proc_job.stdout.closed
    assert proc_job.stderr is not None and proc_job.stderr.closed
    deadline = time.monotonic() + 5.0
    while _is_pid_alive(child_pid_job) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _is_pid_alive(child_pid_job)

    # 2. taskkill /T /F /PID fallback path (no Job Object attached)
    proc_tk, child_pid_tk = _spawn_parent_with_grandchild(tmp_path / "child_tk.pid", attach_job=False)
    _terminate_process(proc_tk)
    assert proc_tk.poll() is not None
    assert proc_tk.stderr is not None and proc_tk.stderr.closed
    deadline = time.monotonic() + 5.0
    while _is_pid_alive(child_pid_tk) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _is_pid_alive(child_pid_tk)


def test_posix_process_group_termination_and_pipe_closing(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_signals: list[tuple[int, int]] = []

    class FakePosixProc:
        def __init__(self) -> None:
            self.pid = 4242
            self.returncode: Optional[int] = None
            self.stdin = io.StringIO()
            self.stdout = io.StringIO()
            self.stderr = io.StringIO()
            self._wait_calls = 0

        def poll(self) -> Optional[int]:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: Optional[float] = None) -> int:
            self._wait_calls += 1
            if self._wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout or 3)
            self.returncode = -9
            return -9

    monkeypatch.setattr(ssh_bridge.sys, "platform", "linux")
    monkeypatch.setattr(ssh_bridge.os, "getpgid", lambda pid: pid + 100, raising=False)
    monkeypatch.setattr(
        ssh_bridge.os,
        "killpg",
        lambda pgid, sig: sent_signals.append((pgid, int(sig))),
        raising=False,
    )

    proc = FakePosixProc()
    _terminate_process(proc)  # type: ignore[arg-type]
    assert sent_signals[0] == (4342, int(signal.SIGTERM))
    assert len(sent_signals) == 2
    assert proc.stdin.closed and proc.stdout.closed and proc.stderr.closed


def test_websocket_stdio_bridge_serializes_send_and_close_with_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    overlap_detected = [False]
    in_critical = [False]
    close_calls = [0]
    sent_chunks: list[bytes] = []

    class FakeWs:
        def __init__(self) -> None:
            self._recv_idx = 0

        def connect(self, _url: str, header=None, timeout: int = 20) -> None:
            return None

        def send_binary(self, chunk: bytes) -> None:
            if in_critical[0]:
                overlap_detected[0] = True
            in_critical[0] = True
            try:
                time.sleep(0.01)
                sent_chunks.append(chunk)
            finally:
                in_critical[0] = False

        def recv_data(self, control_frame: bool = True):
            self._recv_idx += 1
            if self._recv_idx == 1:
                return ssh_bridge.websocket.ABNF.OPCODE_BINARY, b"from-remote"
            time.sleep(0.02)
            return ssh_bridge.websocket.ABNF.OPCODE_CLOSE, b""

        def close(self) -> None:
            if in_critical[0]:
                overlap_detected[0] = True
            in_critical[0] = True
            try:
                close_calls[0] += 1
                time.sleep(0.01)
            finally:
                in_critical[0] = False

    reads = iter([b"chunk-1", b"chunk-2", b""])
    out_buf = io.BytesIO()

    class FakeStdIn:
        class _Buf:
            def fileno(self) -> int:
                return 0

        buffer = _Buf()

    class FakeStdOut:
        buffer = out_buf

    monkeypatch.setattr(ssh_bridge.websocket, "WebSocket", FakeWs)
    monkeypatch.setattr(ssh_bridge.sys, "stdin", FakeStdIn())
    monkeypatch.setattr(ssh_bridge.sys, "stdout", FakeStdOut())
    monkeypatch.setattr(ssh_bridge.os, "read", lambda _fd, _n: next(reads, b""))

    rc = run_websocket_stdio_bridge("wss://rt.colab.test/colab/ssh", "ssh-ed25519 AAAA")
    assert rc == 0
    assert not overlap_detected[0]
    assert close_calls[0] == 1
    assert out_buf.getvalue() == b"from-remote"
    assert len(sent_chunks) >= 1


def test_spawn_multiplexed_ssh_three_attempts_chained_401_then_429_then_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    priv = tmp_path / "id_ed25519"
    priv.write_text("KEY", encoding="utf-8")

    attempts = [0]
    refresh_calls = [0]
    kill_429_calls = [0]

    class SequencePopen:
        def __init__(self, _cmd: list[str], **_kwargs: Any) -> None:
            attempts[0] += 1
            self.attempt = attempts[0]
            self.pid = 7000 + self.attempt
            self.stdin = io.StringIO()
            self.stdout = io.StringIO()
            if self.attempt == 1:
                self.returncode: Optional[int] = 1
                self.stderr = io.StringIO("[colab-boost-ssh] HTTP 401: expired token\n")
            elif self.attempt == 2:
                self.returncode = 1
                self.stderr = io.StringIO("[colab-boost-ssh] HTTP 429: too many ssh sessions\n")
            else:
                self.returncode = None
                self.stderr = io.StringIO("")

        def poll(self) -> Optional[int]:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: Optional[float] = None) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    monkeypatch.setattr(ssh_bridge, "_attach_process_job", lambda _p: None)
    monkeypatch.setattr(ssh_bridge.subprocess, "Popen", SequencePopen)
    monkeypatch.setattr(ssh_bridge, "is_local_port_open", lambda _port: attempts[0] >= 3)
    monkeypatch.setattr(ssh_bridge.time, "sleep", lambda _s: None)

    def refresh_cb() -> tuple[str, str]:
        refresh_calls[0] += 1
        return ("https://rt.colab.test", f"tok-fresh-{refresh_calls[0]}")

    def kill_429_cb() -> None:
        kill_429_calls[0] += 1

    mgr = SshTunnelManager(WorkspacePaths(tmp_path / "ws"), auto_reconnect=False)
    proc, _err, used_url, used_tok = mgr._spawn_multiplexed_ssh(
        session_name="sess-chain",
        fence=mgr._get_fence_token("sess-chain"),
        ssh_bin="ssh",
        priv_key=priv,
        pubkey="ssh-ed25519 AAAA",
        runtime_url="https://rt.colab.test",
        proxy_token="tok-stale",
        forwards=[(18010, 8010)],
        allow_429_kill=True,
        on_429_kill_stale_sshd=kill_429_cb,
        refresh_credentials=refresh_cb,
    )
    assert attempts[0] == 3
    assert kill_429_calls[0] == 1
    assert refresh_calls[0] >= 2
    assert used_url == "https://rt.colab.test"
    assert used_tok.startswith("tok-fresh-")
    assert proc.poll() is None
    _terminate_process(proc)


def test_reused_dead_entry_with_live_sibling_and_no_stuck_starting_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves reused_dead_entry is found even when live_rt is alive and never gets stuck in 'starting' on failure."""

    class AliveFakeProc:
        def __init__(self, pid: int = 5000) -> None:
            self.pid = pid
            self.returncode: Optional[int] = None
            self.stdin = None
            self.stdout = None
            self.stderr = None

        def poll(self) -> Optional[int]:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: Optional[float] = None) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    paths = WorkspacePaths(tmp_path / "state_machine")
    priv = paths.ssh_dir / "id_ed25519"
    pub = paths.ssh_dir / "id_ed25519.pub"
    paths.ssh_dir.mkdir(parents=True, exist_ok=True)
    priv.write_text("KEY", encoding="utf-8")
    pub.write_text("ssh-ed25519 AAAA test", encoding="utf-8")

    monkeypatch.setattr(ssh_bridge.shutil, "which", lambda _name: "ssh")
    monkeypatch.setattr(ssh_bridge, "_lock_private_key_permissions", lambda _p: None)
    monkeypatch.setattr(ssh_bridge, "is_local_port_open", lambda p: p == 19001)
    monkeypatch.setattr(ssh_bridge.time, "sleep", lambda _s: None)

    mgr = SshTunnelManager(paths, auto_reconnect=False)

    # Pre-seed a LIVE session serving sibling port 8001, plus a DEAD/error entry for port 8002
    live_proc = AliveFakeProc(5001)
    now = datetime.now(timezone.utc)
    tun1_info = TunnelInfo(
        id="tun-live-1",
        session_name="sess-sm",
        account_id="acc-1",
        remote_port=8001,
        local_port=19001,
        label="Port 8001",
        path="/",
        kind="http",
        status="open",
        local_url="http://127.0.0.1:19001",
        created_at=now,
    )
    dead_tun2_info = TunnelInfo(
        id="tun-dead-2",
        session_name="sess-sm",
        account_id="acc-1",
        remote_port=8002,
        local_port=19002,
        label="Port 8002",
        path="/",
        kind="http",
        status="error",
        error="previous failure",
        local_url="http://127.0.0.1:19002",
        created_at=now,
    )
    mgr._tunnels["tun-live-1"] = ActiveTunnelEntry(info=tun1_info, is_mock=False)
    mgr._tunnels["tun-dead-2"] = ActiveTunnelEntry(info=dead_tun2_info, is_mock=False)
    mgr._session_ssh["sess-sm"] = _SessionSshRuntime(
        process=live_proc,  # type: ignore[arg-type]
        stderr_lines=deque(maxlen=STDERR_MAX_LINES),
        runtime_url="https://rt.colab.test",
        proxy_token="tok-1",
        forwarded_ports=frozenset([(19001, 8001)]),
    )

    # 1. Retrying dead port 8002 FAILS on combined forwards, sibling 8001 is restored ->
    #    tun-dead-2 MUST remain status="error" (NEVER stuck in "starting") and NO duplicate entry created!
    def spawn_fail_combined_restore_sibling(**kwargs: Any):
        forwards = kwargs["forwards"]
        if len(forwards) == 2:
            raise RuntimeError("remote port 8002 refused")
        return AliveFakeProc(5002), deque(maxlen=STDERR_MAX_LINES), kwargs["runtime_url"], kwargs["proxy_token"]

    monkeypatch.setattr(mgr, "_spawn_multiplexed_ssh", spawn_fail_combined_restore_sibling)

    with pytest.raises(RuntimeError, match="remote port 8002 refused"):
        mgr.start_real_tunnel(
            session_name="sess-sm",
            account_id="acc-1",
            runtime_url="https://rt.colab.test",
            proxy_token="tok-1",
            remote_port=8002,
            local_port=19002,
        )

    assert set(mgr._tunnels.keys()) == {"tun-live-1", "tun-dead-2"}
    assert mgr._tunnels["tun-dead-2"].info.status == "error"
    assert "remote port 8002 refused" in (mgr._tunnels["tun-dead-2"].info.error or "")
    assert mgr._tunnels["tun-live-1"].info.status == "open"

    # 2. Retrying dead port 8002 SUCCEEDS while sibling is live -> reuses tun-dead-2 in place (same ID, 2 total)!
    monkeypatch.setattr(
        mgr,
        "_spawn_multiplexed_ssh",
        lambda **kwargs: (AliveFakeProc(5003), deque(maxlen=STDERR_MAX_LINES), kwargs["runtime_url"], kwargs["proxy_token"]),
    )
    healed = mgr.start_real_tunnel(
        session_name="sess-sm",
        account_id="acc-1",
        runtime_url="https://rt.colab.test",
        proxy_token="tok-1",
        remote_port=8002,
        local_port=19002,
    )
    assert healed.id == "tun-dead-2"
    assert healed.status == "open"
    assert healed.error is None
    assert set(mgr._tunnels.keys()) == {"tun-live-1", "tun-dead-2"}
    mgr.stop_all()


def test_background_monitor_auto_reconnect_with_backoff_and_clean_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves background monitor detects unexpected SSH exit, calls refresh_credentials, backs off, recovers, and exits cleanly on close()."""

    class ControlledProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: Optional[int] = None
            self.stdin = None
            self.stdout = None
            self.stderr = None

        def poll(self) -> Optional[int]:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: Optional[float] = None) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    paths = WorkspacePaths(tmp_path / "auto_reconnect")
    paths.ssh_dir.mkdir(parents=True, exist_ok=True)
    (paths.ssh_dir / "id_ed25519").write_text("KEY", encoding="utf-8")
    (paths.ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test", encoding="utf-8")

    monkeypatch.setattr(ssh_bridge.shutil, "which", lambda _name: "ssh")
    monkeypatch.setattr(ssh_bridge, "_lock_private_key_permissions", lambda _p: None)
    monkeypatch.setattr(ssh_bridge, "is_local_port_open", lambda _p: True)

    mgr = SshTunnelManager(
        paths,
        auto_reconnect=True,
        health_check_interval_s=0.03,
        reconnect_base_backoff_s=0.08,
        reconnect_max_backoff_s=0.5,
        reconnect_max_attempts=4,
    )

    spawn_times: list[float] = []
    spawn_tokens: list[str] = []
    procs: list[ControlledProc] = []
    refresh_calls = [0]

    def refresh_cb() -> tuple[str, str]:
        refresh_calls[0] += 1
        return ("https://rt.colab.test", f"tok-refreshed-{refresh_calls[0]}")

    def fake_spawn(**kwargs: Any):
        spawn_times.append(time.monotonic())
        spawn_tokens.append(kwargs["proxy_token"])
        idx = len(spawn_times)
        # Call #1: initial tunnel open succeeds
        # Call #2: first auto-reconnect attempt fails (triggers backoff)
        # Call #3: second auto-reconnect attempt succeeds!
        if idx == 2:
            raise RuntimeError("transient network blip")
        p = ControlledProc(6000 + idx)
        procs.append(p)
        return p, deque(maxlen=STDERR_MAX_LINES), kwargs["runtime_url"], kwargs["proxy_token"]

    monkeypatch.setattr(mgr, "_spawn_multiplexed_ssh", fake_spawn)

    try:
        tun = mgr.start_real_tunnel(
            session_name="sess-rec",
            account_id="acc-1",
            runtime_url="https://rt.colab.test",
            proxy_token="tok-initial",
            remote_port=8188,
            local_port=18188,
            refresh_credentials=refresh_cb,
        )
        assert tun.status == "open"
        monitor_ref = mgr._monitor_thread
        assert monitor_ref is not None and monitor_ref.is_alive()

        # Simulate unexpected SSH disconnect!
        procs[0].returncode = 255

        # Wait for auto-reconnect (1 failure + exponential backoff + 1 success)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            tunnels = mgr.list_tunnels("sess-rec")
            if len(spawn_times) >= 3 and tunnels and tunnels[0].status == "open":
                break
            time.sleep(0.03)

        assert len(spawn_times) == 3
        assert refresh_calls[0] >= 2
        assert spawn_tokens[0] == "tok-initial"
        assert spawn_tokens[2].startswith("tok-refreshed-")
        # Verify backoff delay elapsed between failed reconnect #1 and successful reconnect #2
        assert (spawn_times[2] - spawn_times[1]) >= 0.06
        final_tunnels = mgr.list_tunnels("sess-rec")
        assert len(final_tunnels) == 1
        assert final_tunnels[0].status == "open"
        assert final_tunnels[0].error is None
        assert mgr.get_session_proxy_token("sess-rec") == spawn_tokens[2]

        # close() cleanly stops & joins monitor thread and prevents further starts
        mgr.close()
        assert mgr._monitor_thread is None
        assert not monitor_ref.is_alive()
        with pytest.raises(RuntimeError, match="closed"):
            mgr.start_real_tunnel(
                session_name="sess-after-close",
                account_id="acc-1",
                runtime_url="https://rt.colab.test",
                proxy_token="tok-x",
                remote_port=8188,
            )
    finally:
        mgr.stop_all()
