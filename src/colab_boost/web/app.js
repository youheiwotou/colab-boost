/* Colab Boost Console — Minimalist Design-Engineered SPA (Scheme A) */
(() => {
  const IC = {
    arrowRight: '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>',
    refresh: '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>',
    plus: '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M5 12h14"/></svg>',
    stop: '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>',
    clock: '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
    link: '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
    copy: '<svg aria-hidden="true" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    download: '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
    search: '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>',
    sun: '<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2m-7.07-15.07 1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>',
    moon: '<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
    send: '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>',
    chevronDown: '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>',
    chevronUp: '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m18 15-6-6-6 6"/></svg>',
  };

  const DEFAULT_APPROX_BURN = {
    G4: 1.80,
    A100: 6.93,
    H100: 11.78,
    L4: 2.82,
    T4: 1.17,
    V5E1: 2.11,
    V6E1: 3.55,
    NONE: 0.07,
  };

  const DEFAULT_ACCELERATOR_SPECS = [
    { id: "G4", kind: "gpu", vram_mib: [97887], high_mem_only: false, approx_ccu_per_hour: 1.80 },
    { id: "A100", kind: "gpu", vram_mib: [40960, 81920], high_mem_only: false, approx_ccu_per_hour: 6.93 },
    { id: "H100", kind: "gpu", vram_mib: [81920], high_mem_only: false, approx_ccu_per_hour: 11.78 },
    { id: "L4", kind: "gpu", vram_mib: [23034], high_mem_only: true, approx_ccu_per_hour: 2.82 },
    { id: "T4", kind: "gpu", vram_mib: [15360], high_mem_only: false, approx_ccu_per_hour: 1.17 },
    { id: "V5E1", kind: "tpu", vram_mib: [], high_mem_only: true, approx_ccu_per_hour: 2.11 },
    { id: "V6E1", kind: "tpu", vram_mib: [], high_mem_only: true, approx_ccu_per_hour: 3.55 },
    { id: "NONE", kind: "cpu", vram_mib: [], high_mem_only: false, approx_ccu_per_hour: 0.07 },
  ];

  const DEFAULT_HIGH_MEM_ONLY = new Set(["L4", "V5E1", "V6E1"]);
  const VALID_ROUTES = new Set(["overview", "deploy", "sessions", "tunnels", "accounts", "settings"]);
  const ROUTE_ALIASES = { playground: "tunnels" };
  const RECEIPTS_PAGE_SIZE = 25;
  const API_TOKEN_STORAGE_KEY = "colab_boost_api_token";
  const DEFAULT_API_TIMEOUT_MS = 30000;
  const WS_BACKOFF_BASE_MS = 1000;
  const WS_BACKOFF_MAX_MS = 30000;

  const GUARD_FIELDS = [
    { id: "set-autostop", key: "auto_stop_minutes", fallback: 120 },
    { id: "set-idlestop", key: "idle_stop_minutes", fallback: 30 },
    { id: "set-floor", key: "balance_floor_ccu", fallback: 5 },
    { id: "set-cap", key: "session_spend_cap_ccu", fallback: 20 },
    { id: "set-retries", key: "max_vram_retries", fallback: 4 },
    { id: "set-maxsess", key: "max_managed_sessions", fallback: 3 },
  ];

  const MODAL_FIELD_IDS = [
    "m-accel",
    "m-vram",
    "m-autostop",
    "m-highmem",
    "f-rport",
    "f-lport",
    "f-label",
    "oauth-code",
    "api-token-input",
  ];

  let bootstrapReqId = 0;
  let ggufReqId = 0;
  let apiTokenMemory = "";
  let wsSocket = null;
  let wsReconnectTimer = null;
  let wsDebounceTimer = null;
  let wsReconnectDelay = WS_BACKOFF_BASE_MS;
  let pendingScrollChatToBottom = false;
  let modalTriggerEl = null;

  const state = {
    route: "overview",
    sidebarOpen: false,
    receiptsVisibleLimit: RECEIPTS_PAGE_SIZE,
    settings: { backend_mode: "mock", language: "zh-CN", theme: "dark", guards: {} },
    accounts: [],
    activeAccount: null,
    sessions: [],
    provisionOps: [],
    recipes: [],
    runs: [],
    tunnels: [],
    receipts: [],
    secrets: {},
    curatedRepos: [],
    acceleratorSpecs: DEFAULT_ACCELERATOR_SPECS.slice(),
    approxBurnRates: { ...DEFAULT_APPROX_BURN },
    selectedRecipeId: "minimax-h3-studio",
    deployDraft: {},
    advancedOpen: {},
    settingsDraft: {},
    secretDrafts: { hf_token: "", civitai_token: "" },
    ggufPlan: null,
    ggufPlanLoading: false,
    wsState: "connecting",
    chatMessages: [{ role: "assistant", _welcome: true, content: "" }],
    chatBusy: false,
    chatDraft: "",
    chatModel: "auto",
    chatApiKey: "",
    selectedChatTunnelId: "",
    activeModal: null,
    modalEdits: {},
    modalInitialValues: {},
    pendingConfirm: null,
    zeroBurnAlert: null,
    bootstrapError: null,
    busy: {},
    cmdQuery: "",
    cmdActiveIndex: 0,
  };

  const I18N = {
    "zh-CN": {
      brand_title: "Colab Boost",
      cmd_label: "命令面板",
      nav_group_work: "工作台",
      nav_group_audit: "控制与审计",
      nav_overview: "总览看板",
      nav_deploy: "极速部署模型",
      nav_sessions: "算力实例管理",
      nav_tunnels: "端口映射与对话",
      nav_accounts: "多账号与关闭回执",
      nav_settings: "成本护栏与密钥",
      footer_native: "Windows 原生免 WSL 直连引擎",
      footer_receipts: "自动保存 Unassign 关闭回执",
    },
    en: {
      brand_title: "Colab Boost",
      cmd_label: "Command",
      nav_group_work: "Workspace",
      nav_group_audit: "Governance",
      nav_overview: "Overview",
      nav_deploy: "Quick Deploy",
      nav_sessions: "GPU Sessions",
      nav_tunnels: "Tunnels & Chat",
      nav_accounts: "Accounts & Receipts",
      nav_settings: "Guards & Vault",
      footer_native: "Native Cross-Platform Bridge (No WSL)",
      footer_receipts: "Auto-Archived Unassign Receipts",
    },
  };

  function tr(zh, en) {
    return state.settings.language === "en" ? en : zh;
  }

  function locObj(dict) {
    if (!dict) return "";
    const lang = state.settings.language || "zh-CN";
    return dict[lang] || dict["en"] || dict["zh-CN"] || Object.values(dict)[0] || "";
  }

  function tunLabel(tun) {
    if (tun?.labels && Object.keys(tun.labels).length > 0) {
      return locObj(tun.labels);
    }
    return tun?.label || "Port Forward";
  }

  function formatBytes(bytes) {
    const n = Number(bytes || 0);
    if (!Number.isFinite(n) || n <= 0) return "0 B";
    if (n < 1024) return `${Math.round(n)} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
    if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(2)} MiB`;
    return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
  }

  function formatCcu(val) {
    if (val === null || val === undefined || val === "") return "--";
    const n = Number(val);
    return Number.isFinite(n) ? n.toFixed(2) : "--";
  }

  function normalizeRoute(raw) {
    const cleaned = String(raw ?? "")
      .trim()
      .replace(/^#/, "")
      .replace(/^\/+/, "")
      .split(/[/?#]/)[0]
      .toLowerCase();
    if (!cleaned) return null;
    const mapped = ROUTE_ALIASES[cleaned] || cleaned;
    return VALID_ROUTES.has(mapped) ? mapped : null;
  }

  function parseRouteFromHash(fallbackWhenEmpty = null) {
    try {
      if (typeof location === "undefined" || !location || typeof location.hash !== "string") {
        return null;
      }
      const raw = location.hash.trim();
      if (!raw || raw === "#" || raw === "#/") return fallbackWhenEmpty;
      return normalizeRoute(raw);
    } catch (_) {
      return null;
    }
  }

  function syncRouteHash(route) {
    try {
      if (typeof location === "undefined" || !location) return;
      const target = `#/${route}`;
      if (location.hash !== target) {
        location.hash = target;
      }
    } catch (_) {}
  }

  function initRouteFromHash() {
    const parsed = parseRouteFromHash();
    if (parsed) state.route = parsed;
  }

  function syncSidebarDom() {
    const sidebar = document.getElementById("app-sidebar");
    const backdrop = document.getElementById("sidebar-backdrop");
    const toggleBtn = document.getElementById("btn-sidebar-toggle");
    if (sidebar && sidebar.classList && typeof sidebar.classList.toggle === "function") {
      sidebar.classList.toggle("open", !!state.sidebarOpen);
    }
    if (backdrop && backdrop.classList && typeof backdrop.classList.toggle === "function") {
      backdrop.classList.toggle("hidden", !state.sidebarOpen);
    }
    if (toggleBtn && typeof toggleBtn.setAttribute === "function") {
      toggleBtn.setAttribute("aria-expanded", state.sidebarOpen ? "true" : "false");
    }
  }

  function setSidebarOpen(open) {
    state.sidebarOpen = !!open;
    syncSidebarDom();
  }

  function toggleSidebar() {
    setSidebarOpen(!state.sidebarOpen);
  }

  function getApiToken() {
    if (apiTokenMemory) return apiTokenMemory;
    try {
      if (typeof sessionStorage !== "undefined" && typeof sessionStorage.getItem === "function") {
        const stored = (sessionStorage.getItem(API_TOKEN_STORAGE_KEY) || "").trim();
        if (stored) {
          apiTokenMemory = stored;
          return stored;
        }
      }
    } catch (_) {}
    return "";
  }

  function setApiToken(token) {
    const cleaned = String(token ?? "").trim();
    apiTokenMemory = cleaned;
    try {
      if (typeof sessionStorage !== "undefined") {
        if (cleaned && typeof sessionStorage.setItem === "function") {
          sessionStorage.setItem(API_TOKEN_STORAGE_KEY, cleaned);
        } else if (!cleaned && typeof sessionStorage.removeItem === "function") {
          sessionStorage.removeItem(API_TOKEN_STORAGE_KEY);
        }
      }
    } catch (_) {}
  }

  function initApiTokenFromUrl() {
    try {
      const search = typeof location !== "undefined" && location ? (location.search || "") : "";
      if (!search) return;
      const params = new URLSearchParams(search);
      if (!params.has("token")) return;
      const tok = (params.get("token") || "").trim();
      if (tok) setApiToken(tok);
      params.delete("token");
      const remaining = params.toString();
      const pathname = (location && location.pathname) || "/";
      const hash = (location && location.hash) || "";
      const cleanUrl = `${pathname}${remaining ? `?${remaining}` : ""}${hash}`;
      if (typeof history !== "undefined" && typeof history.replaceState === "function") {
        history.replaceState(null, "", cleanUrl);
      }
    } catch (_) {}
  }

  function normalizeAcceleratorSpecs(raw) {
    if (Array.isArray(raw) && raw.length > 0) {
      return raw
        .filter((item) => item && item.id)
        .map((item) => ({
          id: String(item.id),
          kind: item.kind || (item.id === "NONE" ? "cpu" : ["V5E1", "V6E1"].includes(item.id) ? "tpu" : "gpu"),
          vram_mib: Array.isArray(item.vram_mib) ? item.vram_mib : [],
          high_mem_only: item.high_mem_only !== undefined ? !!item.high_mem_only : DEFAULT_HIGH_MEM_ONLY.has(String(item.id)),
          approx_ccu_per_hour:
            item.approx_ccu_per_hour !== undefined && item.approx_ccu_per_hour !== null
              ? Number(item.approx_ccu_per_hour)
              : (DEFAULT_APPROX_BURN[item.id] ?? 2.0),
        }));
    }
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      const entries = Object.entries(raw);
      if (entries.length > 0) {
        return entries.map(([id, item]) => ({
          id,
          kind: item?.kind || (id === "NONE" ? "cpu" : ["V5E1", "V6E1"].includes(id) ? "tpu" : "gpu"),
          vram_mib: Array.isArray(item?.vram_mib) ? item.vram_mib : [],
          high_mem_only: item?.high_mem_only !== undefined ? !!item.high_mem_only : DEFAULT_HIGH_MEM_ONLY.has(id),
          approx_ccu_per_hour:
            item?.approx_ccu_per_hour !== undefined && item?.approx_ccu_per_hour !== null
              ? Number(item.approx_ccu_per_hour)
              : (DEFAULT_APPROX_BURN[id] ?? 2.0),
        }));
      }
    }
    return DEFAULT_ACCELERATOR_SPECS.slice();
  }

  function normalizeBurnRates(rawRates, specs) {
    const merged = { ...DEFAULT_APPROX_BURN };
    for (const sp of specs || []) {
      if (sp && sp.id && Number.isFinite(Number(sp.approx_ccu_per_hour))) {
        merged[sp.id] = Number(sp.approx_ccu_per_hour);
      }
    }
    if (rawRates && typeof rawRates === "object") {
      for (const [k, v] of Object.entries(rawRates)) {
        const num = Number(v);
        if (Number.isFinite(num)) merged[k] = num;
      }
    }
    return merged;
  }

  function getAcceleratorSpecs() {
    return Array.isArray(state.acceleratorSpecs) && state.acceleratorSpecs.length > 0
      ? state.acceleratorSpecs
      : DEFAULT_ACCELERATOR_SPECS;
  }

  function getAcceleratorSpec(accelId) {
    return getAcceleratorSpecs().find((s) => s.id === accelId) || null;
  }

  function isHighMemOnlyAccelerator(accelId) {
    const spec = getAcceleratorSpec(accelId);
    if (spec && spec.high_mem_only !== undefined) return !!spec.high_mem_only;
    return DEFAULT_HIGH_MEM_ONLY.has(accelId);
  }

  function getBurnRate(accelId) {
    if (state.approxBurnRates && state.approxBurnRates[accelId] !== undefined) {
      return Number(state.approxBurnRates[accelId]);
    }
    const spec = getAcceleratorSpec(accelId);
    if (spec && spec.approx_ccu_per_hour !== undefined) return Number(spec.approx_ccu_per_hour);
    return DEFAULT_APPROX_BURN[accelId] ?? 2.0;
  }

  function getReusableSessions() {
    return state.sessions.filter(
      (s) => s && s.managed !== false && s.status !== "stopping" && s.status !== "lost"
    );
  }

  function hasDirtySettingsDraft() {
    return Object.keys(state.settingsDraft).length > 0;
  }

  function esc(val) {
    return String(val ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function safeHttpUrl(url) {
    const raw = String(url ?? "").trim();
    if (!raw) return "#";
    if (typeof URL === "function") {
      try {
        const parsed = new URL(raw);
        if (parsed.protocol === "http:" || parsed.protocol === "https:") {
          return parsed.href;
        }
        return "#";
      } catch (_) {
        return "#";
      }
    }
    return /^https?:\/\/[^\s"'<>]+$/i.test(raw) ? raw : "#";
  }

  function formatApiDetail(detail, status) {
    if (detail === null || detail === undefined) {
      return `HTTP ${status}`;
    }
    if (typeof detail === "string") {
      return detail.trim() || `HTTP ${status}`;
    }
    if (Array.isArray(detail)) {
      const parts = detail
        .map((item) => {
          if (typeof item === "string") return item;
          if (!item || typeof item !== "object") return "";
          const loc = Array.isArray(item.loc)
            ? item.loc.filter((seg) => seg !== "body" && seg !== "query").join(".")
            : "";
          const msg = item.msg || item.message || "";
          if (loc && msg) return `${loc}: ${msg}`;
          return msg || loc || "";
        })
        .filter(Boolean);
      return parts.length > 0 ? parts.join("; ") : `HTTP ${status}`;
    }
    if (typeof detail === "object") {
      return detail.message || detail.msg || detail.error || JSON.stringify(detail) || `HTTP ${status}`;
    }
    return String(detail) || `HTTP ${status}`;
  }

  function isBusy(key) {
    return !!state.busy[key];
  }

  async function runLocked(key, triggerEl, fn, rerenderChrome = false) {
    if (state.busy[key]) return;
    state.busy[key] = true;
    if (triggerEl) {
      triggerEl.disabled = true;
      if (triggerEl.classList && typeof triggerEl.classList.add === "function") {
        triggerEl.classList.add("is-busy");
      }
    }
    if (rerenderChrome) renderChrome();
    else renderMain();
    try {
      return await fn();
    } finally {
      delete state.busy[key];
      if (triggerEl) {
        triggerEl.disabled = false;
        if (triggerEl.classList && typeof triggerEl.classList.remove === "function") {
          triggerEl.classList.remove("is-busy");
        }
      }
      if (rerenderChrome) renderChrome();
      else renderMain();
    }
  }

  function toast(msg, kind = "info") {
    const stack = document.getElementById("toast-stack");
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    if (typeof el.setAttribute === "function") {
      el.setAttribute("role", kind === "err" ? "alert" : "status");
    }
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }

  async function api(path, options = {}) {
    const { timeoutMs, headers, ...fetchOpts } = options;
    const reqHeaders = { "Content-Type": "application/json", ...(headers || {}) };
    const hasAuthHeader = Object.keys(reqHeaders).some((k) => k.toLowerCase() === "authorization");
    const token = getApiToken();
    if (token && !hasAuthHeader) {
      reqHeaders.Authorization = `Bearer ${token}`;
    }

    const effectiveTimeout = timeoutMs !== undefined ? timeoutMs : DEFAULT_API_TIMEOUT_MS;
    let controller = null;
    let timeoutId = null;
    let signal = fetchOpts.signal;
    if (!signal && effectiveTimeout > 0) {
      if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
        signal = AbortSignal.timeout(effectiveTimeout);
      } else if (typeof AbortController !== "undefined") {
        controller = new AbortController();
        signal = controller.signal;
        timeoutId = setTimeout(() => controller.abort(), effectiveTimeout);
      }
    }

    let resp;
    try {
      const init = { ...fetchOpts, headers: reqHeaders };
      if (signal) init.signal = signal;
      resp = await fetch(path, init);
    } catch (err) {
      if (err && (err.name === "AbortError" || err.name === "TimeoutError")) {
        const timeoutErr = new Error(
          tr("请求超时，请检查后端服务状态后重试", "Request timed out; check backend service and retry")
        );
        timeoutErr.isTimeout = true;
        throw timeoutErr;
      }
      throw err;
    } finally {
      if (timeoutId) clearTimeout(timeoutId);
    }

    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const msg = formatApiDetail(data?.detail, resp.status);
      const err = new Error(msg);
      err.status = resp.status;
      err.detail = data?.detail;
      if (resp.status === 401) {
        const detailKind = data?.detail && typeof data.detail === "object" ? data.detail.kind : "";
        const isColabOAuthErr = detailKind === "auth_error" || detailKind === "missing_credential";
        if (!isColabOAuthErr && state.activeModal?.type !== "api_token") {
          openApiTokenModal(msg);
        }
      }
      throw err;
    }
    return data;
  }

  function snapshotModalInitialValues() {
    const initial = {};
    for (const fid of MODAL_FIELD_IDS) {
      const el = document.getElementById(fid);
      if (el && el.value !== undefined) initial[fid] = el.value;
    }
    state.modalInitialValues = initial;
  }

  function openModal(html, modalMeta = null, opts = {}) {
    const preserveEdits = !!opts.preserveEdits;
    const wasOpen = !!state.activeModal;
    if (!wasOpen && !preserveEdits) {
      modalTriggerEl = (typeof document !== "undefined" && document.activeElement) ? document.activeElement : null;
    }
    if (!preserveEdits) {
      state.modalEdits = {};
      state.modalInitialValues = {};
    }
    state.activeModal = modalMeta;
    const bd = document.getElementById("modal-backdrop");
    const body = document.getElementById("modal-body");
    if (body) {
      body.innerHTML = html;
      if (typeof body.setAttribute === "function") {
        body.setAttribute("role", "dialog");
        body.setAttribute("aria-modal", "true");
        if (String(html).includes('id="modal-title"')) {
          body.setAttribute("aria-labelledby", "modal-title");
          if (typeof body.removeAttribute === "function") body.removeAttribute("aria-label");
        } else {
          if (typeof body.removeAttribute === "function") body.removeAttribute("aria-labelledby");
          body.setAttribute("aria-label", opts.ariaLabel || tr("对话框", "Dialog"));
        }
      }
    }
    if (bd) bd.classList.remove("hidden");
    if (!preserveEdits) {
      snapshotModalInitialValues();
      if (body) {
        let firstFocusable = null;
        if (typeof body.querySelector === "function") {
          firstFocusable = body.querySelector(
            'input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'
          );
        }
        if (firstFocusable && typeof firstFocusable.focus === "function") {
          firstFocusable.focus();
        } else if (typeof body.focus === "function") {
          body.focus();
        }
      }
    }
  }

  function closeModal() {
    const prevTrigger = modalTriggerEl;
    modalTriggerEl = null;
    state.activeModal = null;
    state.pendingConfirm = null;
    state.modalEdits = {};
    state.modalInitialValues = {};
    state.cmdActiveIndex = 0;
    const bd = document.getElementById("modal-backdrop");
    const body = document.getElementById("modal-body");
    if (bd) bd.classList.add("hidden");
    if (body) body.innerHTML = "";
    if (prevTrigger && typeof prevTrigger.focus === "function" && prevTrigger.isConnected !== false) {
      try {
        prevTrigger.focus();
      } catch (_) {}
    }
  }

  function openApiTokenModal(reason = "", opts = {}) {
    const hasExisting = !!getApiToken();
    openModal(`
      <h3 id="modal-title" style="font-size:15px;font-weight:600;margin-bottom:10px;">${tr("需要控制台 API 访问令牌", "Console API Token Required")}</h3>
      <p style="font-size:12.5px;color:var(--text-secondary);margin-bottom:12px;">
        ${tr(
          "当前后端已启用 <code>COLAB_BOOST_API_TOKEN</code> 访问鉴权。请输入正确的 Bearer Token 后自动重试。",
          "This backend requires a <code>COLAB_BOOST_API_TOKEN</code> bearer token. Enter your token below to authenticate and retry."
        )}
      </p>
      ${reason ? `<div class="banner danger" role="alert" style="margin-bottom:12px;font-size:12px;">${esc(reason)}</div>` : ""}
      <form data-submit="api-token" class="form-grid">
        <div class="field full">
          <label for="api-token-input">${tr("API Bearer Token", "API Bearer Token")}</label>
          <input type="password" id="api-token-input" autocomplete="off" required placeholder="${hasExisting ? tr("已保存令牌（重新输入以替换）", "Token set in session (type to replace)") : tr("输入 COLAB_BOOST_API_TOKEN", "Enter COLAB_BOOST_API_TOKEN")}" />
        </div>
        <div class="field full row-actions" style="justify-content:flex-end;">
          <button type="button" class="btn" data-action="close-modal">${tr("取消", "Cancel")}</button>
          <button type="submit" class="btn btn-primary">${tr("保存并重试", "Save & Retry")}</button>
        </div>
      </form>
    `, { type: "api_token", reason }, opts);
    const inp = document.getElementById("api-token-input");
    if (inp && typeof inp.focus === "function") inp.focus();
  }

  function openConfirmModal(meta, onConfirm, opts = {}) {
    state.pendingConfirm = onConfirm;
    const isDanger = meta.tone !== "primary";
    const title = typeof meta.title === "function" ? meta.title() : meta.title;
    const bodyHtml = typeof meta.bodyHtml === "function" ? meta.bodyHtml() : meta.bodyHtml;
    const confirmLabel = typeof meta.confirmLabel === "function" ? meta.confirmLabel() : meta.confirmLabel;
    openModal(`
      <h3 id="modal-title" style="font-size:15px;font-weight:600;margin-bottom:10px;">${esc(title)}</h3>
      <div class="confirm-body">${bodyHtml}</div>
      <div class="row-actions" style="justify-content:flex-end;margin-top:16px;">
        <button class="btn" type="button" data-action="close-modal">${tr("取消", "Cancel")}</button>
        <button class="btn ${isDanger ? "btn-danger" : "btn-primary"}" type="button" data-action="confirm-ok">${esc(confirmLabel)}</button>
      </div>
    `, { type: "confirm", meta }, opts);
  }

  function renderGlobalAlert() {
    const host = document.getElementById("global-alert-banner");
    if (!host) return;
    const alert = state.zeroBurnAlert;
    if (!alert) {
      host.innerHTML = "";
      host.classList.add("hidden");
      return;
    }

    let title = "";
    let detailHtml = "";
    if (alert.kind === "stop_all_unverified") {
      title = tr(
        "零燃费核验未通过：仍有实例或账号未确认停止计费！",
        "Zero-Burn Verification Failed: Active or Unverified Billing Detected!"
      );
      const parts = [];
      parts.push(
        tr(
          `已发送释放 ${alert.stoppedCount} 个实例，但残留活跃会话: ${alert.remaining}`,
          `Stopped ${alert.stoppedCount} session(s), but remaining active sessions: ${alert.remaining}`
        )
      );
      if ((alert.timedOutProvisions || 0) > 0) {
        parts.push(
          tr(
            `超时未结束的在途分配任务 (${alert.timedOutProvisions})：请稍候再次点击重试停止以防漏网计费`,
            `Timed-out in-flight provisions (${alert.timedOutProvisions}): retry Stop All shortly to prevent leaked billing`
          )
        );
      }
      if ((alert.failedAccounts || []).length > 0) {
        const accList = alert.failedAccounts
          .map((item) => (typeof item === "string" ? item : (item?.email || item?.account_id || JSON.stringify(item))))
          .join(", ");
        parts.push(tr(`查询失败账号 (${alert.failedAccounts.length}): ${accList}`, `Failed account checks (${alert.failedAccounts.length}): ${accList}`));
      }
      if ((alert.stopErrors || []).length > 0) {
        const errList = alert.stopErrors
          .map((item) => (typeof item === "string" ? item : (item?.error || item?.message || JSON.stringify(item))))
          .join("; ");
        parts.push(tr(`停机异常 (${alert.stopErrors.length}): ${errList}`, `Stop errors (${alert.stopErrors.length}): ${errList}`));
      }
      detailHtml = parts.map((p) => `<div>${esc(p)}</div>`).join("");
    } else if (alert.kind === "session_unverified") {
      title = tr(
        `单实例释放未通过核验：${alert.sessionName} 可能仍在云端计费！`,
        `Unassign Not Verified: ${alert.sessionName} May Still Be Billing!`
      );
      detailHtml = `<div>${esc(
        tr(
          `Endpoint: ${alert.endpoint}${alert.receiptId ? ` · 回执 #${alert.receiptId}` : ""}。请立即重试停止或在 Google Colab 页面人工核验。`,
          `Endpoint: ${alert.endpoint}${alert.receiptId ? ` · Receipt #${alert.receiptId}` : ""}. Retry stopping immediately or verify in Google Colab.`
        )
      )}</div>`;
    } else {
      title = tr(
        "紧急停止全部实例请求失败：云端实例可能仍在计费！",
        "Emergency Stop-All Request Failed: Cloud Runtimes May Still Be Billing!"
      );
      detailHtml = `<div>${esc(alert.message || tr("未知错误", "Unknown error"))}</div>`;
    }

    const busyStopAll = isBusy("sessions:stop-all");
    host.innerHTML = `
      <div class="global-alert-inner">
        <div>
          <div class="global-alert-title">
            <span class="dot dot-danger"></span>
            <span>${esc(title)}</span>
          </div>
          <div class="global-alert-details">${detailHtml}</div>
        </div>
        <div class="row-actions">
          <button class="btn btn-danger btn-sm${busyStopAll ? " is-busy" : ""}" type="button" data-action="retry-stop-all" ${busyStopAll ? "disabled" : ""}>
            ${IC.stop} <span>${busyStopAll ? tr("正在重试停止...", "Retrying Stop All...") : tr("一键重试停止全部", "Retry Stop All")}</span>
          </button>
          <button class="btn btn-sm" type="button" data-action="dismiss-alert">
            ${tr("关闭提示", "Dismiss")}
          </button>
        </div>
      </div>
    `;
    host.classList.remove("hidden");
  }

  function renderActiveModal() {
    const active = state.activeModal;
    if (!active) return;
    if (active.type === "cmd_palette") {
      openCommandPalette();
      return;
    }

    const saved = {};
    for (const fid of MODAL_FIELD_IDS) {
      const el = document.getElementById(fid);
      const wasEdited = Object.prototype.hasOwnProperty.call(state.modalEdits, fid);
      if (wasEdited) {
        saved[fid] = el && el.value !== undefined ? el.value : state.modalEdits[fid];
      } else if (el && el.value !== undefined) {
        const hasInit = Object.prototype.hasOwnProperty.call(state.modalInitialValues, fid);
        if ((hasInit && el.value !== state.modalInitialValues[fid]) || (!hasInit && el.value !== "")) {
          saved[fid] = el.value;
        }
      }
    }

    const rerenderOpts = { preserveEdits: true };
    if (active.type === "new_session") window._app.openNewSessionModal(rerenderOpts);
    else if (active.type === "forward") window._app.openForwardModal(active.sessionName, rerenderOpts);
    else if (active.type === "login" && active.flow) window._app.renderLoginModalWithFlow(active.flow, rerenderOpts);
    else if (active.type === "toggle_mode") openToggleModeModal(active.nextMode, rerenderOpts);
    else if (active.type === "api_token") openApiTokenModal(active.reason || "", rerenderOpts);
    else if (active.type === "confirm" && active.meta && state.pendingConfirm) openConfirmModal(active.meta, state.pendingConfirm, rerenderOpts);

    for (const fid of MODAL_FIELD_IDS) {
      const restored = document.getElementById(fid);
      if (!restored) continue;
      if (Object.prototype.hasOwnProperty.call(saved, fid)) {
        restored.value = saved[fid];
      } else if (restored.value !== undefined) {
        state.modalInitialValues[fid] = restored.value;
      }
    }
  }

  document.getElementById("modal-backdrop").addEventListener("click", (ev) => {
    if (ev.target.id === "modal-backdrop") closeModal();
  });

  async function refreshBootstrap() {
    const reqId = ++bootstrapReqId;
    let snap;
    try {
      snap = await api("/api/bootstrap");
    } catch (err) {
      if (reqId !== bootstrapReqId) return null;
      throw err;
    }
    if (reqId !== bootstrapReqId) return null;

    state.settings = snap.settings;
    state.accounts = snap.accounts || [];
    state.activeAccount = snap.active_account;
    state.sessions = snap.sessions || [];
    state.provisionOps = snap.provision_ops || [];
    state.recipes = snap.recipes || [];
    state.runs = snap.runs || [];
    state.tunnels = snap.tunnels || [];
    state.receipts = snap.receipts || [];
    state.secrets = snap.secrets || {};
    state.curatedRepos = snap.curated_gguf_repos || [];
    state.acceleratorSpecs = normalizeAcceleratorSpecs(snap.accelerator_specs);
    state.approxBurnRates = normalizeBurnRates(snap.approx_burn_rates, state.acceleratorSpecs);
    state.bootstrapError = null;

    if (!state.recipes.find((r) => r.id === state.selectedRecipeId) && state.recipes.length > 0) {
      state.selectedRecipeId = state.recipes[0].id;
    }
    document.documentElement.setAttribute("data-theme", state.settings.theme || "dark");
    renderChrome();
    renderMain();
    if (state.activeModal) renderActiveModal();
    return snap;
  }

  function formatRemainingDeadline(epochSec) {
    if (!epochSec) return tr("未设限", "No limit");
    const diffMin = Math.max(0, Math.round((epochSec - Date.now() / 1000) / 60));
    const h = Math.floor(diffMin / 60);
    const m = diffMin % 60;
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  }

  function refreshWatchdogCountdowns() {
    if (state.route !== "sessions") return;
    const view = document.getElementById("main-view");
    if (!view || typeof view.querySelectorAll !== "function") return;
    view.querySelectorAll("[data-watchdog-deadline]").forEach((node) => {
      const raw = node.getAttribute("data-watchdog-deadline");
      const epoch = raw ? Number(raw) : 0;
      node.textContent = formatRemainingDeadline(epoch || null);
    });
  }

  function renderWsIndicator() {
    const ind = document.getElementById("ws-indicator");
    const dot = document.getElementById("ws-dot");
    if (ind) {
      if (state.wsState === "connected") {
        ind.textContent = tr("直连状态流 · 已连接", "Live Stream Connected");
      } else if (state.wsState === "reconnecting") {
        ind.textContent = tr("状态流重连中...", "Reconnecting Stream...");
      } else {
        ind.textContent = tr("正在连接状态流...", "Connecting Stream...");
      }
    }
    if (dot) {
      const dotCls =
        state.wsState === "connected"
          ? "dot-emerald"
          : state.wsState === "reconnecting"
            ? "dot-amber"
            : "dot-muted";
      dot.className = `dot ${dotCls}`;
    }
  }

  function renderChrome() {
    document.documentElement.lang = state.settings.language || "zh-CN";
    document.title = tr(
      "Colab Boost 控制台 — 极速模型部署与算力护栏控制台",
      "Colab Boost Console — Rapid Model Deployment & Cost-Guard Control Plane"
    );

    const dict = I18N[state.settings.language] || I18N["zh-CN"];
    const crumb = document.getElementById("crumb-current");
    if (crumb) {
      crumb.textContent = dict[`nav_${state.route}`] || dict.nav_overview;
    }

    const isMock = state.settings.backend_mode === "mock";
    const modeBtn = document.getElementById("btn-toggle-mode");
    modeBtn.className = `mode-badge ${isMock ? "mock" : "real"}`;
    modeBtn.title = tr("点击切换演示沙盒与真实 Colab 模式", "Click to switch between Sandbox Demo and Real Colab Mode");
    if (typeof modeBtn.setAttribute === "function") {
      modeBtn.setAttribute("aria-label", modeBtn.title);
    }
    modeBtn.textContent = isMock
      ? tr("演示沙盒 · 0 计费", "Sandbox · 0 Billed")
      : tr("真实 Colab · 实时计费", "Real Colab · Live");

    const sidebarToggleBtn = document.getElementById("btn-sidebar-toggle");
    if (sidebarToggleBtn && typeof sidebarToggleBtn.setAttribute === "function") {
      const toggleLabel = tr("切换导航菜单", "Toggle navigation menu");
      sidebarToggleBtn.setAttribute("aria-label", toggleLabel);
      sidebarToggleBtn.title = toggleLabel;
    }
    const sidebarEl = document.getElementById("app-sidebar");
    if (sidebarEl && typeof sidebarEl.setAttribute === "function") {
      sidebarEl.setAttribute("aria-label", tr("主导航", "Primary navigation"));
    }
    const sidebarNavEl = document.getElementById("sidebar-nav");
    if (sidebarNavEl && typeof sidebarNavEl.setAttribute === "function") {
      sidebarNavEl.setAttribute("aria-label", tr("控制台导航", "Console navigation"));
    }
    const cmdPaletteBtn = document.getElementById("btn-cmd-palette");
    if (cmdPaletteBtn && typeof cmdPaletteBtn.setAttribute === "function") {
      cmdPaletteBtn.setAttribute("aria-label", tr("打开命令面板", "Open command palette"));
    }

    const acc = state.activeAccount;
    const accEl = document.getElementById("hud-account");
    const balEl = document.getElementById("hud-balance");
    const burnEl = document.getElementById("hud-burn");

    if (!acc) {
      accEl.innerHTML = `${tr("账号", "Account")} <strong>${tr("未登录", "Signed out")}</strong>`;
      balEl.innerHTML = `${tr("余额", "Balance")} <strong>--</strong>`;
      burnEl.innerHTML = `${tr("消耗", "Burn")} <strong>--</strong>`;
    } else {
      accEl.innerHTML = `${tr("账号", "Account")} <strong>${esc(acc.email)}</strong>`;
      const u = acc.usage || {};
      if (u.status === "unknown" || u.balance_ccu === null || u.balance_ccu === undefined) {
        balEl.innerHTML = `${tr("余额", "Balance")} <strong>${tr("未知", "Unknown")}</strong>`;
        burnEl.innerHTML = `${tr("消耗", "Burn")} <strong>${tr("未知", "Unknown")}</strong>`;
      } else {
        const staleMark = u.status === "stale" ? ` (${tr("陈旧", "Stale")})` : "";
        balEl.innerHTML = `${tr("余额", "Balance")} <strong>${u.balance_ccu.toFixed(2)} CCU${staleMark}</strong>`;
        const hasHrs = u.hours_remaining !== null && u.hours_remaining !== undefined;
        const hrs = hasHrs ? ` · ~${u.hours_remaining}h` : "";
        burnEl.innerHTML = `${tr("消耗", "Burn")} <strong>${(u.burn_rate_hourly ?? 0).toFixed(2)}/h${hrs}</strong>`;
      }
    }

    const stopAllBtn = document.getElementById("btn-stop-all");
    const stopAllGroup = document.getElementById("stop-all-group");
    const stopAllBusy = isBusy("sessions:stop-all");
    if (state.sessions.length > 0) {
      if (stopAllGroup) stopAllGroup.style.display = "inline-flex";
      stopAllBtn.style.display = "inline-flex";
      stopAllBtn.disabled = stopAllBusy;
      stopAllBtn.textContent = stopAllBusy
        ? tr("正在停止全部...", "Stopping All...")
        : tr(`停止全部 (${state.sessions.length})`, `Stop All (${state.sessions.length})`);
      if (typeof stopAllBtn.setAttribute === "function") {
        stopAllBtn.setAttribute("aria-label", tr("停止全部活跃实例", "Stop all active runtimes"));
      }
    } else {
      if (stopAllGroup) stopAllGroup.style.display = "none";
      stopAllBtn.style.display = "none";
      stopAllBtn.disabled = false;
    }

    const langBtn = document.getElementById("btn-lang");
    langBtn.textContent = state.settings.language === "en" ? "中文" : "EN";
    if (typeof langBtn.setAttribute === "function") {
      langBtn.setAttribute("aria-label", state.settings.language === "en" ? "Switch to Chinese" : "切换至英文");
    }
    const themeBtn = document.getElementById("btn-theme");
    themeBtn.innerHTML = state.settings.theme === "light" ? IC.moon : IC.sun;
    themeBtn.title = state.settings.theme === "light"
      ? tr("切换至炭黑暗色主题", "Switch to Dark Theme")
      : tr("切换至极简瓷白主题", "Switch to Light Theme");
    if (typeof themeBtn.setAttribute === "function") {
      themeBtn.setAttribute("aria-label", themeBtn.title);
    }

    document.querySelectorAll("[data-i18n]").forEach((node) => {
      const k = node.getAttribute("data-i18n");
      if (dict[k]) node.textContent = dict[k];
    });

    document.querySelectorAll(".nav-btn").forEach((btn) => {
      const isCurrent = btn.dataset.route === state.route;
      btn.classList.toggle("active", isCurrent);
      if (typeof btn.setAttribute === "function") {
        if (isCurrent) {
          btn.setAttribute("aria-current", "page");
        } else if (typeof btn.removeAttribute === "function") {
          btn.removeAttribute("aria-current");
        } else {
          btn.setAttribute("aria-current", "false");
        }
      }
    });
    document.getElementById("badge-runs").textContent = state.runs.filter((r) => ["provisioning", "starting", "running", "ready"].includes(r.state)).length;
    document.getElementById("badge-sessions").textContent = state.sessions.length;
    document.getElementById("badge-tunnels").textContent = state.tunnels.length;
    document.getElementById("badge-receipts").textContent = state.receipts.length;
    syncSidebarDom();
    renderWsIndicator();
    renderGlobalAlert();
  }

  /* ========================== Views ========================== */

  function renderOverview() {
    const acc = state.activeAccount;
    const u = acc?.usage || {};
    const orphans = state.sessions.filter((s) => !s.managed);
    const activeRuns = state.runs.slice().reverse().slice(0, 5);

    const balText = (u.balance_ccu !== null && u.balance_ccu !== undefined) ? `${u.balance_ccu.toFixed(2)}` : tr("未知", "Unknown");
    const burnText = (u.burn_rate_hourly !== null && u.burn_rate_hourly !== undefined) ? `${u.burn_rate_hourly.toFixed(2)}` : "--";
    const hasHrs = u.hours_remaining !== null && u.hours_remaining !== undefined;
    const timeText = hasHrs ? `${u.hours_remaining}` : (u.burn_rate_hourly === 0 ? "INF" : "--");

    let html = `
      <div class="page-header">
        <div>
          <h1 class="page-title">${tr("算力与模型总览", "Compute & Model Overview")}</h1>
          <p class="page-subtitle">${tr("实时掌握多账号 CCU 消耗、未受管实例预警、声明式模型部署与即用即关回执。", "Live CCU telemetry, untracked runtime guardrails, declarative model deployment, and verified unassign receipts.")}</p>
        </div>
        <div class="row-actions">
          <button class="btn" type="button" data-action="refresh-usage">${IC.refresh} <span>${tr("刷新余额", "Refresh CCU")}</span></button>
          <button class="btn btn-primary" type="button" data-action="go" data-route="deploy"><span>${tr("部署新模型", "Deploy Model")}</span> ${IC.arrowRight}</button>
        </div>
      </div>
    `;

    if (orphans.length > 0) {
      html += `
        <div class="banner warning">
          <div>
            <div class="row-actions" style="gap:7px;margin-bottom:2px;">
              <span class="dot dot-amber"></span>
              <strong>${tr(`检测到 ${orphans.length} 个未受管 Colab 实例正在计费`, `${orphans.length} untracked Colab runtime(s) currently billing`)}</strong>
            </div>
            <div style="font-size:12px;color:var(--text-secondary);">
              ${tr("可能是浏览器打开或上次遗留的实例，未挂载自动关机看门狗。建议立即释放以免白扣算力单元。", "Opened outside this console with no VM auto-stop watchdog attached. Release immediately if unused.")}
            </div>
          </div>
          <div class="row-actions">
            ${orphans.map((o) => {
              const busy = isBusy(`session:stop:${o.endpoint}`);
              return `<button class="btn btn-danger btn-sm${busy ? " is-busy" : ""}" type="button" data-action="stop-session" data-endpoint="${esc(o.endpoint)}" data-account-id="${esc(o.account_id)}" data-session-name="${esc(o.name)}" data-accelerator="${esc(o.gpu_name || o.accelerator)}" ${busy ? "disabled" : ""}>${tr("释放", "Release")} ${esc(o.accelerator)} (${esc(o.name)})</button>`;
            }).join("")}
          </div>
        </div>
      `;
    }

    html += `
      <div class="kpi-grid">
        <div class="card">
          <div class="kpi-label">${tr("当前账号算力余额", "Compute Unit Balance")}</div>
          <div class="kpi-value">${esc(balText)} <small>CCU</small></div>
          <div class="kpi-foot">${acc ? esc(acc.email) : tr("请先在「多账号」页登录", "Sign in on Accounts tab")}</div>
        </div>
        <div class="card">
          <div class="kpi-label">${tr("实时消耗速率", "Hourly Burn Rate")}</div>
          <div class="kpi-value">${esc(burnText)} <small>CCU/h</small></div>
          <div class="kpi-foot">${tr("红线保护", "Balance floor")}: <span class="mono">${state.settings.guards?.balance_floor_ccu ?? 5} CCU</span></div>
        </div>
        <div class="card">
          <div class="kpi-label">${tr("预计可用续航", "Estimated Time Left")}</div>
          <div class="kpi-value">${esc(timeText)} <small>${timeText === "INF" ? tr("0 消耗", "0 burn") : "hours"}</small></div>
          <div class="kpi-foot">${tr("VM 看门狗默认限时", "Default auto-stop")}: <span class="mono">${state.settings.guards?.auto_stop_minutes ?? 120} min</span></div>
        </div>
        <div class="card">
          <div class="kpi-label">${tr("活跃实例 / 本地映射", "Active Runtimes / Tunnels")}</div>
          <div class="kpi-value">${state.sessions.length} <small>/ ${state.tunnels.length} ${tr("映射", "tunnels")}</small></div>
          <div class="kpi-foot">${state.sessions.length === 0 ? `<span class="ok-text">${tr("当前零计费消耗", "Zero active billing")}</span>` : tr("关闭窗口不会停止云端计费", "Closing app does not stop VM billing")}</div>
        </div>
      </div>

      <div class="section-title">
        <span>${tr("一键部署模板 (Model Boost Recipes)", "One-Click Model Deployment Recipes")}</span>
        <span class="mono card-hint">${state.recipes.length} ${tr("套内置配方", "RECIPES")}</span>
      </div>
      <div class="grid-2">
        ${state.recipes.map((r) => `
          <div class="card" style="display:flex;flex-direction:column;justify-content:space-between;gap:12px;">
            <div>
              <div class="row-actions" style="justify-content:space-between;margin-bottom:6px;">
                <strong style="font-size:14px;letter-spacing:-0.01em;">${esc(locObj(r.name))}</strong>
                <span class="tag accent mono">${esc(r.provision.accelerator)}${r.provision.min_vram_mib ? ` >=${Math.round(r.provision.min_vram_mib / 1024)}G` : ""}</span>
              </div>
              <p style="font-size:12.5px;color:var(--text-secondary);line-height:1.55;">${esc(locObj(r.description))}</p>
              <div class="preset-chips">
                ${(r.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}
                ${(r.keep || []).map((k) => `<span class="tag ok" title="${esc(k.path)}">${tr("保留", "Keep")}: ${esc(locObj(k.label) || k.path)}</span>`).join("")}
              </div>
            </div>
            <div class="row-actions" style="justify-content:flex-end;">
              <button class="btn btn-sm" type="button" data-action="pick-recipe" data-recipe-id="${esc(r.id)}">
                <span>${tr("配置并部署", "Configure & Deploy")}</span> ${IC.arrowRight}
              </button>
            </div>
          </div>
        `).join("")}
      </div>
    `;

    if (activeRuns.length > 0) {
      html += `
        <div class="section-title"><span>${tr("最近部署任务", "Recent Deployments")}</span></div>
        <div class="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>${tr("任务 ID", "Run ID")}</th>
                <th>${tr("模板", "Recipe")}</th>
                <th>${tr("绑定实例", "Session")}</th>
                <th>${tr("状态", "State")}</th>
                <th>${tr("最新进度 / 端点", "Status / Endpoint")}</th>
              </tr>
            </thead>
            <tbody>
              ${activeRuns.map((r) => `
                <tr>
                  <td><code>${esc(r.run_id)}</code></td>
                  <td>${esc(locObj(r.recipe_name))}</td>
                  <td class="mono">${esc(r.session_name)}</td>
                  <td><span class="tag ${r.state === "ready" ? "ok" : r.state === "failed" ? "err" : "accent"}">${esc(r.state)}</span></td>
                  <td>${esc(r.message)} ${r.endpoints?.[0] ? `— <a href="${esc(safeHttpUrl(r.endpoints[0].local_url))}" target="_blank" rel="noopener noreferrer" style="color:var(--text-primary);text-decoration:underline;"><code>${esc(r.endpoints[0].local_url)}</code></a>` : ""}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      `;
    }
    return html;
  }

  function ensureDeployDraft(recipe) {
    if (!state.deployDraft[recipe.id]) {
      const accel = recipe.provision.accelerator;
      const init = {
        _sessionMode: "new",
        _accelerator: accel,
        _minVram: recipe.provision.min_vram_mib || 0,
        _highMem: !!recipe.provision.high_mem || isHighMemOnlyAccelerator(accel),
      };
      for (const inp of recipe.inputs || []) {
        init[inp.id] = inp.default !== null && inp.default !== undefined ? inp.default : "";
      }
      state.deployDraft[recipe.id] = init;
    }
    return state.deployDraft[recipe.id];
  }

  function renderDeploySideRail(recipe, draft) {
    const acc = state.activeAccount;
    const bal = acc?.usage?.balance_ccu;
    const accel = draft._accelerator || recipe.provision.accelerator || "A100";
    const estBurn = getBurnRate(accel);
    const estHours = (bal !== null && bal !== undefined && estBurn > 0) ? (bal / estBurn).toFixed(1) : "--";
    const g = state.settings.guards || {};
    const managedSessions = state.sessions.filter((s) => s.managed);
    const orphans = state.sessions.filter((s) => !s.managed);

    return `
      <aside class="side-col">
        <div class="card">
          <div class="card-head">
            <span class="card-title">${tr("本次部署预算与护栏", "Deployment Budget & Guards")}</span>
            <span class="dot dot-emerald"></span>
          </div>
          <div class="metric-big">
            <div class="kpi-label">${tr("预计可用续航", "Estimated Runway")}</div>
            <div class="metric-val mono">${esc(estHours)} <small>hours</small></div>
            <div class="kpi-foot">${tr(`按当前余额与 ${esc(accel)} 约 ${estBurn.toFixed(2)} CCU/h 估算`, `Based on balance & ${esc(accel)} (~${estBurn.toFixed(2)} CCU/h)`)}</div>
          </div>
          <div class="kv-list">
            <div class="kv-row">
              <span>${tr("VM 内部看门狗限时", "VM Auto-Stop Watchdog")}</span>
              <span class="mono strong">${g.auto_stop_minutes ?? 120} min</span>
            </div>
            <div class="kv-row">
              <span>${tr("GPU 空闲自动释放", "GPU Idle Auto-Release")}</span>
              <span class="mono strong">${g.idle_stop_minutes ?? 30} min (&lt;5%)</span>
            </div>
            <div class="kv-row">
              <span>${tr("余额保护红线", "Account Balance Floor")}</span>
              <span class="mono strong">${Number(g.balance_floor_ccu ?? 5).toFixed(2)} CCU</span>
            </div>
            <div class="kv-row">
              <span>${tr("失败自动回滚释放", "Auto-Release on Failure")}</span>
              <span class="mono ok-text">${tr("已启用", "Enabled")}</span>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <span class="card-title">${tr("活跃实例与显存遥测", "Live Runtimes & VRAM")}</span>
            <span class="mono card-hint">${state.sessions.length} ONLINE</span>
          </div>
          ${managedSessions.length === 0 && orphans.length === 0 ? `
            <div class="kpi-foot">${tr("当前无在线实例（0 计费消耗）", "No active runtimes (0 CCU billing)")}</div>
          ` : ""}
          ${managedSessions.slice(0, 3).map((s) => {
            const pct = s.vram_total_mib ? Math.min(100, Math.round(((s.vram_used_mib || 0) / s.vram_total_mib) * 100)) : 0;
            const tun = (s.tunnels || [])[0];
            return `
              <div class="sess-mini">
                <div class="sess-mini-head">
                  <strong>${esc(s.name)}</strong>
                  <span class="mono card-hint">${esc(s.gpu_name || s.accelerator)}</span>
                </div>
                ${s.vram_total_mib ? `
                  <div>
                    <div class="sess-mini-head mono" style="font-size:11px;color:var(--text-secondary);margin-bottom:4px;">
                      <span>VRAM ${((s.vram_used_mib || 0) / 1024).toFixed(1)} / ${(s.vram_total_mib / 1024).toFixed(0)} GB</span>
                      <span>${pct}%</span>
                    </div>
                    <div class="progress-track" style="margin:0;"><div class="progress-fill" style="width:${pct}%"></div></div>
                  </div>
                ` : ""}
                <div class="sess-mini-foot">
                  <span class="mono" style="font-size:11px;color:var(--text-secondary);">${tun ? esc(tun.local_url.replace("http://", "")) : esc(s.endpoint)}</span>
                  <span class="${tun ? "ok-text" : "card-hint"}" style="font-size:11px;">${tun ? tr("隧道已连通", "Tunnel Open") : tr("待映射", "Idle")}</span>
                </div>
              </div>
            `;
          }).join("")}
          ${orphans.slice(0, 2).map((o) => {
            const busy = isBusy(`session:stop:${o.endpoint}`);
            return `
            <div class="orphan-box">
              <div class="orphan-head">
                <span class="dot dot-amber"></span>
                <span>${tr("发现未受管浏览器实例", "Untracked Browser Runtime")}</span>
              </div>
              <div class="mono" style="font-size:11px;color:var(--text-secondary);">${esc(o.name)} · ${esc(o.gpu_name || o.accelerator)}</div>
              <button class="btn btn-sm${busy ? " is-busy" : ""}" type="button" style="align-self:flex-start;" data-action="stop-session" data-endpoint="${esc(o.endpoint)}" data-account-id="${esc(o.account_id)}" data-session-name="${esc(o.name)}" data-accelerator="${esc(o.gpu_name || o.accelerator)}" ${busy ? "disabled" : ""}>${tr("立即释放并生成回执", "Release & Archive Receipt")}</button>
            </div>
          `;
          }).join("")}
        </div>

        ${(recipe.keep || []).length > 0 ? `
          <div class="card">
            <div class="card-head">
              <span class="card-title">${tr("自动归档保留目录", "Auto-Archived Keep Paths")}</span>
              <span class="mono card-hint">[[keep]]</span>
            </div>
            <div class="keep-list mono">
              ${recipe.keep.map((k) => `
                <div class="keep-item">
                  <span>${esc(k.path)}</span>
                  <span class="card-hint">${tr("关机前同步", "Pre-stop sync")}</span>
                </div>
              `).join("")}
            </div>
          </div>
        ` : ""}
      </aside>
    `;
  }

  function buildDeployGpuTiers() {
    const tierMeta = {
      G4: { name: "G4 Blackwell", vram: "96 GB", sub: "sm120 · High-RAM", minVram: 80000, highMem: true, tag: tr("视频推荐", "Video") },
      A100: { name: "A100 80GB", vram: "80 GB", sub: tr("自动退回 40G", "Auto-retry 40G"), minVram: 70000, highMem: true, tag: tr("锁显存", "80G Lock") },
      H100: { name: "H100 80GB", vram: "80 GB", sub: tr("Hopper · 旗舰", "Hopper · Flagship"), minVram: 70000, highMem: true, tag: tr("旗舰", "Flagship") },
      L4: { name: "L4", vram: "24 GB", sub: tr("Ada · High-RAM", "Ada · High-RAM"), minVram: 0, highMem: true, tag: "" },
      T4: { name: "T4", vram: "16 GB", sub: tr("轻量 / 音频", "Light / ASR"), minVram: 0, highMem: false, tag: "" },
    };
    const gpuSpecs = getAcceleratorSpecs().filter((sp) => sp.kind === "gpu" || tierMeta[sp.id]);
    const orderedIds = gpuSpecs.length > 0 ? gpuSpecs.map((sp) => sp.id) : ["G4", "A100", "H100", "L4", "T4"];
    return orderedIds
      .filter((id) => tierMeta[id] || getAcceleratorSpec(id)?.kind === "gpu")
      .map((id) => {
        const spec = getAcceleratorSpec(id);
        const meta = tierMeta[id] || {};
        const maxVramMib = spec && Array.isArray(spec.vram_mib) && spec.vram_mib.length > 0
          ? Math.max(...spec.vram_mib)
          : 0;
        const vramLabel = meta.vram || (maxVramMib > 0 ? `${Math.round(maxVramMib / 1024)} GB` : "--");
        const highMem = meta.highMem !== undefined ? (meta.highMem || isHighMemOnlyAccelerator(id)) : isHighMemOnlyAccelerator(id);
        return {
          id,
          name: meta.name || id,
          vram: vramLabel,
          sub: meta.sub || (highMem ? "High-RAM" : "Standard"),
          minVram: meta.minVram ?? 0,
          highMem,
          tag: meta.tag || "",
          burnRate: getBurnRate(id),
        };
      });
  }

  function formatAcceleratorOptionLabel(spec) {
    const id = spec.id;
    if (id === "NONE") return "CPU (NONE)";
    if (id === "G4") return "G4 Blackwell (96 GB)";
    if (id === "A100") return "A100 GPU (40/80 GB)";
    if (id === "H100") return "H100 GPU (80 GB)";
    if (id === "L4") return "L4 GPU (24 GB · High-RAM)";
    if (id === "T4") return "T4 GPU (16 GB)";
    if (id === "V5E1") return "TPU v5e-1 (High-RAM)";
    if (id === "V6E1") return "TPU v6e-1 (High-RAM)";
    return id;
  }

  function renderDeploy() {
    const recipe = state.recipes.find((r) => r.id === state.selectedRecipeId) || state.recipes[0];
    if (!recipe) return `<div class="card">${tr("未找到部署模板", "No deployment recipes found")}</div>`;
    const draft = ensureDeployDraft(recipe);

    const allInputs = recipe.inputs || [];
    const primaryInputs = allInputs.slice(0, 4);
    const advancedInputs = allInputs.slice(4);
    const advOpen = !!state.advancedOpen[recipe.id];
    const reusableSessions = getReusableSessions();
    if (draft._sessionMode !== "new" && !reusableSessions.find((s) => s.name === draft._sessionMode)) {
      draft._sessionMode = "new";
    }
    const firstWarm = reusableSessions[0] || null;
    const gpuTiers = buildDeployGpuTiers();
    const advItemCount = advancedInputs.length + (draft._sessionMode === "new" ? 4 : 1);

    let leftHtml = `
      <div class="recipe-strip">
        ${state.recipes.map((r) => `
          <button type="button" class="recipe-pill ${r.id === recipe.id ? "active" : ""}" data-action="pick-recipe" data-recipe-id="${esc(r.id)}">
            <span class="rp-title">${esc(locObj(r.name))}</span>
            <span class="rp-meta mono">${esc(r.provision.accelerator)} · ${esc((r.tags || [])[0] || r.category)}</span>
          </button>
        `).join("")}
      </div>

      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title" style="font-size:14.5px;">${esc(locObj(recipe.name))}</div>
            <div class="card-hint" style="margin-top:2px;">${esc(locObj(recipe.description))}</div>
          </div>
        </div>

        <form id="deploy-form" data-submit="deploy">
          <div class="card-head" style="margin-top:6px;margin-bottom:8px;">
            <span class="card-title">${tr("目标算力规格", "Target Compute Tier")}</span>
            <span class="mono card-hint">${tr("自动显存校验 · 不达标秒级退回", "Auto VRAM check · Instant release if unmet")}</span>
          </div>

          <div class="gpu-grid">
            ${gpuTiers.map((g) => {
              const isSel = draft._sessionMode === "new" && draft._accelerator === g.id;
              return `
                <button type="button" class="gpu-card ${isSel ? "selected" : ""}" data-action="pick-gpu-tier" data-recipe-id="${esc(recipe.id)}" data-accel="${esc(g.id)}" data-min-vram="${g.minVram}" data-high-mem="${g.highMem ? "1" : "0"}">
                  <div class="gpu-top">
                    <span class="gpu-name">${esc(g.name)}</span>
                    ${g.tag ? `<span class="tag">${esc(g.tag)}</span>` : ""}
                  </div>
                  <div class="gpu-vram mono">${esc(g.vram)} <small>VRAM</small></div>
                  <div class="gpu-foot mono">
                    <span>${esc(g.sub)}</span>
                    <span>~${g.burnRate.toFixed(2)}/h</span>
                  </div>
                </button>
              `;
            }).join("")}
            ${firstWarm ? `
              <button type="button" class="gpu-card ${draft._sessionMode !== "new" ? "selected" : ""}" data-action="update-draft-click" data-recipe-id="${esc(recipe.id)}" data-key="_sessionMode" data-val="${esc(firstWarm.name)}" data-rerender="1">
                <div class="gpu-top">
                  <span class="gpu-name">${tr("复用热实例", "Reuse Warm VM")}</span>
                  <span class="dot dot-emerald"></span>
                </div>
                <div class="gpu-vram mono">0s <small>${tr("冷启动", "warmup")}</small></div>
                <div class="gpu-foot mono">
                  <span>${esc(firstWarm.name)}</span>
                  <span class="ok-text">${tr("已就绪", "Ready")}</span>
                </div>
              </button>
            ` : ""}
          </div>

          <div class="divider"></div>

          <div class="form-grid">
            ${primaryInputs.map((inp) => renderRecipeInputField(recipe.id, inp, draft[inp.id])).join("")}
          </div>

          ${recipe.id === "llama-cpp-gguf" ? `<div id="gguf-planner-host">${renderGgufPlannerBox(draft)}</div>` : ""}

          <button type="button" class="disclosure-btn" data-action="toggle-advanced" data-recipe-id="${esc(recipe.id)}" aria-expanded="${advOpen ? "true" : "false"}" aria-controls="advanced-drawer-${esc(recipe.id)}">
            <span>${tr("高级参数与精细显存保底设置", "Advanced Recipe Inputs & Hardware Fine-Tuning")}</span>
            <span class="mono row-actions" style="gap:5px;">
              <span>${advOpen ? tr("收起", "Hide") : tr(`展开 ${advItemCount} 项`, `Show ${advItemCount}`)}</span>
              ${advOpen ? IC.chevronUp : IC.chevronDown}
            </span>
          </button>

          ${advOpen ? `
            <div class="advanced-drawer" id="advanced-drawer-${esc(recipe.id)}">
              <div class="form-grid">
                ${advancedInputs.map((inp) => renderRecipeInputField(recipe.id, inp, draft[inp.id])).join("")}
                <div class="field">
                  <label for="deploy-session-mode">${tr("运行位置", "Target Session")}</label>
                  <select id="deploy-session-mode" name="_sessionMode" data-change="draft" data-recipe-id="${esc(recipe.id)}" data-key="_sessionMode" data-rerender="1">
                    <option value="new" ${draft._sessionMode === "new" ? "selected" : ""}>${tr("新建专属 GPU 实例（失败自动释放）", "Create Fresh GPU Session")}</option>
                    ${reusableSessions.map((s) => `
                      <option value="${esc(s.name)}" ${draft._sessionMode === s.name ? "selected" : ""}>
                        ${tr("复用运行中", "Reuse Active")}: ${esc(s.name)} (${esc(s.gpu_name || s.accelerator)})
                      </option>
                    `).join("")}
                  </select>
                </div>
                ${draft._sessionMode === "new" ? `
                  <div class="field">
                    <label for="deploy-accelerator">${tr("加速器型号", "Accelerator")}</label>
                    <select id="deploy-accelerator" name="_accelerator" data-change="draft" data-recipe-id="${esc(recipe.id)}" data-key="_accelerator" data-rerender="1">
                      ${getAcceleratorSpecs().map((sp) => `
                        <option value="${esc(sp.id)}" ${draft._accelerator === sp.id ? "selected" : ""}>${esc(formatAcceleratorOptionLabel(sp))}</option>
                      `).join("")}
                    </select>
                  </div>
                  <div class="field">
                    <label for="deploy-min-vram">${tr("最低显存保底 (MiB，不达标自动重抽)", "Min VRAM MiB (Auto-Retry)")}</label>
                    <input type="number" id="deploy-min-vram" min="0" max="163840" step="1024" value="${draft._minVram !== null && draft._minVram !== undefined ? esc(draft._minVram) : ""}"
                      data-input="draft" data-change="draft" data-recipe-id="${esc(recipe.id)}" data-key="_minVram" data-val-type="number-optional" />
                  </div>
                  <div class="field">
                    <label for="deploy-high-mem">${tr("高内存机型 (High-RAM)", "High-RAM Shape")}</label>
                    <select id="deploy-high-mem" data-change="draft" data-recipe-id="${esc(recipe.id)}" data-key="_highMem" data-val-type="bool">
                      <option value="0" ${!draft._highMem ? "selected" : ""} ${isHighMemOnlyAccelerator(draft._accelerator) ? "disabled" : ""}>Standard RAM</option>
                      <option value="1" ${draft._highMem ? "selected" : ""}>High-RAM</option>
                    </select>
                  </div>
                ` : ""}
              </div>
            </div>
          ` : ""}

          <div class="row-actions" style="justify-content:flex-end;margin-top:16px;">
            <button type="submit" class="btn btn-primary${isBusy("deploy:submit") ? " is-busy" : ""}" ${isBusy("deploy:submit") ? "disabled" : ""}>
              <span>${isBusy("deploy:submit") ? tr("正在启动部署...", "Launching...") : tr("一键启动部署并自动映射", "Launch Deployment & Auto-Forward")}</span>
              ${IC.arrowRight}
            </button>
          </div>
        </form>
      </div>
    `;

    const runs = state.runs.slice().reverse();
    if (runs.length > 0) {
      for (const r of runs) {
        const rManifest = state.recipes.find((x) => x.id === r.recipe_id);
        const stages = rManifest?.stages || [];
        const curIdx = stages.findIndex((s) => s.id === r.stage);
        const pct = r.progress !== null && r.progress !== undefined ? Math.round(r.progress * 100) : (r.state === "ready" ? 100 : 15);
        const cancelBusy = isBusy(`run:cancel:${r.run_id}`);
        const syncBusy = isBusy(`run:sync:${r.run_id}`);
        leftHtml += `
          <div class="card">
            <div class="card-head">
              <div class="row-actions">
                <span class="dot ${r.state === "ready" ? "dot-emerald" : r.state === "failed" || r.state === "cancelled" ? "dot-danger" : "dot-amber"}"></span>
                <span class="card-title">${esc(locObj(r.recipe_name))}</span>
                <code>${esc(r.run_id)}</code>
                <code>${esc(r.session_name)}</code>
                <span class="tag ${r.state === "ready" ? "ok" : r.state === "failed" || r.state === "cancelled" ? "err" : "accent"}">${esc(r.state.toUpperCase())}</span>
              </div>
              <div class="row-actions">
                ${["provisioning", "starting", "running"].includes(r.state) ? `
                  <button class="btn btn-danger btn-sm${cancelBusy ? " is-busy" : ""}" type="button" data-action="cancel-run" data-run-id="${esc(r.run_id)}" data-session-name="${esc(r.session_name)}" ${cancelBusy ? "disabled" : ""}>${tr("取消并回收", "Cancel & Release")}</button>
                ` : ""}
                ${r.state === "ready" && r.endpoints?.length ? `
                  <button class="btn btn-primary btn-sm" type="button" data-action="go" data-route="tunnels">${tr("前往对话测试", "Open Playground")}</button>
                ` : ""}
              </div>
            </div>

            <div class="stage-pills">
              ${stages.map((st, idx) => {
                let cls = "";
                if (r.state === "ready" || curIdx > idx) cls = "done";
                else if (curIdx === idx && r.state === "running") cls = "current";
                return `<span class="stage-pill ${cls}"><span class="st-num">0${idx + 1}</span> ${esc(locObj(st.label))}</span>`;
              }).join("")}
            </div>

            <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
            <div style="font-size:12.5px;color:var(--text-secondary);margin-bottom:8px;">${esc(r.message)}</div>

            ${r.outputs && Object.keys(r.outputs).length > 0 ? `
              <div class="row-actions" style="margin-bottom:10px;">
                ${r.outputs.model_id ? `<span class="tag ok mono">model: ${esc(r.outputs.model_id)}</span>` : ""}
                ${r.outputs.quant ? `<span class="tag accent mono">${tr("量化", "Quant")}: ${esc(r.outputs.quant)}</span>` : ""}
                ${r.outputs.tok_per_s ? `<span class="tag ok mono">${tr("吞吐", "Speed")}: ~${esc(r.outputs.tok_per_s)} tok/s</span>` : ""}
                ${r.outputs.vram_used_mib ? `<span class="tag mono">VRAM: ${esc(r.outputs.vram_used_mib)} / ${esc(r.outputs.vram_total_mib)} MiB</span>` : ""}
              </div>
            ` : ""}

            ${(r.endpoints || []).map((ep) => `
              <div class="banner info" style="margin-bottom:10px;">
                <div class="row-actions">
                  ${IC.link}
                  <strong>${esc(tunLabel(ep))}</strong>
                  <code>${esc(ep.local_url)}</code>
                </div>
                <button class="btn btn-sm" type="button" data-action="copy-text" data-text="${esc(ep.local_url)}">${IC.copy} <span>${tr("复制 URL", "Copy URL")}</span></button>
              </div>
            `).join("")}

            ${(r.keep_paths || []).length > 0 ? `
              <div class="banner info" style="margin-bottom:10px;">
                <div>
                  <strong>${tr("保留成品目录", "Kept Artifact Paths")}:</strong>
                  ${r.keep_paths.map((kp) => `
                    <span class="tag ${kp.local_archive ? "ok" : ""}" style="margin-left:6px;">
                      ${esc(locObj(kp.label) || kp.path)}
                      ${kp.local_archive ? ` (${kp.file_count} ${tr("文件", "files")} · ${formatBytes(kp.total_bytes)})` : ` (${tr("未同步", "Not synced")})`}
                    </span>
                  `).join("")}
                </div>
                <div class="row-actions">
                  <button class="btn btn-sm${syncBusy ? " is-busy" : ""}" type="button" data-action="sync-keep" data-run-id="${esc(r.run_id)}" ${syncBusy ? "disabled" : ""}>${IC.refresh} <span>${tr("同步到本地", "Sync Locally")}</span></button>
                  ${r.keep_paths.map((kp, idx) => kp.local_archive ? `<a class="btn btn-primary btn-sm" href="/api/deployments/${encodeURIComponent(r.run_id)}/keep/download?index=${idx}">${IC.download} <span>${tr("下载归档", "Download Archive")}</span></a>` : "").join("")}
                </div>
              </div>
            ` : ""}

            ${r.log_tail?.length ? `<div class="terminal" data-run-id="${esc(r.run_id)}">${esc(r.log_tail.join("\n"))}</div>` : ""}
          </div>
        `;
      }
    }

    return `
      <div class="page-header">
        <div>
          <h1 class="page-title">${tr("模型极速部署", "Rapid Model Deployment")}</h1>
          <p class="page-subtitle">${tr("在 Colab GPU 上全自动完成并发环境配置、显存量化适配、看门狗挂载与本地 127.0.0.1 映射。", "Automated parallel bootstrap, VRAM quantization fitting, VM cost-guard watchdog, and localhost port forwarding.")}</p>
        </div>
      </div>
      <div class="main-split">
        <section class="primary-col">${leftHtml}</section>
        ${renderDeploySideRail(recipe, draft)}
      </div>
    `;
  }

  function renderRecipeInputField(recipeId, inp, val) {
    const label = locObj(inp.label);
    const help = locObj(inp.help);
    const fkey = `${recipeId}:${inp.id}`;
    const fieldId = `rf-${String(`${recipeId}-${inp.id}`).replace(/[^A-Za-z0-9_-]/g, "_")}`;
    let control = "";
    if (inp.type === "select") {
      control = `
        <select id="${esc(fieldId)}" data-field-key="${esc(fkey)}" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}">
          ${(inp.options || []).map((opt) => `<option value="${esc(opt)}" ${String(val) === String(opt) ? "selected" : ""}>${esc(opt)}</option>`).join("")}
        </select>
      `;
    } else if (inp.type === "bool") {
      const checked = val === true || val === "1" || val === "true";
      control = `
        <select id="${esc(fieldId)}" data-field-key="${esc(fkey)}" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}" data-val-type="bool">
          <option value="1" ${checked ? "selected" : ""}>${tr("是 (Enabled)", "Enabled")}</option>
          <option value="0" ${!checked ? "selected" : ""}>${tr("否 (Disabled)", "Disabled")}</option>
        </select>
      `;
    } else if (inp.type === "number") {
      const hasMin = inp.min !== null && inp.min !== undefined;
      const hasMax = inp.max !== null && inp.max !== undefined;
      const numVal = val !== null && val !== undefined ? esc(val) : "";
      control = `
        <input type="number" id="${esc(fieldId)}" data-field-key="${esc(fkey)}" value="${numVal}" ${hasMin ? `min="${inp.min}"` : ""} ${hasMax ? `max="${inp.max}"` : ""}
          data-input="draft" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}" data-val-type="number-optional" />
      `;
    } else if (inp.type === "secret") {
      const hasSecret = typeof val === "string" ? val.length > 0 : !!val;
      const secretPlaceholder = hasSecret
        ? tr("已在内存中暂存密钥（输入新值覆盖，或清空）", "Secret entered in memory (type to replace)")
        : tr("留空表示不启用密钥验证", "Leave blank if no API key required");
      control = `
        <input type="password" id="${esc(fieldId)}" autocomplete="off" data-field-key="${esc(fkey)}" data-secret-field="1" placeholder="${esc(secretPlaceholder)}"
          data-input="draft" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}" />
      `;
    } else if (inp.type === "hf-gguf") {
      control = `
        <div class="row-actions" style="width:100%;">
          <input type="text" id="${esc(fieldId)}" data-field-key="${esc(fkey)}" style="flex:1;" value="${esc(val)}" placeholder="owner/repo-GGUF"
            data-input="draft" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}" />
          <button type="button" class="btn btn-sm" data-action="analyze-gguf">${IC.search} <span>${tr("测算显存适配", "Analyze VRAM Fit")}</span></button>
        </div>
        <div class="preset-chips">
          ${state.curatedRepos.map((repo) => `<button type="button" class="chip mono" data-action="pick-curated-repo" data-repo="${esc(repo)}">${esc(repo)}</button>`).join("")}
        </div>
      `;
    } else if (recipeId === "llama-cpp-gguf" && inp.id === "quant") {
      const discovered = (state.ggufPlan?.variants || []).map((v) => v.quant);
      const suggestions = Array.from(new Set(["auto", ...discovered, "Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "UD-Q4_K_XL", "IQ4_XS", "Q3_K_M", "BF16"]));
      control = `
        <input type="text" id="${esc(fieldId)}" list="quant-datalist" data-field-key="${esc(fkey)}" value="${esc(val)}"
          data-input="draft" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}" />
        <datalist id="quant-datalist">
          ${suggestions.map((q) => `<option value="${esc(q)}"></option>`).join("")}
        </datalist>
      `;
    } else {
      control = `
        <input type="text" id="${esc(fieldId)}" data-field-key="${esc(fkey)}" value="${esc(val)}"
          data-input="draft" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}" />
      `;
    }

    return `
      <div class="field ${inp.type === "hf-gguf" ? "full" : ""}">
        <label for="${esc(fieldId)}">${esc(label)}${inp.required ? " *" : ""}</label>
        ${control}
        ${help ? `<span class="field-help">${esc(help)}</span>` : ""}
      </div>
    `;
  }

  function extractGgufWarnings(plan) {
    if (!plan || typeof plan !== "object") return [];
    const seen = new Set();
    const warnings = [];
    const pushMsg = (msg) => {
      const cleaned = String(msg ?? "").trim();
      if (cleaned && !seen.has(cleaned)) {
        seen.add(cleaned);
        warnings.push(cleaned);
      }
    };
    if (typeof plan.header_warning === "string") pushMsg(plan.header_warning);
    if (Array.isArray(plan.warnings)) {
      for (const item of plan.warnings) {
        if (typeof item === "string") pushMsg(item);
        else if (item && typeof item === "object") pushMsg(item.message || item.msg || item.warning || "");
      }
    } else if (typeof plan.warnings === "string") {
      pushMsg(plan.warnings);
    }
    if (Array.isArray(plan.hardware_matrix)) {
      for (const row of plan.hardware_matrix) {
        if (row && typeof row.header_warning === "string") pushMsg(row.header_warning);
      }
    }
    const truncated =
      plan.header_truncated === true ||
      (Array.isArray(plan.hardware_matrix) && plan.hardware_matrix.some((row) => row && row.header_truncated === true));
    if (warnings.length === 0 && truncated) {
      pushMsg(
        tr(
          "GGUF 文件头元数据在探测上限处被截断，缺失的架构/KV 字段已按保守默认值估算。",
          "GGUF metadata header was truncated at the probe limit; missing architecture/KV fields fall back to conservative defaults."
        )
      );
    }
    return warnings;
  }

  function renderGgufPlannerBox() {
    if (state.ggufPlanLoading) {
      return `<div class="banner info" role="status" style="margin-top:14px;">${tr("正在读取 Hugging Face GGUF 仓库与二进制文件头 KV 元数据...", "Inspecting Hugging Face GGUF repository & binary header KV metadata...")}</div>`;
    }
    const p = state.ggufPlan;
    if (!p) return "";
    const ggufWarnings = extractGgufWarnings(p);
    return `
      <div class="card" style="margin-top:14px;background:var(--bg-input);">
        <div class="card-head">
          <span class="card-title">${tr("GGUF 多卡显存自动适配矩阵", "GGUF Multi-GPU VRAM Fit Matrix")} — <code>${esc(p.repo)}</code></span>
          <span class="tag accent mono">${tr("架构", "Arch")}: ${esc(p.architecture || "general")} | Max Ctx: ${esc(p.model_ctx || 32768)}</span>
        </div>
        ${ggufWarnings.length > 0 ? `
          <div class="banner warning gguf-warning-banner" role="status" style="margin-bottom:12px;font-size:12px;">
            <div style="display:flex;flex-direction:column;gap:4px;">
              <div class="row-actions" style="gap:7px;">
                <span class="dot dot-amber" aria-hidden="true"></span>
                <strong>${tr("GGUF 元数据降级提示", "GGUF Metadata Fallback Notice")}</strong>
              </div>
              ${ggufWarnings.map((w) => `<div style="color:var(--text-secondary);">${esc(w)}</div>`).join("")}
            </div>
          </div>
        ` : ""}
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>${tr("Colab GPU 规格", "Colab GPU Tier")}</th>
                <th>${tr("推荐量化档位", "Best Quantization")}</th>
                <th>${tr("KV 缓存", "KV Cache")}</th>
                <th>${tr("可用上下文", "Context")}</th>
                <th>${tr("预计显存 / 预算", "Est. VRAM / Budget")}</th>
                <th>${tr("快捷选用", "Action")}</th>
              </tr>
            </thead>
            <tbody>
              ${(p.hardware_matrix || []).map((row) => `
                <tr>
                  <td><strong>${esc(row.gpu_label)}</strong></td>
                  <td>${row.fits ? `<span class="tag ok mono">${esc(row.quant)} (${row.size_gib} GiB)</span>` : `<span class="tag err">${tr("放不下", "Too large")}</span>`}</td>
                  <td class="mono">${row.fits ? esc(row.kv) : "--"}</td>
                  <td class="mono">${row.fits ? esc(row.ctx) : "--"}</td>
                  <td class="mono">${row.fits ? `${row.estimate_mib} / ${row.budget_mib} MiB` : esc(row.reason || "")}</td>
                  <td>
                    ${row.fits ? `
                      <button type="button" class="btn btn-sm" data-action="apply-hw-row" data-accel="${esc(row.accelerator)}" data-min-vram="${row.vram_mib >= 70000 ? 70000 : 0}">
                        ${tr("选用此卡", "Select GPU")}
                      </button>
                    ` : ""}
                  </td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  function renderSessions() {
    const activeOps = state.provisionOps.filter((o) => ["allocating", "probing", "retrying", "watchdog"].includes(o.state));
    return `
      <div class="page-header">
        <div>
          <h1 class="page-title">${tr("算力实例管理", "Colab GPU Sessions")}</h1>
          <p class="page-subtitle">${tr("统一管理全部账号下的受管实例与未受管孤儿实例，支持延长看门狗、端口转发与核验释放。", "Manage all runtimes across accounts, including untracked browser sessions, VM watchdogs, and instant unassign.")}</p>
        </div>
        <div class="row-actions">
          <button class="btn" type="button" data-action="refresh-bootstrap">${IC.refresh} <span>${tr("刷新列表", "Refresh")}</span></button>
          <button class="btn btn-primary" type="button" data-action="open-new-session">${IC.plus} <span>${tr("新建纯净实例", "New GPU Session")}</span></button>
        </div>
      </div>

      ${activeOps.map((op) => {
        const cancelProvBusy = isBusy(`provision:cancel:${op.op_id}`);
        return `
        <div class="banner info">
          <div>
            <strong>${tr("正在分配实例", "Provisioning")} <code>${esc(op.session_name)}</code> (${esc(op.accelerator)} · ${op.attempt}/${op.max_attempts})</strong>
            <div style="font-size:12px;color:var(--text-secondary);">${esc(op.message)}</div>
          </div>
          <button class="btn btn-danger btn-sm${cancelProvBusy ? " is-busy" : ""}" type="button" data-action="cancel-provision" data-op-id="${esc(op.op_id)}" ${cancelProvBusy ? "disabled" : ""}>${tr("取消分配 (0 残留)", "Cancel & Release")}</button>
        </div>
      `;
      }).join("")}

      ${state.sessions.length === 0 ? `
        <div class="card" style="text-align:center;padding:42px;">
          <div style="font-size:15px;font-weight:600;margin-bottom:6px;">${tr("当前没有任何运行中的 Colab 实例（0 计费消耗）", "No active Colab runtimes (0 CCU billing)")}</div>
          <div style="font-size:12.5px;color:var(--text-secondary);margin-bottom:18px;">${tr("可直接前往「极速部署模型」一键拉起推理服务，或点击右上角单独申请 GPU 实例。", "Head to Quick Deploy to launch a model server in one click, or allocate a standalone GPU session.")}</div>
          <button class="btn btn-primary" type="button" data-action="go" data-route="deploy"><span>${tr("前往部署模型", "Deploy a Model")}</span> ${IC.arrowRight}</button>
        </div>
      ` : `
        <div class="grid-2">
          ${state.sessions.map((s) => {
            const vramPct = s.vram_total_mib ? Math.min(100, Math.round(((s.vram_used_mib || 0) / s.vram_total_mib) * 100)) : 0;
            const extBusy = isBusy(`session:extend:${s.name}`);
            const stopBusy = isBusy(`session:stop:${s.endpoint}`);
            return `
              <div class="card" style="display:flex;flex-direction:column;gap:12px;">
                <div class="row-actions" style="justify-content:space-between;">
                  <div class="row-actions" style="gap:7px;">
                    <span class="dot ${s.managed ? "dot-emerald" : "dot-amber"}"></span>
                    <strong style="font-size:15px;">${esc(s.name)}</strong>
                    <span class="tag ${s.managed ? "ok" : "warn"}">${s.managed ? tr("受管", "Managed") : tr("未受管孤儿", "Untracked")}</span>
                  </div>
                  <span class="tag accent mono">${esc(s.gpu_name || s.accelerator)} · ${esc(s.machine_shape)}</span>
                </div>

                <div style="font-size:12px;color:var(--text-secondary);">
                  ${tr("账号", "Account")}: <strong>${esc(s.account_email)}</strong> · Endpoint: <code>${esc(s.endpoint)}</code>
                </div>

                ${s.vram_total_mib ? `
                  <div>
                    <div class="mono" style="display:flex;justify-content:space-between;font-size:11.5px;color:var(--text-secondary);">
                      <span>${tr(`GPU 显存 (${s.gpu_util_percent ?? 0}% 核心利用率)`, `GPU VRAM (${s.gpu_util_percent ?? 0}% Util)`)}</span>
                      <span>${s.vram_used_mib ?? 0} / ${s.vram_total_mib} MiB (${vramPct}%)</span>
                    </div>
                    <div class="progress-track"><div class="progress-fill" style="width:${vramPct}%"></div></div>
                  </div>
                ` : ""}

                <div class="kv-row mono" style="font-size:12px;background:var(--bg-input);border:1px solid var(--border-subtle);padding:9px 11px;border-radius:var(--radius-sm);">
                  <span>${tr("预估速率", "Est. Burn")}: <strong>~${formatCcu(s.estimated_burn_hourly)} CCU/h</strong> (${tr("累计", "Spent")} ~${formatCcu(s.estimated_spent_ccu)} CCU)</span>
                  <span>${tr("看门狗倒计时", "Watchdog")}: <strong data-watchdog-deadline="${s.watchdog?.deadline_epoch ?? ""}">${formatRemainingDeadline(s.watchdog?.deadline_epoch)}</strong></span>
                </div>

                ${s.watchdog?.warning ? `<div style="font-size:12px;color:var(--warning);">${esc(s.watchdog.warning)}</div>` : ""}

                <div class="row-actions" style="justify-content:flex-end;">
                  <button class="btn btn-sm${extBusy ? " is-busy" : ""}" type="button" data-action="extend-session" data-session-name="${esc(s.name)}" ${extBusy ? "disabled" : ""}>${IC.clock} <span>${tr("延长 +2h", "Extend +2h")}</span></button>
                  <button class="btn btn-sm" type="button" data-action="open-forward" data-session-name="${esc(s.name)}">${IC.link} <span>${tr("映射端口", "Forward Port")}</span></button>
                  <button class="btn btn-danger btn-sm${stopBusy ? " is-busy" : ""}" type="button" data-action="stop-session" data-endpoint="${esc(s.endpoint)}" data-account-id="${esc(s.account_id)}" data-session-name="${esc(s.name)}" data-accelerator="${esc(s.gpu_name || s.accelerator)}" ${stopBusy ? "disabled" : ""}>${IC.stop} <span>${tr("停止并结束计费", "Stop & End Billing")}</span></button>
                </div>
              </div>
            `;
          }).join("")}
        </div>
      `}
    `;
  }

  function renderTunnelsAndChat() {
    const openTunnels = state.tunnels
      .filter((t) => t.status === "open")
      .slice()
      .sort((a, b) => (a.kind === "openai" ? 0 : 1) - (b.kind === "openai" ? 0 : 1));
    if (state.selectedChatTunnelId && !openTunnels.find((t) => t.id === state.selectedChatTunnelId)) {
      state.selectedChatTunnelId = "";
    }
    const activeTunnelId = state.selectedChatTunnelId || (openTunnels[0]?.id || "");
    const isReal = state.settings.backend_mode === "real";
    const noRealTunnel = isReal && openTunnels.length === 0;
    const forwardableSession = getReusableSessions()[0] || state.sessions.find((s) => s.status !== "stopping" && s.status !== "lost") || null;
    const welcomeText = tr(
      "部署好 GGUF 或 vLLM 模型并开启本地映射后，可直接在此测试推理延迟与吞吐速度 (tok/s)。",
      "Deploy a GGUF or vLLM model and forward its local port to benchmark inference latency and throughput (tok/s) here."
    );
    const apiKeyPlaceholder = state.chatApiKey
      ? tr("API Key（已输入）", "API Key (entered)")
      : tr("API Key (可选)", "API Key (optional)");
    return `
      <div class="page-header">
        <div>
          <h1 class="page-title">${tr("本地端口映射与对话演练场", "Localhost Port Forwards & API Playground")}</h1>
          <p class="page-subtitle">${tr("通过原生 OpenSSH + Colab WebSocket 多路复用将云端端口安全映射至 127.0.0.1（不修改 ~/.ssh/config）。", "Multiplexes Colab VM ports to 127.0.0.1 via native OpenSSH over WebSocket without editing ~/.ssh/config.")}</p>
        </div>
        ${forwardableSession ? `<button class="btn btn-primary" type="button" data-action="open-forward" data-session-name="${esc(forwardableSession.name)}">${IC.plus} <span>${tr("新建端口映射", "New Port Forward")}</span></button>` : ""}
      </div>

      <div class="banner warning">
        <div class="row-actions" style="gap:8px;">
          <span class="dot dot-amber"></span>
          <span><strong>${tr("计费提醒：", "Billing Notice:")}</strong> ${tr("关闭本地端口映射隧道<strong>不会</strong>停止云端 Colab GPU 计费。用完后请点击「停止并结束计费」。", "Closing a local port forward does <strong>NOT</strong> stop Colab GPU billing. Click 'Stop & End Billing' when done.")}</span>
        </div>
      </div>

      <div class="card table-wrap" style="margin-bottom:16px;">
        <div class="card-head">
          <span class="card-title">${tr("活跃的本地端口映射", "Active Localhost Port Forwards")}</span>
          <span class="mono card-hint">${state.tunnels.length} ACTIVE</span>
        </div>
        ${state.tunnels.length === 0 ? `<div class="kpi-foot">${tr("暂无活跃映射。部署模型就绪后会自动开启映射，也可在实例卡片上手动映射任意端口（禁填 8080）。", "No active port forwards. Endpoints auto-forward when a deployment reaches READY, or click Forward Port on any session (avoid 8080).")}</div>` : `
          <table>
            <thead>
              <tr>
                <th>${tr("实例", "Session")}</th>
                <th>${tr("说明", "Label")}</th>
                <th>${tr("云端端口 -> 本地地址", "VM Port -> Local URL")}</th>
                <th>${tr("状态", "Status")}</th>
                <th>${tr("操作", "Actions")}</th>
              </tr>
            </thead>
            <tbody>
              ${state.tunnels.map((t) => `
                <tr>
                  <td class="mono"><strong>${esc(t.session_name)}</strong></td>
                  <td>${esc(tunLabel(t))}</td>
                  <td><code>VM:${t.remote_port}</code> -> <a href="${esc(safeHttpUrl(t.local_url))}" target="_blank" rel="noopener noreferrer" style="color:var(--text-primary);text-decoration:underline;"><code>${esc(t.local_url)}</code></a></td>
                  <td><span class="tag ${t.status === "open" ? "ok" : "err"}">${esc(t.status)}</span></td>
                  <td class="row-actions">
                    <button class="btn btn-sm" type="button" data-action="copy-text" data-text="${esc(t.local_url)}">${IC.copy} <span>${tr("复制", "Copy")}</span></button>
                    <button class="btn btn-danger btn-sm" type="button" data-action="close-tunnel" data-tunnel-id="${esc(t.id)}">${tr("断开", "Disconnect")}</button>
                  </td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        `}
      </div>

      <div class="card">
        <div class="card-head" style="flex-wrap:wrap;">
          <span class="card-title">${tr("OpenAI 兼容推理对话测速 (Chat Playground)", "OpenAI-Compatible Chat & Speed Playground")}</span>
          <div class="row-actions" style="flex-wrap:wrap;">
            <select id="chat-tunnel-select" aria-label="${tr("选择本地推理隧道", "Select local inference tunnel")}" data-change="chat-tunnel" style="padding:6px 10px;border-radius:var(--radius-sm);background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-subtle);font-size:12px;">
              ${openTunnels.length > 0
                ? openTunnels.map((t) => `<option value="${esc(t.id)}" ${t.id === activeTunnelId ? "selected" : ""}>${esc(t.session_name)} — ${esc(t.local_url)}</option>`).join("")
                : (isReal
                  ? `<option value="">${tr("请先开启/绑定本地隧道", "Please open or bind a local tunnel first")}</option>`
                  : `<option value="">${tr("默认沙盒端点 (127.0.0.1:8000/v1)", "Default Sandbox (127.0.0.1:8000/v1)")}</option>`)}
            </select>
            <input type="text" id="chat-model-input" aria-label="${tr("模型 ID", "Model ID")}" value="${esc(state.chatModel)}" data-input="chat-meta" data-meta-field="chatModel"
              placeholder="model (auto)" title="${tr("模型名，auto 自动识别已部署模型", "Model ID; 'auto' detects active model")}"
              style="width:130px;padding:6px 9px;border-radius:var(--radius-sm);background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-subtle);font-size:12px;" />
            <input type="password" autocomplete="off" id="chat-apikey-input" aria-label="${tr("推理服务 API Key", "Inference API Key")}" data-input="chat-meta" data-meta-field="chatApiKey"
              placeholder="${esc(apiKeyPlaceholder)}"
              style="width:135px;padding:6px 9px;border-radius:var(--radius-sm);background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-subtle);font-size:12px;" />
          </div>
        </div>
        ${noRealTunnel ? `
          <div class="banner info" role="status" style="margin-bottom:12px;font-size:12px;">
            <span>${tr("真实 Colab 模式下暂无可用本地隧道：请先完成模型部署或点击上方「新建端口映射」绑定本地端口后再发起推理对话。", "No open local tunnel in Real Colab Mode: deploy a model or open a local port forward above before sending prompts.")}</span>
          </div>
        ` : ""}
        <div class="chat-box" id="chat-box" role="log" aria-live="polite" aria-label="${tr("推理对话记录", "Chat transcript")}">
          ${state.chatMessages.map((m) => {
            const cls = `chat-msg ${esc(m.role)}${m._error ? " error" : ""}`;
            const text = esc(m._welcome ? welcomeText : m.content);
            const metaHtml = m.meta
              ? `<div class="chat-msg-meta mono">— ${esc(m.meta.latency_ms)} ms | ${esc(m.meta.tok_per_s)} tok/s</div>`
              : "";
            return `<div class="${cls}"><div>${text}</div>${metaHtml}</div>`;
          }).join("")}
        </div>
        <form class="row-actions" data-submit="chat">
          <input type="text" id="chat-input" aria-label="${tr("测试提示词输入", "Test prompt input")}" value="${esc(state.chatDraft)}" data-input="chat-meta" data-meta-field="chatDraft"
            style="flex:1;padding:9px 11px;border-radius:var(--radius-sm);border:1px solid var(--border-subtle);background:var(--bg-input);color:var(--text-primary);font-size:13px;"
            placeholder="${noRealTunnel ? tr("请先开启/绑定本地隧道...", "Open or bind a local tunnel first...") : tr("输入测试提示词以实测延迟与 tok/s...", "Enter a test prompt to measure latency & tok/s...")}" ${state.chatBusy || noRealTunnel ? "disabled" : ""} />
          <button type="submit" class="btn btn-primary" ${state.chatBusy || noRealTunnel ? "disabled" : ""}>${IC.send} <span>${state.chatBusy ? tr("生成中...", "Generating...") : tr("发送测试", "Send")}</span></button>
        </form>
      </div>
    `;
  }

  function renderAccountsAndReceipts() {
    return `
      <div class="page-header">
        <div>
          <h1 class="page-title">${tr("多 Google 账号与即用即关回执", "Multi-Account Vault & Unassign Receipts")}</h1>
          <p class="page-subtitle">${tr("每个 Google 账号凭证在 OS Keyring 中独立隔离保存，同屏实时观测全部账号余额与落盘关闭回执。", "Per-account OAuth tokens isolated in OS Credential Manager; view all balances and verifiable unassign receipts.")}</p>
        </div>
        <div class="row-actions">
          ${state.settings.backend_mode === "real" ? `<button class="btn" type="button" data-action="import-cli-account">${IC.download} <span>${tr("导入 colab-cli 登录态", "Import colab-cli Token")}</span></button>` : ""}
          <button class="btn btn-primary" type="button" data-action="open-login">${IC.plus} <span>${tr("添加 Google 账号", "Add Google Account")}</span></button>
        </div>
      </div>

      <div class="card table-wrap" style="margin-bottom:16px;">
        <div class="card-head">
          <span class="card-title">${tr("已保存的 Google 账号", "Saved Google Accounts")}</span>
          <span class="mono card-hint">${state.accounts.length} SAVED</span>
        </div>
        ${state.accounts.length === 0 ? `<div class="kpi-foot">${tr("尚未添加账号。点击右上角「添加 Google 账号」完成网页授权。", "No accounts saved yet. Click 'Add Google Account' to authorize.")}</div>` : `
          <table>
            <thead>
              <tr>
                <th>${tr("账号邮箱", "Account Email")}</th>
                <th>${tr("状态", "Status")}</th>
                <th>${tr("算力余额 (CCU)", "Balance (CCU)")}</th>
                <th>${tr("消耗速率", "Burn Rate")}</th>
                <th>${tr("活跃实例", "Assignments")}</th>
                <th>${tr("操作", "Actions")}</th>
              </tr>
            </thead>
            <tbody>
              ${state.accounts.map((a) => {
                const remBusy = isBusy(`account:remove:${a.id}`);
                const swBusy = isBusy(`account:switch:${a.id}`);
                return `
                <tr>
                  <td><strong>${esc(a.email)}</strong> <div style="font-size:11.5px;color:var(--text-muted);">${esc(a.display_name)}</div></td>
                  <td>${a.is_active ? `<span class="tag ok">${tr("当前激活", "Active")}</span>` : `<span class="tag">${tr("备用", "Standby")}</span>`}</td>
                  <td class="mono"><strong>${a.usage?.balance_ccu !== null && a.usage?.balance_ccu !== undefined ? `${a.usage.balance_ccu.toFixed(2)} CCU` : tr("未知", "Unknown")}</strong></td>
                  <td class="mono">${a.usage?.burn_rate_hourly !== null && a.usage?.burn_rate_hourly !== undefined ? `${a.usage.burn_rate_hourly.toFixed(2)} /h` : "--"}</td>
                  <td class="mono">${a.usage?.assignments_count ?? "--"}</td>
                  <td class="row-actions">
                    ${!a.is_active ? `<button class="btn btn-sm${swBusy ? " is-busy" : ""}" type="button" data-action="switch-account" data-account-id="${esc(a.id)}" ${swBusy ? "disabled" : ""}>${tr("切换至此账号", "Switch Active")}</button>` : ""}
                    <button class="btn btn-danger btn-sm${remBusy ? " is-busy" : ""}" type="button" data-action="remove-account" data-account-id="${esc(a.id)}" data-email="${esc(a.email)}" ${remBusy ? "disabled" : ""}>${tr("移除", "Remove")}</button>
                  </td>
                </tr>
              `;
              }).join("")}
            </tbody>
          </table>
        `}
      </div>

      <div class="card table-wrap">
        <div class="card-head">
          <span class="card-title">${tr("即用即关释放回执库", "Verified Unassign Receipts")}</span>
          <div class="row-actions" style="gap:8px;">
            ${state.receipts.length > 0 ? `<span class="mono card-hint" id="receipts-count-summary">${tr(
              `已显示 ${Math.min(state.receipts.length, Math.max(RECEIPTS_PAGE_SIZE, Number(state.receiptsVisibleLimit || RECEIPTS_PAGE_SIZE)))} / 共 ${state.receipts.length} 条`,
              `Showing ${Math.min(state.receipts.length, Math.max(RECEIPTS_PAGE_SIZE, Number(state.receiptsVisibleLimit || RECEIPTS_PAGE_SIZE)))} of ${state.receipts.length}`
            )}</span>` : ""}
            <code>data/receipts/</code>
          </div>
        </div>
        ${(() => {
          const totalReceipts = state.receipts.length;
          if (totalReceipts === 0) {
            return `<div class="kpi-foot">${tr("暂无释放回执记录。每次手动关机、显存重抽或护栏触发释放实例均会在此生成可核验回执。", "No unassign receipts yet. Every manual stop, VRAM retry release, and cost-guard stop writes a verifiable receipt here.")}</div>`;
          }
          const visibleLimit = Math.max(RECEIPTS_PAGE_SIZE, Number(state.receiptsVisibleLimit || RECEIPTS_PAGE_SIZE));
          const visibleReceipts = state.receipts.slice(0, visibleLimit);
          const shownCount = visibleReceipts.length;
          const remainingCount = Math.max(0, totalReceipts - shownCount);
          const nextStep = Math.min(RECEIPTS_PAGE_SIZE, remainingCount);
          return `
            <table>
              <thead>
                <tr>
                  <th>${tr("时间 (UTC)", "Timestamp (UTC)")}</th>
                  <th>${tr("会话 / Endpoint", "Session / Endpoint")}</th>
                  <th>GPU</th>
                  <th>${tr("账号", "Account")}</th>
                  <th>${tr("释放原因", "Reason")}</th>
                  <th>${tr("释放后余额", "Balance After")}</th>
                  <th>${tr("核验状态", "Verified")}</th>
                </tr>
              </thead>
              <tbody>
                ${visibleReceipts.map((rc) => `
                  <tr>
                    <td class="mono" style="font-size:11.5px;">${esc((rc.timestamp || "").replace("T", " ").slice(0, 19))}</td>
                    <td><strong>${esc(rc.session_name)}</strong><br/><code style="font-size:11px;">${esc(rc.endpoint)}</code></td>
                    <td><span class="tag mono">${esc(rc.accelerator)}</span></td>
                    <td>${esc(rc.account_email)}</td>
                    <td>${esc(rc.reason)}</td>
                    <td class="mono">${rc.balance_after_ccu !== null && rc.balance_after_ccu !== undefined ? `${rc.balance_after_ccu.toFixed(2)} CCU` : "--"}</td>
                    <td><span class="tag ${rc.verified_unassigned ? "ok" : "warn"}">${rc.verified_unassigned ? tr("已确认释放", "Verified") : tr("待确认", "Pending")}</span></td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
            ${totalReceipts > RECEIPTS_PAGE_SIZE ? `
              <div class="receipts-pager">
                <span class="card-hint mono">
                  ${remainingCount > 0
                    ? tr(`当前显示 ${shownCount} / ${totalReceipts} 条回执（还有 ${remainingCount} 条）`, `Showing ${shownCount} of ${totalReceipts} receipts (${remainingCount} more)`)
                    : tr(`已展开全部 ${totalReceipts} 条回执`, `Showing all ${totalReceipts} receipts`)}
                </span>
                <div class="row-actions">
                  ${remainingCount > 0 ? `
                    <button type="button" class="btn btn-sm" data-action="receipts-more">${tr(`显示更多 (+${nextStep})`, `Show More (+${nextStep})`)}</button>
                    <button type="button" class="btn btn-sm btn-primary" data-action="receipts-all">${tr(`展开全部 (${totalReceipts})`, `Show All (${totalReceipts})`)}</button>
                  ` : `
                    <button type="button" class="btn btn-sm" data-action="receipts-collapse">${tr(`收起至前 ${RECEIPTS_PAGE_SIZE} 条`, `Collapse to ${RECEIPTS_PAGE_SIZE}`)}</button>
                  `}
                </div>
              </div>
            ` : ""}
          `;
        })()}
      </div>
    `;
  }

  function getGuardDraftVal(key, fallback) {
    if (Object.prototype.hasOwnProperty.call(state.settingsDraft, key)) {
      return state.settingsDraft[key];
    }
    const g = state.settings.guards || {};
    return g[key] ?? fallback;
  }

  function renderSettings() {
    const dirty = hasDirtySettingsDraft();
    const hfDraftSet = !!(state.secretDrafts && state.secretDrafts.hf_token);
    const civDraftSet = !!(state.secretDrafts && state.secretDrafts.civitai_token);
    return `
      <div class="page-header">
        <div>
          <h1 class="page-title">${tr("双层成本护栏与密钥保险箱", "Dual-Layer Cost Guards & Secret Vault")}</h1>
          <p class="page-subtitle">${tr("VM 侧看门狗在关闭本机控制台后依然生效；算力余额红线与单实例消费上限在控制台运行期间实时巡检。", "VM-side watchdog persists after closing the app; balance floor & spend cap run continuously while open.")}</p>
        </div>
      </div>

      <div class="grid-2">
        <div class="card">
          <div class="card-head">
            <span class="card-title">${tr("算力成本护栏策略", "Compute Cost Guard Policies")}</span>
            ${dirty ? `<span class="tag warn">${tr("有未保存修改", "Unsaved changes")}</span>` : ""}
          </div>
          <form data-submit="guard-settings" class="form-grid">
            <div class="field">
              <label for="set-autostop">${tr("默认 VM 自动关机限时 (分钟，0=不限时)", "Default VM Auto-Stop (minutes, 0=unlimited)")}</label>
              <input type="number" id="set-autostop" min="0" max="1440" value="${esc(getGuardDraftVal("auto_stop_minutes", 120))}"
                data-input="settings-guard" data-change="settings-guard" data-guard-key="auto_stop_minutes" />
              <span class="field-help">${tr("运行在 Colab VM 内部，关闭本地电脑也会准时 unassign", "Runs detached inside Colab VM; unassigns even if console is closed")}</span>
            </div>
            <div class="field">
              <label for="set-idlestop">${tr("GPU 空闲自动关机 (分钟，0=禁用)", "GPU Idle Auto-Stop (minutes, 0=disabled)")}</label>
              <input type="number" id="set-idlestop" min="0" max="720" value="${esc(getGuardDraftVal("idle_stop_minutes", 30))}"
                data-input="settings-guard" data-change="settings-guard" data-guard-key="idle_stop_minutes" />
              <span class="field-help">${tr("同时检测任务心跳与 nvidia-smi 利用率 <5%", "Checks both worker heartbeat and nvidia-smi <5%")}</span>
            </div>
            <div class="field">
              <label for="set-floor">${tr("账号算力余额保护红线 (CCU)", "Account Balance Floor (CCU)")}</label>
              <input type="number" step="0.5" min="0" id="set-floor" value="${esc(getGuardDraftVal("balance_floor_ccu", 5))}"
                data-input="settings-guard" data-change="settings-guard" data-guard-key="balance_floor_ccu" />
              <span class="field-help">${tr("低于此余额自动释放该账号下所有实例", "Stops all runtimes on the account when live balance drops to floor")}</span>
            </div>
            <div class="field">
              <label for="set-cap">${tr("单实例累计消耗上限 (CCU)", "Per-Session Spend Cap (CCU)")}</label>
              <input type="number" step="1" min="0" id="set-cap" value="${esc(getGuardDraftVal("session_spend_cap_ccu", 20))}"
                data-input="settings-guard" data-change="settings-guard" data-guard-key="session_spend_cap_ccu" />
            </div>
            <div class="field">
              <label for="set-retries">${tr("显存不达标最大重抽次数", "Max VRAM Provisioning Retries")}</label>
              <input type="number" min="1" max="8" id="set-retries" value="${esc(getGuardDraftVal("max_vram_retries", 4))}"
                data-input="settings-guard" data-change="settings-guard" data-guard-key="max_vram_retries" />
            </div>
            <div class="field">
              <label for="set-maxsess">${tr("最大受管实例并发上限", "Max Managed Sessions")}</label>
              <input type="number" min="1" max="5" id="set-maxsess" value="${esc(getGuardDraftVal("max_managed_sessions", 3))}"
                data-input="settings-guard" data-change="settings-guard" data-guard-key="max_managed_sessions" />
            </div>
            <div class="field full row-actions" style="justify-content:flex-end;">
              ${dirty ? `<button type="button" class="btn" data-action="reset-guard-draft">${tr("重置未保存修改", "Reset Unsaved")}</button>` : ""}
              <button type="submit" class="btn btn-primary${isBusy("settings:guards") ? " is-busy" : ""}" ${isBusy("settings:guards") ? "disabled" : ""}>${tr("保存护栏配置", "Save Guard Settings")}</button>
            </div>
          </form>
        </div>

        <div class="card">
          <div class="card-head">
            <span class="card-title">${tr("模型中心访问令牌 (OS Keyring)", "Model Hub Token Vault (OS Keyring)")}</span>
          </div>
          <p style="font-size:12px;color:var(--text-secondary);margin-bottom:14px;">
            ${tr("密钥保存在操作系统凭据管理器中，仅在部署声明需要时通过内存注入，日志自动脱敏为 <code>***</code>。", "Stored in OS Credential Manager and injected in memory only when declared by a recipe; scrubbed to <code>***</code> in logs.")}
          </p>
          <div class="field" style="margin-bottom:14px;">
            <label for="inp-hf-token">Hugging Face Token — ${state.secrets.hf_token ? `<span class="tag ok">${tr("已保存", "Saved")}</span>` : `<span class="tag">${tr("未配置", "Not set")}</span>`}</label>
            <div class="row-actions">
              <input type="password" autocomplete="off" id="inp-hf-token" style="flex:1;"
                data-input="secret-draft" data-slot="hf_token"
                placeholder="${hfDraftSet ? tr("已输入未保存令牌（点击保存生效）", "Unsaved token entered (click Save)") : "hf_xxxxxxxxxxxxxxxxxxxx"}" />
              <button class="btn btn-sm btn-primary${isBusy("secret:save:hf_token") ? " is-busy" : ""}" type="button" data-action="save-secret" data-slot="hf_token" data-input-id="inp-hf-token" ${isBusy("secret:save:hf_token") ? "disabled" : ""}>${tr("保存", "Save")}</button>
              ${state.secrets.hf_token ? `<button class="btn btn-sm btn-danger${isBusy("secret:delete:hf_token") ? " is-busy" : ""}" type="button" data-action="delete-secret" data-slot="hf_token" ${isBusy("secret:delete:hf_token") ? "disabled" : ""}>${tr("清除", "Clear")}</button>` : ""}
            </div>
          </div>

          <div class="field" style="margin-bottom:18px;">
            <label for="inp-civ-token">Civitai API Token — ${state.secrets.civitai_token ? `<span class="tag ok">${tr("已保存", "Saved")}</span>` : `<span class="tag">${tr("未配置", "Not set")}</span>`}</label>
            <div class="row-actions">
              <input type="password" autocomplete="off" id="inp-civ-token" style="flex:1;"
                data-input="secret-draft" data-slot="civitai_token"
                placeholder="${civDraftSet ? tr("已输入未保存令牌（点击保存生效）", "Unsaved token entered (click Save)") : "Civitai API Token"}" />
              <button class="btn btn-sm btn-primary${isBusy("secret:save:civitai_token") ? " is-busy" : ""}" type="button" data-action="save-secret" data-slot="civitai_token" data-input-id="inp-civ-token" ${isBusy("secret:save:civitai_token") ? "disabled" : ""}>${tr("保存", "Save")}</button>
              ${state.secrets.civitai_token ? `<button class="btn btn-sm btn-danger${isBusy("secret:delete:civitai_token") ? " is-busy" : ""}" type="button" data-action="delete-secret" data-slot="civitai_token" ${isBusy("secret:delete:civitai_token") ? "disabled" : ""}>${tr("清除", "Clear")}</button>` : ""}
            </div>
          </div>

          ${state.settings.backend_mode === "mock" ? `
            <div class="divider"></div>
            <div class="field-group-label" id="mock-scenario-label">${tr("演示沙盒预设场景", "Sandbox Demo Scenario")}</div>
            <div class="row-actions" role="group" aria-labelledby="mock-scenario-label" style="margin-top:8px;">
              ${["demo", "single", "signed_out"].map((sc) => `
                <button class="btn btn-sm ${state.settings.mock_scenario === sc ? "btn-primary" : ""}" type="button"
                  data-action="set-mock-scenario" data-scenario="${esc(sc)}">${esc(sc)}</button>
              `).join("")}
            </div>
          ` : ""}
        </div>
      </div>
    `;
  }

  function restoreInMemorySecretInputs(view) {
    if (state.route === "settings") {
      const hfEl = document.getElementById("inp-hf-token");
      if (hfEl && state.secretDrafts.hf_token) hfEl.value = state.secretDrafts.hf_token;
      const civEl = document.getElementById("inp-civ-token");
      if (civEl && state.secretDrafts.civitai_token) civEl.value = state.secretDrafts.civitai_token;
    } else if (state.route === "tunnels") {
      const keyEl = document.getElementById("chat-apikey-input");
      if (keyEl && state.chatApiKey) keyEl.value = state.chatApiKey;
    } else if (state.route === "deploy" && view && typeof view.querySelector === "function") {
      const recipe = state.recipes.find((r) => r.id === state.selectedRecipeId) || state.recipes[0];
      const draft = recipe ? state.deployDraft[recipe.id] : null;
      if (recipe && draft) {
        for (const inp of recipe.inputs || []) {
          if (inp.type === "secret" && draft[inp.id]) {
            const fkey = `${recipe.id}:${inp.id}`;
            const el = view.querySelector(`[data-field-key="${CSS.escape(fkey)}"]`);
            if (el) el.value = draft[inp.id];
          }
        }
      }
    }
  }

  function renderMain() {
    const view = document.getElementById("main-view");
    if (!view) return;
    const active = document.activeElement;
    let focusSnap = null;
    if (active && typeof view.contains === "function" && view.contains(active) && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)) {
      focusSnap = {
        id: active.id || "",
        fkey: typeof active.getAttribute === "function" ? (active.getAttribute("data-field-key") || "") : "",
        value: active.value,
        preserveValue: ["inp-hf-token", "inp-civ-token", "set-autostop", "set-idlestop", "set-floor", "set-cap", "set-retries", "set-maxsess"].includes(active.id || ""),
        selStart: typeof active.selectionStart === "number" ? active.selectionStart : null,
        selEnd: typeof active.selectionEnd === "number" ? active.selectionEnd : null,
      };
    }

    // Preserve .terminal and #chat-box scroll positions across re-renders
    let chatScrollSnap = null;
    const prevChatBox = document.getElementById("chat-box");
    if (prevChatBox && typeof prevChatBox.scrollTop === "number") {
      const sh = Number(prevChatBox.scrollHeight || 0);
      const ch = Number(prevChatBox.clientHeight || 0);
      chatScrollSnap = {
        scrollTop: prevChatBox.scrollTop,
        atBottom: sh > 0 && ch > 0 ? (sh - prevChatBox.scrollTop - ch <= 24) : false,
      };
    }
    const terminalScrollSnaps = new Map();
    if (typeof view.querySelectorAll === "function") {
      view.querySelectorAll(".terminal").forEach((el, idx) => {
        if (typeof el.scrollTop === "number") {
          const key = (typeof el.getAttribute === "function" && el.getAttribute("data-run-id")) || `idx:${idx}`;
          terminalScrollSnaps.set(key, el.scrollTop);
        }
      });
    }

    if (state.route === "overview") view.innerHTML = renderOverview();
    else if (state.route === "deploy") view.innerHTML = renderDeploy();
    else if (state.route === "sessions") view.innerHTML = renderSessions();
    else if (state.route === "tunnels") view.innerHTML = renderTunnelsAndChat();
    else if (state.route === "accounts") view.innerHTML = renderAccountsAndReceipts();
    else if (state.route === "settings") view.innerHTML = renderSettings();

    restoreInMemorySecretInputs(view);

    // Restore scroll positions
    const nextChatBox = document.getElementById("chat-box");
    if (nextChatBox) {
      if (pendingScrollChatToBottom) {
        nextChatBox.scrollTop = nextChatBox.scrollHeight || 0;
        pendingScrollChatToBottom = false;
      } else if (chatScrollSnap) {
        nextChatBox.scrollTop = chatScrollSnap.atBottom && nextChatBox.scrollHeight
          ? nextChatBox.scrollHeight
          : chatScrollSnap.scrollTop;
      }
    }
    if (terminalScrollSnaps.size > 0 && typeof view.querySelectorAll === "function") {
      view.querySelectorAll(".terminal").forEach((el, idx) => {
        const key = (typeof el.getAttribute === "function" && el.getAttribute("data-run-id")) || `idx:${idx}`;
        if (terminalScrollSnaps.has(key)) {
          el.scrollTop = terminalScrollSnaps.get(key);
        }
      });
    }

    if (focusSnap) {
      let restored = null;
      if (focusSnap.id) restored = document.getElementById(focusSnap.id);
      else if (focusSnap.fkey && typeof view.querySelector === "function") {
        restored = view.querySelector(`[data-field-key="${CSS.escape(focusSnap.fkey)}"]`);
      }
      if (restored) {
        if (focusSnap.preserveValue) restored.value = focusSnap.value;
        if (typeof restored.focus === "function") restored.focus({ preventScroll: true });
        if (focusSnap.selStart !== null && typeof restored.setSelectionRange === "function") {
          try { restored.setSelectionRange(focusSnap.selStart, focusSnap.selEnd); } catch (_) {}
        }
      }
    }
  }

  function getFilteredCommandActions() {
    const q = (state.cmdQuery || "").trim().toLowerCase();
    return [
      { label: tr("前往：总览看板", "Go to: Overview"), sub: "overview", kind: "go", arg: "overview" },
      { label: tr("前往：模型极速部署", "Go to: Quick Deploy"), sub: "deploy", kind: "go", arg: "deploy" },
      { label: tr("前往：算力实例管理", "Go to: GPU Sessions"), sub: "sessions", kind: "go", arg: "sessions" },
      { label: tr("前往：端口映射与对话", "Go to: Tunnels & Chat"), sub: "tunnels", kind: "go", arg: "tunnels" },
      { label: tr("前往：多账号与关闭回执", "Go to: Accounts & Receipts"), sub: "accounts", kind: "go", arg: "accounts" },
      { label: tr("前往：成本护栏与密钥", "Go to: Guards & Vault"), sub: "settings", kind: "go", arg: "settings" },
      { label: tr("新建纯净 GPU 实例", "Allocate New GPU Session"), sub: "session", kind: "new-session", arg: "" },
      { label: tr("刷新全部账号 CCU 余额", "Refresh All Account CCU Balances"), sub: "usage", kind: "refresh-usage", arg: "" },
      ...state.recipes.map((r) => ({
        label: `${tr("部署配方", "Recipe")}: ${locObj(r.name)}`,
        sub: r.id,
        kind: "pick-recipe",
        arg: r.id,
      })),
    ].filter((a) => !q || a.label.toLowerCase().includes(q) || a.sub.toLowerCase().includes(q));
  }

  function renderCommandPaletteItems() {
    const actions = getFilteredCommandActions();
    if (actions.length === 0) {
      state.cmdActiveIndex = 0;
      return `<div class="kpi-foot" style="padding:12px;text-align:center;">${tr("无匹配命令", "No matching commands")}</div>`;
    }
    if (state.cmdActiveIndex < 0 || state.cmdActiveIndex >= actions.length) {
      state.cmdActiveIndex = 0;
    }
    return actions.map((a, idx) => `
      <button type="button" role="option" class="cmd-item${idx === state.cmdActiveIndex ? " active" : ""}" data-action="cmd-exec" data-cmd-idx="${idx}" data-cmd-kind="${esc(a.kind)}" data-cmd-arg="${esc(a.arg)}" aria-selected="${idx === state.cmdActiveIndex ? "true" : "false"}">
        <span>${esc(a.label)}</span>
        <span class="cmd-sub mono">${esc(a.sub)}</span>
      </button>
    `).join("");
  }

  async function executeCommandAction(kind, arg) {
    if (kind === "go") {
      closeModal();
      window._app.go(arg);
    } else if (kind === "new-session") {
      window._app.openNewSessionModal();
    } else if (kind === "refresh-usage") {
      closeModal();
      await window._app.refreshUsage();
    } else if (kind === "pick-recipe") {
      closeModal();
      window._app.pickRecipe(arg);
    }
  }

  function openCommandPalette(opts = {}) {
    const placeholder = tr(
      "输入命令、页面或配方名快速跳转 (↑↓ 选择 · Enter 执行 · Esc 关闭)...",
      "Type a command, page, or recipe name (↑↓ select · Enter run · Esc close)..."
    );
    const existingSearch = document.getElementById("cmd-search");
    const existingList = document.getElementById("cmd-list");
    const modalBody = document.getElementById("modal-body");
    const alreadyMounted =
      state.activeModal &&
      state.activeModal.type === "cmd_palette" &&
      existingSearch &&
      existingList &&
      (!modalBody || typeof modalBody.contains !== "function" || modalBody.contains(existingSearch));

    if (alreadyMounted) {
      // Static shell: NEVER rebuild #cmd-search so Chinese IME composition is never interrupted
      existingSearch.placeholder = placeholder;
      if (opts.resetQuery) existingSearch.value = state.cmdQuery;
      existingList.innerHTML = renderCommandPaletteItems();
      return;
    }

    state.cmdActiveIndex = 0;
    openModal(`
      <div class="field">
        <input type="text" id="cmd-search" autocomplete="off" value="${esc(state.cmdQuery)}" placeholder="${esc(placeholder)}"
          aria-label="${tr("命令面板搜索", "Search commands")}" data-input="cmd-filter" />
      </div>
      <div class="cmd-list" id="cmd-list" role="listbox" aria-label="${tr("快捷命令列表", "Quick commands")}">
        ${renderCommandPaletteItems()}
      </div>
    `, { type: "cmd_palette" }, { ariaLabel: tr("命令面板", "Command Palette") });
    const inp = document.getElementById("cmd-search");
    if (inp && typeof inp.focus === "function") inp.focus();
  }

  /* ========================== Actions ========================== */

  async function executeStopAll(triggerEl = null) {
    return runLocked("sessions:stop-all", triggerEl, async () => {
      try {
        const res = await api("/api/sessions/stop-all", { method: "POST", timeoutMs: 90000 });
        const remaining = Number(res.remaining_sessions || 0);
        const timedOutProvisions = Number(res.timed_out_provisions || 0);
        const failedAccounts = Array.isArray(res.failed_accounts) ? res.failed_accounts : [];
        const stopErrors = Array.isArray(res.stop_errors) ? res.stop_errors : [];
        const verified =
          res.zero_burn_verified === true &&
          remaining === 0 &&
          timedOutProvisions === 0 &&
          failedAccounts.length === 0 &&
          stopErrors.length === 0;

        if (verified) {
          state.zeroBurnAlert = null;
          renderGlobalAlert();
          toast(
            tr(
              `已全部释放 ${res.stopped_count} 个实例（零残留核验通过）`,
              `Released ${res.stopped_count} runtime(s) (Zero-burn verified)`
            ),
            "ok"
          );
        } else {
          state.zeroBurnAlert = {
            kind: "stop_all_unverified",
            stoppedCount: res.stopped_count ?? 0,
            remaining,
            timedOutProvisions,
            failedAccounts,
            stopErrors,
          };
          renderGlobalAlert();
          toast(
            tr(
              `零燃费核验失败：残留 ${remaining} 个实例${timedOutProvisions > 0 ? `、${timedOutProvisions} 个超时分配` : ""}或存在未确认异常，请查看顶部告警并重试！`,
              `Zero-burn verification failed: ${remaining} remaining session(s)${timedOutProvisions > 0 ? `, ${timedOutProvisions} timed-out provision(s)` : ""} or unverified errors — check alert banner and retry!`
            ),
            "err"
          );
        }
        await refreshBootstrap();
      } catch (err) {
        state.zeroBurnAlert = {
          kind: "stop_all_error",
          message: err.message,
        };
        renderGlobalAlert();
        toast(err.message, "err");
      }
    }, true);
  }

  function requestStopAll(triggerEl = null) {
    const count = state.sessions.length;
    openConfirmModal(
      {
        tone: "danger",
        title: () => tr("确认紧急停止全部实例", "Confirm Emergency Stop All"),
        bodyHtml: () => `
          <p>${tr(
            `即将立即释放全部账号下的 <strong>${count}</strong> 个活跃 Colab 实例并断开本地映射。`,
            `This will immediately unassign all <strong>${count}</strong> active Colab runtime(s) across all accounts and close local tunnels.`
          )}</p>
          <div class="confirm-cost-box danger">
            <span>${tr("注意：紧急停机优先止损，不会等待 [[keep]] 成品同步，VM 内未保存文件将丢失。", "Note: Emergency stop prioritizes immediate zero-burn and skips [[keep]] sync; unsaved VM files will be lost.")}</span>
          </div>
        `,
        confirmLabel: () => tr(`确认停止全部 (${count})`, `Confirm Stop All (${count})`),
      },
      async () => {
        closeModal();
        await executeStopAll(triggerEl);
      }
    );
  }

  function parseDraftFieldValue(el) {
    const valType = el.dataset.valType || "";
    if (valType === "bool") return el.value === "1";
    if (valType === "number" || valType === "number-optional") return el.value === "" ? "" : Number(el.value);
    return el.value;
  }

  window._app = {
    toast,
    closeModal,
    refreshBootstrap,
    openCommandPalette,
    openApiTokenModal,
    getApiToken,
    setApiToken,
    refreshWatchdogCountdowns,
    filterCmdPalette(val) {
      state.cmdQuery = val;
      state.cmdActiveIndex = 0;
      const list = document.getElementById("cmd-list");
      if (list) list.innerHTML = renderCommandPaletteItems();
      else openCommandPalette();
    },
    async copyText(text) {
      try {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
          await navigator.clipboard.writeText(text);
          toast(tr("已复制地址", "Copied URL"), "ok");
        } else {
          toast(tr("当前环境不支持剪贴板写入", "Clipboard unavailable in this context"), "err");
        }
      } catch (_) {
        toast(tr("复制失败，请手动复制", "Failed to copy; please copy manually"), "err");
      }
    },
    go(route, opts = {}) {
      const target = normalizeRoute(route) || "overview";
      state.route = target;
      if (state.sidebarOpen) setSidebarOpen(false);
      if (!opts.fromHash) syncRouteHash(target);
      renderChrome();
      renderMain();
    },
    pickRecipe(id) {
      state.selectedRecipeId = id;
      state.route = "deploy";
      if (state.sidebarOpen) setSidebarOpen(false);
      syncRouteHash("deploy");
      renderChrome();
      renderMain();
    },
    setSidebarOpen,
    toggleSidebar,
    showMoreReceipts() {
      const cur = Math.max(RECEIPTS_PAGE_SIZE, Number(state.receiptsVisibleLimit || RECEIPTS_PAGE_SIZE));
      state.receiptsVisibleLimit = Math.min(Math.max(RECEIPTS_PAGE_SIZE, state.receipts.length), cur + RECEIPTS_PAGE_SIZE);
      renderMain();
    },
    showAllReceipts() {
      state.receiptsVisibleLimit = Math.max(RECEIPTS_PAGE_SIZE, state.receipts.length);
      renderMain();
    },
    collapseReceipts() {
      state.receiptsVisibleLimit = RECEIPTS_PAGE_SIZE;
      renderMain();
    },
    toggleAdvanced(recipeId) {
      state.advancedOpen[recipeId] = !state.advancedOpen[recipeId];
      renderMain();
    },
    pickGpuTier(recipeId, accel, minVram, highMem) {
      const d = state.deployDraft[recipeId] || {};
      d._sessionMode = "new";
      d._accelerator = accel;
      d._minVram = minVram;
      d._highMem = !!highMem || isHighMemOnlyAccelerator(accel);
      state.deployDraft[recipeId] = d;
      renderMain();
    },
    updateDraft(recipeId, key, val, rerender = false) {
      if (!state.deployDraft[recipeId]) state.deployDraft[recipeId] = {};
      const prevVal = state.deployDraft[recipeId][key];
      state.deployDraft[recipeId][key] = val;
      if (key === "_accelerator" && isHighMemOnlyAccelerator(val)) {
        state.deployDraft[recipeId]._highMem = true;
      }
      if (recipeId === "llama-cpp-gguf" && key === "repo" && val !== prevVal) {
        ggufReqId += 1;
        state.ggufPlan = null;
        state.ggufPlanLoading = false;
        if (!rerender) {
          const host = document.getElementById("gguf-planner-host");
          if (host) host.innerHTML = "";
        }
      }
      if (rerender) renderMain();
    },
    pickCuratedRepo(repo) {
      this.updateDraft("llama-cpp-gguf", "repo", repo, true);
      this.analyzeGguf();
    },
    applyHardwareRow(accel, minVram) {
      const d = state.deployDraft["llama-cpp-gguf"] || {};
      d._sessionMode = "new";
      d._accelerator = accel;
      d._minVram = minVram;
      d._highMem = isHighMemOnlyAccelerator(accel) || minVram >= 70000;
      state.deployDraft["llama-cpp-gguf"] = d;
      toast(
        tr(
          `已设为 ${accel}${minVram ? ` (最低显存 ${minVram} MiB)` : ""}`,
          `Selected ${accel}${minVram ? ` (min VRAM ${minVram} MiB)` : ""}`
        ),
        "ok"
      );
      renderMain();
    },
    async analyzeGguf() {
      const d = state.deployDraft["llama-cpp-gguf"] || {};
      const repo = (d.repo || "").trim();
      if (!repo) return toast(tr("请先输入 Hugging Face GGUF 仓库名", "Please enter a Hugging Face GGUF repository ID"), "err");
      const reqId = ++ggufReqId;
      state.ggufPlanLoading = true;
      renderMain();
      try {
        const q = new URLSearchParams({
          repo,
          quant: d.quant || "auto",
          ctx: String(d.ctx || 0),
          kv: d.kv || "auto",
        });
        const plan = await api(`/api/hf/gguf-plan?${q.toString()}`);
        if (reqId !== ggufReqId) return;
        state.ggufPlan = plan;
      } catch (err) {
        if (reqId !== ggufReqId) return;
        toast(err.message, "err");
      } finally {
        if (reqId === ggufReqId) {
          state.ggufPlanLoading = false;
          renderMain();
        }
      }
    },
    async submitDeploy(ev, triggerEl = null) {
      ev.preventDefault();
      if (isBusy("deploy:submit")) return;
      const recipe = state.recipes.find((r) => r.id === state.selectedRecipeId);
      if (!recipe) return;
      const draft = ensureDeployDraft(recipe);
      const inputs = {};
      for (const inp of recipe.inputs || []) {
        const v = draft[inp.id];
        if (v !== "" && v !== null && v !== undefined) inputs[inp.id] = v;
      }
      const accel = draft._accelerator || recipe.provision.accelerator;
      const minVram = Number(draft._minVram || 0);
      const body = {
        recipe_id: recipe.id,
        inputs,
        session_name: draft._sessionMode === "new" ? null : draft._sessionMode,
        provision: draft._sessionMode === "new" ? {
          accelerator: accel,
          min_vram_mib: minVram,
          high_mem: !!draft._highMem || isHighMemOnlyAccelerator(accel),
        } : null,
      };

      const executeDeploy = () => runLocked("deploy:submit", triggerEl, async () => {
        try {
          await api("/api/deployments", { method: "POST", body: JSON.stringify(body) });
          for (const inp of recipe.inputs || []) {
            if (inp.type === "secret") draft[inp.id] = "";
          }
          toast(tr("部署任务已启动，正在实时追踪进度...", "Deployment started; streaming live progress..."), "ok");
          await refreshBootstrap();
        } catch (err) {
          toast(err.message, "err");
        }
      });

      if (state.settings.backend_mode === "real" && draft._sessionMode === "new") {
        const estBurn = getBurnRate(accel);
        const bal = state.activeAccount?.usage?.balance_ccu;
        const autoStop = state.settings.guards?.auto_stop_minutes ?? 120;
        openConfirmModal(
          {
            tone: "danger",
            title: () => tr("确认启动真实计费部署", "Confirm Live Billing Deployment"),
            bodyHtml: () => `
              <p>${tr(
                "当前处于 <strong>真实 Google Colab 模式</strong>，本次部署将新建云端 GPU 实例并真实消耗账号 Compute Units (CCU)。",
                "You are in <strong>Real Google Colab Mode</strong>. Launching this deployment allocates a live GPU VM and spends real Compute Units (CCU)."
              )}</p>
              <div class="confirm-cost-box">
                <div class="kv-row"><span>${tr("部署模板", "Recipe")}</span><span class="strong">${esc(locObj(recipe.name))}</span></div>
                <div class="kv-row"><span>${tr("目标算力", "Target Accelerator")}</span><span class="mono strong">${esc(accel)}${minVram > 0 ? ` (>= ${minVram} MiB)` : ""}${body.provision?.high_mem ? " · High-RAM" : ""}</span></div>
                <div class="kv-row"><span>${tr("预估消耗速率", "Estimated Burn Rate")}</span><span class="mono strong">~${estBurn.toFixed(2)} CCU/h</span></div>
                <div class="kv-row"><span>${tr("当前账号余额", "Account Balance")}</span><span class="mono strong">${bal !== null && bal !== undefined ? `${bal.toFixed(2)} CCU` : tr("未知", "Unknown")}</span></div>
                <div class="kv-row"><span>${tr("VM 看门狗限时", "VM Auto-Stop Limit")}</span><span class="mono strong">${autoStop > 0 ? `${autoStop} min` : tr("不限时 (高风险)", "Unlimited (High Risk)")}</span></div>
              </div>
            `,
            confirmLabel: () => tr("确认启动真实计费部署", "Confirm & Launch Live Deployment"),
          },
          async () => {
            closeModal();
            await executeDeploy();
          }
        );
        return;
      }

      await executeDeploy();
    },
    async cancelRun(runId, meta = {}, triggerEl = null) {
      if (isBusy(`run:cancel:${runId}`)) return;
      const sessName = meta.sessionName || "";
      openConfirmModal(
        {
          tone: "danger",
          title: () => tr("确认取消部署并回收实例", "Confirm Cancel Deployment & Release"),
          bodyHtml: () => `
            <p>${tr(
              `确定要取消部署任务 <code>${esc(runId)}</code>${sessName ? `（会话 <code>${esc(sessName)}</code>）` : ""} 吗？若本次任务新建了实例，将自动释放以停止计费。`,
              `Cancel deployment <code>${esc(runId)}</code>${sessName ? ` (session <code>${esc(sessName)}</code>)` : ""}? Any newly provisioned runtime will be unassigned immediately.`
            )}</p>
          `,
          confirmLabel: () => tr("确认取消并回收", "Confirm Cancel & Release"),
        },
        async () => {
          closeModal();
          await runLocked(`run:cancel:${runId}`, triggerEl, async () => {
            try {
              await api(`/api/deployments/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
              toast(tr("已发送取消指令并回收新建实例", "Cancellation requested; releasing provisioned runtime"), "ok");
              await refreshBootstrap();
            } catch (err) {
              toast(err.message, "err");
            }
          });
        }
      );
    },
    async syncKeep(runId, triggerEl = null) {
      return runLocked(`run:sync:${runId}`, triggerEl, async () => {
        try {
          await api(`/api/deployments/${encodeURIComponent(runId)}/keep/sync`, { method: "POST", timeoutMs: 90000 });
          toast(tr("已同步并打包保留成品目录至本地 data/keep/", "Synced keep artifacts locally to data/keep/"), "ok");
          await refreshBootstrap();
        } catch (err) {
          toast(err.message, "err");
        }
      });
    },
    async refreshUsage() {
      try {
        await api("/api/accounts/refresh", { method: "POST" });
        await refreshBootstrap();
        toast(tr("已刷新全部账号算力余额", "Refreshed CCU balances for all accounts"), "ok");
      } catch (err) {
        toast(err.message, "err");
      }
    },
    async stopSession(identifier, accountId, meta = {}, triggerEl = null) {
      if (isBusy(`session:stop:${identifier}`)) return;
      const dispName = meta.sessionName || identifier;
      const dispAccel = meta.accelerator || "";
      openConfirmModal(
        {
          tone: "danger",
          title: () => tr("确认停止实例并结束计费", "Confirm Stop & End Billing"),
          bodyHtml: () => `
            <p>${tr(
              `确定要释放实例 <strong>${esc(dispName)}</strong>${dispAccel ? ` (${esc(dispAccel)})` : ""} 吗？释放后将立即结束该实例的 Colab 算力计费，VM 内未同步到本地的文件会丢失。`,
              `Unassign runtime <strong>${esc(dispName)}</strong>${dispAccel ? ` (${esc(dispAccel)})` : ""}? Billing stops immediately and unsynced files inside the VM will be lost.`
            )}</p>
            <div class="mono" style="font-size:11.5px;color:var(--text-muted);">Endpoint: <code>${esc(identifier)}</code></div>
          `,
          confirmLabel: () => tr("确认停止并结束计费", "Confirm Stop & End Billing"),
        },
        async () => {
          closeModal();
          await runLocked(`session:stop:${identifier}`, triggerEl, async () => {
            try {
              const q = accountId ? `?account_id=${encodeURIComponent(accountId)}` : "";
              const rc = await api(`/api/sessions/${encodeURIComponent(identifier)}/stop${q}`, { method: "POST" });
              if (rc.verified_unassigned === false) {
                state.zeroBurnAlert = {
                  kind: "session_unverified",
                  sessionName: rc.session_name || dispName,
                  endpoint: rc.endpoint || identifier,
                  receiptId: rc.receipt_id || "",
                };
                renderGlobalAlert();
                toast(
                  tr(
                    `警告：实例 ${rc.session_name || dispName} 释放未通过核验，云端可能仍在计费（回执 #${rc.receipt_id || "--"}）！`,
                    `WARNING: Unassign of ${rc.session_name || dispName} was NOT verified — billing may still be active (Receipt #${rc.receipt_id || "--"})!`
                  ),
                  "err"
                );
              } else {
                toast(
                  tr(
                    `已释放实例 ${rc.session_name} 并生成关闭回执 #${rc.receipt_id}`,
                    `Released ${rc.session_name} (Unassign Receipt #${rc.receipt_id})`
                  ),
                  "ok"
                );
              }
              await refreshBootstrap();
            } catch (err) {
              toast(err.message, "err");
            }
          });
        }
      );
    },
    async extendSession(name, triggerEl = null) {
      return runLocked(`session:extend:${name}`, triggerEl, async () => {
        try {
          await api(`/api/sessions/${encodeURIComponent(name)}/extend`, {
            method: "POST",
            body: JSON.stringify({ add_minutes: 120 }),
          });
          toast(tr(`已将 ${name} 自动关机时间延长 2 小时`, `Extended ${name} auto-stop deadline by 2 hours`), "ok");
          await refreshBootstrap();
        } catch (err) {
          toast(err.message, "err");
        }
      });
    },
    openNewSessionModal(opts = {}) {
      openModal(`
        <h3 id="modal-title" style="font-size:15px;font-weight:600;margin-bottom:14px;">${tr("新建 Colab 算力实例", "New Colab GPU Session")}</h3>
        <form data-submit="new-session" class="form-grid">
          <div class="field">
            <label for="m-accel">${tr("加速器型号", "Accelerator")}</label>
            <select id="m-accel">
              ${getAcceleratorSpecs().map((sp) => `<option value="${esc(sp.id)}">${esc(formatAcceleratorOptionLabel(sp))}</option>`).join("")}
            </select>
          </div>
          <div class="field">
            <label for="m-vram">${tr("最低显存要求 (MiB，0=不限)", "Min VRAM (MiB, 0 = any)")}</label>
            <input type="number" id="m-vram" min="0" step="1024" value="0" />
            <span class="field-help">${tr("填 70000 可自动退回 40G A100 直至抽中 80G", "Set 70000 to auto-release 40 GB A100s until an 80 GB VM is assigned")}</span>
          </div>
          <div class="field">
            <label for="m-autostop">${tr("自动关机限时 (分钟)", "Auto-Stop Limit (minutes)")}</label>
            <input type="number" id="m-autostop" min="0" max="1440" value="${state.settings.guards?.auto_stop_minutes ?? 120}" />
          </div>
          <div class="field">
            <label for="m-highmem">${tr("高内存 (High-RAM)", "High-RAM Shape")}</label>
            <select id="m-highmem">
              <option value="0">${tr("标准内存 (Standard)", "Standard")}</option>
              <option value="1">${tr("高内存 (High-RAM)", "High-RAM")}</option>
            </select>
          </div>
          <div class="field full row-actions" style="justify-content:flex-end;margin-top:8px;">
            <button type="button" class="btn" data-action="close-modal">${tr("取消", "Cancel")}</button>
            <button type="submit" class="btn btn-primary${isBusy("session:create") ? " is-busy" : ""}" ${isBusy("session:create") ? "disabled" : ""}>${tr("立即申请实例", "Allocate Session")}</button>
          </div>
        </form>
      `, { type: "new_session" }, opts);
    },
    async submitNewSession(ev, triggerEl = null) {
      ev.preventDefault();
      if (isBusy("session:create")) return;
      const accelEl = document.getElementById("m-accel");
      const vramEl = document.getElementById("m-vram");
      const autoEl = document.getElementById("m-autostop");
      const highEl = document.getElementById("m-highmem");
      const accel = accelEl ? accelEl.value : "G4";
      const body = {
        accelerator: accel,
        min_vram_mib: Number((vramEl && vramEl.value) || 0),
        auto_stop_minutes: autoEl && autoEl.value !== "" ? Number(autoEl.value) : 120,
        high_mem: isHighMemOnlyAccelerator(accel) ? true : (highEl ? highEl.value === "1" : false),
      };

      const executeProvision = () => runLocked("session:create", triggerEl, async () => {
        try {
          await api("/api/sessions?async_mode=true", { method: "POST", body: JSON.stringify(body) });
          toast(tr("已发起实例分配请求", "Session provisioning started"), "ok");
          await refreshBootstrap();
        } catch (err) {
          toast(err.message, "err");
        }
      });

      if (state.settings.backend_mode === "real") {
        const estBurn = getBurnRate(body.accelerator);
        const bal = state.activeAccount?.usage?.balance_ccu;
        openConfirmModal(
          {
            tone: "danger",
            title: () => tr("确认申请真实计费 GPU 实例", "Confirm Live Billing GPU Allocation"),
            bodyHtml: () => `
              <p>${tr(
                "当前处于 <strong>真实 Google Colab 模式</strong>，申请成功后将按小时真实消耗你的 Compute Units (CCU)。",
                "You are in <strong>Real Google Colab Mode</strong>. Once assigned, this runtime will continuously spend real Compute Units (CCU)."
              )}</p>
              <div class="confirm-cost-box">
                <div class="kv-row"><span>${tr("加速器型号", "Accelerator")}</span><span class="mono strong">${esc(body.accelerator)}${body.min_vram_mib > 0 ? ` (>= ${body.min_vram_mib} MiB)` : ""}${body.high_mem ? " · High-RAM" : ""}</span></div>
                <div class="kv-row"><span>${tr("预估消耗速率", "Estimated Burn Rate")}</span><span class="mono strong">~${estBurn.toFixed(2)} CCU/h</span></div>
                <div class="kv-row"><span>${tr("当前账号余额", "Account Balance")}</span><span class="mono strong">${bal !== null && bal !== undefined ? `${bal.toFixed(2)} CCU` : tr("未知", "Unknown")}</span></div>
                <div class="kv-row"><span>${tr("自动关机限时", "Auto-Stop Limit")}</span><span class="mono strong">${body.auto_stop_minutes > 0 ? `${body.auto_stop_minutes} min` : tr("不限时 (高风险)", "Unlimited (High Risk)")}</span></div>
              </div>
            `,
            confirmLabel: () => tr("确认申请真实计费实例", "Confirm & Allocate Live GPU"),
          },
          async () => {
            closeModal();
            await executeProvision();
          }
        );
        return;
      }

      closeModal();
      await executeProvision();
    },
    async cancelProvision(opId, triggerEl = null) {
      return runLocked(`provision:cancel:${opId}`, triggerEl, async () => {
        try {
          await api(`/api/provision/${encodeURIComponent(opId)}/cancel`, { method: "POST" });
          toast(tr("正在取消分配并清理任何在途实例...", "Cancelling allocation & cleaning up any in-flight VM..."), "ok");
          await refreshBootstrap();
        } catch (err) {
          toast(err.message, "err");
        }
      });
    },
    openForwardModal(sessionName, opts = {}) {
      openModal(`
        <h3 id="modal-title" style="font-size:15px;font-weight:600;margin-bottom:14px;">${tr("映射云端端口至本机 127.0.0.1", "Forward Colab VM Port to 127.0.0.1")}</h3>
        <form data-submit="forward" data-session-name="${esc(sessionName)}" class="form-grid">
          <div class="field">
            <label for="f-rport">${tr("云端 VM 端口 (禁填 8080)", "Colab VM Port (avoid 8080)")}</label>
            <input type="number" id="f-rport" min="1024" max="65535" value="8000" required />
          </div>
          <div class="field">
            <label for="f-lport">${tr("本地监听端口 (0=同云端或自动分配)", "Local Port (0 = match VM or auto-pick)")}</label>
            <input type="number" id="f-lport" min="0" max="65535" value="8000" />
          </div>
          <div class="field full">
            <label for="f-label">${tr("备注名称", "Label")}</label>
            <input type="text" id="f-label" value="Custom Service" />
          </div>
          <div class="field full row-actions" style="justify-content:flex-end;margin-top:8px;">
            <button type="button" class="btn" data-action="close-modal">${tr("取消", "Cancel")}</button>
            <button type="submit" class="btn btn-primary">${tr("开启映射", "Start Forward")}</button>
          </div>
        </form>
      `, { type: "forward", sessionName }, opts);
    },
    async submitForward(ev, sessionName) {
      ev.preventDefault();
      const body = {
        session_name: sessionName,
        remote_port: Number(document.getElementById("f-rport").value),
        local_port: Number(document.getElementById("f-lport").value || 0),
        label: document.getElementById("f-label").value || "Port Forward",
      };
      closeModal();
      try {
        const tun = await api("/api/tunnels", { method: "POST", body: JSON.stringify(body) });
        toast(tr(`端口已映射至 ${tun.local_url}`, `Port forwarded to ${tun.local_url}`), "ok");
        await refreshBootstrap();
      } catch (err) {
        toast(err.message, "err");
      }
    },
    async closeTunnel(id) {
      try {
        const res = await api(`/api/tunnels/${encodeURIComponent(id)}`, { method: "DELETE" });
        toast(
          tr(
            "已关闭本地端口映射（注意：不会停止云端 GPU 计费）",
            res.billing_notice || "Closed local port forward (Colab VM billing continues)"
          ),
          "info"
        );
        await refreshBootstrap();
      } catch (err) {
        toast(err.message, "err");
      }
    },
    setChatTunnel(tid) {
      state.selectedChatTunnelId = tid || "";
    },
    setChatMeta(field, val) {
      state[field] = val;
    },
    async sendChat(ev) {
      ev.preventDefault();
      if (state.chatBusy) return;
      const inp = document.getElementById("chat-input");
      const text = (inp?.value ?? state.chatDraft ?? "").trim();
      if (!text) return;
      const tunSel = document.getElementById("chat-tunnel-select");
      const tunnelId = tunSel?.value || state.selectedChatTunnelId || null;
      if (state.settings.backend_mode === "real" && !tunnelId) {
        toast(tr("真实模式下请先开启/绑定本地隧道后再发送对话", "Please open or bind a local tunnel first in Real Mode"), "err");
        return;
      }
      state.selectedChatTunnelId = tunnelId || "";
      state.chatMessages.push({ role: "user", content: text });
      state.chatDraft = "";
      if (inp) inp.value = "";
      state.chatBusy = true;
      pendingScrollChatToBottom = true;
      renderMain();

      const outboundMessages = state.chatMessages
        .filter((m) => (m.role === "user" || m.role === "assistant") && !m._welcome && !m._error)
        .map((m) => ({ role: m.role, content: m.content }));

      try {
        const res = await api("/api/playground/chat", {
          method: "POST",
          timeoutMs: 185000,
          body: JSON.stringify({
            tunnel_id: tunnelId || null,
            model: (state.chatModel || "auto").trim() || "auto",
            api_key: (state.chatApiKey || "").trim() || null,
            messages: outboundMessages,
          }),
        });
        state.chatMessages.push({
          role: "assistant",
          content: res.reply,
          meta: {
            latency_ms: res.latency_ms,
            tok_per_s: res.tok_per_s,
            model: res.model || "",
          },
        });
      } catch (err) {
        state.chatMessages.push({
          role: "assistant",
          _error: true,
          content: `[ERROR] ${tr("请求失败", "Request failed")}: ${err.message}`,
        });
      } finally {
        state.chatBusy = false;
        pendingScrollChatToBottom = true;
        renderMain();
      }
    },
    renderLoginModalWithFlow(flow, opts = {}) {
      const isMock = state.settings.backend_mode === "mock";
      openModal(`
        <h3 id="modal-title" style="font-size:15px;font-weight:600;margin-bottom:10px;">${tr("添加 Google Colab 账号", "Add Google Colab Account")}</h3>
        <p style="font-size:12.5px;color:var(--text-secondary);margin-bottom:12px;">
          ${isMock
            ? tr(
                "当前处于「演示沙盒模式」：无需真实 Google 授权，直接输入任意邮箱（如 demo2@gmail.com）即可模拟新账号登录。",
                "Sandbox Demo Mode is active: no real Google authorization needed — enter any email (e.g. demo2@gmail.com) to simulate adding an account."
              )
            : tr(
                "1. 点击下方链接在浏览器完成 Google 账号授权；<br/>2. 将页面显示的授权码 (Authorization Code) 粘贴至下方：",
                "1. Open the link below to authorize your Google account in the browser;<br/>2. Paste the returned Authorization Code below:"
              )}
        </p>
        ${!isMock ? `<div style="margin-bottom:12px;"><a href="${esc(safeHttpUrl(flow.auth_url))}" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-primary">${tr("打开 Google 官方授权页面", "Open Google Authorization Page")}</a></div>` : ""}
        <form data-submit="complete-login" data-flow-id="${esc(flow.flow_id)}" class="form-grid">
          <div class="field full">
            <label for="oauth-code">${isMock ? tr("模拟邮箱或任意授权码", "Simulated Email or Auth Code") : tr("Google 授权码 (Authorization Code)", "Google Authorization Code")}</label>
            <input type="text" id="oauth-code" autocomplete="off" required placeholder="${isMock ? "researcher@gmail.com" : "4/0AeanS0..."}" />
          </div>
          <div class="field full row-actions" style="justify-content:flex-end;">
            <button type="button" class="btn" data-action="close-modal">${tr("取消", "Cancel")}</button>
            <button type="submit" class="btn btn-primary">${tr("完成验证并保存", "Verify & Save Account")}</button>
          </div>
        </form>
      `, { type: "login", flow }, opts);
    },
    async openLoginModal() {
      try {
        const flow = await api("/api/accounts/login/begin", { method: "POST" });
        this.renderLoginModalWithFlow(flow);
      } catch (err) {
        toast(err.message, "err");
      }
    },
    async completeLogin(ev, flowId) {
      ev.preventDefault();
      const code = document.getElementById("oauth-code").value.trim();
      closeModal();
      try {
        const acc = await api("/api/accounts/login/complete", {
          method: "POST",
          body: JSON.stringify({ flow_id: flowId, code }),
        });
        toast(tr(`已登录并切换至 ${acc.email}`, `Signed in and switched to ${acc.email}`), "ok");
        await refreshBootstrap();
      } catch (err) {
        toast(err.message, "err");
      }
    },
    async importCliAccount() {
      try {
        const acc = await api("/api/accounts/import-cli", { method: "POST" });
        toast(tr(`成功导入 colab-cli 账号: ${acc.email}`, `Imported colab-cli account: ${acc.email}`), "ok");
        await refreshBootstrap();
      } catch (err) {
        toast(err.message, "err");
      }
    },
    async switchAccount(id, triggerEl = null) {
      return runLocked(`account:switch:${id}`, triggerEl, async () => {
        try {
          const res = await api(`/api/accounts/${encodeURIComponent(id)}/switch`, { method: "POST" });
          toast(
            tr(
              `已切换主操作账号${res.closed_tunnels ? `（自动关闭旧账号 ${res.closed_tunnels} 条本地隧道）` : ""}`,
              `Switched active account${res.closed_tunnels ? ` (closed ${res.closed_tunnels} tunnel(s) from previous account)` : ""}`
            ),
            "ok"
          );
          await refreshBootstrap();
        } catch (err) {
          toast(err.message, "err");
        }
      });
    },
    async removeAccount(id, meta = {}, triggerEl = null) {
      if (isBusy(`account:remove:${id}`)) return;
      const dispEmail = meta.email || id;
      openConfirmModal(
        {
          tone: "danger",
          title: () => tr("确认移除 Google 账号", "Confirm Remove Google Account"),
          bodyHtml: () => `
            <p>${tr(
              `确定要从本机控制台移除账号 <strong>${esc(dispEmail)}</strong> 并清除其保存在系统凭据管理器中的 OAuth 凭证吗？`,
              `Remove account <strong>${esc(dispEmail)}</strong> and delete its saved OAuth credentials from OS Keyring?`
            )}</p>
            <div class="confirm-cost-box">
              <span>${tr("注意：移除账号不会自动停止该账号上仍在运行的云端 Colab 实例，请先确认已停止计费。", "Note: Removing an account does not stop active cloud Colab runtimes on that account. Stop billing first.")}</span>
            </div>
          `,
          confirmLabel: () => tr("确认移除账号", "Confirm Remove Account"),
        },
        async () => {
          closeModal();
          await runLocked(`account:remove:${id}`, triggerEl, async () => {
            try {
              await api(`/api/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
              toast(tr("账号已从控制台移除", "Account removed from console"), "ok");
              await refreshBootstrap();
            } catch (err) {
              toast(err.message, "err");
            }
          });
        }
      );
    },
    setGuardDraftField(key, rawVal) {
      if (!key) return;
      state.settingsDraft[key] = rawVal;
    },
    resetGuardDraft() {
      state.settingsDraft = {};
      renderMain();
    },
    setSecretDraft(slot, val) {
      if (!slot) return;
      state.secretDrafts[slot] = val;
    },
    async submitApiToken(ev) {
      ev.preventDefault();
      const inp = document.getElementById("api-token-input");
      const val = (inp ? inp.value : "").trim();
      if (!val) {
        toast(tr("请输入有效的 API Token", "Please enter a valid API token"), "err");
        return;
      }
      setApiToken(val);
      closeModal();
      wsReconnectDelay = WS_BACKOFF_BASE_MS;
      await initConsole();
    },
    async saveGuardSettings(ev, triggerEl = null) {
      ev.preventDefault();
      const guards = {};
      for (const f of GUARD_FIELDS) {
        const el = document.getElementById(f.id);
        const raw = el ? el.value : getGuardDraftVal(f.key, f.fallback);
        if (raw === "" || raw === null || raw === undefined || !Number.isFinite(Number(raw))) {
          toast(tr("护栏数值不能为空或无效数字", "Guard settings require valid numeric values"), "err");
          return;
        }
        guards[f.key] = Number(raw);
      }
      return runLocked("settings:guards", triggerEl, async () => {
        try {
          await api("/api/settings", { method: "PATCH", body: JSON.stringify({ guards }) });
          state.settingsDraft = {};
          toast(tr("成本护栏配置已保存", "Cost guard settings saved"), "ok");
          await refreshBootstrap();
        } catch (err) {
          toast(err.message, "err");
        }
      });
    },
    async saveSecret(slot, inputId, triggerEl = null) {
      const inp = document.getElementById(inputId);
      const val = ((inp ? inp.value : "") || state.secretDrafts[slot] || "").trim();
      if (!val) return toast(tr("请输入有效 Token", "Please enter a valid token"), "err");
      return runLocked(`secret:save:${slot}`, triggerEl, async () => {
        try {
          await api(`/api/secrets/${encodeURIComponent(slot)}`, {
            method: "PUT",
            body: JSON.stringify({ value: val }),
          });
          state.secretDrafts[slot] = "";
          if (inp) inp.value = "";
          toast(tr(`${slot} 已安全存入系统凭据管理器`, `${slot} saved to OS credential vault`), "ok");
          await refreshBootstrap();
        } catch (err) {
          toast(err.message, "err");
        }
      });
    },
    async deleteSecret(slot, triggerEl = null) {
      if (isBusy(`secret:delete:${slot}`)) return;
      openConfirmModal(
        {
          tone: "danger",
          title: () => tr("确认清除访问令牌", "Confirm Clear Secret Token"),
          bodyHtml: () => `
            <p>${tr(
              `确定要从系统凭据管理器中清除 <code>${esc(slot)}</code> 吗？清除后需要受保护模型权重的部署模板将无法自动注入该令牌。`,
              `Clear <code>${esc(slot)}</code> from OS Credential Manager? Recipes requiring this token will no longer receive it automatically.`
            )}</p>
          `,
          confirmLabel: () => tr("确认清除密钥", "Confirm Clear Token"),
        },
        async () => {
          closeModal();
          await runLocked(`secret:delete:${slot}`, triggerEl, async () => {
            try {
              await api(`/api/secrets/${encodeURIComponent(slot)}`, { method: "DELETE" });
              state.secretDrafts[slot] = "";
              toast(tr(`${slot} 已清除`, `${slot} cleared`), "ok");
              await refreshBootstrap();
            } catch (err) {
              toast(err.message, "err");
            }
          });
        }
      );
    },
    async setMockScenario(sc) {
      try {
        await api("/api/settings", { method: "PATCH", body: JSON.stringify({ mock_scenario: sc }) });
        toast(tr(`已切换沙盒预设场景: ${sc}`, `Switched sandbox scenario: ${sc}`), "ok");
        await refreshBootstrap();
      } catch (err) {
        toast(err.message, "err");
      }
    },
  };

  /* ========================== Delegated Event Handlers ========================== */

  async function handleActionClick(ev) {
    if (!ev.target || typeof ev.target.closest !== "function") return;
    const el = ev.target.closest("[data-action]");
    if (!el || el.disabled) return;
    const action = el.dataset.action;
    if (action === "close-modal") {
      closeModal();
    } else if (action === "confirm-ok") {
      const fn = state.pendingConfirm;
      state.pendingConfirm = null;
      if (typeof fn === "function") await fn();
    } else if (action === "confirm-mode-switch") {
      const nextMode = el.dataset.nextMode || "mock";
      closeModal();
      try {
        await api("/api/settings", { method: "PATCH", body: JSON.stringify({ backend_mode: nextMode }) });
        toast(
          nextMode === "real"
            ? tr("已切换至真实 Colab 模式", "Switched to Real Colab Mode")
            : tr("已切换至演示沙盒模式", "Switched to Sandbox Demo Mode"),
          "ok"
        );
        await refreshBootstrap();
      } catch (err) {
        toast(err.message, "err");
      }
    } else if (action === "retry-bootstrap") {
      await initConsole();
    } else if (action === "open-api-token") {
      openApiTokenModal(state.bootstrapError?.message || "");
    } else if (action === "reset-guard-draft") {
      window._app.resetGuardDraft();
    } else if (action === "retry-stop-all") {
      await executeStopAll(el);
    } else if (action === "dismiss-alert") {
      state.zeroBurnAlert = null;
      renderGlobalAlert();
    } else if (action === "go") {
      window._app.go(el.dataset.route || "overview");
    } else if (action === "refresh-usage") {
      await window._app.refreshUsage();
    } else if (action === "refresh-bootstrap") {
      try {
        await refreshBootstrap();
      } catch (err) {
        toast(err.message, "err");
      }
    } else if (action === "pick-recipe") {
      window._app.pickRecipe(el.dataset.recipeId || "");
    } else if (action === "pick-gpu-tier") {
      window._app.pickGpuTier(
        el.dataset.recipeId || "",
        el.dataset.accel || "G4",
        Number(el.dataset.minVram || 0),
        el.dataset.highMem === "1"
      );
    } else if (action === "update-draft-click") {
      window._app.updateDraft(
        el.dataset.recipeId || "",
        el.dataset.key || "",
        el.dataset.val || "",
        el.dataset.rerender === "1"
      );
    } else if (action === "toggle-advanced") {
      window._app.toggleAdvanced(el.dataset.recipeId || "");
    } else if (action === "analyze-gguf") {
      await window._app.analyzeGguf();
    } else if (action === "pick-curated-repo") {
      window._app.pickCuratedRepo(el.dataset.repo || "");
    } else if (action === "apply-hw-row") {
      window._app.applyHardwareRow(el.dataset.accel || "A100", Number(el.dataset.minVram || 0));
    } else if (action === "cancel-run") {
      await window._app.cancelRun(el.dataset.runId || "", { sessionName: el.dataset.sessionName || "" }, el);
    } else if (action === "sync-keep") {
      await window._app.syncKeep(el.dataset.runId || "", el);
    } else if (action === "copy-text") {
      await window._app.copyText(el.dataset.text || "");
    } else if (action === "open-new-session") {
      window._app.openNewSessionModal();
    } else if (action === "cancel-provision") {
      await window._app.cancelProvision(el.dataset.opId || "", el);
    } else if (action === "extend-session") {
      await window._app.extendSession(el.dataset.sessionName || "", el);
    } else if (action === "open-forward") {
      window._app.openForwardModal(el.dataset.sessionName || "");
    } else if (action === "stop-session") {
      await window._app.stopSession(
        el.dataset.endpoint || "",
        el.dataset.accountId || "",
        { sessionName: el.dataset.sessionName || "", accelerator: el.dataset.accelerator || "" },
        el
      );
    } else if (action === "close-tunnel") {
      await window._app.closeTunnel(el.dataset.tunnelId || "");
    } else if (action === "import-cli-account") {
      await window._app.importCliAccount();
    } else if (action === "open-login") {
      await window._app.openLoginModal();
    } else if (action === "switch-account") {
      await window._app.switchAccount(el.dataset.accountId || "", el);
    } else if (action === "remove-account") {
      await window._app.removeAccount(el.dataset.accountId || "", { email: el.dataset.email || "" }, el);
    } else if (action === "save-secret") {
      await window._app.saveSecret(el.dataset.slot || "", el.dataset.inputId || "", el);
    } else if (action === "delete-secret") {
      await window._app.deleteSecret(el.dataset.slot || "", el);
    } else if (action === "set-mock-scenario") {
      await window._app.setMockScenario(el.dataset.scenario || "demo");
    } else if (action === "receipts-more") {
      window._app.showMoreReceipts();
    } else if (action === "receipts-all") {
      window._app.showAllReceipts();
    } else if (action === "receipts-collapse") {
      window._app.collapseReceipts();
    } else if (action === "cmd-exec") {
      await executeCommandAction(el.dataset.cmdKind || "", el.dataset.cmdArg || "");
    }
  }

  function handleFieldInput(ev) {
    const el = ev.target;
    if (!el) return;
    if (el.id && MODAL_FIELD_IDS.includes(el.id)) {
      state.modalEdits[el.id] = el.value;
    }
    if (!el.dataset) return;
    const mode = el.dataset.input;
    if (mode === "draft") {
      window._app.updateDraft(el.dataset.recipeId || "", el.dataset.key || "", parseDraftFieldValue(el), false);
    } else if (mode === "chat-meta") {
      window._app.setChatMeta(el.dataset.metaField || "", el.value);
    } else if (mode === "cmd-filter") {
      window._app.filterCmdPalette(el.value);
    } else if (mode === "settings-guard") {
      window._app.setGuardDraftField(el.dataset.guardKey || "", el.value);
    } else if (mode === "secret-draft") {
      window._app.setSecretDraft(el.dataset.slot || "", el.value);
    }
  }

  function handleFieldChange(ev) {
    const el = ev.target;
    if (!el) return;
    if (el.id && MODAL_FIELD_IDS.includes(el.id)) {
      state.modalEdits[el.id] = el.value;
    }
    if (el.id === "m-accel" && isHighMemOnlyAccelerator(el.value)) {
      const highEl = document.getElementById("m-highmem");
      if (highEl) {
        highEl.value = "1";
        state.modalEdits["m-highmem"] = "1";
      }
    }
    if (!el.dataset) return;
    const mode = el.dataset.change;
    if (mode === "draft") {
      window._app.updateDraft(
        el.dataset.recipeId || "",
        el.dataset.key || "",
        parseDraftFieldValue(el),
        el.dataset.rerender === "1"
      );
    } else if (mode === "chat-tunnel") {
      window._app.setChatTunnel(el.value);
    } else if (mode === "settings-guard") {
      window._app.setGuardDraftField(el.dataset.guardKey || "", el.value);
    }
  }

  async function handleFormSubmit(ev) {
    const form = ev.target && typeof ev.target.closest === "function" ? ev.target.closest("form[data-submit]") : ev.target;
    if (!form || !form.dataset || !form.dataset.submit) return;
    const kind = form.dataset.submit;
    const submitBtn = typeof form.querySelector === "function" ? form.querySelector('button[type="submit"]') : null;
    if (kind === "deploy") {
      await window._app.submitDeploy(ev, submitBtn);
    } else if (kind === "chat") {
      await window._app.sendChat(ev);
    } else if (kind === "guard-settings") {
      await window._app.saveGuardSettings(ev, submitBtn);
    } else if (kind === "new-session") {
      await window._app.submitNewSession(ev, submitBtn);
    } else if (kind === "forward") {
      await window._app.submitForward(ev, form.dataset.sessionName || "");
    } else if (kind === "complete-login") {
      await window._app.completeLogin(ev, form.dataset.flowId || "");
    } else if (kind === "api-token") {
      await window._app.submitApiToken(ev);
    }
  }

  ["main-view", "modal-body", "global-alert-banner"].forEach((cid) => {
    const container = document.getElementById(cid);
    if (!container || typeof container.addEventListener !== "function") return;
    container.addEventListener("click", handleActionClick);
    container.addEventListener("input", handleFieldInput);
    container.addEventListener("change", handleFieldChange);
    container.addEventListener("submit", handleFormSubmit);
  });

  /* ========================== Topbar Bindings, Hotkeys & WebSocket ========================== */

  document.getElementById("sidebar-nav").addEventListener("click", (ev) => {
    const btn = ev.target && typeof ev.target.closest === "function" ? ev.target.closest(".nav-btn") : null;
    if (btn && btn.dataset.route) window._app.go(btn.dataset.route);
  });

  const sidebarToggleBtn = document.getElementById("btn-sidebar-toggle");
  if (sidebarToggleBtn && typeof sidebarToggleBtn.addEventListener === "function") {
    sidebarToggleBtn.addEventListener("click", () => {
      toggleSidebar();
    });
  }

  const sidebarBackdrop = document.getElementById("sidebar-backdrop");
  if (sidebarBackdrop && typeof sidebarBackdrop.addEventListener === "function") {
    sidebarBackdrop.addEventListener("click", () => {
      setSidebarOpen(false);
    });
  }

  const cmdBtn = document.getElementById("btn-cmd-palette");
  if (cmdBtn) {
    cmdBtn.addEventListener("click", () => {
      state.cmdQuery = "";
      state.cmdActiveIndex = 0;
      openCommandPalette({ resetQuery: true });
    });
  }

  async function handleGlobalKeydown(ev) {
    if (!ev) return;
    if ((ev.ctrlKey || ev.metaKey) && String(ev.key || "").toLowerCase() === "k") {
      ev.preventDefault();
      state.cmdQuery = "";
      state.cmdActiveIndex = 0;
      openCommandPalette({ resetQuery: true });
      return;
    }
    if (ev.key === "Escape" && state.activeModal) {
      closeModal();
      return;
    }
    if (ev.key === "Escape" && state.sidebarOpen) {
      setSidebarOpen(false);
      const toggleEl = document.getElementById("btn-sidebar-toggle");
      if (toggleEl && typeof toggleEl.focus === "function") toggleEl.focus();
      return;
    }
    if (state.activeModal && state.activeModal.type === "cmd_palette") {
      if (ev.isComposing || ev.keyCode === 229) return;
      const actions = getFilteredCommandActions();
      if (actions.length === 0) return;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        state.cmdActiveIndex = (state.cmdActiveIndex + 1) % actions.length;
        const list = document.getElementById("cmd-list");
        if (list) list.innerHTML = renderCommandPaletteItems();
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        state.cmdActiveIndex = (state.cmdActiveIndex - 1 + actions.length) % actions.length;
        const list = document.getElementById("cmd-list");
        if (list) list.innerHTML = renderCommandPaletteItems();
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        const chosen = actions[state.cmdActiveIndex] || actions[0];
        if (chosen) await executeCommandAction(chosen.kind, chosen.arg);
      }
    }
  }

  if (typeof document.addEventListener === "function") {
    document.addEventListener("keydown", handleGlobalKeydown);
  }

  function openToggleModeModal(nextMode, opts = {}) {
    openModal(`
      <h3 id="modal-title" style="font-size:15px;font-weight:600;margin-bottom:10px;">${tr("切换控制台后端模式", "Switch Console Backend Mode")}</h3>
      <p style="font-size:12.5px;color:var(--text-secondary);margin-bottom:16px;">
        ${nextMode === "real"
          ? tr(
              "即将切换至 <strong>真实 Google Colab 模式</strong>。在此模式下创建的 GPU 实例将真实消耗你 Google 账号的 Compute Units (CCU)。",
              "Switch to <strong>Real Google Colab Mode</strong>. GPU runtimes allocated in this mode will spend real Compute Units (CCU) on your Google account."
            )
          : tr(
              "即将切换至 <strong>演示沙盒模式 (Mock Sandbox)</strong>。所有账号与实例均为本地确定性模拟，不会触碰真实 Google 账号，0 计费消耗。",
              "Switch to <strong>Sandbox Demo Mode</strong>. All accounts and GPU runtimes are simulated locally with zero Google Colab billing."
            )}
      </p>
      <div class="row-actions" style="justify-content:flex-end;">
        <button class="btn" type="button" data-action="close-modal">${tr("取消", "Cancel")}</button>
        <button class="btn ${nextMode === "real" ? "btn-danger" : "btn-primary"}" type="button" id="confirm-mode-switch" data-action="confirm-mode-switch" data-next-mode="${esc(nextMode)}">${nextMode === "real" ? tr("确认切换至真实模式", "Switch to Real Mode") : tr("确认切换至演示沙盒", "Switch to Sandbox Demo")}</button>
      </div>
    `, { type: "toggle_mode", nextMode }, opts);
  }

  document.getElementById("btn-toggle-mode").addEventListener("click", async () => {
    const nextMode = state.settings.backend_mode === "mock" ? "real" : "mock";
    openToggleModeModal(nextMode);
  });

  document.getElementById("btn-stop-all").addEventListener("click", async (ev) => {
    if (isBusy("sessions:stop-all")) return;
    requestStopAll(ev.currentTarget || document.getElementById("btn-stop-all"));
  });

  document.getElementById("btn-lang").addEventListener("click", async () => {
    if (state.busy["settings:lang"]) return;
    state.busy["settings:lang"] = true;
    try {
      const next = state.settings.language === "en" ? "zh-CN" : "en";
      await api("/api/settings", { method: "PATCH", body: JSON.stringify({ language: next }) });
      await refreshBootstrap();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      delete state.busy["settings:lang"];
    }
  });

  document.getElementById("btn-theme").addEventListener("click", async () => {
    if (state.busy["settings:theme"]) return;
    state.busy["settings:theme"] = true;
    try {
      const next = state.settings.theme === "light" ? "dark" : "light";
      await api("/api/settings", { method: "PATCH", body: JSON.stringify({ theme: next }) });
      await refreshBootstrap();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      delete state.busy["settings:theme"];
    }
  });

  function connectEventsWs() {
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = null;
    }
    if (wsDebounceTimer) {
      clearTimeout(wsDebounceTimer);
      wsDebounceTimer = null;
    }
    if (wsSocket) {
      const prev = wsSocket;
      wsSocket = null;
      prev.onopen = null;
      prev.onmessage = null;
      prev.onerror = null;
      prev.onclose = null;
      try {
        if (typeof prev.close === "function") prev.close();
      } catch (_) {}
    }

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const tok = getApiToken();
    const qs = tok ? `?token=${encodeURIComponent(tok)}` : "";
    const ws = new WebSocket(`${proto}//${location.host}/ws/events${qs}`);
    wsSocket = ws;

    ws.onopen = () => {
      if (ws !== wsSocket) return;
      wsReconnectDelay = WS_BACKOFF_BASE_MS;
      state.wsState = "connected";
      renderWsIndicator();
    };
    ws.onmessage = () => {
      if (ws !== wsSocket) return;
      clearTimeout(wsDebounceTimer);
      wsDebounceTimer = setTimeout(() => {
        wsDebounceTimer = null;
        refreshBootstrap().catch(() => {});
      }, 180);
    };
    ws.onerror = () => {
      if (ws !== wsSocket) return;
      clearTimeout(wsDebounceTimer);
      wsDebounceTimer = null;
      state.wsState = "reconnecting";
      renderWsIndicator();
    };
    ws.onclose = () => {
      if (ws !== wsSocket) return;
      clearTimeout(wsDebounceTimer);
      wsDebounceTimer = null;
      state.wsState = "reconnecting";
      renderWsIndicator();
      const delay = wsReconnectDelay;
      wsReconnectDelay = Math.min(WS_BACKOFF_MAX_MS, wsReconnectDelay * 2);
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = setTimeout(() => {
        wsReconnectTimer = null;
        connectEventsWs();
      }, delay);
    };
  }

  window._app.connectEventsWs = connectEventsWs;

  function renderBootstrapErrorCard(err) {
    const view = document.getElementById("main-view");
    if (!view) return;
    const isAuth = err && err.status === 401;
    view.innerHTML = `
      <div class="card init-error-card">
        <div class="card-head" style="margin-bottom:0;">
          <span class="card-title">${tr("控制台初始化失败", "Console Initialization Failed")}</span>
          <span class="dot dot-danger"></span>
        </div>
        <div class="init-error-msg mono">${esc(err?.message || tr("无法连接到后端服务", "Unable to reach backend service"))}</div>
        <div class="row-actions" style="justify-content:flex-end;">
          ${isAuth ? `<button type="button" class="btn" data-action="open-api-token">${tr("输入 API Token", "Enter API Token")}</button>` : ""}
          <button type="button" class="btn btn-primary" data-action="retry-bootstrap">${IC.refresh} <span>${tr("重试连接", "Retry")}</span></button>
        </div>
      </div>
    `;
  }

  async function initConsole() {
    try {
      await refreshBootstrap();
      connectEventsWs();
    } catch (err) {
      state.bootstrapError = err;
      renderBootstrapErrorCard(err);
    }
  }

  function handleHashChange() {
    const nextRoute = parseRouteFromHash("overview");
    if (!nextRoute) return;
    if (nextRoute !== state.route) {
      window._app.go(nextRoute, { fromHash: true });
    }
  }

  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    window.addEventListener("hashchange", handleHashChange);
  }

  if (typeof setInterval === "function") {
    const wdTimer = setInterval(refreshWatchdogCountdowns, 30000);
    if (wdTimer && typeof wdTimer.unref === "function") wdTimer.unref();
  }

  initRouteFromHash();
  initApiTokenFromUrl();
  initConsole();
})();
