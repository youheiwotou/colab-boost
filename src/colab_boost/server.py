"""FastAPI control-plane server, WebSocket live event stream, and Playground proxy."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import logging
import os
from pathlib import Path
import time
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

from colab_boost.backends.base import ColabBackendError
from colab_boost.gguf_planner import (
    GgufRepoPlan,
    build_hardware_fit_matrix,
    inspect_hf_gguf_repo,
    search_hf_gguf_repos,
    validate_hf_repo_id,
)
from colab_boost.models import (
    ACCELERATOR_VRAM_REFERENCE_MIB,
    APPROX_CCU_PER_HOUR,
    GPU_ACCELERATORS,
    HIGH_MEM_ONLY_ACCELERATORS,
    TPU_ACCELERATORS,
    Accelerator,
    DeployRequest,
    ProvisionRequest,
    SettingsPatch,
)
from colab_boost.orchestrator import ConsoleOrchestrator
from colab_boost.recipe_loader import RecipeValidationError
from colab_boost.vault import redact_secrets

logger = logging.getLogger(__name__)


def _safe_error_text(
    orch: ConsoleOrchestrator,
    exc: BaseException,
    extra_secrets: Optional[list[str]] = None,
) -> str:
    try:
        secrets = list(orch.vault.all_known_secrets())
    except Exception:
        secrets = []
    if extra_secrets:
        secrets.extend(s for s in extra_secrets if s)
    return redact_secrets(f"{type(exc).__name__}: {exc}", secrets)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_WS_QUEUE_MAX = 256
_ACCELERATOR_DISPLAY_ORDER = (
    Accelerator.G4,
    Accelerator.A100,
    Accelerator.H100,
    Accelerator.L4,
    Accelerator.T4,
    Accelerator.V5E1,
    Accelerator.V6E1,
    Accelerator.NONE,
)


def _build_accelerator_specs() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for acc in _ACCELERATOR_DISPLAY_ORDER:
        kind = "gpu" if acc in GPU_ACCELERATORS else ("tpu" if acc in TPU_ACCELERATORS else "cpu")
        specs.append(
            {
                "id": acc.value,
                "kind": kind,
                "vram_mib": list(ACCELERATOR_VRAM_REFERENCE_MIB.get(acc.value, [])),
                "high_mem_only": acc in HIGH_MEM_ONLY_ACCELERATORS,
                "approx_ccu_per_hour": float(APPROX_CCU_PER_HOUR.get(acc.value, 0.0)),
            }
        )
    return specs


def _extract_hostname_from_host(host_header: Optional[str]) -> Optional[str]:
    raw = (host_header or "").strip()
    if not raw or any(ch.isspace() for ch in raw) or "/" in raw or "\\" in raw or "@" in raw:
        return None
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return None
        host = raw[1:end].strip().lower()
        rest = raw[end + 1 :]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                return None
        return host or None
    if ":" in raw:
        host, port = raw.rsplit(":", 1)
        if not port.isdigit():
            return None
        return host.strip().lower() or None
    return raw.lower()


def _get_allowed_hosts() -> set[str]:
    allowed = set(_DEFAULT_ALLOWED_HOSTS)
    extra = os.environ.get("COLAB_BOOST_ALLOWED_HOSTS", "").strip()
    if extra:
        for item in extra.split(","):
            parsed = _extract_hostname_from_host(item)
            if parsed:
                allowed.add(parsed)
    return allowed


def _is_allowed_origin(origin_header: str, allowed_hosts: set[str]) -> bool:
    raw = origin_header.strip()
    if not raw or raw.lower() == "null":
        return False
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return parsed.hostname.lower() in allowed_hosts


def _extract_bearer_token(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    scheme, sep, token = auth_header.strip().partition(" ")
    if not sep or scheme.lower() != "bearer":
        return None
    cleaned = token.strip()
    return cleaned or None


def _resolve_api_token(explicit_token: Optional[str]) -> str:
    if explicit_token is not None:
        return explicit_token.strip()
    return os.environ.get("COLAB_BOOST_API_TOKEN", "").strip()


class OAuthCompleteBody(BaseModel):
    flow_id: str
    code: str


class ExtendDeadlineBody(BaseModel):
    add_minutes: int = Field(default=120, ge=1, le=1440)


class SessionExecBody(BaseModel):
    code: str
    shell: bool = False
    env: Optional[dict[str, str]] = None
    timeout_s: float = Field(default=60.0, ge=1.0, le=1800.0)


class SessionUploadBody(BaseModel):
    remote_path: str
    local_path: Optional[str] = None
    content_b64: Optional[str] = None


class SessionDownloadBody(BaseModel):
    remote_path: str
    local_path: Optional[str] = None


class OpenTunnelBody(BaseModel):
    session_name: str
    remote_port: int = Field(..., ge=1024, le=65535)
    local_port: int = Field(default=0, ge=0, le=65535)
    label: str = "Port Forward"
    path: str = "/"
    kind: str = "http"


class SecretUpdateBody(BaseModel):
    value: str


class PlaygroundChatBody(BaseModel):
    tunnel_id: Optional[str] = None
    base_url: Optional[str] = None
    model: str = "default"
    api_key: Optional[str] = None
    messages: list[dict[str, str]] = Field(default_factory=list)
    max_tokens: int = Field(default=256, ge=1, le=8192)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


# Built-in preset GGUF catalog for instant fallback or quick-pick suggestions
CURATED_GGUF_CATALOG: dict[str, dict[str, Any]] = {
    "Qwen/Qwen2.5-7B-Instruct-GGUF": {
        "arch": "qwen2",
        "ctx": 32768,
        "kv_bpt": 57344,
        "files": [
            ("qwen2.5-7b-instruct-q8_0.gguf", int(8.10 * 1024**3)),
            ("qwen2.5-7b-instruct-q6_k.gguf", int(6.25 * 1024**3)),
            ("qwen2.5-7b-instruct-q5_k_m.gguf", int(5.45 * 1024**3)),
            ("qwen2.5-7b-instruct-q4_k_m.gguf", int(4.68 * 1024**3)),
        ],
    },
    "unsloth/Qwen3-32B-GGUF": {
        "arch": "qwen3",
        "ctx": 40960,
        "kv_bpt": 262144,
        "files": [
            ("Qwen3-32B-BF16.gguf", int(61.2 * 1024**3)),
            ("Qwen3-32B-Q8_0.gguf", int(32.5 * 1024**3)),
            ("Qwen3-32B-Q6_K.gguf", int(25.1 * 1024**3)),
            ("Qwen3-32B-Q4_K_M.gguf", int(18.5 * 1024**3)),
            ("Qwen3-32B-IQ4_XS.gguf", int(16.4 * 1024**3)),
        ],
    },
    "bartowski/DeepSeek-R1-Distill-Qwen-32B-GGUF": {
        "arch": "qwen2",
        "ctx": 65536,
        "kv_bpt": 262144,
        "files": [
            ("DeepSeek-R1-Distill-Qwen-32B-Q8_0.gguf", int(32.6 * 1024**3)),
            ("DeepSeek-R1-Distill-Qwen-32B-Q6_K.gguf", int(25.2 * 1024**3)),
            ("DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf", int(18.6 * 1024**3)),
        ],
    },
}


def _map_domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RecipeValidationError):
        return HTTPException(status_code=422, detail={"kind": "validation_error", "field": exc.field, "message": str(exc)})
    if isinstance(exc, ColabBackendError):
        code = 400
        if exc.kind in ("auth_error", "missing_credential"):
            code = 401
        elif exc.kind == "not_entitled":
            code = 403
        elif exc.kind in ("runtime_lost", "session_missing"):
            code = 404
        elif exc.kind in ("capacity_412", "session_cap", "duplicate_name"):
            code = 409
        elif exc.kind in ("watchdog_failed", "kernel_unreachable", "kernel_dead", "exec_disconnected"):
            code = 502
        return HTTPException(status_code=code, detail={"kind": exc.kind, "message": exc.message})
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail={"kind": "not_found", "message": str(exc).strip("'")})
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail={"kind": "invalid_input", "message": str(exc)})
    return HTTPException(status_code=500, detail={"kind": "internal_error", "message": f"{type(exc).__name__}: {exc}"})


def create_app(
    orchestrator: Optional[ConsoleOrchestrator] = None,
    *,
    api_token: Optional[str] = None,
    guard_interval_s: float = 30.0,
) -> FastAPI:
    orch = orchestrator or ConsoleOrchestrator()
    ws_queues: dict[WebSocket, asyncio.Queue[Optional[dict[str, Any]]]] = {}
    loop_ref: list[Optional[asyncio.AbstractEventLoop]] = [None]

    def _enqueue_on_loop(event: dict[str, Any]) -> None:
        for q in list(ws_queues.values()):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    def on_orch_event(event: dict[str, Any]) -> None:
        ev_loop = loop_ref[0]
        if not ev_loop or not ws_queues:
            return
        try:
            ev_loop.call_soon_threadsafe(_enqueue_on_loop, event)
        except RuntimeError as exc:
            logger.debug("Skipping WebSocket event dispatch on closed loop: %s", exc)

    unsubscribe = orch.subscribe(on_orch_event)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        loop_ref[0] = asyncio.get_running_loop()
        stop_bg = asyncio.Event()
        poll_interval = max(0.01, float(guard_interval_s))

        async def guard_monitor_loop() -> None:
            while not stop_bg.is_set():
                try:
                    await asyncio.wait_for(stop_bg.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    try:
                        await asyncio.to_thread(orch.evaluate_cost_guards)
                    except Exception:
                        logger.exception("Cost guard background evaluation failed; continuing monitor loop")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Unexpected error in guard_monitor_loop; continuing")

        guard_task = asyncio.create_task(guard_monitor_loop())
        try:
            yield
        finally:
            stop_bg.set()
            guard_task.cancel()
            try:
                await guard_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error awaiting cancelled guard_monitor_loop")
            unsubscribe()
            await asyncio.to_thread(orch.shutdown)

    app = FastAPI(
        title="Colab Boost Console API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.orchestrator = orch

    @app.middleware("http")
    async def security_guard_middleware(request: Request, call_next):
        allowed_hosts = _get_allowed_hosts()
        host_header = request.headers.get("host")
        hostname = _extract_hostname_from_host(host_header)
        if not hostname or hostname not in allowed_hosts:
            return JSONResponse(
                status_code=403,
                content={"detail": {"kind": "forbidden_host", "message": f"Disallowed Host header: {host_header!r}"}},
            )

        path = request.url.path
        if path.startswith("/api/"):
            if request.method.upper() in _MUTATING_METHODS:
                sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
                if sec_fetch_site == "cross-site":
                    return JSONResponse(
                        status_code=403,
                        content={"detail": {"kind": "csrf_blocked", "message": "Cross-site mutating request rejected"}},
                    )
                origin = request.headers.get("origin")
                if origin is not None and not _is_allowed_origin(origin, allowed_hosts):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": {"kind": "csrf_blocked", "message": f"Disallowed Origin header: {origin!r}"}},
                    )

            if path != "/api/health":
                expected_token = _resolve_api_token(api_token)
                if expected_token:
                    provided = _extract_bearer_token(request.headers.get("authorization"))
                    if not provided or not hmac.compare_digest(provided, expected_token):
                        return JSONResponse(
                            status_code=401,
                            content={"detail": {"kind": "unauthorized", "message": "Missing or invalid API bearer token"}},
                            headers={"WWW-Authenticate": "Bearer"},
                        )

        return await call_next(request)

    web_dir = Path(__file__).resolve().parent / "web"
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        return FileResponse(str(web_dir / "index.html"))

    @app.get("/api/health")
    async def api_health() -> dict[str, Any]:
        return {
            "status": "ok",
            "backend_mode": orch.settings.backend_mode,
            "is_mock": orch.backend.is_mock,
        }

    @app.get("/api/bootstrap")
    async def api_bootstrap() -> dict[str, Any]:
        accounts = await asyncio.to_thread(orch.list_accounts, False)
        sessions = await asyncio.to_thread(orch.list_sessions, True)
        recipes = await asyncio.to_thread(orch.list_recipes)
        secrets = await asyncio.to_thread(orch.vault.secret_status)
        return {
            "settings": orch.get_settings(),
            "accounts": accounts,
            "active_account": orch.active_account(),
            "sessions": sessions,
            "provision_ops": orch.list_provision_ops(),
            "recipes": recipes,
            "runs": orch.list_runs(),
            "tunnels": orch.tunnels.list_tunnels(),
            "receipts": orch.list_receipts(),
            "secrets": secrets,
            "curated_gguf_repos": list(CURATED_GGUF_CATALOG.keys()),
            "accelerator_specs": _build_accelerator_specs(),
            "approx_burn_rates": dict(APPROX_CCU_PER_HOUR),
        }

    @app.patch("/api/settings")
    async def api_update_settings(patch: SettingsPatch) -> Any:
        try:
            return await asyncio.to_thread(orch.update_settings, patch)
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    # -------------------- Accounts --------------------

    @app.post("/api/accounts/login/begin")
    async def api_login_begin() -> dict[str, str]:
        try:
            return await asyncio.to_thread(orch.begin_account_login)
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/accounts/login/complete")
    async def api_login_complete(body: OAuthCompleteBody) -> Any:
        try:
            return await asyncio.to_thread(orch.complete_account_login, body.flow_id, body.code)
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/accounts/import-cli")
    async def api_import_cli() -> Any:
        try:
            return await asyncio.to_thread(orch.import_colab_cli_credential)
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/accounts/{account_id}/switch")
    async def api_switch_account(account_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(orch.switch_active_account, account_id)
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.delete("/api/accounts/{account_id}")
    async def api_delete_account(account_id: str) -> dict[str, Any]:
        ok = await asyncio.to_thread(orch.remove_account, account_id)
        if not ok:
            raise HTTPException(status_code=404, detail={"kind": "not_found", "message": "Account not found"})
        return {"removed": account_id}

    @app.post("/api/accounts/refresh")
    async def api_refresh_accounts() -> Any:
        return await asyncio.to_thread(orch.list_accounts, True)

    # -------------------- Sessions & Cost Guards --------------------

    @app.get("/api/sessions")
    async def api_list_sessions() -> Any:
        return await asyncio.to_thread(orch.list_sessions, True)

    @app.post("/api/sessions")
    async def api_create_session(req: ProvisionRequest, async_mode: bool = Query(default=False)) -> Any:
        try:
            if async_mode:
                return await asyncio.to_thread(orch.start_provision_async, req)
            return await asyncio.to_thread(orch.provision_session, req)
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/provision/{op_id}/cancel")
    async def api_cancel_provision(op_id: str) -> dict[str, Any]:
        ok = await asyncio.to_thread(orch.cancel_provision, op_id)
        if not ok:
            raise HTTPException(status_code=404, detail={"kind": "not_found", "message": "Provision operation not active"})
        return {"cancelled": op_id}

    @app.post("/api/sessions/stop-all")
    async def api_stop_all_sessions() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(orch.stop_all_sessions_detailed, "emergency_stop_all")
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/sessions/{identifier}/stop")
    async def api_stop_session(identifier: str, account_id: Optional[str] = Query(default=None)) -> Any:
        try:
            return await asyncio.to_thread(orch.stop_session, identifier, account_id=account_id, reason="manual_stop")
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/sessions/{session_name}/extend")
    async def api_extend_session(session_name: str, body: ExtendDeadlineBody) -> Any:
        try:
            return await asyncio.to_thread(orch.extend_session_deadline, session_name, body.add_minutes)
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/sessions/{identifier}/exec")
    async def api_exec_session(identifier: str, body: SessionExecBody) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                orch.exec_on_session,
                identifier,
                body.code,
                shell=body.shell,
                env=body.env,
                timeout_s=body.timeout_s,
            )
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/sessions/{identifier}/upload")
    async def api_upload_session(identifier: str, body: SessionUploadBody) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                orch.upload_to_session,
                identifier,
                body.remote_path,
                local_path=body.local_path,
                content_b64=body.content_b64,
            )
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/sessions/{identifier}/download")
    async def api_download_session(identifier: str, body: SessionDownloadBody) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                orch.download_from_session,
                identifier,
                body.remote_path,
                local_path=body.local_path,
            )
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/guards/evaluate")
    async def api_evaluate_guards() -> dict[str, Any]:
        receipts = await asyncio.to_thread(orch.evaluate_cost_guards)
        return {"triggered_count": len(receipts), "receipts": receipts}

    @app.get("/api/receipts")
    async def api_list_receipts() -> Any:
        return orch.list_receipts()

    # -------------------- Tunnels --------------------

    @app.get("/api/tunnels")
    async def api_list_tunnels() -> Any:
        return orch.tunnels.list_tunnels()

    @app.post("/api/tunnels")
    async def api_open_tunnel(body: OpenTunnelBody) -> Any:
        try:
            return await asyncio.to_thread(
                orch.open_tunnel,
                body.session_name,
                body.remote_port,
                body.local_port,
                body.label,
                body.path,
                body.kind,
            )
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.delete("/api/tunnels/{tunnel_id}")
    async def api_close_tunnel(tunnel_id: str) -> dict[str, Any]:
        ok = await asyncio.to_thread(orch.close_tunnel, tunnel_id)
        if not ok:
            raise HTTPException(status_code=404, detail={"kind": "not_found", "message": "Tunnel not found"})
        return {"closed": tunnel_id, "billing_notice": "Closing a port forward does NOT stop Colab VM billing."}

    # -------------------- Recipes & Deployments --------------------

    @app.get("/api/recipes")
    async def api_list_recipes() -> Any:
        return await asyncio.to_thread(orch.list_recipes)

    @app.get("/api/deployments")
    async def api_list_deployments() -> Any:
        return orch.list_runs()

    @app.post("/api/deployments")
    async def api_start_deployment(req: DeployRequest, sync: bool = Query(default=False)) -> Any:
        try:
            return await asyncio.to_thread(orch.start_deployment, req, run_sync=sync)
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.get("/api/deployments/{run_id}")
    async def api_get_deployment(run_id: str) -> Any:
        try:
            return orch.get_run(run_id)
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/deployments/{run_id}/cancel")
    async def api_cancel_deployment(run_id: str) -> Any:
        try:
            return await asyncio.to_thread(orch.cancel_deployment, run_id)
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.post("/api/deployments/{run_id}/keep/sync")
    async def api_sync_keep_artifacts(run_id: str) -> dict[str, Any]:
        try:
            items = await asyncio.to_thread(orch.sync_keep_artifacts, run_id)
            return {"run_id": run_id, "keep_paths": items}
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.get("/api/deployments/{run_id}/keep/download")
    async def api_download_keep_archive(run_id: str, index: int = Query(default=0, ge=0)) -> FileResponse:
        try:
            run = orch.get_run(run_id)
        except Exception as exc:
            raise _map_domain_error(exc) from exc
        if index >= len(run.keep_paths):
            raise HTTPException(status_code=404, detail={"kind": "not_found", "message": "Keep path index out of range"})
        kp = run.keep_paths[index]
        if not kp.local_archive:
            raise HTTPException(
                status_code=404,
                detail={"kind": "not_synced", "message": "Sync keep artifacts first before downloading archive"},
            )
        arc_file = Path(kp.local_archive).resolve()
        keep_root = orch.paths.keep_dir.resolve()
        if keep_root not in arc_file.parents or not arc_file.is_file():
            raise HTTPException(status_code=404, detail={"kind": "not_found", "message": "Local archive file not found"})
        return FileResponse(str(arc_file), media_type="application/gzip", filename=arc_file.name)

    # -------------------- Hugging Face GGUF Search & VRAM Planner --------------------

    @app.get("/api/hf/search")
    async def api_hf_search(q: str = Query(default="")) -> dict[str, Any]:
        hf_tok = await asyncio.to_thread(orch.vault.get_secret, "hf_token")
        try:
            items = await search_hf_gguf_repos(q, hf_token=hf_tok, limit=10)
            return {"source": "huggingface", "items": items}
        except Exception as exc:
            logger.warning(
                "Hugging Face GGUF search failed; falling back to curated catalog: %s",
                _safe_error_text(orch, exc, [hf_tok] if hf_tok else None),
            )
            # Graceful fallback to curated catalog if offline or rate-limited
            ql = q.strip().lower()
            fallback = [
                {"id": repo_id, "downloads": 125000, "likes": 420, "pipeline_tag": "text-generation"}
                for repo_id in CURATED_GGUF_CATALOG
                if not ql or ql in repo_id.lower()
            ]
            return {"source": "curated_fallback", "items": fallback}

    @app.get("/api/hf/gguf-plan")
    async def api_hf_gguf_plan(
        repo: str = Query(...),
        quant: str = Query(default="auto"),
        ctx: int = Query(default=0, ge=0, le=1048576),
        kv: str = Query(default="auto"),
    ) -> dict[str, Any]:
        try:
            repo_id = validate_hf_repo_id(repo)
        except ValueError as exc:
            raise _map_domain_error(exc) from exc

        hf_tok = await asyncio.to_thread(orch.vault.get_secret, "hf_token")
        plan: Optional[GgufRepoPlan] = None
        source = "huggingface"
        try:
            plan = await inspect_hf_gguf_repo(repo_id, hf_token=hf_tok)
        except Exception as exc:
            preset = CURATED_GGUF_CATALOG.get(repo_id)
            if preset:
                logger.warning(
                    "Hugging Face GGUF inspection failed for %s; using curated fallback: %s",
                    repo_id,
                    _safe_error_text(orch, exc, [hf_tok] if hf_tok else None),
                )
                source = "curated_fallback"
                from colab_boost.gguf_planner import group_gguf_files

                variants, mmproj = group_gguf_files(preset["files"])
                plan = GgufRepoPlan(
                    repo=repo_id,
                    variants=variants,
                    mmproj_files=mmproj,
                    preferred_mmproj=mmproj[0][0] if mmproj else None,
                    architecture=preset["arch"],
                    model_ctx=preset["ctx"],
                    kv_bytes_per_token_f16=preset["kv_bpt"],
                    has_mtp=False,
                )
            else:
                logger.warning(
                    "Hugging Face GGUF inspection failed for %s: %s",
                    repo_id,
                    _safe_error_text(orch, exc, [hf_tok] if hf_tok else None),
                )
                raise HTTPException(
                    status_code=502,
                    detail={"kind": "hf_unreachable", "message": f"Could not inspect Hugging Face repo '{repo_id}'."},
                ) from exc

        matrix = build_hardware_fit_matrix(plan, quant=quant, requested_ctx=ctx, kv_type=kv)
        warnings_list = [plan.header_warning] if plan.header_warning else []
        return {
            "source": source,
            "repo": plan.repo,
            "architecture": plan.architecture,
            "model_ctx": plan.model_ctx,
            "kv_bytes_per_token_f16": plan.kv_bytes_per_token_f16,
            "has_mtp": plan.has_mtp,
            "sliding_window": plan.sliding_window,
            "preferred_mmproj": plan.preferred_mmproj,
            "header_truncated": bool(plan.header_truncated),
            "header_warning": plan.header_warning,
            "warnings": warnings_list,
            "variants": [
                {"quant": v.quant, "size_gib": v.size_gib, "files": v.files}
                for v in plan.variants
            ],
            "hardware_matrix": matrix,
        }

    # -------------------- Vault Secrets --------------------

    @app.put("/api/secrets/{slot}")
    async def api_set_secret(slot: str, body: SecretUpdateBody) -> dict[str, Any]:
        try:
            await asyncio.to_thread(orch.vault.set_secret, slot, body.value)
            status = await asyncio.to_thread(orch.vault.secret_status)
            return {"secrets": status}
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    @app.delete("/api/secrets/{slot}")
    async def api_delete_secret(slot: str) -> dict[str, Any]:
        try:
            await asyncio.to_thread(orch.vault.delete_secret, slot)
            status = await asyncio.to_thread(orch.vault.secret_status)
            return {"secrets": status}
        except Exception as exc:
            raise _map_domain_error(exc) from exc

    # -------------------- Built-in OpenAI Playground Chat --------------------

    @app.post("/api/playground/chat")
    async def api_playground_chat(body: PlaygroundChatBody) -> dict[str, Any]:
        if not body.messages:
            raise HTTPException(status_code=400, detail={"kind": "invalid_input", "message": "messages cannot be empty"})

        target_url = (body.base_url or "").strip().rstrip("/")
        tun = None
        if body.tunnel_id:
            tun = next((t for t in orch.tunnels.list_tunnels() if t.id == body.tunnel_id), None)
            if not tun:
                raise HTTPException(status_code=404, detail={"kind": "not_found", "message": "Tunnel not found"})
            target_url = tun.local_url.rstrip("/")

        resolved_model = (body.model or "").strip()
        if resolved_model in ("", "default", "auto") and tun is not None:
            for run in reversed(orch.list_runs()):
                if run.session_name == tun.session_name and run.state == "ready" and run.outputs.get("model_id"):
                    resolved_model = str(run.outputs["model_id"])
                    break

        if orch.backend.is_mock:
            eff_model = resolved_model or "default"
            t0 = time.perf_counter()
            await asyncio.sleep(0.08)
            last_user = next((m.get("content", "") for m in reversed(body.messages) if m.get("role") == "user"), "")
            endpoint_disp = target_url or "http://127.0.0.1:8000/v1"
            if orch.settings.language == "en":
                reply = (
                    f"[Sandbox Demo Inference | Model `{eff_model}`]\n"
                    f"Received via local tunnel `{endpoint_disp}`: \"{last_user[:80]}\".\n"
                    "Switch to Real Colab Mode and sign in with Google to forward requests to a live llama.cpp / vLLM GPU instance."
                )
            else:
                reply = (
                    f"[演示沙盒推理回执 | 模型 `{eff_model}`]\n"
                    f"已通过本地映射端点 `{endpoint_disp}` 收到消息：\"{last_user[:80]}\"。\n"
                    "切换到「真实 Colab 模式」登录 Google 账号后，此处将直接转发至 Colab GPU 上的真实 llama.cpp / vLLM 实例。"
                )
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return {
                "reply": reply,
                "model": eff_model,
                "latency_ms": elapsed_ms,
                "tok_per_s": 78.4,
                "is_mock": True,
            }

        if not target_url:
            raise HTTPException(status_code=400, detail={"kind": "invalid_input", "message": "Select an active OpenAI tunnel or enter base_url"})

        parsed_target = urlparse(target_url)
        if parsed_target.scheme not in ("http", "https") or not parsed_target.hostname:
            raise HTTPException(status_code=400, detail={"kind": "invalid_input", "message": "Invalid base_url (must be http:// or https://)"})
        if parsed_target.username or parsed_target.password:
            raise HTTPException(status_code=400, detail={"kind": "invalid_input", "message": "Credentials in base_url are not allowed"})

        if tun is None:
            allow_custom = os.environ.get("COLAB_BOOST_ALLOW_CUSTOM_PLAYGROUND_URL", "").strip().lower() in ("1", "true", "yes")
            if not allow_custom:
                target_host = parsed_target.hostname.lower()
                target_port = parsed_target.port or (443 if parsed_target.scheme == "https" else 80)
                registered_ports = {
                    t.local_port for t in orch.tunnels.list_tunnels() if t.status == "open" and t.local_port
                }
                if target_host not in _LOOPBACK_HOSTS or target_port not in registered_ports:
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "kind": "ssrf_blocked",
                            "message": (
                                "Playground base_url must target an active registered local tunnel port "
                                "(or set COLAB_BOOST_ALLOW_CUSTOM_PLAYGROUND_URL=1)."
                            ),
                        },
                    )

        if not target_url.endswith("/v1") and "/v1/" not in target_url:
            target_url = target_url + "/v1"

        headers = {"Content-Type": "application/json"}
        if body.api_key:
            headers["Authorization"] = f"Bearer {body.api_key}"

        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=180.0, follow_redirects=False) as client:
                if resolved_model in ("", "default", "auto"):
                    try:
                        m_resp = await client.get(f"{target_url}/models", headers=headers, timeout=10.0)
                        if m_resp.status_code == 200:
                            m_items = (m_resp.json() or {}).get("data") or []
                            if isinstance(m_items, list) and m_items and isinstance(m_items[0], dict) and m_items[0].get("id"):
                                resolved_model = str(m_items[0]["id"])
                    except Exception as disc_exc:
                        logger.debug(
                            "Playground /models auto-discovery failed for %s: %s",
                            target_url,
                            _safe_error_text(orch, disc_exc, [body.api_key] if body.api_key else None),
                        )

                eff_model = resolved_model or "default"
                resp = await client.post(
                    f"{target_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": eff_model,
                        "messages": body.messages,
                        "max_tokens": body.max_tokens,
                        "temperature": body.temperature,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            safe_msg = _safe_error_text(orch, exc, [body.api_key] if body.api_key else None)
            logger.warning("Playground chat request to %s failed: %s", target_url, safe_msg)
            raise HTTPException(
                status_code=502,
                detail={"kind": "upstream_error", "message": f"Model endpoint request failed: {safe_msg}"},
            ) from exc

        elapsed_s = max(0.001, time.perf_counter() - t0)
        choice = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        comp_tokens = (data.get("usage") or {}).get("completion_tokens") or 0
        tps = round(float((data.get("timings") or {}).get("predicted_per_second") or (comp_tokens / elapsed_s if comp_tokens else 0.0)), 1)
        return {
            "reply": choice,
            "model": data.get("model") or eff_model,
            "latency_ms": round(elapsed_s * 1000, 1),
            "tok_per_s": tps,
            "is_mock": False,
        }

    # -------------------- Real-Time WebSocket Stream --------------------

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        allowed_hosts = _get_allowed_hosts()
        hostname = _extract_hostname_from_host(ws.headers.get("host"))
        if not hostname or hostname not in allowed_hosts:
            await ws.close(code=1008)
            return

        sec_fetch_site = (ws.headers.get("sec-fetch-site") or "").strip().lower()
        if sec_fetch_site == "cross-site":
            await ws.close(code=1008)
            return

        origin = ws.headers.get("origin")
        if origin is not None and not _is_allowed_origin(origin, allowed_hosts):
            await ws.close(code=1008)
            return

        expected_token = _resolve_api_token(api_token)
        if expected_token:
            provided = _extract_bearer_token(ws.headers.get("authorization")) or (ws.query_params.get("token") or "").strip() or None
            if not provided or not hmac.compare_digest(provided, expected_token):
                await ws.close(code=1008)
                return

        await ws.accept()
        q: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue(maxsize=_WS_QUEUE_MAX)
        try:
            await ws.send_json({"type": "connected", "data": {"backend_mode": orch.settings.backend_mode}})
        except Exception as exc:
            logger.debug("WebSocket initial handshake send failed: %s", type(exc).__name__)
            return

        ws_queues[ws] = q

        async def _sender() -> None:
            while True:
                item = await q.get()
                if item is None:
                    return
                await ws.send_json(item)

        sender_task = asyncio.create_task(_sender())
        try:
            while True:
                receive_task = asyncio.create_task(ws.receive_text())
                done, _ = await asyncio.wait(
                    {receive_task, sender_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receive_task in done:
                    receive_task.result()
                if sender_task in done:
                    receive_task.cancel()
                    try:
                        await receive_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        logger.debug("WebSocket receive task cleanup error: %s", type(exc).__name__)
                    break
        except WebSocketDisconnect:
            pass
        finally:
            ws_queues.pop(ws, None)
            sender_task.cancel()
            try:
                await sender_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("WebSocket sender task cleanup error: %s", type(exc).__name__)

    return app
