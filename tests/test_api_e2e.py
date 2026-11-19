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

