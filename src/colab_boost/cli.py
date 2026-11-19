"""Command-line entry point & agent control client for Colab Boost Console."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any
import webbrowser

import httpx
import uvicorn

from colab_boost.config import SettingsStore, WorkspacePaths
from colab_boost.models import BackendMode
from colab_boost.orchestrator import ConsoleOrchestrator
from colab_boost.server import create_app

CLIENT_SUBCOMMANDS = frozenset({"status", "recipes", "deploy", "exec", "upload", "download", "stop", "stop-all"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="colab-boost",
        description="Colab Boost Console — Rapid model deployment & cost-guarded Google Colab control plane.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--mock", action="store_true", help="Start in deterministic Sandbox/Demo mode (0 billing)")
    mode_group.add_argument("--real", action="store_true", help="Start in Real Google Colab mode")
    parser.add_argument(
        "--mock-scenario",
        choices=["demo", "single", "signed_out"],
        default=None,
        help="Initial demo scenario when running in --mock mode",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    parser.add_argument("--open-browser", action="store_true", help="Open console URL in default browser")
    return parser.parse_args(argv)


def _default_api_url() -> str:
    return (os.environ.get("COLAB_BOOST_API_URL") or "http://127.0.0.1:8765").rstrip("/")


def _parse_kv_pairs(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            raise ValueError(f"Expected KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        k = k.strip()
        if not k:
            raise ValueError(f"Empty key in {item!r}")
        lv = v.lower()
        if lv in ("true", "false"):
            out[k] = lv == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
    return out


def _build_client_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="colab-boost",
        description="Agent & terminal control commands for a running Colab Boost Console server.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--api-url", default=_default_api_url(), help="Running console URL (default: http://127.0.0.1:8765)")
        p.add_argument("--json", action="store_true", dest="as_json", help="Emit raw JSON output")

    _add_common(sub.add_parser("status", help="Show backend mode, active account CCU, sessions, tunnels & runs"))
    _add_common(sub.add_parser("recipes", help="List available model deployment recipes"))

    p_dep = sub.add_parser("deploy", help="Launch a recipe deployment (optionally waiting until READY)")
    _add_common(p_dep)
    p_dep.add_argument("recipe_id", help="Recipe ID (e.g. minimax-h3-studio, llama-cpp-gguf, comfyui-studio)")
    p_dep.add_argument("--session", default=None, help="Reuse an existing running session name")
    p_dep.add_argument("--accel", default=None, help="Override accelerator (G4, A100, H100, L4, T4, NONE)")
    p_dep.add_argument("--min-vram", type=int, default=None, help="Minimum GPU VRAM in MiB (auto-retries smaller GPUs)")
    p_dep.add_argument("--high-mem", action="store_true", help="Request High-RAM VM shape")
    p_dep.add_argument("--input", action="append", default=[], dest="inputs", help="Recipe input KEY=VALUE (repeatable)")
    p_dep.add_argument("--wait", action="store_true", help="Wait until deployment reaches ready/failed/cancelled")
    p_dep.add_argument("--poll-interval", type=float, default=3.0, help="Polling interval in seconds when --wait is set")

    p_ex = sub.add_parser("exec", help="Execute Python or Bash directly inside an active Colab VM")
    _add_common(p_ex)
    p_ex.add_argument("session", help="Session name or endpoint")
    p_ex.add_argument("code", help="Python code or Bash command to run")
    p_ex.add_argument("--shell", action="store_true", help="Run via bash -lc instead of Python kernel")
    p_ex.add_argument("--timeout", type=float, default=120.0, help="Execution timeout in seconds")

    p_up = sub.add_parser("upload", help="Upload a local file to an active Colab VM with SHA256 verification")
    _add_common(p_up)
    p_up.add_argument("session", help="Session name or endpoint")
    p_up.add_argument("local_path", help="Local file path")
    p_up.add_argument("remote_path", help="Absolute destination path on VM (e.g. /content/input.zip)")

    p_dl = sub.add_parser("download", help="Download a file from an active Colab VM with SHA256 verification")
    _add_common(p_dl)
    p_dl.add_argument("session", help="Session name or endpoint")
    p_dl.add_argument("remote_path", help="Absolute source path on VM")
    p_dl.add_argument("local_path", help="Local destination file path")

    p_stop = sub.add_parser("stop", help="Stop a single Colab session and record an unassign receipt")
    _add_common(p_stop)
    p_stop.add_argument("identifier", help="Session name or endpoint")

    _add_common(sub.add_parser("stop-all", help="Stop ALL active Colab sessions and verify zero billing"))
    return parser


def _api_call(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    resp = client.request(method, path, **kwargs)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"detail": resp.text}
    if not resp.is_success:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        msg = detail.get("message") if isinstance(detail, dict) else str(detail)
        raise RuntimeError(f"HTTP {resp.status_code}: {msg}")
    return payload


def run_client_subcommand(argv: list[str], *, client: httpx.Client | None = None) -> int:
    args = _build_client_parser().parse_args(argv)
    owns_client = client is None
    http = client or httpx.Client(base_url=args.api_url.rstrip("/"), timeout=300.0)
    try:
        if args.cmd == "status":
            data = _api_call(http, "GET", "/api/bootstrap")
            if args.as_json:
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                acc = data.get("active_account") or {}
                usage = acc.get("usage") or {}
                print(f"Mode     : {data['settings']['backend_mode']}")
                print(f"Account  : {acc.get('email', '(none)')} | Balance: {usage.get('balance_ccu')} CCU | Burn: {usage.get('burn_rate_hourly')} CCU/h")
                print(f"Sessions : {len(data.get('sessions', []))}")
                for s in data.get("sessions", []):
                    print(f"  - {s['name']} [{s['accelerator']}] ({s.get('gpu_name') or 'CPU'}) endpoint={s['endpoint']}")
                print(f"Tunnels  : {len(data.get('tunnels', []))}")
                for t in data.get("tunnels", []):
                    print(f"  - {t['session_name']} VM:{t['remote_port']} -> {t['local_url']} ({t['status']})")
            return 0

        if args.cmd == "recipes":
            items = _api_call(http, "GET", "/api/recipes")
            if args.as_json:
                print(json.dumps(items, indent=2, ensure_ascii=False))
            else:
                for r in items:
                    prov = r.get("provision") or {}
                    print(f"- {r['id']:22s} [{prov.get('accelerator')}] {r['name'].get('zh-CN') or r['name'].get('en')}")
            return 0

        if args.cmd == "deploy":
            inputs = _parse_kv_pairs(args.inputs)
            provision: dict[str, Any] | None = None
            if args.session is None and (args.accel or args.min_vram is not None or args.high_mem):
                provision = {}
                if args.accel:
                    provision["accelerator"] = args.accel.upper()
                if args.min_vram is not None:
                    provision["min_vram_mib"] = args.min_vram
                if args.high_mem:
                    provision["high_mem"] = True
            body: dict[str, Any] = {
                "recipe_id": args.recipe_id,
                "session_name": args.session,
                "inputs": inputs,
            }
            if provision is not None:
                body["provision"] = provision
            run = _api_call(http, "POST", "/api/deployments", json=body)
            if args.wait:
                while run.get("state") in ("provisioning", "starting", "running"):
                    time.sleep(max(0.1, args.poll_interval))
                    run = _api_call(http, "GET", f"/api/deployments/{run['run_id']}")
            if args.as_json:
                print(json.dumps(run, indent=2, ensure_ascii=False))
            else:
                print(f"Run {run['run_id']} [{run['state']}] session={run['session_name']}: {run.get('message', '')}")
                for ep in run.get("endpoints") or []:
                    print(f"  Endpoint: {ep['local_url']} (VM:{ep['remote_port']})")
            return 0 if run.get("state") not in ("failed", "cancelled") else 1

        if args.cmd == "exec":
            res = _api_call(
                http,
                "POST",
                f"/api/sessions/{args.session}/exec",
                json={"code": args.code, "shell": args.shell, "timeout_s": args.timeout},
            )
            if args.as_json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                if res.get("stdout"):
                    sys.stdout.write(res["stdout"])
                if res.get("stderr"):
                    sys.stderr.write(res["stderr"])
            return 0 if res.get("ok") else 1

        if args.cmd == "upload":
            res = _api_call(
                http,
                "POST",
                f"/api/sessions/{args.session}/upload",
                json={"local_path": args.local_path, "remote_path": args.remote_path},
            )
            if args.as_json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                print(f"Uploaded {res['bytes']} bytes -> {res['session_name']}:{res['remote_path']} (sha256={res['sha256'][:12]})")
            return 0

        if args.cmd == "download":
            res = _api_call(
                http,
                "POST",
                f"/api/sessions/{args.session}/download",
                json={"remote_path": args.remote_path, "local_path": args.local_path},
            )
            if args.as_json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                print(f"Downloaded {res['bytes']} bytes -> {res['local_path']} (sha256={res['sha256'][:12]})")
            return 0

        if args.cmd == "stop":
            rc = _api_call(http, "POST", f"/api/sessions/{args.identifier}/stop")
            if args.as_json:
                print(json.dumps(rc, indent=2, ensure_ascii=False))
            else:
                print(f"Stopped {rc['session_name']} (receipt={rc['receipt_id']}, verified={rc['verified_unassigned']})")
            return 0

        if args.cmd == "stop-all":
            res = _api_call(http, "POST", "/api/sessions/stop-all")
            if args.as_json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                print(
                    f"Stopped {res['stopped_count']} session(s) | "
                    f"remaining={res['remaining_sessions']} | zero_burn_verified={res['zero_burn_verified']}"
                )
            return 0

        return 2
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    finally:
        if owns_client:
            http.close()


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] in CLIENT_SUBCOMMANDS:
        return run_client_subcommand(raw_argv)

    args = parse_args(raw_argv)
    if args.mock_scenario and args.real:
        sys.stderr.write("Error: --mock-scenario cannot be combined with --real\n")
        return 2

    paths = WorkspacePaths()
    store = SettingsStore(paths)
    settings = store.load()
    if args.mock:
        settings.backend_mode = BackendMode.MOCK
    elif args.real:
        settings.backend_mode = BackendMode.REAL
    if args.mock_scenario:
        settings.mock_scenario = args.mock_scenario
    store.save(settings)

    orch = ConsoleOrchestrator(paths=paths)
    app = create_app(orch)

    url = f"http://{args.host}:{args.port}"
    mode_banner = "SANDBOX DEMO (Zero Billing)" if orch.backend.is_mock else "REAL GOOGLE COLAB"
    print("============================================================")
    print(f"  Colab Boost Console  |  {url}")
    print(f"  Backend Mode         |  {mode_banner}")
    print("============================================================", flush=True)

    if args.open_browser:
        def _launch() -> None:
            time.sleep(0.8)
            webbrowser.open(url)

        threading.Thread(target=_launch, daemon=True).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
