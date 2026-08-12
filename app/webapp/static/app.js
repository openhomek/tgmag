const tg = window.Telegram?.WebApp;
const INIT_DATA_SESSION_KEY = "tgmag.telegramInitData";
const ACCOUNT_PAGE_SIZE = 10;
const state = {
  bootstrap: null,
  activeView: "dashboard",
  activePane: "phone",
  activeAccountId: null,
  accountPage: 1,
  accountPages: 0,
  accountTotal: 0,
  accountQuery: "",
  accountRequestGeneration: 0,
  noticeTimer: null,
  qrLoginGeneration: 0,
  qrLoginId: null,
};

const qs = (selector, root = document) => root.querySelector(selector);
const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];

function telegramInitData() {
  const sdkValue = window.Telegram?.WebApp?.initData;
  let hashValue = "";
  try {
    hashValue = new URLSearchParams(window.location.hash.replace(/^#/, "")).get("tgWebAppData") || "";
  } catch (_) {
    hashValue = "";
  }
  const freshValue = sdkValue || hashValue;
  if (freshValue) {
    try { window.sessionStorage.setItem(INIT_DATA_SESSION_KEY, freshValue); } catch (_) { /* unavailable */ }
    return freshValue;
  }
  try { return window.sessionStorage.getItem(INIT_DATA_SESSION_KEY) || ""; } catch (_) { return ""; }
}

async function waitForTelegramInitData(timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  let value = telegramInitData();
  while (!value && Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 50));
    value = telegramInitData();
  }
  return value;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function statusClass(status) {
  return ["normal", "limited", "banned", "unknown", "active", "new", "session_invalid", "running", "pending", "completed", "succeeded", "finished", "finished_with_errors", "failed"]
    .includes(status) ? status : "unknown";
}

function statusLabel(status) {
  const labels = {
    normal: "正常", limited: "限制", banned: "封禁", unknown: "未知",
    active: "未检测", new: "未检测", session_invalid: "Session 失效",
    pending: "等待中", running: "运行中", completed: "已完成", finished: "已完成",
    succeeded: "已成功", failed: "失败", partial: "部分完成", finished_with_errors: "部分失败",
  };
  return labels[status] || status || "未知";
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function showNotice(text, type = "") {
  const box = qs("#notice");
  window.clearTimeout(state.noticeTimer);
  box.textContent = text;
  box.className = `notice ${type}`.trim();
  if (!text) box.classList.add("hidden");
  if (text && ["ok", "error"].includes(type)) {
    tg?.HapticFeedback?.notificationOccurred(type === "ok" ? "success" : "error");
  }
  if (text && type === "ok") {
    state.noticeTimer = window.setTimeout(() => showNotice(""), 3200);
  }
}

function setBusy(button, busy, busyText = "处理中…") {
  if (!button) return;
  if (busy) {
    button.dataset.originalText = button.textContent;
    button.textContent = busyText;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    button.disabled = false;
  }
}

function confirmAction(message) {
  if (typeof tg?.showConfirm === "function") {
    return new Promise((resolve) => tg.showConfirm(message, resolve));
  }
  return Promise.resolve(window.confirm(message));
}

async function api(path, options = {}) {
  const isFormData = options.body instanceof FormData;
  const response = await fetch(`/mini-app/api${path}`, {
    ...options,
    headers: {
      "X-Telegram-Init-Data": telegramInitData(),
      ...(isFormData ? {} : { "Content-Type": "application/json" }),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    if (response.status === 401) {
      try { window.sessionStorage.removeItem(INIT_DATA_SESSION_KEY); } catch (_) { /* unavailable */ }
    }
    const error = new Error(text || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function downloadApi(path, payload) {
  const response = await fetch(`/mini-app/api${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": telegramInitData(),
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`);
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const filename = disposition.match(/filename="([^"]+)"/)?.[1] || "tg_sessions.txt";
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  return {
    exported: response.headers.get("X-Exported-Count"),
    skipped: response.headers.get("X-Skipped-Count"),
  };
}

const pageMeta = {
  dashboard: ["CONTROL CENTER", "运行概览"],
  accounts: ["ACCOUNTS", "账号管理"],
  actions: ["OPERATIONS", "快捷操作"],
  settings: ["CONFIGURATION", "运行设置"],
};

function scrollPageTop(behavior = "smooth") {
  const shell = qs(".shell");
  if (window.matchMedia("(max-width: 920px)").matches && shell) {
    shell.scrollTo({ top: 0, behavior });
    return;
  }
  window.scrollTo({ top: 0, behavior });
}

function switchView(view) {
  if (!pageMeta[view]) return;
  state.activeView = view;
  document.body.dataset.view = view;
  if (view !== "accounts") closeAccountDetail(false);
  qsa(".tab").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  qsa(".view").forEach((section) => section.classList.remove("active"));
  qs(`#${view}View`)?.classList.add("active");
  qs("#pageEyebrow").textContent = pageMeta[view][0];
  qs("#pageTitle").textContent = pageMeta[view][1];
  updateBackButton();
  scrollPageTop();
}

function switchActionPane(pane) {
  if (!qs(`.action-pane[data-pane="${pane}"]`)) return;
  state.activePane = pane;
  qsa(".segment").forEach((button) => button.classList.toggle("active", button.dataset.actionPane === pane));
  qsa(".action-pane").forEach((section) => section.classList.toggle("active", section.dataset.pane === pane));
}

function updateBackButton() {
  if (!tg?.BackButton) return;
  if (state.activeAccountId || state.activeView !== "dashboard") tg.BackButton.show();
  else tg.BackButton.hide();
}

function accountRow(account, compact = false) {
  const row = document.createElement("article");
  row.className = `account-row${compact ? " compact-row" : ""}`;
  const displayName = account.name || account.username || account.phone_masked || `账号 ${account.id}`;
  const secondary = account.username ? `@${account.username}` : account.phone_masked;
  const initial = String(displayName).trim().slice(0, 1).toUpperCase() || "T";
  row.innerHTML = `
    <div class="row-main">
      <div class="row-identity">
        <span class="avatar">${escapeHtml(initial)}</span>
        <div>
          <div class="row-title">${escapeHtml(displayName)} <span class="muted">#${Number(account.id)}</span></div>
          <div class="row-meta">${escapeHtml(secondary)} · TG ${escapeHtml(account.user_id || "—")}</div>
        </div>
      </div>
      <span class="status ${statusClass(account.status)}">${escapeHtml(statusLabel(account.status))}</span>
    </div>`;
  if (!compact) {
    const footer = document.createElement("div");
    footer.className = "account-card-footer";
    footer.innerHTML = `<small>${account.last_login_at ? `最近登录 ${escapeHtml(formatTime(account.last_login_at))}` : "暂无登录时间"}</small><button class="link-button" type="button">管理 ›</button>`;
    row.append(footer);
  }
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.addEventListener("click", () => openAccountDetail(account.id));
  row.addEventListener("keydown", (event) => {
    if (["Enter", " "].includes(event.key)) {
      event.preventDefault();
      openAccountDetail(account.id);
    }
  });
  return row;
}

function renderBootstrap(data) {
  state.bootstrap = data;
  const status = data.status;
  qs("#metricAccounts").textContent = status.accounts;
  qs("#metricUsable").textContent = `${status.usable} 个 active · 上限 50`;
  qs("#metricConnected").textContent = status.connected;
  qs("#connectedHint").textContent = `${status.connected}/${status.usable} 个 active 已连接`;
  const monitorRunning = Boolean(status.monitor_enabled && status.monitor_running);
  qs("#metricMonitor").textContent = monitorRunning ? "运行中" : "未运行";
  qs("#monitorHint").textContent = status.monitor_enabled && !status.monitor_running ? "开关已开，但任务异常" : monitorRunning ? "后台任务正常" : "实时保护已停止";
  qs("#metricJobs").textContent = status.running_jobs;
  qs("#monitorToggleBtn").textContent = monitorRunning ? "关闭监听" : "开启监听";
  qs("#monitorToggleBtn").classList.toggle("danger-button", monitorRunning);
  qs("#sidebarStatus").textContent = monitorRunning ? "服务与监听在线" : "服务在线，监听未运行";
  qs(".live-dot")?.classList.toggle("online", monitorRunning);

  const recentAccounts = data.recent_accounts || [];
  const recent = qs("#recentAccounts");
  recent.replaceChildren(...recentAccounts.map((account) => accountRow(account, true)));
  if (!recentAccounts.length) recent.innerHTML = '<div class="empty-state">还没有账号，先添加一个账号</div>';
  renderTargets(data.targets);
  renderRates(data.rates);
}

function renderAccounts(accounts, pagination) {
  const list = qs("#accountsList");
  list.replaceChildren(...accounts.map((account) => accountRow(account)));
  state.accountPage = Number(pagination.page || 1);
  state.accountPages = Number(pagination.pages || 0);
  state.accountTotal = Number(pagination.total || 0);
  const pageText = state.accountPages ? ` · 第 ${state.accountPage}/${state.accountPages} 页` : "";
  qs("#accountCount").textContent = `共 ${state.accountTotal} 个账号${pageText}`;
  if (!accounts.length) list.innerHTML = '<div class="panel empty-state">没有匹配的账号</div>';
  const pager = qs("#accountPagination");
  pager.classList.toggle("hidden", state.accountPages <= 1);
  qs("#accountPageIndicator").textContent = `第 ${state.accountPage} / ${Math.max(state.accountPages, 1)} 页`;
  qs("#accountPrevPage").disabled = state.accountPage <= 1;
  qs("#accountNextPage").disabled = state.accountPage >= state.accountPages;
}

async function loadAccountPage(page = 1, { query = null, scroll = false } = {}) {
  const generation = ++state.accountRequestGeneration;
  const normalizedQuery = query === null ? qs("#accountSearch").value.trim() : query.trim();
  state.accountQuery = normalizedQuery;
  const params = new URLSearchParams({ page: String(page) });
  if (normalizedQuery) params.set("q", normalizedQuery);
  const data = await api(`/accounts?${params}`);
  if (generation !== state.accountRequestGeneration) return;
  renderAccounts(data.accounts, data.pagination || {
    page: 1,
    page_size: ACCOUNT_PAGE_SIZE,
    pages: data.accounts.length ? 1 : 0,
    total: data.accounts.length,
  });
  if (scroll) qs("#accountBrowser")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderTargets(targets) {
  const list = qs("#targetsList");
  list.replaceChildren(...targets.map((target) => {
    const row = document.createElement("article");
    row.className = "account-row";
    const ref = escapeHtml(target.target_ref);
    row.innerHTML = `<div class="row-main"><div><div class="row-title">${ref}</div><div class="row-meta">${escapeHtml(target.target_type)} · ${escapeHtml(target.title || "未备注")}</div></div><button class="link-button" type="button">删除</button></div>`;
    qs("button", row).addEventListener("click", () => removeTarget(target.target_ref));
    return row;
  }));
  if (!targets.length) list.innerHTML = '<div class="empty-state">暂未授权任何目标</div>';
}

function renderRates(rates) {
  const list = qs("#ratesList");
  list.replaceChildren(...rates.map((rate) => {
    const row = document.createElement("article");
    row.className = "account-row compact-row";
    row.innerHTML = `<div class="row-title">${escapeHtml(rate.scope)}</div><div class="row-meta">${Number(rate.max_actions)} 次 / ${Number(rate.per_seconds)} 秒 · 随机等待 ${Number(rate.jitter_min)}–${Number(rate.jitter_max)} 秒</div>`;
    if (rate.scope === "batch") {
      qs("#rateForm [name=max_actions]").value = rate.max_actions;
      qs("#rateForm [name=per_seconds]").value = rate.per_seconds;
      qs("#rateForm [name=jitter_min]").value = rate.jitter_min;
      qs("#rateForm [name=jitter_max]").value = rate.jitter_max;
    }
    return row;
  }));
}

async function loadBootstrap({ quiet = false } = {}) {
  if (!quiet) showNotice("正在读取 Telegram 安全登录信息…");
  const initData = await waitForTelegramInitData();
  if (!initData) {
    const message = tg
      ? "Telegram 未提供登录信息。可先点击右上角刷新重试；仍失败时，请完全关闭此页后再从机器人的“打开”按钮进入。"
      : "当前页面缺少 Telegram 安全登录信息，请从机器人资料页的“打开应用”按钮进入。";
    showNotice(message, "error");
    return;
  }
  const refresh = qs("#refreshBtn");
  refresh.classList.add("busy");
  if (!quiet) showNotice("正在同步运行数据…");
  try {
    const data = await api("/bootstrap");
    renderBootstrap(data);
    await loadAccountPage(state.accountPage);
    if (!quiet) showNotice("");
    loadJobs();
  } catch (error) {
    const detail = error.status === 401
      ? "Telegram 登录信息已过期，请完全关闭后从机器人的“打开”按钮重新进入"
      : error.message;
    showNotice(`加载失败：${detail}`, "error");
  } finally {
    refresh.classList.remove("busy");
  }
}

async function loadJobs() {
  try {
    const data = await api("/jobs?limit=8");
    const list = qs("#jobsList");
    list.replaceChildren(...data.jobs.map((job) => {
      const row = document.createElement("article");
      row.className = "account-row compact-row";
      const succeeded = Number(job.items?.ok || job.items?.succeeded || job.items?.success || 0);
      const failed = Number(job.items?.failed || 0);
      row.innerHTML = `<div class="row-main"><div><div class="row-title">任务 #${Number(job.id)} · ${escapeHtml(job.type)}</div><div class="row-meta">${escapeHtml(formatTime(job.started_at))} · 成功 ${succeeded} / 失败 ${failed}${job.error ? ` · <span class="job-error">${escapeHtml(job.error)}</span>` : ""}</div></div><span class="status ${statusClass(job.status)}">${escapeHtml(statusLabel(job.status))}</span></div>`;
      return row;
    }));
    if (!data.jobs.length) list.innerHTML = '<div class="empty-state">暂无任务记录</div>';
  } catch (error) {
    qs("#jobsList").innerHTML = `<div class="empty-state">任务记录加载失败：${escapeHtml(error.message)}</div>`;
  }
}

async function runSecurityCheck() {
  const button = qs("#securityCheckBtn");
  setBusy(button, true, "正在逐项检测…");
  qs("#healthTitle").textContent = "正在验证真实运行链路";
  qs("#healthDescription").textContent = "正在连接数据库、Gmail IMAP，并核对监听任务和全部 active 账号连接。";
  try {
    const data = await api("/security-health");
    qs("#healthIcon").className = `health-icon ${data.available ? "pass" : "fail"}`;
    qs("#healthIcon").textContent = data.available ? "✓" : "!";
    qs("#healthTitle").textContent = data.available ? "安全防护链路可用" : "安全防护链路不可用";
    qs("#healthDescription").textContent = `${data.summary} · 检测于 ${formatTime(data.checked_at)}`;
    const checks = qs("#healthChecks");
    checks.classList.remove("hidden");
    checks.replaceChildren(...data.checks.map((check) => {
      const row = document.createElement("div");
      row.className = "health-row";
      const icon = document.createElement("span");
      icon.className = `check-icon ${check.status}`;
      icon.textContent = check.status === "pass" ? "✓" : check.status === "warn" ? "△" : "×";
      const name = document.createElement("strong");
      name.textContent = check.name;
      const detail = document.createElement("div");
      detail.textContent = check.detail;
      if (check.status !== "pass" && check.fix) {
        const fix = document.createElement("div");
        fix.className = "fix-note";
        fix.textContent = `修复：${check.fix}`;
        detail.append(fix);
      }
      row.append(icon, name, detail);
      return row;
    }));
    if (!data.available) tg?.HapticFeedback?.notificationOccurred("error");
  } catch (error) {
    qs("#healthIcon").className = "health-icon fail";
    qs("#healthIcon").textContent = "!";
    qs("#healthTitle").textContent = "检测请求失败";
    qs("#healthDescription").textContent = error.message;
    showNotice(`安全检测失败：${error.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

async function toggleMonitor() {
  const currentlyRunning = Boolean(state.bootstrap?.status?.monitor_enabled && state.bootstrap?.status?.monitor_running);
  const action = currentlyRunning ? "off" : "on";
  if (action === "off" && !(await confirmAction("关闭实时监听会断开全部账号，并暂停登录安全提醒。确认继续？"))) return;
  const button = qs("#monitorToggleBtn");
  setBusy(button, true, action === "on" ? "正在连接账号…" : "正在停止…");
  try {
    const data = await api("/monitor", { method: "POST", body: JSON.stringify({ action }) });
    showNotice(data.message, "ok");
    await loadBootstrap({ quiet: true });
  } catch (error) {
    showNotice(`监听操作失败：${error.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

async function searchAccounts() {
  const button = qs("#searchBtn");
  setBusy(button, true, "搜索中…");
  try {
    await loadAccountPage(1, { scroll: true });
  } catch (error) {
    showNotice(`搜索失败：${error.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

function closeAccountDetail(scroll = true) {
  state.activeAccountId = null;
  qs("#accountDetail")?.classList.add("hidden");
  qs("#accountBrowser")?.classList.remove("hidden");
  if (state.activeView === "accounts") {
    qs("#pageTitle").textContent = pageMeta.accounts[1];
    if (scroll) scrollPageTop();
  }
  updateBackButton();
}

async function openAccountDetail(accountId) {
  switchView("accounts");
  state.activeAccountId = Number(accountId);
  qs("#accountBrowser").classList.add("hidden");
  const detail = qs("#accountDetail");
  detail.classList.remove("hidden");
  detail.innerHTML = '<div class="panel empty-state">正在加载账号详情…</div>';
  qs("#pageTitle").textContent = `账号 #${Number(accountId)}`;
  updateBackButton();
  scrollPageTop();
  try {
    const data = await api(`/accounts/${accountId}`);
    if (state.activeAccountId !== Number(accountId)) return;
    renderAccountDetail(data.account);
  } catch (error) {
    detail.innerHTML = `<div class="panel empty-state">详情加载失败：${escapeHtml(error.message)}</div>`;
  }
}

function renderAccountDetail(account) {
  const detail = qs("#accountDetail");
  const privacyCount = Object.keys(account.privacy || {}).length;
  const displayName = account.name || account.username || account.phone_masked;
  detail.innerHTML = `
    <div class="detail-header">
      <div class="detail-title"><button class="back-button" type="button" aria-label="返回账号列表">‹</button><div><h2>${escapeHtml(displayName)} <span class="muted">#${Number(account.id)}</span></h2><p>${escapeHtml(account.username ? `@${account.username}` : account.phone_masked)}</p></div></div>
      <span class="status ${statusClass(account.status)}">${escapeHtml(statusLabel(account.status))}</span>
    </div>
    <div class="detail-summary">
      <div class="detail-stat"><span>TELEGRAM ID</span><strong>${escapeHtml(account.user_id || "—")}</strong></div>
      <div class="detail-stat"><span>ACTIVE SESSION</span><strong>${account.has_active_session ? "可用" : "不可用"}</strong></div>
      <div class="detail-stat"><span>2FA</span><strong>${account.has_2fa ? "已启用" : "未启用 / 未知"}</strong></div>
      <div class="detail-stat"><span>SPAMBOT</span><strong>${escapeHtml(account.latest_spam ? statusLabel(account.latest_spam.status) : "未检测")}</strong></div>
    </div>
    <div class="detail-actions">
      <button class="primary-button" type="button" data-account-action="refresh_status">完整刷新检测</button>
      <button class="secondary-button" type="button" data-account-action="reconnect">重新连接</button>
      <button class="secondary-button" type="button" data-account-action="spam">检查 SpamBot</button>
      <button class="secondary-button" type="button" data-account-action="service_check">同步服务消息</button>
      <button class="secondary-button" type="button" data-account-action="export_session">导出 Session</button>
    </div>
    <div class="detail-sections">
      <details class="detail-section" open><summary>资料与头像</summary><div class="detail-section-body">
        <form id="profileForm" class="form">
          <div class="split"><label><span>First Name</span><input name="first_name" value="${escapeHtml(account.first_name || "")}" /></label><label><span>Last Name</span><input name="last_name" value="${escapeHtml(account.last_name || "")}" /></label></div>
          <label><span>用户名</span><input name="username" value="${escapeHtml(account.username || "")}" placeholder="不用带 @" /></label>
          <label><span>简介（留空不修改）</span><textarea name="bio" rows="3" placeholder="输入新简介"></textarea></label>
          <button class="primary-button" type="submit">保存资料</button>
        </form>
        <div class="form-divider"><span>头像</span></div>
        <form id="avatarForm" class="form"><label><span>上传图片，最大 5MB</span><input name="avatar" type="file" accept="image/*" /></label><div class="button-row"><button class="secondary-button" type="submit" data-mode="random">使用随机头像</button><button class="primary-button" type="submit" data-mode="upload">上传头像</button></div></form>
      </div></details>
      <details class="detail-section"><summary>隐私设置 <span class="muted">已保存 ${privacyCount} 项</span></summary><div class="detail-section-body"><form id="privacyForm" class="form"><div class="split"><label><span>隐私项</span><select name="key"><option value="phone">手机号</option><option value="last_seen">在线时间</option><option value="profile_photo">头像</option><option value="forwards">转发来源</option><option value="calls">通话</option><option value="groups">拉群</option></select></label><label><span>允许范围</span><select name="rule"><option value="everybody">所有人</option><option value="contacts">联系人</option><option value="nobody">没有人</option></select></label></div><button class="primary-button" type="submit">保存该项隐私</button></form></div></details>
      <details class="detail-section"><summary>两步验证（2FA）</summary><div class="detail-section-body"><form id="twofaForm" class="form"><label><span>操作</span><select name="action"><option value="check">查询状态</option><option value="set">设置 2FA</option><option value="change">修改 2FA</option><option value="email">配置 2FA 邮箱</option><option value="disable">关闭 2FA</option><option value="confirm">确认邮箱验证码</option></select></label><div class="split"><label><span>当前密码</span><input name="current_password" type="password" autocomplete="current-password" /></label><label><span>新密码</span><input name="new_password" type="password" autocomplete="new-password" /></label></div><div class="split"><label><span>密码提示</span><input name="hint" /></label><label><span>恢复邮箱</span><input name="email" type="email" /></label></div><label><span>邮箱验证码</span><input name="code" inputmode="numeric" autocomplete="one-time-code" /></label><button class="primary-button" type="submit">执行 2FA 操作</button></form></div></details>
      <details class="detail-section"><summary>登录邮箱保护</summary><div class="detail-section-body">
        <form id="loginEmailWindowForm" class="form"><label><span>收到登录通知后等待时长（小时）</span><input name="hours" type="number" min="0" max="720" step="1" required value="${account.login_email_window_hours ?? 0}" /></label><p class="muted">填 0 表示立即换绑；每个账号独立设置。修改后从下一条登录通知生效，当前已开始的窗口不变。</p><button class="primary-button" type="submit">保存保护时长</button></form>
        <div class="form-divider"><span>手动换绑登录邮箱</span></div>
        <form id="loginEmailForm" class="form"><label><span>新登录邮箱</span><input name="email" type="email" /></label><label><span>邮箱验证码</span><input name="code" inputmode="numeric" autocomplete="one-time-code" /></label><div class="button-row"><button class="secondary-button" type="submit" data-action="send">发送验证码</button><button class="primary-button" type="submit" data-action="confirm">确认验证码</button></div></form>
      </div></details>
      <details class="detail-section"><summary>Telegram 服务消息</summary><div class="detail-section-body"><button class="secondary-button" id="loadServiceMessagesBtn" type="button">加载最近消息</button><div id="serviceMessagesList" class="list"></div></div></details>
    </div>`;
  qs(".back-button", detail).addEventListener("click", () => closeAccountDetail());
  qsa("[data-account-action]", detail).forEach((button) => button.addEventListener("click", () => {
    const action = button.dataset.accountAction;
    if (action === "export_session") exportSessions({ mode: "single", account_id: account.id }, button);
    else runAccountAction(account.id, action, button);
  }));
  qs("#profileForm", detail).addEventListener("submit", (event) => submitProfile(event, account.id));
  qs("#avatarForm", detail).addEventListener("submit", (event) => submitAvatar(event, account.id));
  qs("#privacyForm", detail).addEventListener("submit", (event) => submitPrivacy(event, account.id));
  qs("#twofaForm", detail).addEventListener("submit", (event) => submitTwoFA(event, account.id));
  qs("#loginEmailWindowForm", detail).addEventListener("submit", (event) => submitLoginEmailWindow(event, account.id));
  qs("#loginEmailForm", detail).addEventListener("submit", (event) => submitLoginEmail(event, account.id));
  qs("#loadServiceMessagesBtn", detail).addEventListener("click", (event) => loadServiceMessages(account.id, event.currentTarget));
}

async function submitPhoneLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter;
  const step = button?.dataset.step || "start";
  const payload = Object.fromEntries(new FormData(form).entries());
  setBusy(button, true, step === "start" ? "正在发送…" : "正在验证…");
  try {
    if (step === "start") {
      await cancelQrLogin({ quiet: true });
      const data = await api("/accounts/login/start", { method: "POST", body: JSON.stringify({ phone: payload.phone }) });
      if (data.already_exists) {
        showNotice(data.message, "ok");
        await loadBootstrap({ quiet: true });
        await openAccountDetail(data.account.id);
        return;
      }
      form.elements.login_id.value = data.login_id;
      showNotice(data.message || "验证码已发送", "ok");
      form.elements.code.focus();
      return;
    }
    if (!payload.login_id) throw new Error("请先发送验证码");
    const data = await api("/accounts/login/verify", { method: "POST", body: JSON.stringify({ login_id: payload.login_id, code: payload.code, password: payload.password }) });
    if (data.needs_password) {
      showNotice(data.message || "该账号需要 2FA 密码");
      form.elements.password.focus();
      return;
    }
    showNotice(data.message, "ok");
    form.reset();
    await loadBootstrap({ quiet: true });
    await openAccountDetail(data.account.id);
  } catch (error) {
    showNotice(`添加账号失败：${error.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

function renderQrLogin(data) {
  const panel = qs("#qrLoginPanel");
  panel.classList.remove("hidden");
  if (data.qr_image) qs("#qrLoginImage").src = data.qr_image;
  qs("#qrLoginStatus").textContent = data.message || "等待扫码确认";
  const needsPassword = Boolean(data.needs_password);
  qs("#qrPasswordLabel").classList.toggle("hidden", !needsPassword);
  qs("#qrPasswordSubmitBtn").classList.toggle("hidden", !needsPassword);
  if (needsPassword) {
    qs("#qrLoginStatus").textContent = `${data.message || "请输入 2FA 密码"}（提示：${data.hint || "-"}）`;
    qs("#qrPassword").focus();
  }
}

async function finishQrLogin(data) {
  state.qrLoginGeneration += 1;
  state.qrLoginId = null;
  qs("#qrLoginPanel").classList.add("hidden");
  qs("#qrPassword").value = "";
  showNotice(data.message || "二维码登录完成", "ok");
  await loadBootstrap({ quiet: true });
  await openAccountDetail(data.account.id);
}

async function pollQrLogin(loginId, generation) {
  while (state.qrLoginId === loginId && state.qrLoginGeneration === generation) {
    try {
      const data = await api("/accounts/login/qr/poll", {
        method: "POST",
        body: JSON.stringify({ login_id: loginId }),
      });
      if (state.qrLoginId !== loginId || state.qrLoginGeneration !== generation) return;
      renderQrLogin(data);
      if (data.status === "complete") {
        await finishQrLogin(data);
        return;
      }
      if (data.needs_password) return;
    } catch (error) {
      if (state.qrLoginId !== loginId || state.qrLoginGeneration !== generation) return;
      state.qrLoginId = null;
      qs("#qrLoginPanel").classList.add("hidden");
      showNotice(`二维码登录失败：${error.message}`, "error");
      return;
    }
  }
}

async function startQrLogin() {
  const button = qs("#qrLoginStartBtn");
  setBusy(button, true, "正在生成…");
  await cancelQrLogin({ quiet: true });
  const generation = state.qrLoginGeneration + 1;
  state.qrLoginGeneration = generation;
  try {
    const data = await api("/accounts/login/qr/start", { method: "POST", body: "{}" });
    state.qrLoginId = data.login_id;
    qs("#phoneLoginForm").elements.login_id.value = "";
    renderQrLogin(data);
    showNotice(data.message || "二维码已生成");
    void pollQrLogin(data.login_id, generation);
  } catch (error) {
    showNotice(`生成二维码失败：${error.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

async function submitQrPassword() {
  const button = qs("#qrPasswordSubmitBtn");
  const loginId = state.qrLoginId;
  const password = qs("#qrPassword").value;
  if (!loginId) return showNotice("二维码登录流程已过期，请重新生成", "error");
  if (!password) return showNotice("请输入目标账号的 2FA 密码", "error");
  setBusy(button, true, "正在验证…");
  try {
    const data = await api("/accounts/login/qr/poll", {
      method: "POST",
      body: JSON.stringify({ login_id: loginId, password }),
    });
    renderQrLogin(data);
    if (data.status === "complete") await finishQrLogin(data);
  } catch (error) {
    showNotice(`2FA 验证失败：${error.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

async function cancelQrLogin({ quiet = false } = {}) {
  const loginId = state.qrLoginId;
  state.qrLoginGeneration += 1;
  state.qrLoginId = null;
  qs("#qrLoginPanel")?.classList.add("hidden");
  if (!loginId) return;
  try {
    await api("/accounts/login/qr/cancel", {
      method: "POST",
      body: JSON.stringify({ login_id: loginId }),
    });
    if (!quiet) showNotice("二维码登录已取消");
  } catch (error) {
    if (!quiet) showNotice(`取消失败：${error.message}`, "error");
  }
}

async function submitSessionImport(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter;
  setBusy(button, true, "正在导入…");
  try {
    const data = await api("/accounts/import-session", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form).entries())) });
    showNotice(data.message, "ok");
    form.reset();
    await loadBootstrap({ quiet: true });
    await openAccountDetail(data.account.id);
  } catch (error) {
    showNotice(`Session 导入失败：${error.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

async function exportSessions(payload, button = null) {
  setBusy(button, true, "正在导出…");
  try {
    const result = await downloadApi("/accounts/export-sessions", payload);
    showNotice(`导出完成：${result.exported || 0} 个，跳过 ${result.skipped || 0} 个`, "ok");
  } catch (error) {
    showNotice(`Session 导出失败：${error.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

async function submitSessionExport(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter;
  const mode = button?.dataset.mode || "selection";
  const raw = Object.fromEntries(new FormData(form).entries());
  if (mode === "range") {
    if (!raw.start_id || !raw.count) return showNotice("请填写起始账号 ID 和数量", "error");
    return exportSessions({ mode: "range", start_id: Number(raw.start_id), count: Number(raw.count) }, button);
  }
  if (!raw.selection.trim()) return showNotice("请填写账号 ID，例如 1,3,5-8", "error");
  return exportSessions({ mode: "selection", selection: raw.selection }, button);
}

async function submitProfile(event, accountId) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter;
  const raw = Object.fromEntries(new FormData(form).entries());
  const payload = { first_name: raw.first_name, last_name: raw.last_name, username: raw.username };
  if (raw.bio) payload.bio = raw.bio;
  setBusy(button, true, "正在保存…");
  try {
    const data = await api(`/accounts/${accountId}/profile`, { method: "POST", body: JSON.stringify(payload) });
    showNotice(data.message, "ok");
    await loadBootstrap({ quiet: true });
    await openAccountDetail(accountId);
  } catch (error) {
    showNotice(`资料保存失败：${error.message}`, "error");
  } finally { setBusy(button, false); }
}

async function submitAvatar(event, accountId) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter;
  const mode = button?.dataset.mode || "upload";
  const formData = new FormData();
  formData.append("mode", mode);
  if (mode === "upload") {
    const file = qs("[name=avatar]", form).files[0];
    if (!file) return showNotice("请先选择头像图片", "error");
    formData.append("avatar", file);
  }
  setBusy(button, true, "正在设置…");
  try {
    const data = await api(`/accounts/${accountId}/avatar`, { method: "POST", body: formData });
    showNotice(data.message, "ok");
  } catch (error) {
    showNotice(`头像设置失败：${error.message}`, "error");
  } finally { setBusy(button, false); }
}

async function submitPrivacy(event, accountId) {
  event.preventDefault();
  const button = event.submitter;
  setBusy(button, true, "正在保存…");
  try {
    const data = await api(`/accounts/${accountId}/privacy`, { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget).entries())) });
    showNotice(data.message, "ok");
    await openAccountDetail(accountId);
  } catch (error) {
    showNotice(`隐私设置失败：${error.message}`, "error");
  } finally { setBusy(button, false); }
}

async function submitTwoFA(event, accountId) {
  event.preventDefault();
  const button = event.submitter;
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  if (payload.action === "disable" && !(await confirmAction("确认关闭该账号的 2FA？"))) return;
  setBusy(button, true, "正在执行…");
  try {
    const data = await api(`/accounts/${accountId}/twofa`, { method: "POST", body: JSON.stringify(payload) });
    showNotice(data.message || JSON.stringify(data.info || {}), data.needs_code ? "" : "ok");
    if (!data.needs_code) await openAccountDetail(accountId);
  } catch (error) {
    showNotice(`2FA 操作失败：${error.message}`, "error");
  } finally { setBusy(button, false); }
}

async function submitLoginEmail(event, accountId) {
  event.preventDefault();
  const button = event.submitter;
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  payload.action = button?.dataset.action || "send";
  setBusy(button, true, "正在处理…");
  try {
    const data = await api(`/accounts/${accountId}/login-email`, { method: "POST", body: JSON.stringify(payload) });
    showNotice(data.message || "登录邮箱操作完成", data.needs_code ? "" : "ok");
  } catch (error) {
    showNotice(`登录邮箱操作失败：${error.message}`, "error");
  } finally { setBusy(button, false); }
}

async function submitLoginEmailWindow(event, accountId) {
  event.preventDefault();
  const button = event.submitter;
  const hours = Number(new FormData(event.currentTarget).get("hours"));
  setBusy(button, true, "正在保存…");
  try {
    const data = await api(`/accounts/${accountId}/login-email-window`, {
      method: "PUT",
      body: JSON.stringify({ hours }),
    });
    showNotice(data.message, "ok");
    await openAccountDetail(accountId);
  } catch (error) {
    showNotice(`保存保护时长失败：${error.message}`, "error");
  } finally { setBusy(button, false); }
}

async function loadServiceMessages(accountId, button) {
  setBusy(button, true, "正在加载…");
  try {
    const data = await api(`/accounts/${accountId}/service-messages?limit=20`);
    const list = qs("#serviceMessagesList");
    list.replaceChildren(...data.messages.map((message) => {
      const row = document.createElement("article");
      row.className = "account-row";
      const title = document.createElement("div");
      title.className = "row-title";
      title.textContent = `来源 ${message.source_user_id || "—"} · 消息 #${message.message_id}`;
      const meta = document.createElement("div");
      meta.className = "row-meta";
      meta.textContent = formatTime(message.received_at);
      const body = document.createElement("p");
      body.className = "message-body";
      body.textContent = message.text || message.text_preview || "";
      row.append(title, meta, body);
      return row;
    }));
    if (!data.messages.length) list.innerHTML = '<div class="empty-state">暂无服务消息</div>';
  } catch (error) {
    showNotice(`服务消息加载失败：${error.message}`, "error");
  } finally { setBusy(button, false); }
}

async function runAccountAction(accountId, action, button) {
  setBusy(button, true, "正在执行…");
  try {
    const data = await api(`/accounts/${accountId}/action`, { method: "POST", body: JSON.stringify({ action }) });
    showNotice(data.message || "操作完成", "ok");
    await loadBootstrap({ quiet: true });
    await openAccountDetail(accountId);
  } catch (error) {
    showNotice(`操作失败：${error.message}`, "error");
  } finally { setBusy(button, false); }
}

async function removeTarget(targetRef) {
  if (!(await confirmAction(`确认删除授权目标 ${targetRef}？`))) return;
  try {
    const data = await api("/targets", { method: "POST", body: JSON.stringify({ action: "remove", target_ref: targetRef }) });
    showNotice(data.message, "ok");
    await loadBootstrap({ quiet: true });
  } catch (error) { showNotice(`删除失败：${error.message}`, "error"); }
}

function batchFieldMode() {
  const type = qs("#batchForm [name=type]").value;
  qsa(".message-fields").forEach((node) => node.classList.toggle("hidden", !["react", "unreact", "view_post", "forward"].includes(type)));
  qsa(".react-only").forEach((node) => node.classList.toggle("hidden", type !== "react"));
  qsa(".forward-only").forEach((node) => node.classList.toggle("hidden", type !== "forward"));
  qsa(".text-only").forEach((node) => node.classList.toggle("hidden", type !== "send"));
}

async function submitBatch(event) {
  event.preventDefault();
  const button = event.submitter;
  const data = Object.fromEntries(new FormData(event.currentTarget).entries());
  const payload = { ...data, account_mode: "range", start_id: Number(data.start_id), count: Number(data.count), message_id: data.message_id ? Number(data.message_id) : undefined };
  setBusy(button, true, "任务运行中…");
  try {
    const result = await api("/batch/run", { method: "POST", body: JSON.stringify(payload) });
    showNotice(result.message, "ok");
    await loadBootstrap({ quiet: true });
    await loadJobs();
  } catch (error) { showNotice(`批量任务失败：${error.message}`, "error"); }
  finally { setBusy(button, false); }
}

async function submitTarget(event) {
  event.preventDefault();
  const button = event.submitter;
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  payload.action = "add";
  setBusy(button, true, "添加中…");
  try {
    const result = await api("/targets", { method: "POST", body: JSON.stringify(payload) });
    showNotice(result.message, "ok");
    event.currentTarget.reset();
    await loadBootstrap({ quiet: true });
  } catch (error) { showNotice(`添加失败：${error.message}`, "error"); }
  finally { setBusy(button, false); }
}

async function submitRate(event) {
  event.preventDefault();
  const button = event.submitter;
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  ["max_actions", "per_seconds", "jitter_min", "jitter_max"].forEach((key) => { payload[key] = Number(payload[key]); });
  setBusy(button, true, "保存中…");
  try {
    const result = await api("/rates", { method: "POST", body: JSON.stringify(payload) });
    showNotice(result.message, "ok");
    await loadBootstrap({ quiet: true });
  } catch (error) { showNotice(`保存失败：${error.message}`, "error"); }
  finally { setBusy(button, false); }
}

qsa(".tab").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
qsa(".segment").forEach((button) => button.addEventListener("click", () => switchActionPane(button.dataset.actionPane)));
qsa("[data-view-jump]").forEach((button) => button.addEventListener("click", () => {
  switchView(button.dataset.viewJump);
  if (button.dataset.actionPane) switchActionPane(button.dataset.actionPane);
}));
qs("#refreshBtn").addEventListener("click", () => loadBootstrap());
qs("#securityCheckBtn").addEventListener("click", runSecurityCheck);
qs("#monitorToggleBtn").addEventListener("click", toggleMonitor);
qs("#reloadJobsBtn").addEventListener("click", loadJobs);
qs("#searchBtn").addEventListener("click", searchAccounts);
qs("#accountSearch").addEventListener("input", (event) => {
  if (!event.currentTarget.value) loadAccountPage(1, { query: "" }).catch((error) => showNotice(`加载账号失败：${error.message}`, "error"));
});
qs("#accountSearch").addEventListener("keydown", (event) => { if (event.key === "Enter") searchAccounts(); });
qs("#accountPrevPage").addEventListener("click", () => {
  loadAccountPage(state.accountPage - 1, { query: state.accountQuery, scroll: true })
    .catch((error) => showNotice(`翻页失败：${error.message}`, "error"));
});
qs("#accountNextPage").addEventListener("click", () => {
  loadAccountPage(state.accountPage + 1, { query: state.accountQuery, scroll: true })
    .catch((error) => showNotice(`翻页失败：${error.message}`, "error"));
});
qs("#batchForm").addEventListener("submit", submitBatch);
qs("#batchForm [name=type]").addEventListener("change", batchFieldMode);
qs("#phoneLoginForm").addEventListener("submit", submitPhoneLogin);
qs("#qrLoginStartBtn").addEventListener("click", startQrLogin);
qs("#qrLoginCancelBtn").addEventListener("click", () => cancelQrLogin());
qs("#qrPasswordSubmitBtn").addEventListener("click", submitQrPassword);
qs("#sessionImportForm").addEventListener("submit", submitSessionImport);
qs("#sessionExportForm").addEventListener("submit", submitSessionExport);
qs("#targetForm").addEventListener("submit", submitTarget);
qs("#rateForm").addEventListener("submit", submitRate);
qs("#reloadTargetsBtn").addEventListener("click", () => loadBootstrap());

if (tg) {
  tg.ready();
  tg.expand();
  tg.disableVerticalSwipes?.();
  tg.BackButton?.onClick(() => {
    if (state.activeAccountId) closeAccountDetail();
    else switchView("dashboard");
  });
}

batchFieldMode();
loadBootstrap();
