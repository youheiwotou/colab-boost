/* Colab Boost Console — Minimalist Design-Engineered SPA (Scheme A) */
(() => {
  const IC = {
    arrowRight: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>',
    refresh: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>',
    plus: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M5 12h14"/></svg>',
    stop: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>',
    clock: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
    link: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
    copy: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    download: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
    search: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>',
    sun: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2m-7.07-15.07 1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>',
    moon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
    send: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>',
    chevronDown: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>',
    chevronUp: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m18 15-6-6-6 6"/></svg>',
  };

  const APPROX_BURN = {
    G4: 1.80,
    A100: 6.93,
    H100: 11.78,
    L4: 2.82,
    T4: 1.17,
    V5E1: 2.11,
    V6E1: 3.55,
    NONE: 0.07,
  };

  const state = {
    route: "overview",
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
    selectedRecipeId: "minimax-h3-studio",
    deployDraft: {},
    advancedOpen: {},
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
    pendingConfirm: null,
    zeroBurnAlert: null,
    busy: {},
    cmdQuery: "",
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
    if (n <= 0) return "0 B";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
    return `${(n / (1024 * 1024)).toFixed(2)} MiB`;
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
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(formatApiDetail(data?.detail, resp.status));
    }
    return data;
  }

  function openModal(html, modalMeta = null) {
    state.activeModal = modalMeta;
    const bd = document.getElementById("modal-backdrop");
    const body = document.getElementById("modal-body");
    body.innerHTML = html;
    bd.classList.remove("hidden");
  }

  function closeModal() {
    state.activeModal = null;
    state.pendingConfirm = null;
    const bd = document.getElementById("modal-backdrop");
    const body = document.getElementById("modal-body");
    if (bd) bd.classList.add("hidden");
    if (body) body.innerHTML = "";
  }

  function openConfirmModal(meta, onConfirm) {
    state.pendingConfirm = onConfirm;
    const isDanger = meta.tone !== "primary";
    const title = typeof meta.title === "function" ? meta.title() : meta.title;
    const bodyHtml = typeof meta.bodyHtml === "function" ? meta.bodyHtml() : meta.bodyHtml;
    const confirmLabel = typeof meta.confirmLabel === "function" ? meta.confirmLabel() : meta.confirmLabel;
    openModal(`
      <h3 style="font-size:15px;font-weight:600;margin-bottom:10px;">${esc(title)}</h3>
      <div class="confirm-body">${bodyHtml}</div>
      <div class="row-actions" style="justify-content:flex-end;margin-top:16px;">
        <button class="btn" type="button" data-action="close-modal">${tr("取消", "Cancel")}</button>
        <button class="btn ${isDanger ? "btn-danger" : "btn-primary"}" type="button" data-action="confirm-ok">${esc(confirmLabel)}</button>
      </div>
    `, { type: "confirm", meta });
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
      if (alert.failedAccounts.length > 0) {
        const accList = alert.failedAccounts
          .map((item) => (typeof item === "string" ? item : (item?.email || item?.account_id || JSON.stringify(item))))
          .join(", ");
        parts.push(tr(`查询失败账号 (${alert.failedAccounts.length}): ${accList}`, `Failed account checks (${alert.failedAccounts.length}): ${accList}`));
      }
      if (alert.stopErrors.length > 0) {
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
    const modalFieldIds = ["m-accel", "m-vram", "m-autostop", "m-highmem", "f-rport", "f-lport", "f-label", "oauth-code", "cmd-search"];
    const saved = {};
    for (const fid of modalFieldIds) {
      const el = document.getElementById(fid);
      if (el && el.value !== undefined && el.value !== "") saved[fid] = el.value;
    }
    if (active.type === "new_session") window._app.openNewSessionModal();
    else if (active.type === "forward") window._app.openForwardModal(active.sessionName);
    else if (active.type === "login" && active.flow) window._app.renderLoginModalWithFlow(active.flow);
    else if (active.type === "toggle_mode") openToggleModeModal(active.nextMode);
    else if (active.type === "cmd_palette") openCommandPalette();
    else if (active.type === "confirm" && active.meta && state.pendingConfirm) openConfirmModal(active.meta, state.pendingConfirm);
    for (const [fid, val] of Object.entries(saved)) {
      const restored = document.getElementById(fid);
      if (restored) restored.value = val;
    }
  }

  document.getElementById("modal-backdrop").addEventListener("click", (ev) => {
    if (ev.target.id === "modal-backdrop") closeModal();
  });

  async function refreshBootstrap() {
    const snap = await api("/api/bootstrap");
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

    if (!state.recipes.find((r) => r.id === state.selectedRecipeId) && state.recipes.length > 0) {
      state.selectedRecipeId = state.recipes[0].id;
    }
    document.documentElement.setAttribute("data-theme", state.settings.theme || "dark");
    renderChrome();
    renderMain();
    if (state.activeModal) renderActiveModal();
  }

  function formatRemainingDeadline(epochSec) {
    if (!epochSec) return tr("未设限", "No limit");
    const diffMin = Math.max(0, Math.round((epochSec - Date.now() / 1000) / 60));
    const h = Math.floor(diffMin / 60);
    const m = diffMin % 60;
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
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
      dot.className = `dot ${state.wsState === "connected" ? "dot-emerald" : "dot-amber"}`;
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
    modeBtn.textContent = isMock
      ? tr("演示沙盒 · 0 计费", "Sandbox · 0 Billed")
      : tr("真实 Colab · 实时计费", "Real Colab · Live");

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
        const hrs = u.hours_remaining ? ` · ~${u.hours_remaining}h` : "";
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
    } else {
      if (stopAllGroup) stopAllGroup.style.display = "none";
      stopAllBtn.style.display = "none";
      stopAllBtn.disabled = false;
    }

    document.getElementById("btn-lang").textContent = state.settings.language === "en" ? "中文" : "EN";
    const themeBtn = document.getElementById("btn-theme");
    themeBtn.innerHTML = state.settings.theme === "light" ? IC.moon : IC.sun;
    themeBtn.title = state.settings.theme === "light"
      ? tr("切换至炭黑暗色主题", "Switch to Dark Theme")
      : tr("切换至极简瓷白主题", "Switch to Light Theme");

    document.querySelectorAll("[data-i18n]").forEach((node) => {
      const k = node.getAttribute("data-i18n");
      if (dict[k]) node.textContent = dict[k];
    });

    document.querySelectorAll(".nav-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.route === state.route);
    });
    document.getElementById("badge-runs").textContent = state.runs.filter((r) => ["provisioning", "starting", "running", "ready"].includes(r.state)).length;
    document.getElementById("badge-sessions").textContent = state.sessions.length;
    document.getElementById("badge-tunnels").textContent = state.tunnels.length;
    document.getElementById("badge-receipts").textContent = state.receipts.length;
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
    const timeText = u.hours_remaining ? `${u.hours_remaining}` : (u.burn_rate_hourly === 0 ? "INF" : "--");

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
      const init = {
        _sessionMode: "new",
        _accelerator: recipe.provision.accelerator,
        _minVram: recipe.provision.min_vram_mib || 0,
        _highMem: !!recipe.provision.high_mem,
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
    const estBurn = APPROX_BURN[accel] ?? 2.0;
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

  function renderDeploy() {
    const recipe = state.recipes.find((r) => r.id === state.selectedRecipeId) || state.recipes[0];
    if (!recipe) return `<div class="card">${tr("未找到部署模板", "No deployment recipes found")}</div>`;
    const draft = ensureDeployDraft(recipe);

    const allInputs = recipe.inputs || [];
    const primaryInputs = allInputs.slice(0, 4);
    const advancedInputs = allInputs.slice(4);
    const advOpen = !!state.advancedOpen[recipe.id];
    const firstWarm = state.sessions[0] || null;

    const gpuTiers = [
      { id: "G4", name: "G4 Blackwell", vram: "96 GB", sub: "sm120 · High-RAM", minVram: 80000, highMem: true, tag: tr("视频推荐", "Video") },
      { id: "A100", name: "A100 80GB", vram: "80 GB", sub: tr("自动退回 40G", "Auto-retry 40G"), minVram: 70000, highMem: true, tag: tr("锁显存", "80G Lock") },
      { id: "L4", name: "L4", vram: "24 GB", sub: tr("Ada · 性价比", "Ada · Balanced"), minVram: 0, highMem: false, tag: "" },
      { id: "T4", name: "T4", vram: "16 GB", sub: tr("轻量 / 音频", "Light / ASR"), minVram: 0, highMem: false, tag: "" },
    ];

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
                    <span>~${(APPROX_BURN[g.id] || 1.0).toFixed(2)}/h</span>
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

          ${recipe.id === "llama-cpp-gguf" ? renderGgufPlannerBox(draft) : ""}

          ${advancedInputs.length > 0 || true ? `
            <button type="button" class="disclosure-btn" data-action="toggle-advanced" data-recipe-id="${esc(recipe.id)}">
              <span>${tr("高级参数与精细显存保底设置", "Advanced Recipe Inputs & Hardware Fine-Tuning")}</span>
              <span class="mono row-actions" style="gap:5px;">
                <span>${advOpen ? tr("收起", "Hide") : tr(`展开 ${advancedInputs.length + 3} 项`, `Show ${advancedInputs.length + 3}`)}</span>
                ${advOpen ? IC.chevronUp : IC.chevronDown}
              </span>
            </button>
          ` : ""}

          ${advOpen ? `
            <div class="advanced-drawer">
              <div class="form-grid">
                ${advancedInputs.map((inp) => renderRecipeInputField(recipe.id, inp, draft[inp.id])).join("")}
                <div class="field">
                  <label>${tr("运行位置", "Target Session")}</label>
                  <select name="_sessionMode" data-change="draft" data-recipe-id="${esc(recipe.id)}" data-key="_sessionMode" data-rerender="1">
                    <option value="new" ${draft._sessionMode === "new" ? "selected" : ""}>${tr("新建专属 GPU 实例（失败自动释放）", "Create Fresh GPU Session")}</option>
                    ${state.sessions.map((s) => `
                      <option value="${esc(s.name)}" ${draft._sessionMode === s.name ? "selected" : ""}>
                        ${tr("复用运行中", "Reuse Active")}: ${esc(s.name)} (${esc(s.gpu_name || s.accelerator)})
                      </option>
                    `).join("")}
                  </select>
                </div>
                ${draft._sessionMode === "new" ? `
                  <div class="field">
                    <label>${tr("加速器型号", "Accelerator")}</label>
                    <select name="_accelerator" data-change="draft" data-recipe-id="${esc(recipe.id)}" data-key="_accelerator" data-rerender="1">
                      ${["G4", "A100", "H100", "L4", "T4", "NONE"].map((acc) => `
                        <option value="${acc}" ${draft._accelerator === acc ? "selected" : ""}>${acc === "NONE" ? "CPU (NONE)" : acc}</option>
                      `).join("")}
                    </select>
                  </div>
                  <div class="field">
                    <label>${tr("最低显存保底 (MiB，不达标自动重抽)", "Min VRAM MiB (Auto-Retry)")}</label>
                    <input type="number" min="0" max="163840" step="1024" value="${esc(draft._minVram)}"
                      data-change="draft" data-recipe-id="${esc(recipe.id)}" data-key="_minVram" data-val-type="number" />
                  </div>
                  <div class="field">
                    <label>${tr("高内存机型 (High-RAM)", "High-RAM Shape")}</label>
                    <select data-change="draft" data-recipe-id="${esc(recipe.id)}" data-key="_highMem" data-val-type="bool">
                      <option value="0" ${!draft._highMem ? "selected" : ""}>Standard RAM</option>
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

            ${r.log_tail?.length ? `<div class="terminal">${esc(r.log_tail.join("\n"))}</div>` : ""}
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
    let control = "";
    if (inp.type === "select") {
      control = `
        <select data-field-key="${esc(fkey)}" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}">
          ${(inp.options || []).map((opt) => `<option value="${esc(opt)}" ${String(val) === String(opt) ? "selected" : ""}>${esc(opt)}</option>`).join("")}
        </select>
      `;
    } else if (inp.type === "bool") {
      const checked = val === true || val === "1" || val === "true";
      control = `
        <select data-field-key="${esc(fkey)}" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}" data-val-type="bool">
          <option value="1" ${checked ? "selected" : ""}>${tr("是 (Enabled)", "Enabled")}</option>
          <option value="0" ${!checked ? "selected" : ""}>${tr("否 (Disabled)", "Disabled")}</option>
        </select>
      `;
    } else if (inp.type === "number") {
      control = `
        <input type="number" data-field-key="${esc(fkey)}" value="${esc(val)}" ${inp.min != null ? `min="${inp.min}"` : ""} ${inp.max != null ? `max="${inp.max}"` : ""}
          data-input="draft" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}" data-val-type="number-optional" />
      `;
    } else if (inp.type === "secret") {
      control = `
        <input type="password" autocomplete="off" data-field-key="${esc(fkey)}" value="${esc(val)}" placeholder="${tr("留空表示不启用密钥验证", "Leave blank if no API key required")}"
          data-input="draft" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}" />
      `;
    } else if (inp.type === "hf-gguf") {
      control = `
        <div class="row-actions" style="width:100%;">
          <input type="text" data-field-key="${esc(fkey)}" style="flex:1;" value="${esc(val)}" placeholder="owner/repo-GGUF"
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
        <input type="text" list="quant-datalist" data-field-key="${esc(fkey)}" value="${esc(val)}"
          data-input="draft" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}" />
        <datalist id="quant-datalist">
          ${suggestions.map((q) => `<option value="${esc(q)}"></option>`).join("")}
        </datalist>
      `;
    } else {
      control = `
        <input type="text" data-field-key="${esc(fkey)}" value="${esc(val)}"
          data-input="draft" data-change="draft" data-recipe-id="${esc(recipeId)}" data-key="${esc(inp.id)}" />
      `;
    }

    return `
      <div class="field ${inp.type === "hf-gguf" ? "full" : ""}">
        <label>${esc(label)}${inp.required ? " *" : ""}</label>
        ${control}
        ${help ? `<span class="field-help">${esc(help)}</span>` : ""}
      </div>
    `;
  }

  function renderGgufPlannerBox() {
    if (state.ggufPlanLoading) {
      return `<div class="banner info" style="margin-top:14px;">${tr("正在读取 Hugging Face GGUF 仓库与二进制文件头 KV 元数据...", "Inspecting Hugging Face GGUF repository & binary header KV metadata...")}</div>`;
    }
    const p = state.ggufPlan;
    if (!p) return "";
    return `
      <div class="card" style="margin-top:14px;background:var(--bg-input);">
        <div class="card-head">
          <span class="card-title">${tr("GGUF 多卡显存自动适配矩阵", "GGUF Multi-GPU VRAM Fit Matrix")} — <code>${esc(p.repo)}</code></span>
          <span class="tag accent mono">${tr("架构", "Arch")}: ${esc(p.architecture || "general")} | Max Ctx: ${esc(p.model_ctx || 32768)}</span>
        </div>
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
                  <span>${tr("预估速率", "Est. Burn")}: <strong>~${s.estimated_burn_hourly} CCU/h</strong> (${tr("累计", "Spent")} ~${s.estimated_spent_ccu})</span>
                  <span>${tr("看门狗倒计时", "Watchdog")}: <strong>${formatRemainingDeadline(s.watchdog?.deadline_epoch)}</strong></span>
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
    const welcomeText = tr(
      "部署好 GGUF 或 vLLM 模型并开启本地映射后，可直接在此测试推理延迟与吞吐速度 (tok/s)。",
      "Deploy a GGUF or vLLM model and forward its local port to benchmark inference latency and throughput (tok/s) here."
    );
    return `
      <div class="page-header">
        <div>
          <h1 class="page-title">${tr("本地端口映射与对话演练场", "Localhost Port Forwards & API Playground")}</h1>
          <p class="page-subtitle">${tr("通过原生 OpenSSH + Colab WebSocket 多路复用将云端端口安全映射至 127.0.0.1（不修改 ~/.ssh/config）。", "Multiplexes Colab VM ports to 127.0.0.1 via native OpenSSH over WebSocket without editing ~/.ssh/config.")}</p>
        </div>
        ${state.sessions.length > 0 ? `<button class="btn btn-primary" type="button" data-action="open-forward" data-session-name="${esc(state.sessions[0].name)}">${IC.plus} <span>${tr("新建端口映射", "New Port Forward")}</span></button>` : ""}
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
            <select id="chat-tunnel-select" data-change="chat-tunnel" style="padding:6px 10px;border-radius:var(--radius-sm);background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-subtle);font-size:12px;">
              ${openTunnels.length > 0
                ? openTunnels.map((t) => `<option value="${esc(t.id)}" ${t.id === activeTunnelId ? "selected" : ""}>${esc(t.session_name)} — ${esc(t.local_url)}</option>`).join("")
                : `<option value="">${tr("默认沙盒端点 (127.0.0.1:8000/v1)", "Default Sandbox (127.0.0.1:8000/v1)")}</option>`}
            </select>
            <input type="text" id="chat-model-input" value="${esc(state.chatModel)}" data-input="chat-meta" data-meta-field="chatModel"
              placeholder="model (auto)" title="${tr("模型名，auto 自动识别已部署模型", "Model ID; 'auto' detects active model")}"
              style="width:130px;padding:6px 9px;border-radius:var(--radius-sm);background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-subtle);font-size:12px;" />
            <input type="password" autocomplete="off" id="chat-apikey-input" value="${esc(state.chatApiKey)}" data-input="chat-meta" data-meta-field="chatApiKey"
              placeholder="${tr("API Key (可选)", "API Key (optional)")}"
              style="width:135px;padding:6px 9px;border-radius:var(--radius-sm);background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-subtle);font-size:12px;" />
          </div>
        </div>
        <div class="chat-box" id="chat-box">
          ${state.chatMessages.map((m) => `<div class="chat-msg ${esc(m.role)}">${esc(m._welcome ? welcomeText : m.content)}</div>`).join("")}
        </div>
        <form class="row-actions" data-submit="chat">
          <input type="text" id="chat-input" value="${esc(state.chatDraft)}" data-input="chat-meta" data-meta-field="chatDraft"
            style="flex:1;padding:9px 11px;border-radius:var(--radius-sm);border:1px solid var(--border-subtle);background:var(--bg-input);color:var(--text-primary);font-size:13px;"
            placeholder="${tr("输入测试提示词以实测延迟与 tok/s...", "Enter a test prompt to measure latency & tok/s...")}" ${state.chatBusy ? "disabled" : ""} />
          <button type="submit" class="btn btn-primary" ${state.chatBusy ? "disabled" : ""}>${IC.send} <span>${state.chatBusy ? tr("生成中...", "Generating...") : tr("发送测试", "Send")}</span></button>
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
          <code>data/receipts/</code>
        </div>
        ${state.receipts.length === 0 ? `<div class="kpi-foot">${tr("暂无释放回执记录。每次手动关机、显存重抽或护栏触发释放实例均会在此生成可核验回执。", "No unassign receipts yet. Every manual stop, VRAM retry release, and cost-guard stop writes a verifiable receipt here.")}</div>` : `
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
              ${state.receipts.slice(0, 25).map((rc) => `
                <tr>
                  <td class="mono" style="font-size:11.5px;">${esc(rc.timestamp.replace("T", " ").slice(0, 19))}</td>
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
        `}
      </div>
    `;
  }

  function renderSettings() {
    const g = state.settings.guards || {};
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
          </div>
          <form data-submit="guard-settings" class="form-grid">
            <div class="field">
              <label>${tr("默认 VM 自动关机限时 (分钟，0=不限时)", "Default VM Auto-Stop (minutes, 0=unlimited)")}</label>
              <input type="number" id="set-autostop" min="0" max="1440" value="${g.auto_stop_minutes ?? 120}" />
              <span class="field-help">${tr("运行在 Colab VM 内部，关闭本地电脑也会准时 unassign", "Runs detached inside Colab VM; unassigns even if console is closed")}</span>
            </div>
            <div class="field">
              <label>${tr("GPU 空闲自动关机 (分钟，0=禁用)", "GPU Idle Auto-Stop (minutes, 0=disabled)")}</label>
              <input type="number" id="set-idlestop" min="0" max="720" value="${g.idle_stop_minutes ?? 30}" />
              <span class="field-help">${tr("同时检测任务心跳与 nvidia-smi 利用率 <5%", "Checks both worker heartbeat and nvidia-smi <5%")}</span>
            </div>
            <div class="field">
              <label>${tr("账号算力余额保护红线 (CCU)", "Account Balance Floor (CCU)")}</label>
              <input type="number" step="0.5" min="0" id="set-floor" value="${g.balance_floor_ccu ?? 5}" />
              <span class="field-help">${tr("低于此余额自动释放该账号下所有实例", "Stops all runtimes on the account when live balance drops to floor")}</span>
            </div>
            <div class="field">
              <label>${tr("单实例累计消耗上限 (CCU)", "Per-Session Spend Cap (CCU)")}</label>
              <input type="number" step="1" min="0" id="set-cap" value="${g.session_spend_cap_ccu ?? 20}" />
            </div>
            <div class="field">
              <label>${tr("显存不达标最大重抽次数", "Max VRAM Provisioning Retries")}</label>
              <input type="number" min="1" max="8" id="set-retries" value="${g.max_vram_retries ?? 4}" />
            </div>
            <div class="field">
              <label>${tr("最大受管实例并发上限", "Max Managed Sessions")}</label>
              <input type="number" min="1" max="5" id="set-maxsess" value="${g.max_managed_sessions ?? 3}" />
            </div>
            <div class="field full" style="align-items:flex-end;">
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
            <label>Hugging Face Token — ${state.secrets.hf_token ? `<span class="tag ok">${tr("已保存", "Saved")}</span>` : `<span class="tag">${tr("未配置", "Not set")}</span>`}</label>
            <div class="row-actions">
              <input type="password" autocomplete="off" id="inp-hf-token" style="flex:1;" placeholder="hf_xxxxxxxxxxxxxxxxxxxx" />
              <button class="btn btn-sm btn-primary${isBusy("secret:save:hf_token") ? " is-busy" : ""}" type="button" data-action="save-secret" data-slot="hf_token" data-input-id="inp-hf-token" ${isBusy("secret:save:hf_token") ? "disabled" : ""}>${tr("保存", "Save")}</button>
              ${state.secrets.hf_token ? `<button class="btn btn-sm btn-danger${isBusy("secret:delete:hf_token") ? " is-busy" : ""}" type="button" data-action="delete-secret" data-slot="hf_token" ${isBusy("secret:delete:hf_token") ? "disabled" : ""}>${tr("清除", "Clear")}</button>` : ""}
            </div>
          </div>

          <div class="field" style="margin-bottom:18px;">
            <label>Civitai API Token — ${state.secrets.civitai_token ? `<span class="tag ok">${tr("已保存", "Saved")}</span>` : `<span class="tag">${tr("未配置", "Not set")}</span>`}</label>
            <div class="row-actions">
              <input type="password" autocomplete="off" id="inp-civ-token" style="flex:1;" placeholder="Civitai API Token" />
              <button class="btn btn-sm btn-primary${isBusy("secret:save:civitai_token") ? " is-busy" : ""}" type="button" data-action="save-secret" data-slot="civitai_token" data-input-id="inp-civ-token" ${isBusy("secret:save:civitai_token") ? "disabled" : ""}>${tr("保存", "Save")}</button>
              ${state.secrets.civitai_token ? `<button class="btn btn-sm btn-danger${isBusy("secret:delete:civitai_token") ? " is-busy" : ""}" type="button" data-action="delete-secret" data-slot="civitai_token" ${isBusy("secret:delete:civitai_token") ? "disabled" : ""}>${tr("清除", "Clear")}</button>` : ""}
            </div>
          </div>

          ${state.settings.backend_mode === "mock" ? `
            <div class="divider"></div>
            <label style="font-size:12px;font-weight:500;color:var(--text-secondary);">${tr("演示沙盒预设场景", "Sandbox Demo Scenario")}</label>
            <div class="row-actions" style="margin-top:8px;">
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

  function renderMain() {
    const view = document.getElementById("main-view");
    const active = document.activeElement;
    let focusSnap = null;
    if (active && view.contains(active) && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)) {
      focusSnap = {
        id: active.id || "",
        fkey: active.getAttribute("data-field-key") || "",
        value: active.value,
        preserveValue: ["inp-hf-token", "inp-civ-token", "set-autostop", "set-idlestop", "set-floor", "set-cap", "set-retries", "set-maxsess"].includes(active.id || ""),
        selStart: typeof active.selectionStart === "number" ? active.selectionStart : null,
        selEnd: typeof active.selectionEnd === "number" ? active.selectionEnd : null,
      };
    }

    if (state.route === "overview") view.innerHTML = renderOverview();
    else if (state.route === "deploy") view.innerHTML = renderDeploy();
    else if (state.route === "sessions") view.innerHTML = renderSessions();
    else if (state.route === "tunnels") view.innerHTML = renderTunnelsAndChat();
    else if (state.route === "accounts") view.innerHTML = renderAccountsAndReceipts();
    else if (state.route === "settings") view.innerHTML = renderSettings();

    if (focusSnap) {
      let restored = null;
      if (focusSnap.id) restored = document.getElementById(focusSnap.id);
      else if (focusSnap.fkey) restored = view.querySelector(`[data-field-key="${CSS.escape(focusSnap.fkey)}"]`);
      if (restored) {
        if (focusSnap.preserveValue) restored.value = focusSnap.value;
        restored.focus({ preventScroll: true });
        if (focusSnap.selStart !== null && typeof restored.setSelectionRange === "function") {
          try { restored.setSelectionRange(focusSnap.selStart, focusSnap.selEnd); } catch (_) {}
        }
      }
    }
  }

  function renderCommandPaletteItems() {
    const q = (state.cmdQuery || "").trim().toLowerCase();
    const actions = [
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

    return actions.map((a) => `
      <button type="button" class="cmd-item" data-action="cmd-exec" data-cmd-kind="${esc(a.kind)}" data-cmd-arg="${esc(a.arg)}">
        <span>${esc(a.label)}</span>
        <span class="cmd-sub mono">${esc(a.sub)}</span>
      </button>
    `).join("") || `<div class="kpi-foot" style="padding:12px;text-align:center;">${tr("无匹配命令", "No matching commands")}</div>`;
  }

  function openCommandPalette() {
    openModal(`
      <div class="field">
        <input type="text" id="cmd-search" value="${esc(state.cmdQuery)}" placeholder="${tr("输入命令、页面或配方名快速跳转 (Esc 关闭)...", "Type a command, page, or recipe name (Esc to close)...")}"
          data-input="cmd-filter" />
      </div>
      <div class="cmd-list" id="cmd-list">
        ${renderCommandPaletteItems()}
      </div>
    `, { type: "cmd_palette" });
    const inp = document.getElementById("cmd-search");
    if (inp && typeof inp.focus === "function") inp.focus();
  }

  /* ========================== Actions ========================== */

  async function executeStopAll(triggerEl = null) {
    return runLocked("sessions:stop-all", triggerEl, async () => {
      try {
        const res = await api("/api/sessions/stop-all", { method: "POST" });
        const remaining = Number(res.remaining_sessions || 0);
        const failedAccounts = Array.isArray(res.failed_accounts) ? res.failed_accounts : [];
        const stopErrors = Array.isArray(res.stop_errors) ? res.stop_errors : [];
        const verified =
          res.zero_burn_verified === true &&
          remaining === 0 &&
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
            failedAccounts,
            stopErrors,
          };
          renderGlobalAlert();
          toast(
            tr(
              `零燃费核验失败：残留 ${remaining} 个实例或存在未确认异常，请查看顶部告警并重试！`,
              `Zero-burn verification failed: ${remaining} remaining session(s) or unverified errors — check alert banner and retry!`
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
    if (valType === "number") return Number(el.value);
    if (valType === "number-optional") return el.value === "" ? "" : Number(el.value);
    return el.value;
  }

  window._app = {
    toast,
    closeModal,
    refreshBootstrap,
    openCommandPalette,
    filterCmdPalette(val) {
      state.cmdQuery = val;
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
    go(route) {
      state.route = route;
      renderChrome();
      renderMain();
    },
    pickRecipe(id) {
      state.selectedRecipeId = id;
      state.route = "deploy";
      renderChrome();
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
      d._highMem = !!highMem;
      state.deployDraft[recipeId] = d;
      renderMain();
    },
    updateDraft(recipeId, key, val, rerender = false) {
      if (!state.deployDraft[recipeId]) state.deployDraft[recipeId] = {};
      state.deployDraft[recipeId][key] = val;
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
      state.ggufPlanLoading = true;
      renderMain();
      try {
        const q = new URLSearchParams({
          repo,
          quant: d.quant || "auto",
          ctx: String(d.ctx || 0),
          kv: d.kv || "auto",
        });
        state.ggufPlan = await api(`/api/hf/gguf-plan?${q.toString()}`);
      } catch (err) {
        toast(err.message, "err");
      } finally {
        state.ggufPlanLoading = false;
        renderMain();
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
          high_mem: !!draft._highMem,
        } : null,
      };

      const executeDeploy = () => runLocked("deploy:submit", triggerEl, async () => {
        try {
          await api("/api/deployments", { method: "POST", body: JSON.stringify(body) });
          toast(tr("部署任务已启动，正在实时追踪进度...", "Deployment started; streaming live progress..."), "ok");
          await refreshBootstrap();
        } catch (err) {
          toast(err.message, "err");
        }
      });

      if (state.settings.backend_mode === "real" && draft._sessionMode === "new") {
        const estBurn = APPROX_BURN[accel] ?? 2.0;
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
                <div class="kv-row"><span>${tr("目标算力", "Target Accelerator")}</span><span class="mono strong">${esc(accel)}${minVram > 0 ? ` (>= ${minVram} MiB)` : ""}${draft._highMem ? " · High-RAM" : ""}</span></div>
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
          await api(`/api/deployments/${encodeURIComponent(runId)}/keep/sync`, { method: "POST" });
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
    openNewSessionModal() {
      openModal(`
        <h3 style="font-size:15px;font-weight:600;margin-bottom:14px;">${tr("新建 Colab 算力实例", "New Colab GPU Session")}</h3>
        <form data-submit="new-session" class="form-grid">
          <div class="field">
            <label>${tr("加速器型号", "Accelerator")}</label>
            <select id="m-accel">
              <option value="G4">G4 Blackwell (96 GB)</option>
              <option value="A100">A100 GPU (40/80 GB)</option>
              <option value="L4">L4 GPU (24 GB)</option>
              <option value="T4">T4 GPU (16 GB)</option>
              <option value="H100">H100 GPU (80 GB)</option>
              <option value="V5E1">TPU v5e-1</option>
              <option value="NONE">CPU (NONE)</option>
            </select>
          </div>
          <div class="field">
            <label>${tr("最低显存要求 (MiB，0=不限)", "Min VRAM (MiB, 0 = any)")}</label>
            <input type="number" id="m-vram" min="0" step="1024" value="0" />
            <span class="field-help">${tr("填 70000 可自动退回 40G A100 直至抽中 80G", "Set 70000 to auto-release 40 GB A100s until an 80 GB VM is assigned")}</span>
          </div>
          <div class="field">
            <label>${tr("自动关机限时 (分钟)", "Auto-Stop Limit (minutes)")}</label>
            <input type="number" id="m-autostop" min="0" max="1440" value="${state.settings.guards?.auto_stop_minutes ?? 120}" />
          </div>
          <div class="field">
            <label>${tr("高内存 (High-RAM)", "High-RAM Shape")}</label>
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
      `, { type: "new_session" });
    },
    async submitNewSession(ev, triggerEl = null) {
      ev.preventDefault();
      if (isBusy("session:create")) return;
      const accelEl = document.getElementById("m-accel");
      const vramEl = document.getElementById("m-vram");
      const autoEl = document.getElementById("m-autostop");
      const highEl = document.getElementById("m-highmem");
      const body = {
        accelerator: accelEl ? accelEl.value : "G4",
        min_vram_mib: Number((vramEl && vramEl.value) || 0),
        auto_stop_minutes: autoEl && autoEl.value !== "" ? Number(autoEl.value) : 120,
        high_mem: highEl ? highEl.value === "1" : false,
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
        const estBurn = APPROX_BURN[body.accelerator] ?? 2.0;
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
    openForwardModal(sessionName) {
      openModal(`
        <h3 style="font-size:15px;font-weight:600;margin-bottom:14px;">${tr("映射云端端口至本机 127.0.0.1", "Forward Colab VM Port to 127.0.0.1")}</h3>
        <form data-submit="forward" data-session-name="${esc(sessionName)}" class="form-grid">
          <div class="field">
            <label>${tr("云端 VM 端口 (禁填 8080)", "Colab VM Port (avoid 8080)")}</label>
            <input type="number" id="f-rport" min="1024" max="65535" value="8000" required />
          </div>
          <div class="field">
            <label>${tr("本地监听端口 (0=同云端或自动分配)", "Local Port (0 = match VM or auto-pick)")}</label>
            <input type="number" id="f-lport" min="0" max="65535" value="8000" />
          </div>
          <div class="field full">
            <label>${tr("备注名称", "Label")}</label>
            <input type="text" id="f-label" value="Custom Service" />
          </div>
          <div class="field full row-actions" style="justify-content:flex-end;margin-top:8px;">
            <button type="button" class="btn" data-action="close-modal">${tr("取消", "Cancel")}</button>
            <button type="submit" class="btn btn-primary">${tr("开启映射", "Start Forward")}</button>
          </div>
        </form>
      `, { type: "forward", sessionName });
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
      state.selectedChatTunnelId = tunnelId || "";
      state.chatMessages.push({ role: "user", content: text });
      state.chatDraft = "";
      if (inp) inp.value = "";
      state.chatBusy = true;
      renderMain();

      try {
        const res = await api("/api/playground/chat", {
          method: "POST",
          body: JSON.stringify({
            tunnel_id: tunnelId || null,
            model: (state.chatModel || "auto").trim() || "auto",
            api_key: (state.chatApiKey || "").trim() || null,
            messages: state.chatMessages.filter((m) => m.role !== "system" && !m._welcome),
          }),
        });
        state.chatMessages.push({
          role: "assistant",
          content: `${res.reply}\n\n— ${res.latency_ms} ms | ${res.tok_per_s} tok/s`,
        });
      } catch (err) {
        state.chatMessages.push({ role: "assistant", content: `[ERROR] ${tr("请求失败", "Request failed")}: ${err.message}` });
      } finally {
        state.chatBusy = false;
        renderMain();
        const box = document.getElementById("chat-box");
        if (box) box.scrollTop = box.scrollHeight;
      }
    },
    renderLoginModalWithFlow(flow) {
      const isMock = state.settings.backend_mode === "mock";
      openModal(`
        <h3 style="font-size:15px;font-weight:600;margin-bottom:10px;">${tr("添加 Google Colab 账号", "Add Google Colab Account")}</h3>
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
            <label>${isMock ? tr("模拟邮箱或任意授权码", "Simulated Email or Auth Code") : tr("Google 授权码 (Authorization Code)", "Google Authorization Code")}</label>
            <input type="text" id="oauth-code" autocomplete="off" required placeholder="${isMock ? "researcher@gmail.com" : "4/0AeanS0..."}" />
          </div>
          <div class="field full row-actions" style="justify-content:flex-end;">
            <button type="button" class="btn" data-action="close-modal">${tr("取消", "Cancel")}</button>
            <button type="submit" class="btn btn-primary">${tr("完成验证并保存", "Verify & Save Account")}</button>
          </div>
        </form>
      `, { type: "login", flow });
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
    async saveGuardSettings(ev, triggerEl = null) {
      ev.preventDefault();
      const guards = {
        auto_stop_minutes: Number(document.getElementById("set-autostop").value),
        idle_stop_minutes: Number(document.getElementById("set-idlestop").value),
        balance_floor_ccu: Number(document.getElementById("set-floor").value),
        session_spend_cap_ccu: Number(document.getElementById("set-cap").value),
        max_vram_retries: Number(document.getElementById("set-retries").value),
        max_managed_sessions: Number(document.getElementById("set-maxsess").value),
      };
      return runLocked("settings:guards", triggerEl, async () => {
        try {
          await api("/api/settings", { method: "PATCH", body: JSON.stringify({ guards }) });
          toast(tr("成本护栏配置已保存", "Cost guard settings saved"), "ok");
          await refreshBootstrap();
        } catch (err) {
          toast(err.message, "err");
        }
      });
    },
    async saveSecret(slot, inputId, triggerEl = null) {
      const inp = document.getElementById(inputId);
      const val = inp ? inp.value.trim() : "";
      if (!val) return toast(tr("请输入有效 Token", "Please enter a valid token"), "err");
      return runLocked(`secret:save:${slot}`, triggerEl, async () => {
        try {
          await api(`/api/secrets/${encodeURIComponent(slot)}`, {
            method: "PUT",
            body: JSON.stringify({ value: val }),
          });
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
    } else if (action === "cmd-exec") {
      const kind = el.dataset.cmdKind || "";
      const arg = el.dataset.cmdArg || "";
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
  }

  function handleFieldInput(ev) {
    const el = ev.target;
    if (!el || !el.dataset) return;
    const mode = el.dataset.input;
    if (mode === "draft") {
      window._app.updateDraft(el.dataset.recipeId || "", el.dataset.key || "", parseDraftFieldValue(el), false);
    } else if (mode === "chat-meta") {
      window._app.setChatMeta(el.dataset.metaField || "", el.value);
    } else if (mode === "cmd-filter") {
      window._app.filterCmdPalette(el.value);
    }
  }

  function handleFieldChange(ev) {
    const el = ev.target;
    if (!el || !el.dataset) return;
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
    const btn = ev.target.closest(".nav-btn");
    if (btn && btn.dataset.route) window._app.go(btn.dataset.route);
  });

  const cmdBtn = document.getElementById("btn-cmd-palette");
  if (cmdBtn) {
    cmdBtn.addEventListener("click", () => {
      state.cmdQuery = "";
      openCommandPalette();
    });
  }

  if (typeof document.addEventListener === "function") {
    document.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") {
        ev.preventDefault();
        state.cmdQuery = "";
        openCommandPalette();
      } else if (ev.key === "Escape" && state.activeModal) {
        closeModal();
      }
    });
  }

  function openToggleModeModal(nextMode) {
    openModal(`
      <h3 style="font-size:15px;font-weight:600;margin-bottom:10px;">${tr("切换控制台后端模式", "Switch Console Backend Mode")}</h3>
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
    `, { type: "toggle_mode", nextMode });
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
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/events`);
    let debounceTimer = null;

    ws.onopen = () => {
      state.wsState = "connected";
      renderWsIndicator();
    };
    ws.onmessage = () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => refreshBootstrap().catch(() => {}), 180);
    };
    ws.onclose = () => {
      state.wsState = "reconnecting";
      renderWsIndicator();
      setTimeout(connectEventsWs, 2500);
    };
  }

  refreshBootstrap().then(() => connectEventsWs()).catch((err) => {
    document.getElementById("main-view").innerHTML = `<div class="card">${tr("初始化失败", "Initialization failed")}: ${esc(err.message)}</div>`;
  });
})();
