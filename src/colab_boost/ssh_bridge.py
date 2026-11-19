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

import json
import os
import shutil
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
from typing import Callable, Optional
from urllib.parse import urlparse

import websocket

from colab_boost.config import WorkspacePaths
from colab_boost.models import TunnelInfo

PUBKEY_HEADER = "X-Colab-Ssh-Pubkey"
CLIENT_AGENT_HEADER = "X-Colab-Client-Agent"
CLIENT_AGENT_VALUE = "colab-cli"


def build_colab_ssh_ws_url(runtime_url: str, proxy_token: str, mode: str = "proxy") -> str:
    parsed = urlparse(runtime_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return (
        f"{scheme}://{parsed.netloc}/colab/ssh"
        f"?colab-runtime-proxy-token={proxy_token}"
        f"&colab-client-agent={CLIENT_AGENT_VALUE}"
        f"&colab-client-agent=ssh-mode-{mode}"
    )


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

    def pump_stdin() -> None:
        try:
            while True:
                chunk = os.read(stdin_fd, 16384)
                if not chunk:
                    break
                ws.send_binary(chunk)
        except Exception:
            pass
        finally:
            try:
                ws.close()
            except Exception:
                pass

    threading.Thread(target=pump_stdin, daemon=True).start()

    try:
        while True:
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
        try:
            ws.close()
        except Exception:
            pass
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


def _terminate_process(proc: Optional[subprocess.Popen]) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


@dataclass
class _SessionSshRuntime:
    process: subprocess.Popen
    stderr_lines: list[str]
    runtime_url: str
    proxy_token: str
    forwarded_ports: frozenset[tuple[int, int]] = field(default_factory=frozenset)
    on_429_kill_stale_sshd: Optional[Callable[[], None]] = None
    refresh_credentials: Optional[Callable[[], tuple[str, str]]] = None
    replacing: bool = False


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
    calls never race, and keeps ``proxy_token`` fresh across token rotations.
    """

    def __init__(self, paths: WorkspacePaths) -> None:
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

    def _reserve_local_port(self, preferred: int = 0) -> int:
        with self._lock:
            taken = set(self._reserved_local_ports) | {
                e.info.local_port
                for e in self._tunnels.values()
                if e.info.status in ("starting", "open")
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
                                tail = " | ".join(rt.stderr_lines[-3:]).strip()
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
    ) -> tuple[subprocess.Popen, list[str], str, str]:
        last_err = ""
        cur_url = runtime_url
        cur_token = proxy_token

        for attempt in range(2):
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

                # 2. Copy private key to ASCII temp path if workspace path has non-ASCII chars on Windows
                key_for_ssh = priv_key
                if sys.platform == "win32" and not str(priv_key).isascii():
                    kfd, tmp_k = tempfile.mkstemp(prefix=f"cboost_key_tun-{tag}_")
                    os.close(kfd)
                    temp_key_path = Path(tmp_k)
                    temp_key_path.write_bytes(priv_key.read_bytes())
                    _lock_private_key_permissions(temp_key_path)
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

                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=creationflags,
                )
                with self._lock:
                    self._spawning_procs.setdefault(session_name, set()).add(proc)

                stderr_lines: list[str] = []

                def collect_stderr(
                    p: subprocess.Popen = proc,
                    buf: list[str] = stderr_lines,
                    bpath: Optional[str] = bootstrap_str,
                    tkpath: Optional[Path] = temp_key_path,
                    tpath: Optional[str] = ticket_str,
                ) -> None:
                    try:
                        if p.stderr:
                            for line in p.stderr:
                                buf.append(line.rstrip())
                    finally:
                        _unlink_quietly(bpath, tpath, tkpath)

                collector_thread = threading.Thread(target=collect_stderr, daemon=True)
                collector_thread.start()
            except BaseException:
                if proc is not None:
                    with self._lock:
                        s_set = self._spawning_procs.get(session_name)
                        if s_set is not None:
                            s_set.discard(proc)
                if collector_thread is None:
                    _unlink_quietly(bootstrap_str, ticket_str, temp_key_path)
                if proc is not None:
                    _terminate_process(proc)
                if collector_thread is not None:
                    collector_thread.join(timeout=1.5)
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
                _terminate_process(proc)
                if collector_thread is not None:
                    collector_thread.join(timeout=1.5)
                _unlink_quietly(bootstrap_str, ticket_str, temp_key_path)
                raise RuntimeError(f"SSH tunnel cancelled because session '{session_name}' was stopped.")

            if opened and proc.poll() is None:
                # ProxyCommand has already consumed the ticket & bootstrap script
                _unlink_quietly(bootstrap_str, ticket_str)
                return proc, stderr_lines, cur_url, cur_token

            _terminate_process(proc)
            if collector_thread is not None:
                collector_thread.join(timeout=2.0)
            _unlink_quietly(bootstrap_str, ticket_str, temp_key_path)
            joined_err = " | ".join(stderr_lines)
            last_err = joined_err or "SSH tunnel failed to establish local listener"
            if attempt == 0:
                retried = False
                if ("HTTP 401" in joined_err or "HTTP 403" in joined_err) and refresh_credentials is not None:
                    try:
                        fresh_url, fresh_tok = refresh_credentials()
                        if fresh_url and fresh_tok and (fresh_url != cur_url or fresh_tok != cur_token):
                            cur_url, cur_token = fresh_url, fresh_tok
                            retried = True
                    except Exception:
                        pass
                if allow_429_kill and "429" in joined_err and on_429_kill_stale_sshd is not None:
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
            if fence[0] != entry_global_gen:
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

                # Heal an existing dead tunnel entry for the same (session, remote_port) in place instead of duplicating
                reused_dead_entry: Optional[ActiveTunnelEntry] = None
                if live_rt is None:
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
            chosen_local = self._reserve_local_port(preferred_local)
            try:
                if reused_dead_entry is not None:
                    tid = reused_dead_entry.info.id
                    info = reused_dead_entry.info
                    info.local_port = chosen_local
                    info.labels = dict(labels) if labels else info.labels
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
                        if not restored:
                            with self._lock:
                                for e in self._tunnels.values():
                                    if not e.is_mock and e.info.session_name == session_name:
                                        e.info.status = "error"
                                        e.info.error = str(spawn_exc)
                        raise
                    else:
                        raise

                active_set = frozenset(combined_forwards)
                with self._lock:
                    if self._get_fence_token(session_name) != fence:
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

    def stop_all(self) -> int:
        with self._lock:
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
        return count


if __name__ == "__main__":
    sys.exit(proxy_cli_main())
