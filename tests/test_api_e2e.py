"""End-to-end FastAPI REST & WebSocket integration tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from colab_boost.orchestrator import ConsoleOrchestrator
from colab_boost.server import create_app


def test_full_console_e2e_lifecycle(orchestrator: ConsoleOrchestrator) -> None:
    app = create_app(orchestrator)
    with TestClient(app) as client:
        # 1. Serve index HTML & bootstrap
        r_idx = client.get("/")
        assert r_idx.status_code == 200
        assert "Colab Boost" in r_idx.text

        boot = client.get("/api/bootstrap").json()
        assert boot["settings"]["backend_mode"] == "mock"
        assert len(boot["accounts"]) == 2
        assert len(boot["recipes"]) == 5
        # Demo scenario includes 1 managed A100 + 1 untracked browser orphan T4
        assert len(boot["sessions"]) == 2
        assert any(s["managed"] is False for s in boot["sessions"])

        # 2. WebSocket live event stream connects cleanly
        with client.websocket_connect("/ws/events") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "connected"

        # 3. GGUF VRAM Plan endpoint (curated fallback or live)
        plan = client.get(
            "/api/hf/gguf-plan",
            params={"repo": "unsloth/Qwen3-32B-GGUF", "quant": "auto"},
        )
        assert plan.status_code == 200
        pdata = plan.json()
        assert len(pdata["hardware_matrix"]) == 4

        # 4. Deploy GGUF recipe synchronously -> auto-opens tunnel -> test Playground Chat
        dep = client.post(
            "/api/deployments?sync=true",
            json={
                "recipe_id": "llama-cpp-gguf",
                "inputs": {
                    "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                    "quant": "auto",
                    "port": 8005,
                },
                "provision": {
                    "accelerator": "L4",
                    "min_vram_mib": 20000,
                },
            },
        )
        assert dep.status_code == 200
        run_data = dep.json()
        assert run_data["state"] == "ready"
        assert len(run_data["endpoints"]) == 1
        tun_id = run_data["endpoints"][0]["id"]

        # Port 8080 tunnel must be rejected with 400
        bad_tun = client.post(
            "/api/tunnels",
            json={"session_name": run_data["session_name"], "remote_port": 8080},
        )
        assert bad_tun.status_code == 400

        # Test Playground Chat through the deployed tunnel
        chat = client.post(
            "/api/playground/chat",
            json={
                "tunnel_id": tun_id,
                "model": run_data["outputs"]["model_id"],
                "messages": [{"role": "user", "content": "Hello from E2E test!"}],
            },
        )
        assert chat.status_code == 200
        assert "Hello from E2E test!" in chat.json()["reply"]

        # 5. OAuth mock login & account switch
        login_start = client.post("/api/accounts/login/begin").json()
        new_acc = client.post(
            "/api/accounts/login/complete",
            json={"flow_id": login_start["flow_id"], "code": "new.member@studio.ai"},
        )
        assert new_acc.status_code == 200
        assert new_acc.json()["email"] == "new.member@studio.ai"

        # 6. Emergency Stop-All releases EVERY runtime across ALL accounts & verifies zero burn
        stop_all = client.post("/api/sessions/stop-all")
        assert stop_all.status_code == 200
        sa_body = stop_all.json()
        assert sa_body["zero_burn_verified"] is True
        assert sa_body["remaining_sessions"] == 0
        assert sa_body["stopped_count"] >= 3
        assert len(client.get("/api/receipts").json()) >= 3


def test_edge_cases_corrupt_settings_duplicate_names_and_cli_flags(isolated_paths) -> None:
    from colab_boost.cli import main as cli_main
    from colab_boost.config import SettingsStore

    # 1. Corrupt settings.json is moved aside to .corrupt and defaults load cleanly
    isolated_paths.settings_file.write_text("{broken json!!!", encoding="utf-8")
    store = SettingsStore(isolated_paths)
    recovered = store.load()
    assert recovered.backend_mode.value == "mock"
    assert isolated_paths.settings_file.with_suffix(".json.corrupt").exists()

    # 2. Conflicting CLI flags (--real + --mock-scenario) exit with code 2
    assert cli_main(["--real", "--mock-scenario", "demo"]) == 2

    # 3. Duplicate session name & max_managed_sessions cap return 409, empty chat returns 400
    orch = ConsoleOrchestrator(paths=isolated_paths, force_file_vault=True)
    orch.stop_all_sessions()
    orch.update_settings({"guards": {"max_managed_sessions": 2}})
    app = create_app(orch)
    with TestClient(app) as client:
        s1 = client.post("/api/sessions", json={"name": "dup-test", "accelerator": "T4"})
        assert s1.status_code == 200

        # Duplicate name -> 409
        dup = client.post("/api/sessions", json={"name": "dup-test", "accelerator": "T4"})
        assert dup.status_code == 409

        s2 = client.post("/api/sessions", json={"name": "second-sess", "accelerator": "T4"})
        assert s2.status_code == 200

        # Exceeding max_managed_sessions=2 -> 409
        s3 = client.post("/api/sessions", json={"name": "third-sess", "accelerator": "T4"})
        assert s3.status_code == 409

        # Empty chat messages -> 400
        bad_chat = client.post("/api/playground/chat", json={"messages": []})
        assert bad_chat.status_code == 400

        client.post("/api/sessions/stop-all")


def test_english_localization_and_keep_artifact_archive_e2e(orchestrator: ConsoleOrchestrator) -> None:
    """Verifies zero leaked Chinese strings in English mode across backend APIs, ComfyUI [[keep]] sync/download, and SPA views/modals."""
    import io
    import json
    import re
    import shutil
    import subprocess
    import tarfile

    from colab_boost.config import project_root

    cjk_re = re.compile(r"[\u4e00-\u9fff]")
    app = create_app(orchestrator)

    with TestClient(app) as client:
        # 1. Switch console to English ("en")
        s_resp = client.patch("/api/settings", json={"language": "en"})
        assert s_resp.status_code == 200
        assert s_resp.json()["language"] == "en"

        # Untracked runtime warning must be in English
        sessions = client.get("/api/sessions").json()
        orphans = [s for s in sessions if not s["managed"]]
        assert orphans and not cjk_re.search(orphans[0]["watchdog"]["warning"] or "")

        # 2. Deploy ComfyUI (which declares [[keep]] /content/ComfyUI/output) in English mode
        dep = client.post(
            "/api/deployments?sync=true",
            json={
                "recipe_id": "comfyui-studio",
                "inputs": {"port": 8189},
                "provision": {"accelerator": "L4", "min_vram_mib": 20000},
            },
        )
        assert dep.status_code == 200
        run_data = dep.json()
        assert run_data["state"] == "ready"
        assert len(run_data["endpoints"]) == 1
        assert run_data["endpoints"][0]["label"] == "ComfyUI Visual Canvas"
        assert not cjk_re.search(run_data["endpoints"][0]["label"])
        assert len(run_data["keep_paths"]) == 1
        assert run_data["keep_paths"][0]["path"] == "/content/ComfyUI/output"

        # Downloading before sync returns 404 not_synced
        pre_dl = client.get(f"/api/deployments/{run_data['run_id']}/keep/download?index=0")
        assert pre_dl.status_code == 404

        # Sync [[keep]] artifacts and download valid .tar.gz archive
        sync_resp = client.post(f"/api/deployments/{run_data['run_id']}/keep/sync")
        assert sync_resp.status_code == 200
        synced_kp = sync_resp.json()["keep_paths"][0]
        assert synced_kp["local_archive"]

        dl_resp = client.get(f"/api/deployments/{run_data['run_id']}/keep/download?index=0")
        assert dl_resp.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(dl_resp.content), mode="r:gz") as tf:
            names = tf.getnames()
            assert any("keep_manifest.json" in n for n in names)

        # 3. Mock Playground Chat reply in English mode has zero Chinese characters
        chat = client.post(
            "/api/playground/chat",
            json={
                "tunnel_id": run_data["endpoints"][0]["id"],
                "messages": [{"role": "user", "content": "Check English reply"}],
            },
        )
        assert chat.status_code == 200
        reply_text = chat.json()["reply"]
        assert "[Sandbox Demo Inference" in reply_text
        assert not cjk_re.search(reply_text)

        # 4. Execute real frontend SPA (app.js) in Node VM across all 6 routes + all 4 modals in English mode
        node_bin = shutil.which("node")
        if node_bin:
            boot_payload = client.get("/api/bootstrap").json()
            flow_payload = client.post("/api/accounts/login/begin").json()
            app_js_path = project_root() / "src" / "colab_boost" / "web" / "app.js"
            harness = r"""
const fs = require('fs');
const vm = require('vm');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));

function makeEl(id = '', attrs = {}) {
  const listeners = {};
  return {
    id,
    tagName: 'DIV',
    className: '',
    textContent: '',
    innerHTML: '',
    value: '',
    title: '',
    style: {},
    dataset: { ...(attrs.dataset || {}) },
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(k, v) { attrs[k] = v; },
    getAttribute(k) { return attrs[k] || null; },
    appendChild(ch) { this.lastChild = ch; },
    remove() {},
    contains() { return false; },
    focus() {},
    closest() { return null; },
    addEventListener(ev, fn) { listeners[ev] = fn; },
    click() { if (listeners.click) return listeners.click({ target: this, preventDefault() {} }); },
    _listeners: listeners,
  };
}

const els = {};
const getEl = (id) => (els[id] = els[id] || makeEl(id));
[
  'modal-backdrop', 'modal-body', 'toast-stack', 'btn-toggle-mode',
  'hud-account', 'hud-balance', 'hud-burn', 'btn-stop-all', 'btn-lang', 'btn-theme',
  'badge-runs', 'badge-sessions', 'badge-tunnels', 'badge-receipts',
  'ws-indicator', 'main-view', 'sidebar-nav'
].forEach(getEl);

const i18nNodes = [
  'brand_title', 'nav_overview', 'nav_deploy', 'nav_sessions',
  'nav_tunnels', 'nav_accounts', 'nav_settings', 'footer_native', 'footer_receipts'
].map((k) => makeEl('', { 'data-i18n': k }));

const navBtns = ['overview', 'deploy', 'sessions', 'tunnels', 'accounts', 'settings']
  .map((r) => makeEl('', { dataset: { route: r } }));

const doc = {
  title: '',
  lang: 'zh-CN',
  activeElement: null,
  documentElement: { lang: 'zh-CN', setAttribute() {} },
  getElementById: (id) => getEl(id),
  createElement: () => makeEl(),
  querySelectorAll: (sel) => sel === '[data-i18n]' ? i18nNodes : (sel === '.nav-btn' ? navBtns : []),
};

class FakeWS {
  constructor() { setTimeout(() => { if (this.onopen) this.onopen(); }, 1); }
}

const windowObj = {};
const ctx = vm.createContext({
  window: windowObj,
  document: doc,
  location: { protocol: 'http:', host: '127.0.0.1:8765' },
  CSS: { escape: (s) => s },
  URLSearchParams,
  WebSocket: FakeWS,
  navigator: { clipboard: { writeText() {} } },
  setTimeout,
  clearTimeout,
  fetch: async (url) => {
    if (url.startsWith('/api/bootstrap')) return { ok: true, status: 200, json: async () => payload.boot };
    if (url.startsWith('/api/accounts/login/begin')) return { ok: true, status: 200, json: async () => payload.flow };
    return { ok: true, status: 200, json: async () => ({}) };
  },
});

vm.runInContext(fs.readFileSync(payload.appJs, 'utf8'), ctx);

(async () => {
  await new Promise((r) => setTimeout(r, 30));
  const snapshots = [];
  for (const r of ['overview', 'deploy', 'sessions', 'tunnels', 'accounts', 'settings']) {
    windowObj._app.go(r);
    snapshots.push({ scope: `route:${r}`, text: getEl('main-view').innerHTML });
  }
  // Also render llama-cpp-gguf on deploy route
  windowObj._app.pickRecipe('llama-cpp-gguf');
  snapshots.push({ scope: 'route:deploy:gguf', text: getEl('main-view').innerHTML });

  windowObj._app.openNewSessionModal();
  snapshots.push({ scope: 'modal:new_session', text: getEl('modal-body').innerHTML });

  windowObj._app.openForwardModal('boost-a100-qwen');
  snapshots.push({ scope: 'modal:forward', text: getEl('modal-body').innerHTML });

  await windowObj._app.openLoginModal();
  snapshots.push({ scope: 'modal:login_mock', text: getEl('modal-body').innerHTML });

  await getEl('btn-toggle-mode').click();
  snapshots.push({ scope: 'modal:toggle_mode', text: getEl('modal-body').innerHTML });

  snapshots.push({
    scope: 'chrome',
    text: [
      doc.title,
      getEl('btn-toggle-mode').textContent,
      getEl('btn-toggle-mode').title,
      getEl('hud-account').innerHTML,
      getEl('hud-balance').innerHTML,
      getEl('hud-burn').innerHTML,
      getEl('btn-stop-all').textContent,
      getEl('ws-indicator').textContent,
      ...i18nNodes.map((n) => n.textContent),
    ].join(' | '),
  });
  process.stdout.write(JSON.stringify(snapshots));
})();
"""
            proc = subprocess.run(
                [node_bin, "-e", harness],
                input=json.dumps({"boot": boot_payload, "flow": flow_payload, "appJs": str(app_js_path)}),
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            rendered_snaps = json.loads(proc.stdout)
            for item in rendered_snaps:
                leaked = cjk_re.findall(item["text"])
                assert not leaked, f"Leaked CJK characters in {item['scope']} under language='en': {leaked}"

        # 5. Dynamic zh-CN -> en switch AFTER tunnel/deployment creation relocalizes backend labels & open modals
        client.patch("/api/settings", json={"language": "zh-CN"})
        dep_zh = client.post(
            "/api/deployments?sync=true",
            json={
                "recipe_id": "llama-cpp-gguf",
                "inputs": {"repo": "Qwen/Qwen2.5-7B-Instruct-GGUF", "port": 8012},
                "provision": {"accelerator": "T4"},
            },
        ).json()
        assert cjk_re.search(dep_zh["endpoints"][0]["label"])

        # Switch back to English while tunnel & run already exist
        client.patch("/api/settings", json={"language": "en"})
        for api_path in ("/api/tunnels", "/api/deployments", "/api/sessions"):
            body_obj = client.get(api_path).json()
            if api_path == "/api/tunnels":
                labels_now = [t["label"] for t in body_obj]
            elif api_path == "/api/deployments":
                labels_now = [ep["label"] for r in body_obj for ep in r.get("endpoints", [])]
            else:
                labels_now = [ep["label"] for s in body_obj for ep in s.get("tunnels", [])]
            assert labels_now
            for lbl in labels_now:
                assert not cjk_re.search(lbl), f"Stale CJK tunnel label in {api_path}: {lbl}"

        if node_bin:
            # Verify toggling language while Login modal is open re-renders modal in English & preserves typed input
            boot_zh = {**client.get("/api/bootstrap").json()}
            boot_zh["settings"] = {**boot_zh["settings"], "language": "zh-CN"}
            boot_en = client.get("/api/bootstrap").json()
            modal_toggle_js = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let curBoot = input.bootZh;
const els = {};
function makeEl(id = '', attrs = {}) {
  const listeners = {};
  return {
    id, tagName: 'DIV', className: '', textContent: '', innerHTML: '', value: '', title: '', style: {},
    dataset: { ...(attrs.dataset || {}) },
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(k, v) { attrs[k] = v; }, getAttribute(k) { return attrs[k] || null; },
    appendChild() {}, remove() {}, contains() { return false; }, focus() {}, closest() { return null; },
    addEventListener(ev, fn) { listeners[ev] = fn; },
    click() { if (listeners.click) return listeners.click({ target: this, preventDefault() {} }); },
  };
}
const getEl = (id) => (els[id] = els[id] || makeEl(id));
['modal-backdrop','modal-body','toast-stack','btn-toggle-mode','hud-account','hud-balance','hud-burn','btn-stop-all','btn-lang','btn-theme','badge-runs','badge-sessions','badge-tunnels','badge-receipts','ws-indicator','main-view','sidebar-nav','oauth-code'].forEach(getEl);
const doc = {
  title: '', lang: 'zh-CN', activeElement: null,
  documentElement: { lang: 'zh-CN', setAttribute() {} },
  getElementById: getEl, createElement: () => makeEl(), querySelectorAll: () => [],
};
const windowObj = {};
const ctx = vm.createContext({
  window: windowObj, document: doc, location: { protocol: 'http:', host: '127.0.0.1:8765' },
  CSS: { escape: (s) => s }, URLSearchParams, WebSocket: class { constructor() {} },
  navigator: { clipboard: { writeText: async () => {} } }, setTimeout, clearTimeout,
  fetch: async (url) => {
    if (url.startsWith('/api/bootstrap')) return { ok: true, status: 200, json: async () => curBoot };
    if (url.startsWith('/api/accounts/login/begin')) return { ok: true, status: 200, json: async () => input.flow };
    if (url.startsWith('/api/settings')) { curBoot = input.bootEn; return { ok: true, status: 200, json: async () => curBoot.settings }; }
    return { ok: true, status: 200, json: async () => ({}) };
  },
});
vm.runInContext(fs.readFileSync(input.appJs, 'utf8'), ctx);
(async () => {
  await new Promise((r) => setTimeout(r, 20));
  await windowObj._app.openLoginModal();
  getEl('oauth-code').value = 'keep.typed@studio.ai';
  const beforeHtml = getEl('modal-body').innerHTML;
  await getEl('btn-lang').click();
  const afterHtml = getEl('modal-body').innerHTML;
  process.stdout.write(JSON.stringify({ beforeHtml, afterHtml, preserved: getEl('oauth-code').value }));
})();
"""
            mproc = subprocess.run(
                [node_bin, "-e", modal_toggle_js],
                input=json.dumps({"bootZh": boot_zh, "bootEn": boot_en, "flow": flow_payload, "appJs": str(app_js_path)}),
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            mres = json.loads(mproc.stdout)
            assert cjk_re.search(mres["beforeHtml"])
            assert not cjk_re.search(mres["afterHtml"])
            assert mres["preserved"] == "keep.typed@studio.ai"

        client.post("/api/sessions/stop-all")


def test_guard_monitor_loop_survives_exception_and_clean_shutdown(isolated_paths) -> None:
    import time

    orch = ConsoleOrchestrator(paths=isolated_paths, force_file_vault=True)
    ticks = [0]

    def flaky_guards():
        ticks[0] += 1
        if ticks[0] == 1:
            raise RuntimeError("Simulated transient guard error")
        return []

    orch.evaluate_cost_guards = flaky_guards  # type: ignore[method-assign]
    app = create_app(orch, guard_interval_s=0.05)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and ticks[0] < 2:
            time.sleep(0.03)
    assert ticks[0] >= 2


def test_http_and_ws_security_host_origin_csrf_and_bearer_token(orchestrator: ConsoleOrchestrator, monkeypatch) -> None:
    import pytest
    from starlette.websockets import WebSocketDisconnect

    token = "secret-console-token-9988"
    app = create_app(orchestrator, api_token=token)
    with TestClient(app) as client:
        # 1. Disallowed Host header (DNS rebinding) -> 403
        bad_host = client.get("/api/health", headers={"Host": "evil.attacker.example:8765"})
        assert bad_host.status_code == 403
        assert bad_host.json()["detail"]["kind"] == "forbidden_host"

        # Allowed via COLAB_BOOST_ALLOWED_HOSTS
        monkeypatch.setenv("COLAB_BOOST_ALLOWED_HOSTS", "my-dev-box.local")
        ok_custom_host = client.get("/api/health", headers={"Host": "my-dev-box.local:8765"})
        assert ok_custom_host.status_code == 200
        monkeypatch.delenv("COLAB_BOOST_ALLOWED_HOSTS", raising=False)

        # 2. /api/health stays accessible without token, while other /api/* require Bearer token
        assert client.get("/api/health").status_code == 200
        unauth = client.get("/api/bootstrap")
        assert unauth.status_code == 401
        wrong_tok = client.get("/api/bootstrap", headers={"Authorization": "Bearer wrong-token"})
        assert wrong_tok.status_code == 401
        ok_auth = client.get("/api/bootstrap", headers={"Authorization": f"Bearer {token}"})
        assert ok_auth.status_code == 200

        # 3. Cross-site CSRF & bad Origin blocked on mutating /api/* even with valid token
        bad_origin = client.patch(
            "/api/settings",
            json={"theme": "light"},
            headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
        )
        assert bad_origin.status_code == 403
        assert bad_origin.json()["detail"]["kind"] == "csrf_blocked"

        bad_sfs = client.patch(
            "/api/settings",
            json={"theme": "light"},
            headers={"Authorization": f"Bearer {token}", "Sec-Fetch-Site": "cross-site"},
        )
        assert bad_sfs.status_code == 403

        ok_same_origin = client.patch(
            "/api/settings",
            json={"theme": "light"},
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "http://127.0.0.1:8765",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        assert ok_same_origin.status_code == 200

        # 4. WebSocket rejects bad Host, bad Origin, cross-site Sec-Fetch-Site, and missing/wrong token
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/events"):
                pass

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/events",
                headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
            ):
                pass

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/events",
                headers={"Authorization": f"Bearer {token}", "Host": "evil.example"},
            ):
                pass

        # Valid WS with Bearer header or query token connects cleanly
        with client.websocket_connect("/ws/events", headers={"Authorization": f"Bearer {token}"}) as ws:
            assert ws.receive_json()["type"] == "connected"
        with client.websocket_connect(f"/ws/events?token={token}") as ws:
            assert ws.receive_json()["type"] == "connected"


def test_playground_chat_real_mode_ssrf_protection(isolated_paths, monkeypatch) -> None:
    from colab_boost.backends.mock import MockColabBackend

    class RealStub(MockColabBackend):
        @property
        def is_mock(self) -> bool:
            return False

    orch = ConsoleOrchestrator(paths=isolated_paths, direct_backend=RealStub("signed_out"), force_file_vault=True)
    orch.update_settings({"backend_mode": "real"})
    app = create_app(orch)
    with TestClient(app) as client:
        # External / metadata IP blocked with 403
        r_meta = client.post(
            "/api/playground/chat",
            json={"base_url": "http://169.254.169.254/latest", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r_meta.status_code == 403
        assert r_meta.json()["detail"]["kind"] == "ssrf_blocked"

        # Unregistered loopback port blocked with 403
        r_unreg = client.post(
            "/api/playground/chat",
            json={"base_url": "http://127.0.0.1:59999", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r_unreg.status_code == 403
        assert r_unreg.json()["detail"]["kind"] == "ssrf_blocked"

        # Non-HTTP scheme blocked with 400
        r_file = client.post(
            "/api/playground/chat",
            json={"base_url": "file:///etc/passwd", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r_file.status_code == 400

        # Explicit env opt-in bypasses the SSRF allowlist (reaches upstream connection attempt -> 502)
        monkeypatch.setenv("COLAB_BOOST_ALLOW_CUSTOM_PLAYGROUND_URL", "1")
        r_custom = client.post(
            "/api/playground/chat",
            json={"base_url": "http://127.0.0.1:59999", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r_custom.status_code == 502


def test_frontend_zero_inline_handlers_and_neutral_ws_default() -> None:
    """Ensures index.html and app.js have zero inline on*= handlers and neutral initial WS dot."""
    import re

    from colab_boost.config import project_root

    web_dir = project_root() / "src" / "colab_boost" / "web"
    html_src = (web_dir / "index.html").read_text(encoding="utf-8")
    js_src = (web_dir / "app.js").read_text(encoding="utf-8")

    inline_re = re.compile(r"""\bon(?:click|input|change|submit|keydown|keyup|focus|blur|load|error)\s*=\s*["']""", re.I)
    assert not inline_re.search(html_src), "Found inline on*= handler in index.html"
    assert not inline_re.search(js_src), "Found inline on*= handler in app.js"

    # Sidebar WS indicator must not default to green connected dot
    assert 'id="ws-dot"' in html_src
    assert 'class="dot dot-muted" id="ws-dot"' in html_src
    assert "实时状态流已连接" not in html_src


def test_frontend_phase2_state_ime_auth_alerts_and_contract_regressions(orchestrator: ConsoleOrchestrator) -> None:
    """Comprehensive Node VM behavioral regressions for Phase 2 frontend fixes."""
    import json
    import re
    import shutil
    import subprocess

    import pytest

    from colab_boost.config import project_root

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node binary not available")

    app = create_app(orchestrator)
    with TestClient(app) as client:
        boot_payload = client.get("/api/bootstrap").json()

    app_js_path = project_root() / "src" / "colab_boost" / "web" / "app.js"

    # --- Suite A: Command palette IME stability + keyboard nav, 401 token modal + URL token + Bearer/WS,
    # timed_out_provisions banner, Playground decoupled history & real-mode empty tunnel,
    # hours_remaining === 0, L4 highMem & H100/V6E1 & accelerator_specs/approx_burn_rates,
    # warm VM filtering, secret DOM hygiene, dirty settings draft, modal clear, scrollTop, reqId races, WS backoff ---
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

function createHarness(opts = {}) {
  const els = {};
  const docListeners = {};
  const storage = {};
  const replacedUrls = [];
  const wsCalls = [];
  const fetchCalls = [];
  const scheduledTimeouts = [];
  let timerSeq = 1;

  function makeEl(id = '', attrs = {}) {
    const listeners = {};
    const classes = new Set();
    const el = {
      id,
      tagName: 'DIV',
      className: '',
      textContent: '',
      _innerHTML: '',
      value: '',
      placeholder: '',
      title: '',
      disabled: false,
      scrollTop: 0,
      scrollHeight: 500,
      clientHeight: 200,
      style: {},
      dataset: { ...(attrs.dataset || {}) },
      classList: {
        add(...cs) { cs.forEach((c) => classes.add(c)); },
        remove(...cs) { cs.forEach((c) => classes.delete(c)); },
        toggle(c, force) {
          if (force === undefined) {
            if (classes.has(c)) classes.delete(c); else classes.add(c);
          } else if (force) classes.add(c);
          else classes.delete(c);
        },
        contains(c) { return classes.has(c); },
      },
      setAttribute(k, v) { attrs[k] = v; },
      getAttribute(k) { return k in attrs ? attrs[k] : null; },
      appendChild(ch) { this.lastChild = ch; },
      remove() {},
      contains(target) { return target && !!els[target.id]; },
      focus() { doc.activeElement = this; },
      closest(sel) {
        if (sel === '[data-action]' && this.dataset && this.dataset.action) return this;
        if (sel === 'form[data-submit]' && this.dataset && this.dataset.submit) return this;
        return null;
      },
      querySelector(sel) { return (opts.querySelector && opts.querySelector(sel, this)) || null; },
      querySelectorAll(sel) { return (opts.querySelectorAll && opts.querySelectorAll(sel, this)) || []; },
      addEventListener(ev, fn) {
        listeners[ev] = listeners[ev] || [];
        listeners[ev].push(fn);
      },
      dispatch(ev, eventObj = {}) {
        const list = listeners[ev] || [];
        let out;
        for (const fn of list) {
          out = fn({ target: this, currentTarget: this, preventDefault() {}, ...eventObj });
        }
        return out;
      },
      click() { return this.dispatch('click'); },
      _listeners: listeners,
    };
    Object.defineProperty(el, 'innerHTML', {
      get() { return this._innerHTML; },
      set(v) {
        this._innerHTML = String(v);
        if (opts.onInnerHTML) opts.onInnerHTML(this, this._innerHTML);
      },
    });
    return el;
  }

  const getEl = (id) => (els[id] = els[id] || makeEl(id));
  [
    'modal-backdrop', 'modal-body', 'toast-stack', 'btn-toggle-mode', 'btn-cmd-palette',
    'hud-account', 'hud-balance', 'hud-burn', 'btn-stop-all', 'stop-all-group',
    'btn-lang', 'btn-theme', 'badge-runs', 'badge-sessions', 'badge-tunnels',
    'badge-receipts', 'ws-indicator', 'ws-dot', 'main-view', 'sidebar-nav',
    'global-alert-banner', 'crumb-current',
  ].forEach(getEl);

  const doc = {
    title: '',
    lang: 'zh-CN',
    activeElement: null,
    documentElement: { lang: 'zh-CN', setAttribute() {} },
    getElementById: (id) => (opts.strictGetEl ? (els[id] || null) : getEl(id)),
    createElement: () => makeEl(),
    querySelectorAll: () => [],
    addEventListener(ev, fn) {
      docListeners[ev] = docListeners[ev] || [];
      docListeners[ev].push(fn);
    },
    dispatch(ev, eventObj = {}) {
      let out;
      for (const fn of (docListeners[ev] || [])) {
        out = fn({ preventDefault() {}, ...eventObj });
      }
      return out;
    },
  };

  class FakeWS {
    constructor(url) {
      this.url = url;
      this.closed = false;
      wsCalls.push(this);
      if (opts.autoOpenWs !== false) {
        setTimeout(() => { if (this.onopen) this.onopen(); }, 1);
      }
    }
    close() { this.closed = true; }
  }

  const fakeSetTimeout = opts.captureTimeouts
    ? (fn, delay) => {
        const id = timerSeq++;
        scheduledTimeouts.push({ id, fn, delay, cleared: false });
        return id;
      }
    : setTimeout;
  const fakeClearTimeout = opts.captureTimeouts
    ? (id) => {
        const item = scheduledTimeouts.find((t) => t.id === id);
        if (item) item.cleared = true;
      }
    : clearTimeout;

  const windowObj = {};
  const ctx = vm.createContext({
    window: windowObj,
    document: doc,
    location: {
      protocol: 'http:',
      host: '127.0.0.1:8765',
      pathname: '/',
      search: opts.search || '',
      hash: '',
    },
    history: {
      replaceState(_st, _t, url) { replacedUrls.push(url); },
    },
    sessionStorage: {
      getItem(k) { return k in storage ? storage[k] : null; },
      setItem(k, v) { storage[k] = String(v); },
      removeItem(k) { delete storage[k]; },
    },
    CSS: { escape: (s) => s },
    URLSearchParams,
    AbortController,
    WebSocket: FakeWS,
    navigator: {
      clipboard: {
        writeText: opts.clipboardWriteText || (async () => {}),
      },
    },
    setTimeout: fakeSetTimeout,
    clearTimeout: fakeClearTimeout,
    fetch: async (url, init = {}) => {
      fetchCalls.push({ url, init });
      return opts.fetch(url, init);
    },
  });

  vm.runInContext(fs.readFileSync(input.appJs, 'utf8'), ctx);
  return { windowObj, doc, els, getEl, makeEl, storage, replacedUrls, wsCalls, fetchCalls, scheduledTimeouts };
}

(async () => {
  const results = {};

  // 1. URL ?token= scrubbing, Bearer header, WS ?token=, and 401 auto token modal + retry
  {
    let requireToken = 'tok-secret-42';
    const h = createHarness({
      search: '?token=initial-tok&foo=bar',
      fetch: async (url, init) => {
        const auth = (init.headers && init.headers.Authorization) || '';
        if (url.startsWith('/api/bootstrap')) {
          if (auth !== `Bearer ${requireToken}`) {
            return {
              ok: false,
              status: 401,
              json: async () => ({ detail: { kind: 'unauthorized', message: 'Missing or invalid API bearer token' } }),
            };
          }
          return { ok: true, status: 200, json: async () => input.boot };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      },
    });
    await new Promise((r) => setTimeout(r, 30));
    const firstAuthHeader = h.fetchCalls[0].init.headers.Authorization;
    const urlReplaced = h.replacedUrls[0];
    const tokenModalOpenedOn401 = h.getEl('modal-body').innerHTML.includes('api-token-input');
    const retryCardShown = h.getEl('main-view').innerHTML.includes('data-action="retry-bootstrap"');

    // Submit valid token via modal
    h.getEl('api-token-input').value = requireToken;
    await h.windowObj._app.submitApiToken({ preventDefault() {} });
    await new Promise((r) => setTimeout(r, 30));

    const lastBootstrapAuth = h.fetchCalls.filter((c) => c.url.startsWith('/api/bootstrap')).slice(-1)[0].init.headers.Authorization;
    const wsUrlWithToken = h.wsCalls.length > 0 ? h.wsCalls[h.wsCalls.length - 1].url : '';
    const tokenInStorage = h.storage.colab_boost_api_token;
    results.auth = {
      firstAuthHeader,
      urlReplaced,
      tokenModalOpenedOn401,
      retryCardShown,
      lastBootstrapAuth,
      wsUrlWithToken,
      tokenInStorage,
    };
  }

  // 2. Command Palette static shell (#cmd-search NOT rebuilt on filter or refreshBootstrap) + ArrowDown/Enter + IME guard
  {
    let modalBodySetCount = 0;
    const h = createHarness({
      onInnerHTML(el) {
        if (el.id === 'modal-body') modalBodySetCount += 1;
      },
      fetch: async (url) => {
        if (url.startsWith('/api/bootstrap')) return { ok: true, status: 200, json: async () => input.boot };
        return { ok: true, status: 200, json: async () => ({}) };
      },
    });
    await new Promise((r) => setTimeout(r, 25));
    modalBodySetCount = 0;
    h.windowObj._app.openCommandPalette();
    const searchNodeBefore = h.getEl('cmd-search');
    const afterOpenSets = modalBodySetCount;

    // Filter command palette -> must NOT touch #modal-body or recreate #cmd-search
    h.windowObj._app.filterCmdPalette('sessions');
    const afterFilterSets = modalBodySetCount;
    const listHtml = h.getEl('cmd-list').innerHTML;

    // WS-driven refreshBootstrap while command palette is open -> must NOT rebuild #modal-body/#cmd-search
    await h.windowObj._app.refreshBootstrap();
    const afterRefreshSets = modalBodySetCount;
    const sameSearchNode = h.getEl('cmd-search') === searchNodeBefore;

    // IME composing Enter must NOT execute command
    h.doc.dispatch('keydown', { key: 'Enter', isComposing: true });
    const routeAfterComposingEnter = h.getEl('crumb-current').textContent;

    // Real Enter executes filtered command ("sessions")
    await h.doc.dispatch('keydown', { key: 'Enter', isComposing: false });
    const routeAfterRealEnter = h.getEl('crumb-current').textContent;
    const modalClearedOnClose = h.getEl('modal-body').innerHTML === '';

    results.cmdPalette = {
      afterOpenSets,
      afterFilterSets,
      afterRefreshSets,
      sameSearchNode,
      listHasActiveClass: listHtml.includes('cmd-item active'),
      routeAfterComposingEnter,
      routeAfterRealEnter,
      modalClearedOnClose,
    };
  }

  // 3. timed_out_provisions in global alert banner + hours_remaining === 0 + L4 highMem + H100/V6E1 + bootstrap specs + warm VM filter + formatBytes GiB
  {
    const customBoot = JSON.parse(JSON.stringify(input.boot));
    customBoot.active_account.usage.hours_remaining = 0;
    customBoot.active_account.usage.burn_rate_hourly = 6.93;
    customBoot.accelerator_specs = [
      { id: 'G4', kind: 'gpu', vram_mib: [97887], high_mem_only: false, approx_ccu_per_hour: 1.8 },
      { id: 'A100', kind: 'gpu', vram_mib: [40960, 81920], high_mem_only: false, approx_ccu_per_hour: 6.93 },
      { id: 'H100', kind: 'gpu', vram_mib: [81920], high_mem_only: false, approx_ccu_per_hour: 11.78 },
      { id: 'L4', kind: 'gpu', vram_mib: [23034], high_mem_only: true, approx_ccu_per_hour: 2.82 },
      { id: 'T4', kind: 'gpu', vram_mib: [15360], high_mem_only: false, approx_ccu_per_hour: 1.17 },
      { id: 'V5E1', kind: 'tpu', vram_mib: [], high_mem_only: true, approx_ccu_per_hour: 2.11 },
      { id: 'V6E1', kind: 'tpu', vram_mib: [], high_mem_only: true, approx_ccu_per_hour: 3.55 },
      { id: 'NONE', kind: 'cpu', vram_mib: [], high_mem_only: false, approx_ccu_per_hour: 0.07 },
    ];
    customBoot.approx_burn_rates = { G4: 1.8, A100: 6.93, H100: 11.78, L4: 2.82, T4: 1.17, V5E1: 2.11, V6E1: 3.55, NONE: 0.07 };
    // Make managed session "stopping" and keep unmanaged orphan -> NEITHER should be reusable warm VM!
    customBoot.sessions = [
      { ...customBoot.sessions[0], name: 'stopping-vm', managed: true, status: 'stopping' },
      { ...customBoot.sessions[0], name: 'lost-vm', managed: true, status: 'lost' },
      { ...customBoot.sessions[0], name: 'orphan-vm', managed: false, status: 'running' },
    ];
    // Include a run with 2.5 GiB keep artifact to verify formatBytes GiB
    customBoot.runs = [{
      run_id: 'run-gib',
      recipe_id: 'comfyui-studio',
      recipe_name: { 'zh-CN': 'ComfyUI', en: 'ComfyUI' },
      session_name: 'stopping-vm',
      state: 'ready',
      stage: 'ready',
      progress: 1.0,
      message: 'Ready',
      outputs: {},
      endpoints: [],
      keep_paths: [{ path: '/out', label: { 'zh-CN': '输出', en: 'Out' }, local_archive: '/tmp/a.tar.gz', file_count: 3, total_bytes: 2.5 * 1024 * 1024 * 1024 }],
      log_tail: ['line1', 'line2'],
    }];

    const h = createHarness({
      fetch: async (url, init) => {
        if (url.startsWith('/api/bootstrap')) return { ok: true, status: 200, json: async () => customBoot };
        if (url === '/api/sessions/stop-all' && init.method === 'POST') {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              stopped_count: 1,
              remaining_sessions: 0,
              timed_out_provisions: 2,
              failed_accounts: [],
              stop_errors: [],
              zero_burn_verified: false,
            }),
          };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      },
    });
    await new Promise((r) => setTimeout(r, 25));

    const overviewHtml = h.getEl('main-view').innerHTML;
    const hudBurnHtml = h.getEl('hud-burn').innerHTML;

    // Trigger stop-all -> confirm -> check timed_out_provisions in global alert banner
    h.getEl('btn-stop-all').click();
    await h.getEl('modal-body').dispatch('click', {
      target: { closest: () => ({ dataset: { action: 'confirm-ok' }, disabled: false }) },
    });
    const alertZh = h.getEl('global-alert-banner').innerHTML;

    // Deploy view checks: L4 data-high-mem="1", H100 card, no Reuse Warm VM, GiB formatting, advanced V6E1 & exact count
    h.windowObj._app.pickRecipe(customBoot.recipes[0].id);
    h.windowObj._app.toggleAdvanced(customBoot.recipes[0].id);
    const deployHtml = h.getEl('main-view').innerHTML;

    // New session modal checks: H100 & V6E1 options present
    h.windowObj._app.openNewSessionModal();
    const newSessHtml = h.getEl('modal-body').innerHTML;
    h.windowObj._app.closeModal();

    results.contract = {
      overviewHasZeroHours: overviewHtml.includes('>0 <small>hours</small>'),
      hudBurnHasZeroH: hudBurnHtml.includes('~0h'),
      alertMentionsTimedOut2: alertZh.includes('(2)'),
      l4HighMemTrue: /data-accel="L4"[^>]*data-high-mem="1"/.test(deployHtml),
      hasH100Tier: deployHtml.includes('data-accel="H100"'),
      hasV6E1InDeployAdv: deployHtml.includes('value="V6E1"'),
      hasV6E1InNewSession: newSessHtml.includes('value="V6E1"'),
      noWarmVmReused: !deployHtml.includes('复用热实例') && !deployHtml.includes('Reuse Warm VM'),
      hasGiBFormat: deployHtml.includes('2.50 GiB'),
      noHardcodedDeadCount: !deployHtml.includes('undefined'),
    };
  }

  // 4. Playground decoupled [ERROR] & latency/tok/s metadata + real mode empty tunnel guard + secret DOM hygiene
  {
    const sentPayloads = [];
    let chatCallIdx = 0;
    const h = createHarness({
      fetch: async (url, init) => {
        if (url.startsWith('/api/bootstrap')) return { ok: true, status: 200, json: async () => input.boot };
        if (url === '/api/playground/chat') {
          chatCallIdx += 1;
          sentPayloads.push(JSON.parse(init.body));
          if (chatCallIdx === 1) {
            return { ok: true, status: 200, json: async () => ({ reply: 'Clean answer 1', latency_ms: 42.5, tok_per_s: 88.2, model: 'qwen' }) };
          }
          if (chatCallIdx === 2) {
            return { ok: false, status: 502, json: async () => ({ detail: 'Upstream crashed' }) };
          }
          return { ok: true, status: 200, json: async () => ({ reply: 'Clean answer 3', latency_ms: 51.0, tok_per_s: 79.0, model: 'qwen' }) };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      },
    });
    await new Promise((r) => setTimeout(r, 25));
    h.windowObj._app.go('tunnels');
    h.windowObj._app.setChatMeta('chatApiKey', 'sk-super-secret-999');
    h.windowObj._app.go('tunnels');
    const tunnelsHtmlWithSecret = h.getEl('main-view').innerHTML;

    // Turn 1: ok
    h.getEl('chat-input').value = 'Prompt 1';
    await h.windowObj._app.sendChat({ preventDefault() {} });
    // Turn 2: error
    h.getEl('chat-input').value = 'Prompt 2';
    await h.windowObj._app.sendChat({ preventDefault() {} });
    // Turn 3: ok -> inspect messages sent on Turn 3
    h.getEl('chat-input').value = 'Prompt 3';
    await h.windowObj._app.sendChat({ preventDefault() {} });

    const turn3Msgs = sentPayloads[2].messages;
    const chatBoxHtml = h.getEl('main-view').innerHTML;

    // Real mode with 0 tunnels -> clear prompt and sendChat blocked without calling fetch
    const realBootNoTunnels = JSON.parse(JSON.stringify(input.boot));
    realBootNoTunnels.settings.backend_mode = 'real';
    realBootNoTunnels.tunnels = [];
    const hReal = createHarness({
      fetch: async (url) => {
        if (url.startsWith('/api/bootstrap')) return { ok: true, status: 200, json: async () => realBootNoTunnels };
        return { ok: true, status: 200, json: async () => ({}) };
      },
    });
    await new Promise((r) => setTimeout(r, 25));
    hReal.windowObj._app.go('tunnels');
    const realTunnelsHtml = hReal.getEl('main-view').innerHTML;
    const callsBeforeRealSend = hReal.fetchCalls.length;
    hReal.getEl('chat-input').value = 'Should be blocked';
    await hReal.windowObj._app.sendChat({ preventDefault() {} });
    const callsAfterRealSend = hReal.fetchCalls.length;

    results.playground = {
      secretNotInDomHtml: !tunnelsHtmlWithSecret.includes('sk-super-secret-999'),
      turn3Msgs,
      chatBoxShowsMetaUi: chatBoxHtml.includes('42.5 ms | 88.2 tok/s'),
      realModeShowsTunnelPrompt: realTunnelsHtml.includes('请先开启/绑定本地隧道') && !realTunnelsHtml.includes('默认沙盒端点'),
      realModeBlockedFetch: callsAfterRealSend === callsBeforeRealSend,
    };
  }

  // 5. Settings dirty draft survives WS refresh, modal explicit clear (""), reqId out-of-order guard, GGUF repo change clears plan, WS backoff
  {
    let bootCalls = 0;
    let resolveSlowBoot = null;
    const slowBoot = JSON.parse(JSON.stringify(input.boot));
    slowBoot.settings.guards.auto_stop_minutes = 999;
    const fastBoot = JSON.parse(JSON.stringify(input.boot));
    fastBoot.settings.guards.auto_stop_minutes = 75;

    const h = createHarness({
      autoOpenWs: false,
      captureTimeouts: true,
      fetch: async (url) => {
        if (url.startsWith('/api/bootstrap')) {
          bootCalls += 1;
          if (bootCalls === 1) return { ok: true, status: 200, json: async () => input.boot };
          if (bootCalls === 2) {
            return await new Promise((res) => {
              resolveSlowBoot = () => res({ ok: true, status: 200, json: async () => slowBoot });
            });
          }
          return { ok: true, status: 200, json: async () => fastBoot };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      },
    });
    await new Promise((r) => setTimeout(r, 10));

    // Race two refreshBootstrap calls: #2 slow (999), #3 fast (75) -> #2 resolves AFTER #3 and must be discarded!
    const pSlow = h.windowObj._app.refreshBootstrap();
    const pFast = h.windowObj._app.refreshBootstrap();
    await pFast;
    resolveSlowBoot();
    await pSlow;
    h.windowObj._app.go('settings');
    const afterRaceHtml = h.getEl('main-view').innerHTML;

    // Dirty settings draft + token draft survive refreshBootstrap() even when blurred
    h.windowObj._app.setGuardDraftField('auto_stop_minutes', '180');
    h.windowObj._app.setGuardDraftField('balance_floor_ccu', '12.5');
    h.windowObj._app.setSecretDraft('hf_token', 'hf_unsaved_secret_xyz');
    h.doc.activeElement = null;
    await h.windowObj._app.refreshBootstrap();
    const settingsHtmlAfterWs = h.getEl('main-view').innerHTML;
    const hfValPreserved = h.getEl('inp-hf-token').value;
    const hfNotInHtml = !settingsHtmlAfterWs.includes('hf_unsaved_secret_xyz');

    // Modal explicit clear ("") survives renderActiveModal()
    h.windowObj._app.openForwardModal('boost-a100-qwen');
    const lblEl = h.getEl('f-label');
    lblEl.value = '';
    h.getEl('modal-body').dispatch('input', { target: lblEl });
    await h.windowObj._app.refreshBootstrap();
    const clearedLabelPreserved = h.getEl('f-label').value === '';
    h.windowObj._app.closeModal();

    // WS exponential backoff: 1000 -> 2000 -> 4000, onclose clears debounce timer, onerror defined
    const ws1 = h.wsCalls[0];
    const hasOnError = typeof ws1.onerror === 'function';
    ws1.onmessage({});
    const debounceScheduled = h.scheduledTimeouts.find((t) => t.delay === 180);
    ws1.onclose();
    const debounceClearedOnClose = !!(debounceScheduled && debounceScheduled.cleared);
    const delay1 = h.scheduledTimeouts.filter((t) => t.delay >= 1000).slice(-1)[0].delay;
    // Fire reconnect #1 and close again -> delay should double to 2000
    h.scheduledTimeouts.filter((t) => t.delay >= 1000).slice(-1)[0].fn();
    h.wsCalls[h.wsCalls.length - 1].onclose();
    const delay2 = h.scheduledTimeouts.filter((t) => t.delay >= 1000).slice(-1)[0].delay;
    // Fire reconnect #2 and close again -> delay should double to 4000
    h.scheduledTimeouts.filter((t) => t.delay >= 1000).slice(-1)[0].fn();
    h.wsCalls[h.wsCalls.length - 1].onclose();
    const delay3 = h.scheduledTimeouts.filter((t) => t.delay >= 1000).slice(-1)[0].delay;

    results.resilience = {
      raceKeptFast75: afterRaceHtml.includes('value="75"') && !afterRaceHtml.includes('value="999"'),
      dirtyGuardsPreserved: settingsHtmlAfterWs.includes('value="180"') && settingsHtmlAfterWs.includes('value="12.5"'),
      hfValPreserved,
      hfNotInHtml,
      clearedLabelPreserved,
      hasOnError,
      debounceClearedOnClose,
      wsBackoffDelays: [delay1, delay2, delay3],
    };
  }

  // 6. Scroll preservation for #chat-box and .terminal + GGUF reqId race & repo change clears plan + secret cleared on submitDeploy
  {
    let ggufCalls = 0;
    let resolveSlowGguf = null;
    const terminals = [
      { scrollTop: 135, getAttribute: (k) => (k === 'data-run-id' ? 'run-1' : null) },
    ];
    const h = createHarness({
      querySelectorAll(sel) {
        if (sel === '.terminal') return terminals;
        return [];
      },
      fetch: async (url, init) => {
        if (url.startsWith('/api/bootstrap')) return { ok: true, status: 200, json: async () => input.boot };
        if (url.startsWith('/api/hf/gguf-plan')) {
          ggufCalls += 1;
          if (ggufCalls === 1) {
            return await new Promise((res) => {
              resolveSlowGguf = () => res({
                ok: true,
                status: 200,
                json: async () => ({ repo: 'old/repo', architecture: 'old', model_ctx: 4096, variants: [], hardware_matrix: [] }),
              });
            });
          }
          return {
            ok: true,
            status: 200,
            json: async () => ({ repo: 'new/repo', architecture: 'qwen3', model_ctx: 32768, variants: [], hardware_matrix: [] }),
          };
        }
        if (url === '/api/deployments' && init.method === 'POST') {
          return { ok: true, status: 200, json: async () => ({ run_id: 'r-ok' }) };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      },
    });
    await new Promise((r) => setTimeout(r, 25));

    // Scroll preservation
    h.windowObj._app.go('tunnels');
    h.getEl('chat-box').scrollTop = 77;
    h.getEl('chat-box').scrollHeight = 600;
    h.getEl('chat-box').clientHeight = 200;
    await h.windowObj._app.refreshBootstrap();
    const chatScrollPreserved = h.getEl('chat-box').scrollTop === 77;
    const terminalScrollPreserved = terminals[0].scrollTop === 135;

    // GGUF race + repo change clears stale plan
    h.windowObj._app.pickRecipe('llama-cpp-gguf');
    h.windowObj._app.updateDraft('llama-cpp-gguf', 'repo', 'old/repo', false);
    const pOld = h.windowObj._app.analyzeGguf();
    h.windowObj._app.updateDraft('llama-cpp-gguf', 'repo', 'new/repo', false);
    const pNew = h.windowObj._app.analyzeGguf();
    await pNew;
    resolveSlowGguf();
    await pOld;
    const ggufHtmlAfterRace = h.getEl('main-view').innerHTML;

    // Changing repo again clears state.ggufPlan immediately
    h.windowObj._app.updateDraft('llama-cpp-gguf', 'repo', 'changed/repo', false);
    const plannerCleared = h.getEl('gguf-planner-host').innerHTML === '';

    // Deploy secret cleared after submitDeploy succeeds and never written to innerHTML
    h.windowObj._app.updateDraft('llama-cpp-gguf', 'api_key', 'deploy-secret-777', true);
    const deployHtmlBeforeSubmit = h.getEl('main-view').innerHTML;
    await h.windowObj._app.submitDeploy({ preventDefault() {} });
    const deployHtmlAfterSubmit = h.getEl('main-view').innerHTML;

    results.scrollAndGguf = {
      chatScrollPreserved,
      terminalScrollPreserved,
      ggufRaceKeptNew: ggufHtmlAfterRace.includes('new/repo') && !ggufHtmlAfterRace.includes('old/repo'),
      plannerCleared,
      deploySecretNotInHtml: !deployHtmlBeforeSubmit.includes('deploy-secret-777'),
      deploySecretPlaceholderClearedAfterSubmit: !deployHtmlAfterSubmit.includes('已在内存中暂存密钥'),
    };
  }

  process.stdout.write(JSON.stringify(results));
})();
"""

    proc = subprocess.run(
        [node_bin, "-e", harness],
        input=json.dumps({"boot": boot_payload, "appJs": str(app_js_path)}),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    res = json.loads(proc.stdout)

    # 1. Auth & 401 Token Modal
    assert res["auth"]["firstAuthHeader"] == "Bearer initial-tok"
    assert res["auth"]["urlReplaced"] == "/?foo=bar"
    assert res["auth"]["tokenModalOpenedOn401"] is True
    assert res["auth"]["retryCardShown"] is True
    assert res["auth"]["lastBootstrapAuth"] == "Bearer tok-secret-42"
    assert res["auth"]["wsUrlWithToken"].endswith("/ws/events?token=tok-secret-42")
    assert res["auth"]["tokenInStorage"] == "tok-secret-42"

    # 2. Command Palette static shell & keyboard navigation
    assert res["cmdPalette"]["afterOpenSets"] == 1
    assert res["cmdPalette"]["afterFilterSets"] == 1
    assert res["cmdPalette"]["afterRefreshSets"] == 1
    assert res["cmdPalette"]["sameSearchNode"] is True
    assert res["cmdPalette"]["listHasActiveClass"] is True
    assert res["cmdPalette"]["routeAfterComposingEnter"] != res["cmdPalette"]["routeAfterRealEnter"]
    assert res["cmdPalette"]["routeAfterRealEnter"] in ("算力实例管理", "GPU Sessions")
    assert res["cmdPalette"]["modalClearedOnClose"] is True

    # 3. Contract alignment, timed_out_provisions, hours_remaining === 0, L4 highMem, H100/V6E1, warm VM filter, GiB
    assert res["contract"]["overviewHasZeroHours"] is True
    assert res["contract"]["hudBurnHasZeroH"] is True
    assert res["contract"]["alertMentionsTimedOut2"] is True
    assert res["contract"]["l4HighMemTrue"] is True
    assert res["contract"]["hasH100Tier"] is True
    assert res["contract"]["hasV6E1InDeployAdv"] is True
    assert res["contract"]["hasV6E1InNewSession"] is True
    assert res["contract"]["noWarmVmReused"] is True
    assert res["contract"]["hasGiBFormat"] is True
    assert res["contract"]["noHardcodedDeadCount"] is True

    # 4. Playground decoupled history, real mode tunnel guard, secret hygiene
    assert res["playground"]["secretNotInDomHtml"] is True
    assert res["playground"]["chatBoxShowsMetaUi"] is True
    assert res["playground"]["realModeShowsTunnelPrompt"] is True
    assert res["playground"]["realModeBlockedFetch"] is True
    turn3 = res["playground"]["turn3Msgs"]
    assert len(turn3) == 4
    assert [m["role"] for m in turn3] == ["user", "assistant", "user", "user"]
    assert turn3[1]["content"] == "Clean answer 1"
    assert all("[ERROR]" not in m["content"] and "tok/s" not in m["content"] for m in turn3)
    assert all(set(m.keys()) == {"role", "content"} for m in turn3)

    # 5. ReqId race, dirty Settings draft, modal explicit clear, WS backoff
    assert res["resilience"]["raceKeptFast75"] is True
    assert res["resilience"]["dirtyGuardsPreserved"] is True
    assert res["resilience"]["hfValPreserved"] == "hf_unsaved_secret_xyz"
    assert res["resilience"]["hfNotInHtml"] is True
    assert res["resilience"]["clearedLabelPreserved"] is True
    assert res["resilience"]["hasOnError"] is True
    assert res["resilience"]["debounceClearedOnClose"] is True
    assert res["resilience"]["wsBackoffDelays"] == [1000, 2000, 4000]

    # 6. ScrollTop preservation, GGUF race & clear, deploy secret cleanup
    assert res["scrollAndGguf"]["chatScrollPreserved"] is True
    assert res["scrollAndGguf"]["terminalScrollPreserved"] is True
    assert res["scrollAndGguf"]["ggufRaceKeptNew"] is True
    assert res["scrollAndGguf"]["plannerCleared"] is True
    assert res["scrollAndGguf"]["deploySecretNotInHtml"] is True
    assert res["scrollAndGguf"]["deploySecretPlaceholderClearedAfterSubmit"] is True

    # Bonus: ensure new Phase 2 modals/banners have zero leaked CJK in English mode
    cjk_re = re.compile(r"[\u4e00-\u9fff]")
    en_boot = json.loads(json.dumps(boot_payload))
    en_boot["settings"]["language"] = "en"
    en_boot["settings"]["backend_mode"] = "real"
    en_boot["tunnels"] = []
    en_check = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const els = {};
function makeEl(id = '') {
  const ls = {};
  return {
    id, tagName: 'DIV', className: '', textContent: '', innerHTML: '', value: '', placeholder: '', title: '', style: {},
    dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, getAttribute() { return null; }, appendChild() {}, remove() {}, contains() { return false; },
    focus() {}, closest() { return null; }, addEventListener(e, fn) { ls[e] = fn; },
    click() { if (ls.click) return ls.click({ target: this, preventDefault() {} }); },
  };
}
const getEl = (id) => (els[id] = els[id] || makeEl(id));
const doc = {
  title: '', lang: 'en', activeElement: null,
  documentElement: { lang: 'en', setAttribute() {} },
  getElementById: getEl, createElement: () => makeEl(), querySelectorAll: () => [], addEventListener() {},
};
const windowObj = {};
const ctx = vm.createContext({
  window: windowObj, document: doc, location: { protocol: 'http:', host: '127.0.0.1:8765', search: '' },
  CSS: { escape: (s) => s }, URLSearchParams, WebSocket: class { constructor() {} },
  navigator: { clipboard: { writeText: async () => {} } }, setTimeout, clearTimeout,
  fetch: async (url) => {
    if (url.startsWith('/api/bootstrap')) return { ok: true, status: 200, json: async () => input.boot };
    return { ok: true, status: 200, json: async () => ({}) };
  },
});
vm.runInContext(fs.readFileSync(input.appJs, 'utf8'), ctx);
(async () => {
  await new Promise((r) => setTimeout(r, 25));
  const out = {};
  windowObj._app.openApiTokenModal('Unauthorized');
  out.apiTokenModal = getEl('modal-body').innerHTML;
  windowObj._app.openCommandPalette();
  out.cmdPalette = getEl('modal-body').innerHTML;
  windowObj._app.go('tunnels');
  out.realEmptyTunnels = getEl('main-view').innerHTML;
  windowObj._app.go('settings');
  windowObj._app.setGuardDraftField('auto_stop_minutes', '150');
  windowObj._app.go('settings');
  out.dirtySettings = getEl('main-view').innerHTML;
  process.stdout.write(JSON.stringify(out));
})();
"""
    en_proc = subprocess.run(
        [node_bin, "-e", en_check],
        input=json.dumps({"boot": en_boot, "appJs": str(app_js_path)}),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    en_res = json.loads(en_proc.stdout)
    for scope, html_text in en_res.items():
        leaked = cjk_re.findall(html_text)
        assert not leaked, f"Leaked CJK in Phase 2 {scope} under language='en': {leaked}"


