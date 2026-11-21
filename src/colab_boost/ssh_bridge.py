"""Native cross-platform SSH localhost port-forwarding & WebSocket ProxyCommand bridge.

Connects Colab VM ports to ``127.0.0.1:<local_port>`` on Windows, macOS, and Linux
without WSL and without ever modifying ``~/.ssh/config``:
  1. Generates an isolated Ed25519 SSH keypair inside ``<workspace>/data/ssh/``.
  2. Spawns native ``ssh -N -L`` with ``ProxyCommand`` pointing to our own
     ``colab_boost.ssh_bridge`` stdio <-> ``wss://<runtime>/colab/ssh`` relay.
  3. Passes sensitive proxy connection parameters via an ephemeral ASCII-safe
     ticket file in ``%TEMP%`` (deleted immediately upon bridge startup) so tokens
     never appear on process command lines and Unicode workspace paths on Windows
     never break OpenSSH ``ProxyCommand`` parsing.
"""

from __future__ import annotations

from collections import deque
import ctypes
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urlparse

import websocket

from colab_boost.config import WorkspacePaths
from colab_boost.models import TunnelInfo

logger = logging.getLogger(__name__)

PUBKEY_HEADER = "X-Colab-Ssh-Pubkey"
CLIENT_AGENT_HEADER = "X-Colab-Client-Agent"
CLIENT_AGENT_VALUE = "colab-cli"
STDERR_MAX_LINES = 200


def build_colab_ssh_ws_url(runtime_url: str, proxy_token: str, mode: str = "proxy") -> str:
    parsed = urlparse(runtime_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urlencode(
        [
            ("colab-runtime-proxy-token", proxy_token),
            ("colab-client-agent", CLIENT_AGENT_VALUE),
            ("colab-client-agent", f"ssh-mode-{mode}"),
        ]
    )
    return f"{scheme}://{parsed.netloc}/colab/ssh?{query}"


def find_free_local_port(preferred: int = 0) -> int:
    if preferred and 1024 <= preferred <= 65535:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sock.connect_ex(("127.0.0.1", preferred)) != 0:
                try:
                    sock.bind(("127.0.0.1", preferred))
                    return preferred
                except OSError:
                    pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def is_local_port_open(port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


_WINDOWS_USER_SID: Optional[str] = None
_KEYGEN_LOCK = threading.Lock()


def _lock_private_key_permissions(key_path: Path) -> None:
    """Locks private key file permissions to owner-only so OpenSSH never rejects with 'bad permissions'."""
    global _WINDOWS_USER_SID
    if sys.platform == "win32":
        try:
            if not _WINDOWS_USER_SID:
                out = subprocess.run(
                    ["whoami", "/user", "/fo", "csv", "/nh"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                ).stdout.strip()
                if out:
                    _WINDOWS_USER_SID = [p.strip('"') for p in out.splitlines()[0].split(",")][-1]
            if _WINDOWS_USER_SID:
                subprocess.run(
                    ["icacls", str(key_path), "/inheritance:r", "/grant:r", f"*{_WINDOWS_USER_SID}:(F)"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
        except Exception:
            pass
    else:
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass


def ensure_isolated_ed25519_keypair(ssh_dir: Path) -> tuple[Path, str]:
    """Returns ``(private_key_path, public_key_text)`` inside ``ssh_dir`` (thread-safe)."""
    with _KEYGEN_LOCK:
        ssh_dir.mkdir(parents=True, exist_ok=True)
        priv = ssh_dir / "id_ed25519"
        pub = ssh_dir / "id_ed25519.pub"
        if not priv.exists() or not pub.exists():
            keygen = shutil.which("ssh-keygen")
            if not keygen:
                raise RuntimeError("OpenSSH 'ssh-keygen' not found on PATH.")
            for p in (priv, pub):
                if p.exists():
                    p.unlink()
            subprocess.run(
                [keygen, "-t", "ed25519", "-N", "", "-C", "colab-boost", "-f", str(priv)],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
        _lock_private_key_permissions(priv)
        return priv, pub.read_text(encoding="utf-8").strip()


def format_windows_safe_proxy_command(python_exe: str, script_file: str, ticket_file: str) -> str:
    """Formats an OpenSSH ProxyCommand string safe for both Windows CreateProcessW and POSIX sh."""
    if sys.platform == "win32":
        return subprocess.list2cmdline([python_exe, "-u", script_file, "--proxy-ticket", ticket_file])
    import shlex

    return " ".join(shlex.quote(x) for x in [python_exe, "-u", script_file, "--proxy-ticket", ticket_file])


def run_websocket_stdio_bridge(ws_url: str, pubkey: str) -> int:
    """Relays OpenSSH ProxyCommand stdio <-> Colab SSH WebSocket."""
    ws = websocket.WebSocket()
    headers = [
        f"{PUBKEY_HEADER}: {pubkey}",
        f"{CLIENT_AGENT_HEADER}: {CLIENT_AGENT_VALUE}",
    ]
    try:
        ws.connect(ws_url, header=headers, timeout=20)
    except websocket.WebSocketBadStatusException as exc:
        code = getattr(exc, "status_code", None)
        body = getattr(exc, "resp_body", b"") or b""
        if isinstance(body, bytes):
            snippet = body.decode("utf-8", errors="replace")[:200]
        else:
            snippet = str(body)[:200]
        sys.stderr.write(f"[colab-boost-ssh] HTTP {code}: {snippet}\n")
        sys.stderr.flush()
        return 1
    except Exception as exc:
        sys.stderr.write(f"[colab-boost-ssh] connect failed: {type(exc).__name__}: {exc}\n")
        sys.stderr.flush()
        return 1

    stdin_fd = sys.stdin.buffer.fileno()
    stdout_buf = sys.stdout.buffer
    ws_lock = threading.Lock()
    closed = threading.Event()

    def safe_close() -> None:
        if closed.is_set():
            return
        with ws_lock:
            if closed.is_set():
                return
            closed.set()
            try:
                ws.close()
            except Exception:
                pass

    def pump_stdin() -> None:
        try:
            while not closed.is_set():
                chunk = os.read(stdin_fd, 16384)
                if not chunk:
                    break
                with ws_lock:
                    if closed.is_set():
                        break
                    ws.send_binary(chunk)
        except Exception:
            pass
        finally:
            safe_close()

    threading.Thread(target=pump_stdin, daemon=True).start()

    try:
        while not closed.is_set():
            opcode, payload = ws.recv_data(control_frame=True)
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                break
            if opcode in (websocket.ABNF.OPCODE_BINARY, websocket.ABNF.OPCODE_TEXT):
                data = payload.encode("utf-8") if isinstance(payload, str) else payload
                if data:
                    stdout_buf.write(data)
                    stdout_buf.flush()
    except Exception:
        pass
    finally:
        safe_close()
    return 0


def proxy_cli_main(argv: Optional[list[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) >= 2 and args[0] == "--proxy-ticket":
        ticket_path = Path(args[1])
        try:
            payload = json.loads(ticket_path.read_text(encoding="utf-8"))
        finally:
            try:
                ticket_path.unlink(missing_ok=True)
            except OSError:
                pass
        return run_websocket_stdio_bridge(payload["ws_url"], payload["pubkey"])
    sys.stderr.write("Usage: colab-boost-ssh-proxy --proxy-ticket <path>\n")
    return 2


def _unlink_quietly(*paths: Optional[str | Path]) -> None:
    for item in paths:
        if item:
            try:
                Path(item).unlink(missing_ok=True)
            except OSError:
                pass


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class _WindowsProcessJob:
    """Wraps a Windows Job Object configured with KILL_ON_JOB_CLOSE to manage a process tree."""

    def __init__(self, handle: int, kernel32: Any) -> None:
        self._handle: Optional[int] = handle
        self._kernel32 = kernel32
        self._lock = threading.Lock()

    @classmethod
    def create_and_assign(cls, proc: subprocess.Popen) -> Optional["_WindowsProcessJob"]:
        if sys.platform != "win32":
            return None
        proc_handle = getattr(proc, "_handle", None)
        if not proc_handle:
            return None
        try:
            import ctypes.wintypes as wt

            class _IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class _JobObjectBasicLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", wt.LARGE_INTEGER),
                    ("PerJobUserTimeLimit", wt.LARGE_INTEGER),
                    ("LimitFlags", wt.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wt.DWORD),
                    ("SchedulingClass", wt.DWORD),
                ]

            class _JobObjectExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _JobObjectBasicLimitInformation),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
            k32.CreateJobObjectW.restype = wt.HANDLE
            k32.SetInformationJobObject.argtypes = [wt.HANDLE, wt.INT, ctypes.c_void_p, wt.DWORD]
            k32.SetInformationJobObject.restype = wt.BOOL
            k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
            k32.AssignProcessToJobObject.restype = wt.BOOL
            k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
            k32.TerminateJobObject.restype = wt.BOOL
            k32.CloseHandle.argtypes = [wt.HANDLE]
            k32.CloseHandle.restype = wt.BOOL

            job_handle = int(k32.CreateJobObjectW(None, None) or 0)
            if not job_handle:
                return None

            info = _JobObjectExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                job_handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                k32.CloseHandle(job_handle)
                return None

            if not k32.AssignProcessToJobObject(job_handle, int(proc_handle)):
                k32.CloseHandle(job_handle)
                return None

            return cls(job_handle, k32)
        except Exception:
            return None

    def terminate_and_close(self) -> None:
        with self._lock:
            handle = self._handle
            self._handle = None
        if handle:
            try:
                self._kernel32.TerminateJobObject(handle, 1)
            except Exception:
                pass
            try:
                self._kernel32.CloseHandle(handle)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            self._handle = None
        if handle:
            try:
                self._kernel32.CloseHandle(handle)
            except Exception:
                pass


_PROC_JOBS_LOCK = threading.Lock()
_PROC_JOBS: dict[int, _WindowsProcessJob] = {}


def _attach_process_job(proc: subprocess.Popen) -> Optional[_WindowsProcessJob]:
    if sys.platform != "win32":
        return None
    job = _WindowsProcessJob.create_and_assign(proc)
    if job is not None:
        with _PROC_JOBS_LOCK:
            _PROC_JOBS[id(proc)] = job
    return job


def _release_process_job(proc: subprocess.Popen) -> None:
    with _PROC_JOBS_LOCK:
        job = _PROC_JOBS.pop(id(proc), None)
    if job is not None:
        job.close()


def _close_process_pipes(proc: subprocess.Popen, *, close_stderr: bool = True) -> None:
    streams: list[Any] = [getattr(proc, "stdin", None), getattr(proc, "stdout", None)]
    if close_stderr:
        streams.append(getattr(proc, "stderr", None))
    for stream in streams:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def _terminate_process(proc: Optional[subprocess.Popen], *, close_stderr: bool = True) -> None:
    if proc is None:
        return
    with _PROC_JOBS_LOCK:
        job = _PROC_JOBS.pop(id(proc), None)
    try:
        if sys.platform == "win32":
            if job is not None:
                job.terminate_and_close()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            if proc.poll() is None:
                pid = getattr(proc, "pid", None)
                if pid:
                    try:
                        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        subprocess.run(
                            ["taskkill", "/T", "/F", "/PID", str(pid)],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                            creationflags=creationflags,
                        )
                    except Exception:
                        pass
                if proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                            proc.wait(timeout=1)
                        except Exception:
                            pass
                    except Exception:
                        pass
                else:
                    try:
                        proc.wait(timeout=1)
                    except Exception:
                        pass
        else:
            if proc.poll() is None:
                pgid: Optional[int] = None
                getpgid_fn = getattr(os, "getpgid", None)
                killpg_fn = getattr(os, "killpg", None)
                pid = getattr(proc, "pid", None)
                if callable(getpgid_fn) and callable(killpg_fn) and pid:
                    try:
                        pgid = int(getpgid_fn(pid))
                    except OSError:
                        pgid = None
                if pgid is not None and callable(killpg_fn):
                    try:
                        killpg_fn(pgid, signal.SIGTERM)
                    except OSError:
                        proc.terminate()
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    if pgid is not None and callable(killpg_fn):
                        try:
                            killpg_fn(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
                        except OSError:
                            proc.kill()
                    else:
                        proc.kill()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
    finally:
        if job is not None:
            job.close()
        _close_process_pipes(proc, close_stderr=close_stderr)


@dataclass
class _SessionSshRuntime:
    process: subprocess.Popen
    stderr_lines: deque[str]
    runtime_url: str
    proxy_token: str
    forwarded_ports: frozenset[tuple[int, int]] = field(default_factory=frozenset)
    on_429_kill_stale_sshd: Optional[Callable[[], None]] = None
    refresh_credentials: Optional[Callable[[], tuple[str, str]]] = None
    replacing: bool = False
    reconnect_failures: int = 0
    next_reconnect_at: float = 0.0
    reconnect_exhausted: bool = False


@dataclass
class ActiveTunnelEntry:
    info: TunnelInfo
    is_mock: bool = False


class SshTunnelManager:
    """Manages native ``ssh -L`` port-forwarding processes.

    Multiplexes multiple forwarded ports for the same Colab session onto a single
    ``ssh -N -L ... -L ...`` connection so multi-port workflows never hit Colab's
    single-concurrent-SSH-slot HTTP 429 limit. Serializes per-session lifecycle
    mutations so concurrent ``start_real_tunnel`` / ``stop_tunnel`` / ``stop_all``
    calls never race, keeps ``proxy_token`` fresh across token rotations, and
    monitors managed tunnels in the background to automatically reconnect with
    exponential backoff after unexpected SSH drops.
    """

    def __init__(
        self,
        paths: WorkspacePaths,
        *,
        auto_reconnect: bool = True,
        health_check_interval_s: float = 5.0,
        reconnect_base_backoff_s: float = 1.0,
        reconnect_max_backoff_s: float = 30.0,
        reconnect_max_attempts: int = 5,
    ) -> None:
        self.paths = paths
        self._lock = threading.RLock()
        self._language: str = "zh-CN"
        self._global_stop_gen: int = 0
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_stop_gen: dict[str, int] = {}
        self._spawning_procs: dict[str, set[subprocess.Popen]] = {}
        self._reserved_local_ports: set[int] = set()
        self._tunnels: dict[str, ActiveTunnelEntry] = {}
        self._session_ssh: dict[str, _SessionSshRuntime] = {}
        self._auto_reconnect = auto_reconnect
        self._health_check_interval_s = max(0.02, float(health_check_interval_s))
        self._reconnect_base_backoff_s = max(0.01, float(reconnect_base_backoff_s))
        self._reconnect_max_backoff_s = max(self._reconnect_base_backoff_s, float(reconnect_max_backoff_s))
        self._reconnect_max_attempts = max(1, int(reconnect_max_attempts))
        self._monitor_stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._closed = False

    def _localized_tunnel_label(self, labels: Optional[dict[str, str]], fallback: str) -> str:
        if not labels:
            return fallback
        return labels.get(self._language) or labels.get("en") or labels.get("zh-CN") or fallback

    def relocalize_labels(self, language: str) -> None:
        with self._lock:
            self._language = language
            for entry in self._tunnels.values():
                if entry.info.labels:
                    entry.info.label = self._localized_tunnel_label(entry.info.labels, entry.info.label)

    def _get_session_lock(self, session_name: str) -> threading.Lock:
        with self._lock:
            return self._session_locks.setdefault(session_name, threading.Lock())

    def _get_fence_token(self, session_name: str) -> tuple[int, int]:
        with self._lock:
            return (self._global_stop_gen, self._session_stop_gen.get(session_name, 0))

    def _reserve_local_port(self, preferred: int = 0, *, ignore_tunnel_id: Optional[str] = None) -> int:
        with self._lock:
            taken = set(self._reserved_local_ports) | {
                e.info.local_port
                for tid, e in self._tunnels.items()
                if tid != ignore_tunnel_id and e.info.status in ("starting", "open")
            }
            if preferred and 1024 <= preferred <= 65535 and preferred not in taken:
                cand = find_free_local_port(preferred)
                if cand not in taken:
                    self._reserved_local_ports.add(cand)
                    return cand
            for _ in range(24):
                cand = find_free_local_port(0)
                if cand not in taken:
                    self._reserved_local_ports.add(cand)
                    return cand
            cand = find_free_local_port(0)
            self._reserved_local_ports.add(cand)
            return cand

    def _release_reserved_port(self, port: int) -> None:
        with self._lock:
            self._reserved_local_ports.discard(port)

    def update_session_credentials(
        self,
        session_name: str,
        runtime_url: str,
        proxy_token: str,
        *,
        on_429_kill_stale_sshd: Optional[Callable[[], None]] = None,
        refresh_credentials: Optional[Callable[[], tuple[str, str]]] = None,
    ) -> None:
        """Updates cached proxy URL & token on an active session SSH runtime after token rotation."""
        with self._lock:
            rt = self._session_ssh.get(session_name)
            if rt is not None:
                if runtime_url:
                    rt.runtime_url = runtime_url
                if proxy_token:
                    rt.proxy_token = proxy_token
                if on_429_kill_stale_sshd is not None:
                    rt.on_429_kill_stale_sshd = on_429_kill_stale_sshd
                if refresh_credentials is not None:
                    rt.refresh_credentials = refresh_credentials

    def get_session_proxy_token(self, session_name: str) -> Optional[str]:
        with self._lock:
            rt = self._session_ssh.get(session_name)
            return rt.proxy_token if rt is not None else None

    def list_tunnels(self, session_name: Optional[str] = None) -> list[TunnelInfo]:
        with self._lock:
            items: list[TunnelInfo] = []
            for entry in self._tunnels.values():
                if entry.info.labels:
                    entry.info.label = self._localized_tunnel_label(entry.info.labels, entry.info.label)
                if not entry.is_mock:
                    rt = self._session_ssh.get(entry.info.session_name)
                    if rt is None:
                        if entry.info.status in ("starting", "open"):
                            entry.info.status = "error"
                            entry.info.error = entry.info.error or "SSH session disconnected"
                    elif not rt.replacing:
                        if rt.process.poll() is not None:
                            if entry.info.status in ("starting", "open"):
                                tail = " | ".join(list(rt.stderr_lines)[-3:]).strip()
                                entry.info.status = "error"
                                entry.info.error = tail or f"ssh exited ({rt.process.returncode})"
                        elif (entry.info.local_port, entry.info.remote_port) not in rt.forwarded_ports:
                            if entry.info.status in ("starting", "open"):
                                entry.info.status = "error"
                                entry.info.error = entry.info.error or "Port not active on multiplexed SSH session"
                if session_name is None or entry.info.session_name == session_name:
                    items.append(entry.info.model_copy(deep=True))
            return items

    def _spawn_multiplexed_ssh(
        self,
        *,
        session_name: str,
        fence: tuple[int, int],
        ssh_bin: str,
        priv_key: Path,
        pubkey: str,
        runtime_url: str,
        proxy_token: str,
        forwards: list[tuple[int, int]],
        allow_429_kill: bool,
        on_429_kill_stale_sshd: Optional[Callable[[], None]],
        refresh_credentials: Optional[Callable[[], tuple[str, str]]] = None,
    ) -> tuple[subprocess.Popen, deque[str], str, str]:
        last_err = ""
        cur_url = runtime_url
        cur_token = proxy_token
        did_kill_429 = False

        for attempt in range(3):
            if self._get_fence_token(session_name) != fence:
                raise RuntimeError(f"SSH tunnel cancelled because session '{session_name}' was stopped.")

            ws_url = build_colab_ssh_ws_url(cur_url, cur_token, mode="proxy")
            tag = uuid.uuid4().hex[:8]
            ticket_str: Optional[str] = None
            temp_key_path: Optional[Path] = None
            bootstrap_str: Optional[str] = None
            proc: Optional[subprocess.Popen] = None
            collector_thread: Optional[threading.Thread] = None

            try:
                # 1. Ephemeral ticket in system temp dir (ASCII path on Windows)
                fd, ticket_str = tempfile.mkstemp(prefix=f"cboost_tun-{tag}_", suffix=".json")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"ws_url": ws_url, "pubkey": pubkey}, fh)

                # 2. Copy private key to ASCII temp path if workspace path has non-ASCII chars on Windows.
                # Create empty file -> lock ACL -> write plaintext key to avoid any open-ACL window.
                key_for_ssh = priv_key
                if sys.platform == "win32" and not str(priv_key).isascii():
                    kfd, tmp_k = tempfile.mkstemp(prefix=f"cboost_key_tun-{tag}_")
                    os.close(kfd)
                    temp_key_path = Path(tmp_k)
                    _lock_private_key_permissions(temp_key_path)
                    temp_key_path.write_bytes(priv_key.read_bytes())
                    key_for_ssh = temp_key_path

                # 3. Tiny ASCII bootstrap script so ProxyCommand has zero non-ASCII path issues
                bfd, bootstrap_str = tempfile.mkstemp(prefix=f"cboost_bridge_tun-{tag}_", suffix=".py")
                src_root = str(Path(__file__).resolve().parents[1])
                with os.fdopen(bfd, "w", encoding="utf-8") as bfh:
                    bfh.write(
                        f"import sys\nsys.path.insert(0, {src_root!r})\n"
                        "from colab_boost.ssh_bridge import proxy_cli_main\n"
                        "sys.exit(proxy_cli_main())\n"
                    )

                proxy_cmd = format_windows_safe_proxy_command(sys.executable, bootstrap_str, ticket_str)
                null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
                cmd = [
                    ssh_bin,
                    "-F",
                    null_dev,
                    "-N",
                    "-i",
                    str(key_for_ssh),
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    f"UserKnownHostsFile={null_dev}",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-o",
                    "ServerAliveInterval=20",
                    "-o",
                    "ServerAliveCountMax=3",
                    "-o",
                    f"ProxyCommand={proxy_cmd}",
                ]
                for lport, rport in forwards:
                    cmd.extend(["-L", f"127.0.0.1:{lport}:127.0.0.1:{rport}"])
                cmd.append("root@colab-runtime")

                popen_kwargs: dict[str, Any] = {
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.PIPE,
                    "text": True,
                }
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                else:
                    popen_kwargs["start_new_session"] = True

                proc = subprocess.Popen(cmd, **popen_kwargs)
                _attach_process_job(proc)
                with self._lock:
                    self._spawning_procs.setdefault(session_name, set()).add(proc)

                stderr_lines: deque[str] = deque(maxlen=STDERR_MAX_LINES)

                def collect_stderr(
                    p: subprocess.Popen = proc,
                    buf: deque[str] = stderr_lines,
                    bpath: Optional[str] = bootstrap_str,
                    tkpath: Optional[Path] = temp_key_path,
                    tpath: Optional[str] = ticket_str,
                ) -> None:
                    try:
                        if p.stderr:
                            for line in p.stderr:
                                buf.append(line.rstrip())
                    except (OSError, ValueError):
                        pass
                    finally:
                        if p.stderr is not None:
                            try:
                                p.stderr.close()
                            except Exception:
                                pass
                        if p.poll() is not None:
                            _release_process_job(p)
                        _unlink_quietly(bpath, tpath, tkpath)

                collector_thread = threading.Thread(target=collect_stderr, daemon=True)
                collector_thread.start()
            except BaseException:
                if proc is not None:
                    with self._lock:
                        s_set = self._spawning_procs.get(session_name)
                        if s_set is not None:
                            s_set.discard(proc)
                    _terminate_process(proc, close_stderr=False)
                if collector_thread is not None:
                    collector_thread.join(timeout=1.5)
                if proc is not None:
                    _close_process_pipes(proc, close_stderr=True)
                _unlink_quietly(bootstrap_str, ticket_str, temp_key_path)
                raise

            try:
                deadline = time.monotonic() + 12.0
                opened = False
                while time.monotonic() < deadline:
                    if self._get_fence_token(session_name) != fence:
                        break
                    if proc.poll() is not None:
                        break
                    if all(is_local_port_open(lp) for lp, _rp in forwards):
                        opened = True
                        break
                    time.sleep(0.15)
            finally:
                with self._lock:
                    s_set = self._spawning_procs.get(session_name)
                    if s_set is not None:
                        s_set.discard(proc)

            if self._get_fence_token(session_name) != fence:
                _terminate_process(proc, close_stderr=False)
                if collector_thread is not None:
                    collector_thread.join(timeout=1.5)
                _close_process_pipes(proc, close_stderr=True)
                _unlink_quietly(bootstrap_str, ticket_str, temp_key_path)
                raise RuntimeError(f"SSH tunnel cancelled because session '{session_name}' was stopped.")

            if opened and proc.poll() is None:
                # OpenSSH has authenticated and ProxyCommand consumed the ticket & bootstrap script;
                # delete the temporary private key copy immediately so plaintext never lingers in %TEMP%.
                _unlink_quietly(bootstrap_str, ticket_str, temp_key_path)
                return proc, stderr_lines, cur_url, cur_token

            _terminate_process(proc, close_stderr=False)
            if collector_thread is not None:
                collector_thread.join(timeout=2.0)
            _close_process_pipes(proc, close_stderr=True)
            _unlink_quietly(bootstrap_str, ticket_str, temp_key_path)
            joined_err = " | ".join(stderr_lines)
            last_err = joined_err or "SSH tunnel failed to establish local listener"
            if attempt < 2:
                retried = False
                if ("HTTP 401" in joined_err or "HTTP 403" in joined_err) and refresh_credentials is not None:
                    try:
                        fresh_url, fresh_tok = refresh_credentials()
                        if fresh_url and fresh_tok and (fresh_url != cur_url or fresh_tok != cur_token):
                            cur_url, cur_token = fresh_url, fresh_tok
                            retried = True
                    except Exception:
                        pass
                if (
                    allow_429_kill
                    and "429" in joined_err
                    and on_429_kill_stale_sshd is not None
                    and not did_kill_429
                ):
                    did_kill_429 = True
                    on_429_kill_stale_sshd()
                    if refresh_credentials is not None:
                        try:
                            fresh_url, fresh_tok = refresh_credentials()
                            if fresh_url and fresh_tok:
                                cur_url, cur_token = fresh_url, fresh_tok
                        except Exception:
                            pass
                    time.sleep(0.2)
                    retried = True
                if retried:
                    continue
            break

        desc = ", ".join(f"{rp}->{lp}" for lp, rp in forwards)
        raise RuntimeError(f"Failed to open SSH port forward ({desc}): {last_err}")

    def start_real_tunnel(
        self,
        *,
        session_name: str,
        account_id: str,
        runtime_url: str,
        proxy_token: str,
        remote_port: int,
        local_port: int = 0,
        label: str = "Port Forward",
        labels: Optional[dict[str, str]] = None,
        path: str = "/",
        kind: str = "http",
        on_429_kill_stale_sshd: Optional[Callable[[], None]] = None,
        refresh_credentials: Optional[Callable[[], tuple[str, str]]] = None,
    ) -> TunnelInfo:
        if self._closed:
            raise RuntimeError("SshTunnelManager has been closed.")
        if remote_port == 8080:
            raise ValueError("VM port 8080 is reserved by Colab's internal proxy; use another port (e.g. 8000).")
        ssh_bin = shutil.which("ssh")
        if not ssh_bin:
            raise RuntimeError("OpenSSH client ('ssh') not found on PATH.")

        entry_global_gen = self._get_fence_token(session_name)[0]
        priv_key, pubkey = ensure_isolated_ed25519_keypair(self.paths.ssh_dir)
        norm_path = path if path.startswith("/") else f"/{path}"

        sess_lock = self._get_session_lock(session_name)
        with sess_lock:
            fence = self._get_fence_token(session_name)
            if fence[0] != entry_global_gen or self._closed:
                raise RuntimeError(f"SSH tunnel cancelled because session '{session_name}' was stopped.")

            with self._lock:
                eff_label = self._localized_tunnel_label(labels, label)
                existing_rt = self._session_ssh.get(session_name)
                live_rt = existing_rt if (existing_rt and existing_rt.process.poll() is None) else None
                eff_on_429 = on_429_kill_stale_sshd or (existing_rt.on_429_kill_stale_sshd if existing_rt else None)
                eff_refresh = refresh_credentials or (existing_rt.refresh_credentials if existing_rt else None)

                # Reuse identical active forward on the same session if already open & listening,
                # while ALWAYS updating runtime_url, proxy_token & callbacks so rotated tokens never go stale!
                for entry in self._tunnels.values():
                    if (
                        not entry.is_mock
                        and entry.info.session_name == session_name
                        and entry.info.remote_port == remote_port
                        and (local_port == 0 or entry.info.local_port == local_port)
                        and live_rt is not None
                        and (entry.info.local_port, entry.info.remote_port) in live_rt.forwarded_ports
                        and is_local_port_open(entry.info.local_port)
                    ):
                        live_rt.runtime_url = runtime_url
                        live_rt.proxy_token = proxy_token
                        live_rt.on_429_kill_stale_sshd = eff_on_429
                        live_rt.refresh_credentials = eff_refresh
                        entry.info.status = "open"
                        entry.info.error = None
                        if labels is not None:
                            entry.info.labels = dict(labels)
                        entry.info.label = self._localized_tunnel_label(entry.info.labels, eff_label)
                        return entry.info.model_copy(deep=True)

                # Heal an existing dead/errored tunnel entry for the same (session, remote_port) in place
                # even when sibling ports on the same session are still alive.
                reused_dead_entry: Optional[ActiveTunnelEntry] = None
                for entry in self._tunnels.values():
                    if (
                        not entry.is_mock
                        and entry.info.session_name == session_name
                        and entry.info.remote_port == remote_port
                        and (local_port == 0 or entry.info.local_port == local_port)
                    ):
                        reused_dead_entry = entry
                        break

                sibling_entries = [
                    e.info.model_copy(deep=True)
                    for e in self._tunnels.values()
                    if (
                        not e.is_mock
                        and e.info.session_name == session_name
                        and e is not reused_dead_entry
                        and e.info.status in ("open", "starting")
                    )
                ]
                if existing_rt is not None:
                    existing_rt.replacing = True
                    self._session_ssh.pop(session_name, None)

            # Reserve local port BEFORE stopping sibling ssh so we never collide with sibling or parallel sessions
            preferred_local = local_port or (reused_dead_entry.info.local_port if reused_dead_entry else remote_port)
            chosen_local = self._reserve_local_port(
                preferred_local,
                ignore_tunnel_id=reused_dead_entry.info.id if reused_dead_entry else None,
            )
            try:
                if reused_dead_entry is not None:
                    tid = reused_dead_entry.info.id
                    info = reused_dead_entry.info.model_copy(deep=True)
                    info.local_port = chosen_local
                    info.labels = dict(labels) if labels else dict(info.labels)
                    info.label = self._localized_tunnel_label(info.labels, eff_label)
                    info.path = norm_path
                    info.kind = kind
                    info.status = "starting"
                    info.error = None
                    info.local_url = f"http://127.0.0.1:{chosen_local}{norm_path if norm_path != '/' else ''}"
                else:
                    tid = f"tun-{uuid.uuid4().hex[:8]}"
                    info = TunnelInfo(
                        id=tid,
                        session_name=session_name,
                        account_id=account_id,
                        remote_port=remote_port,
                        local_port=chosen_local,
                        label=eff_label,
                        labels=dict(labels) if labels else {},
                        path=norm_path,
                        kind=kind,
                        status="starting",
                        local_url=f"http://127.0.0.1:{chosen_local}{norm_path if norm_path != '/' else ''}",
                        created_at=datetime.now(timezone.utc),
                    )

                # Cleanly replace existing session SSH with a single multiplexed SSH while holding sess_lock
                had_live_sibling = bool(sibling_entries and live_rt is not None)
                if existing_rt is not None:
                    _terminate_process(existing_rt.process)
                    if had_live_sibling:
                        time.sleep(0.25)

                combined_forwards = [(s.local_port, s.remote_port) for s in sibling_entries] + [(chosen_local, remote_port)]

                try:
                    proc, stderr_lines, used_url, used_tok = self._spawn_multiplexed_ssh(
                        session_name=session_name,
                        fence=fence,
                        ssh_bin=ssh_bin,
                        priv_key=priv_key,
                        pubkey=pubkey,
                        runtime_url=runtime_url,
                        proxy_token=proxy_token,
                        forwards=combined_forwards,
                        allow_429_kill=True,
                        on_429_kill_stale_sshd=eff_on_429,
                        refresh_credentials=eff_refresh,
                    )
                except Exception as spawn_exc:
                    if sibling_entries and not had_live_sibling and self._get_fence_token(session_name) == fence:
                        try:
                            combined_forwards = [(chosen_local, remote_port)]
                            proc, stderr_lines, used_url, used_tok = self._spawn_multiplexed_ssh(
                                session_name=session_name,
                                fence=fence,
                                ssh_bin=ssh_bin,
                                priv_key=priv_key,
                                pubkey=pubkey,
                                runtime_url=runtime_url,
                                proxy_token=proxy_token,
                                forwards=combined_forwards,
                                allow_429_kill=True,
                                on_429_kill_stale_sshd=eff_on_429,
                                refresh_credentials=eff_refresh,
                            )
                        except Exception as retry_exc:
                            if reused_dead_entry is not None:
                                with self._lock:
                                    reused_dead_entry.info.status = "error"
                                    reused_dead_entry.info.error = str(retry_exc)
                            raise
                    elif sibling_entries and had_live_sibling and self._get_fence_token(session_name) == fence:
                        restored = False
                        sib_forwards = [(s.local_port, s.remote_port) for s in sibling_entries]
                        fallback_creds: list[tuple[str, str]] = []
                        if eff_refresh is not None:
                            try:
                                f_url, f_tok = eff_refresh()
                                if f_url and f_tok:
                                    fallback_creds.append((f_url, f_tok))
                            except Exception:
                                pass
                        if (runtime_url, proxy_token) not in fallback_creds:
                            fallback_creds.append((runtime_url, proxy_token))
                        if existing_rt and (existing_rt.runtime_url, existing_rt.proxy_token) not in fallback_creds:
                            fallback_creds.append((existing_rt.runtime_url, existing_rt.proxy_token))
                        for fb_url, fb_tok in fallback_creds:
                            try:
                                r_proc, r_err, r_url, r_tok = self._spawn_multiplexed_ssh(
                                    session_name=session_name,
                                    fence=fence,
                                    ssh_bin=ssh_bin,
                                    priv_key=priv_key,
                                    pubkey=pubkey,
                                    runtime_url=fb_url,
                                    proxy_token=fb_tok,
                                    forwards=sib_forwards,
                                    allow_429_kill=True,
                                    on_429_kill_stale_sshd=eff_on_429,
                                    refresh_credentials=eff_refresh,
                                )
                                with self._lock:
                                    if self._get_fence_token(session_name) == fence:
                                        self._session_ssh[session_name] = _SessionSshRuntime(
                                            process=r_proc,
                                            stderr_lines=r_err,
                                            runtime_url=r_url,
                                            proxy_token=r_tok,
                                            forwarded_ports=frozenset(sib_forwards),
                                            on_429_kill_stale_sshd=eff_on_429,
                                            refresh_credentials=eff_refresh,
                                        )
                                        restored = True
                                    else:
                                        _terminate_process(r_proc)
                                break
                            except Exception:
                                continue
                        with self._lock:
                            if reused_dead_entry is not None:
                                reused_dead_entry.info.status = "error"
                                reused_dead_entry.info.error = str(spawn_exc)
                            if not restored:
                                for e in self._tunnels.values():
                                    if not e.is_mock and e.info.session_name == session_name:
                                        e.info.status = "error"
                                        e.info.error = str(spawn_exc)
                        raise
                    else:
                        if reused_dead_entry is not None:
                            with self._lock:
                                reused_dead_entry.info.status = "error"
                                reused_dead_entry.info.error = str(spawn_exc)
                        raise

                active_set = frozenset(combined_forwards)
                with self._lock:
                    if self._get_fence_token(session_name) != fence or self._closed:
                        proc_to_kill = proc
                    else:
                        proc_to_kill = None
                        info.status = "open"
                        info.error = None
                        self._tunnels[tid] = ActiveTunnelEntry(info=info, is_mock=False)
                        for e in self._tunnels.values():
                            if not e.is_mock and e.info.session_name == session_name:
                                if (e.info.local_port, e.info.remote_port) in active_set:
                                    e.info.status = "open"
                                    e.info.error = None
                                elif e.info.status in ("starting", "open"):
                                    e.info.status = "error"
                                    e.info.error = e.info.error or "Port not active on multiplexed SSH session"
                        self._session_ssh[session_name] = _SessionSshRuntime(
                            process=proc,
                            stderr_lines=stderr_lines,
                            runtime_url=used_url,
                            proxy_token=used_tok,
                            forwarded_ports=active_set,
                            on_429_kill_stale_sshd=eff_on_429,
                            refresh_credentials=eff_refresh,
                        )
                        self._ensure_monitor_started_locked()
                if proc_to_kill is not None:
                    _terminate_process(proc_to_kill)
                    raise RuntimeError(f"SSH tunnel cancelled because session '{session_name}' was stopped.")
                return info.model_copy(deep=True)
            finally:
                self._release_reserved_port(chosen_local)

    def register_mock_tunnel(
        self,
        *,
        session_name: str,
        account_id: str,
        remote_port: int,
        local_port: int = 0,
        label: str = "Port Forward",
        labels: Optional[dict[str, str]] = None,
        path: str = "/",
        kind: str = "http",
    ) -> TunnelInfo:
        if remote_port == 8080:
            raise ValueError("VM port 8080 is reserved by Colab's internal proxy; use another port (e.g. 8000).")
        tid = f"tun-{uuid.uuid4().hex[:8]}"
        chosen = local_port or remote_port
        norm_path = path if path.startswith("/") else f"/{path}"
        with self._lock:
            eff_label = self._localized_tunnel_label(labels, label)
            info = TunnelInfo(
                id=tid,
                session_name=session_name,
                account_id=account_id,
                remote_port=remote_port,
                local_port=chosen,
                label=eff_label,
                labels=dict(labels) if labels else {},
                path=norm_path,
                kind=kind,
                status="open",
                local_url=f"http://127.0.0.1:{chosen}{norm_path if norm_path != '/' else ''}",
                created_at=datetime.now(timezone.utc),
            )
            self._tunnels[tid] = ActiveTunnelEntry(info=info, is_mock=True)
            return info.model_copy(deep=True)

    def stop_tunnel(self, tunnel_id: str) -> bool:
        with self._lock:
            peek = self._tunnels.get(tunnel_id)
            if not peek:
                return False
            if peek.is_mock:
                self._tunnels.pop(tunnel_id, None)
                return True
            sess = peek.info.session_name

        sess_lock = self._get_session_lock(sess)
        with sess_lock:
            with self._lock:
                entry = self._tunnels.pop(tunnel_id, None)
                if not entry:
                    return False
                if entry.is_mock:
                    return True
                fence = self._get_fence_token(sess)
                remaining = [
                    e.info.model_copy(deep=True)
                    for e in self._tunnels.values()
                    if not e.is_mock and e.info.session_name == sess and e.info.status == "open"
                ]
                rt = self._session_ssh.pop(sess, None)
                if rt is not None:
                    rt.replacing = True

            if rt is not None:
                _terminate_process(rt.process)
                if remaining and self._get_fence_token(sess) == fence:
                    ssh_bin = shutil.which("ssh")
                    if ssh_bin:
                        try:
                            time.sleep(0.2)
                            priv_key, pubkey = ensure_isolated_ed25519_keypair(self.paths.ssh_dir)
                            respawn_url = rt.runtime_url
                            respawn_tok = rt.proxy_token
                            if rt.refresh_credentials is not None:
                                try:
                                    fresh_url, fresh_tok = rt.refresh_credentials()
                                    if fresh_url and fresh_tok:
                                        respawn_url, respawn_tok = fresh_url, fresh_tok
                                except Exception:
                                    pass
                            rem_forwards = [(s.local_port, s.remote_port) for s in remaining]
                            proc, stderr_lines, used_url, used_tok = self._spawn_multiplexed_ssh(
                                session_name=sess,
                                fence=fence,
                                ssh_bin=ssh_bin,
                                priv_key=priv_key,
                                pubkey=pubkey,
                                runtime_url=respawn_url,
                                proxy_token=respawn_tok,
                                forwards=rem_forwards,
                                allow_429_kill=True,
                                on_429_kill_stale_sshd=rt.on_429_kill_stale_sshd,
                                refresh_credentials=rt.refresh_credentials,
                            )
                            with self._lock:
                                if self._get_fence_token(sess) == fence:
                                    self._session_ssh[sess] = _SessionSshRuntime(
                                        process=proc,
                                        stderr_lines=stderr_lines,
                                        runtime_url=used_url,
                                        proxy_token=used_tok,
                                        forwarded_ports=frozenset(rem_forwards),
                                        on_429_kill_stale_sshd=rt.on_429_kill_stale_sshd,
                                        refresh_credentials=rt.refresh_credentials,
                                    )
                                    kill_after = None
                                else:
                                    kill_after = proc
                            if kill_after is not None:
                                _terminate_process(kill_after)
                        except Exception as exc:
                            with self._lock:
                                for e in self._tunnels.values():
                                    if not e.is_mock and e.info.session_name == sess:
                                        e.info.status = "error"
                                        e.info.error = str(exc)
            return True

    def stop_session_tunnels(self, session_name: str) -> int:
        with self._lock:
            self._session_stop_gen[session_name] = self._session_stop_gen.get(session_name, 0) + 1
            spawning = list(self._spawning_procs.get(session_name, ()))
        for sp in spawning:
            _terminate_process(sp)

        sess_lock = self._get_session_lock(session_name)
        with sess_lock:
            with self._lock:
                matching = [tid for tid, e in self._tunnels.items() if e.info.session_name == session_name]
                for tid in matching:
                    self._tunnels.pop(tid, None)
                rt = self._session_ssh.pop(session_name, None)
            if rt is not None:
                _terminate_process(rt.process)
            return len(matching)

    def _ensure_monitor_started_locked(self) -> None:
        """Starts the lightweight background health-check & auto-reconnect thread if enabled. Caller holds ``_lock``."""
        if not self._auto_reconnect or self._closed:
            return
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()
        th = threading.Thread(target=self._monitor_loop, name="colab-boost-ssh-monitor", daemon=True)
        self._monitor_thread = th
        th.start()

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(self._health_check_interval_s):
            now = time.monotonic()
            candidates: list[tuple[str, tuple[int, int]]] = []
            with self._lock:
                if self._monitor_stop.is_set() or self._closed:
                    break
                for sess_name, rt in list(self._session_ssh.items()):
                    if rt.replacing or rt.reconnect_exhausted:
                        continue
                    if rt.process.poll() is None:
                        continue
                    if now < rt.next_reconnect_at:
                        continue
                    has_managed = any(
                        (not e.is_mock)
                        and e.info.session_name == sess_name
                        and (e.info.local_port, e.info.remote_port) in rt.forwarded_ports
                        for e in self._tunnels.values()
                    )
                    if not has_managed:
                        continue
                    candidates.append((sess_name, self._get_fence_token(sess_name)))

            for sess_name, fence in candidates:
                if self._monitor_stop.is_set() or self._closed:
                    break
                self._attempt_reconnect_session(sess_name, fence)

    def _attempt_reconnect_session(self, session_name: str, expected_fence: tuple[int, int]) -> None:
        if self._monitor_stop.is_set() or self._closed:
            return
        sess_lock = self._get_session_lock(session_name)
        if not sess_lock.acquire(blocking=False):
            return
        prev_failures = 0
        try:
            with self._lock:
                if self._monitor_stop.is_set() or self._closed:
                    return
                if self._get_fence_token(session_name) != expected_fence:
                    return
                rt = self._session_ssh.get(session_name)
                if rt is None or rt.replacing or rt.reconnect_exhausted or rt.process.poll() is None:
                    return
                forwards = sorted(rt.forwarded_ports)
                if not forwards:
                    return
                rt.replacing = True
                cur_url = rt.runtime_url
                cur_token = rt.proxy_token
                on_429 = rt.on_429_kill_stale_sshd
                refresh_cb = rt.refresh_credentials
                prev_failures = rt.reconnect_failures

            _terminate_process(rt.process)

            ssh_bin = shutil.which("ssh")
            if not ssh_bin:
                raise RuntimeError("OpenSSH client ('ssh') not found on PATH.")
            priv_key, pubkey = ensure_isolated_ed25519_keypair(self.paths.ssh_dir)

            if refresh_cb is not None:
                try:
                    fresh_url, fresh_tok = refresh_cb()
                    if fresh_url and fresh_tok:
                        cur_url, cur_token = fresh_url, fresh_tok
                except Exception as exc:
                    logger.warning("SSH auto-reconnect credential refresh failed for '%s': %s", session_name, exc)

            proc, stderr_lines, used_url, used_tok = self._spawn_multiplexed_ssh(
                session_name=session_name,
                fence=expected_fence,
                ssh_bin=ssh_bin,
                priv_key=priv_key,
                pubkey=pubkey,
                runtime_url=cur_url,
                proxy_token=cur_token,
                forwards=forwards,
                allow_429_kill=True,
                on_429_kill_stale_sshd=on_429,
                refresh_credentials=refresh_cb,
            )

            active_set = frozenset(forwards)
            proc_to_kill: Optional[subprocess.Popen] = None
            with self._lock:
                if self._monitor_stop.is_set() or self._closed or self._get_fence_token(session_name) != expected_fence:
                    proc_to_kill = proc
                else:
                    self._session_ssh[session_name] = _SessionSshRuntime(
                        process=proc,
                        stderr_lines=stderr_lines,
                        runtime_url=used_url,
                        proxy_token=used_tok,
                        forwarded_ports=active_set,
                        on_429_kill_stale_sshd=on_429,
                        refresh_credentials=refresh_cb,
                        reconnect_failures=0,
                        next_reconnect_at=0.0,
                        reconnect_exhausted=False,
                    )
                    for e in self._tunnels.values():
                        if (
                            not e.is_mock
                            and e.info.session_name == session_name
                            and (e.info.local_port, e.info.remote_port) in active_set
                        ):
                            e.info.status = "open"
                            e.info.error = None
            if proc_to_kill is not None:
                _terminate_process(proc_to_kill)
        except Exception as exc:
            logger.warning("SSH auto-reconnect attempt failed for '%s': %s", session_name, exc)
            with self._lock:
                cur_rt = self._session_ssh.get(session_name)
                if cur_rt is not None and self._get_fence_token(session_name) == expected_fence:
                    cur_rt.replacing = False
                    cur_rt.reconnect_failures = prev_failures + 1
                    if cur_rt.reconnect_failures >= self._reconnect_max_attempts:
                        cur_rt.reconnect_exhausted = True
                    else:
                        delay = min(
                            self._reconnect_max_backoff_s,
                            self._reconnect_base_backoff_s * (2 ** (cur_rt.reconnect_failures - 1)),
                        )
                        cur_rt.next_reconnect_at = time.monotonic() + delay
                    for e in self._tunnels.values():
                        if not e.is_mock and e.info.session_name == session_name:
                            e.info.status = "error"
                            e.info.error = (
                                f"SSH auto-reconnect failed "
                                f"({cur_rt.reconnect_failures}/{self._reconnect_max_attempts}): {exc}"
                            )
        finally:
            sess_lock.release()

    def stop_all(self) -> int:
        with self._lock:
            self._monitor_stop.set()
            monitor_thread = self._monitor_thread
            self._monitor_thread = None
            self._global_stop_gen += 1
            sessions = (
                set(self._session_locks.keys())
                | set(self._session_ssh.keys())
                | {e.info.session_name for e in self._tunnels.values()}
            )
            for s in sessions:
                self._session_stop_gen[s] = self._session_stop_gen.get(s, 0) + 1
            spawning = [p for procs in self._spawning_procs.values() for p in procs]
            count = len(self._tunnels)
            self._tunnels.clear()
            runtimes = list(self._session_ssh.values())
            self._session_ssh.clear()

        for sp in spawning:
            _terminate_process(sp)
        for rt in runtimes:
            _terminate_process(rt.process)
        for s in sorted(sessions):
            with self._get_session_lock(s):
                pass
        if monitor_thread is not None and monitor_thread is not threading.current_thread():
            monitor_thread.join(timeout=5.0)
        return count

    def close(self) -> int:
        with self._lock:
            self._closed = True
        return self.stop_all()


if __name__ == "__main__":
    sys.exit(proxy_cli_main())
