// B站收藏夹整理 - 前端逻辑
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const logBody = $("log-body");

  // ---------- 界面主题 ----------
  const savedTheme = localStorage.getItem("theme-mode") || "system";
  $("theme-mode").value = savedTheme;
  function applyTheme(mode) {
    if (mode === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", mode);
    localStorage.setItem("theme-mode", mode);
  }
  applyTheme(savedTheme);
  $("theme-mode").addEventListener("change", () => applyTheme($("theme-mode").value));

  const connectionToggle = $("connection-toggle");
  const connectionContent = $("connection-content");
  function setConnectionCollapsed(collapsed) {
    connectionContent.hidden = collapsed;
    connectionToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    connectionToggle.querySelector(".conn-toggle-icon").textContent = collapsed ? "▸" : "⌄";
    localStorage.setItem("connection-config-collapsed", collapsed ? "true" : "false");
  }
  setConnectionCollapsed(localStorage.getItem("connection-config-collapsed") === "true");
  connectionToggle.addEventListener("click", () => {
    setConnectionCollapsed(connectionToggle.getAttribute("aria-expanded") === "true");
  });

  // ---------- 日志 ----------
  function log(msg, level = "info") {
    const line = document.createElement("div");
    line.className = "log-line log-" + level;
    const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    const t = document.createElement("span");
    t.className = "log-time";
    t.textContent = `[${time}]`;
    const m = document.createElement("span");
    m.textContent = msg;
    line.appendChild(t);
    line.appendChild(m);
    logBody.appendChild(line);
    logBody.scrollTop = logBody.scrollHeight;
  }

  $("clear-log").addEventListener("click", () => { logBody.innerHTML = ""; });

  // ---------- HTTP ----------
  const API_TOKEN = (typeof window !== "undefined" && window.__BILI_FAV_TOKEN__) || "";
  async function api(method, url, body, options = {}) {
    const opt = { method, headers: {} };
    if (API_TOKEN) opt.headers["X-BiliFav-Token"] = API_TOKEN;
    let timeoutId = null;
    const timeoutMs = options.timeoutMs || 30000;
    {
      const controller = new AbortController();
      opt.signal = controller.signal;
      timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    }
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    try {
      const res = await fetch(url, opt);
      const txt = await res.text();
      let data = null;
      try { data = txt ? JSON.parse(txt) : null; } catch (_) { /* 非 JSON */ }
      if (!res.ok) {
        const message = (data && (data.error || data.detail)) || ("HTTP " + res.status);
        const err = new Error(message);
        err.error = message;
        if (data && data.code === "scan_issue_pending") refreshScanIssues(true);
        if (data && data.code === "app_locked") {
          const screen = document.getElementById("app-lock-screen");
          if (screen) screen.hidden = false;
        }
        if (data && data.code === "app_onboarding") {
          const modal = document.getElementById("vault-onboarding-modal");
          if (modal) modal.style.display = "flex";
        }
        throw err;
      }
      return data;
    } catch (e) {
      if (e && e.name === "AbortError") {
        const err = new Error("请求超时（" + Math.round(timeoutMs / 1000) + "s）");
        err.error = err.message;
        throw err;
      }
      if (e && e.name === "TypeError") {
        const err = new Error("网络连接失败，请确认本地服务仍在运行");
        err.error = err.message;
        throw err;
      }
      throw e;
    } finally {
      if (timeoutId !== null) clearTimeout(timeoutId);
    }
  }

  // ---------- 应用密码 ----------
  const lockScreen = $("app-lock-screen");
  const lockModal = $("app-lock-modal");

  function showLockScreen(show) {
    if (!lockScreen) return;
    lockScreen.hidden = !show;
    if (show) {
      const input = $("app-lock-password");
      if (input) { input.value = ""; input.focus(); }
    }
  }

  async function refreshAppLock() {
    try {
      const st = await api("GET", "/api/app-lock/status");
      const vs = await api("GET", "/api/vault/status");
      const vaultLocked = !!(vs && vs.configured && vs.onboarding_complete && !vs.unlocked);
      const appLocked = !!(st.password_set && !st.unlocked);
      showLockScreen(vaultLocked || appLocked);
      const statusText = $("app-lock-status-text");
      if (statusText) {
        if (vs && vs.configured) {
          statusText.textContent = vs.unlocked
            ? "已设置应用密码 · 当前已解锁。可修改密码。"
            : "已设置应用密码 · 当前已锁定，请先解锁。";
        } else if (st.password_set) {
          statusText.textContent = st.unlocked
            ? "已设置应用密码 · 当前已解锁" : "已设置应用密码 · 当前已锁定";
        } else {
          statusText.textContent = "尚未设置应用密码。请先完成首次初始化。";
        }
      }
      const remember = $("app-lock-remember");
      if (remember) remember.checked = !!st.auto_unlock_available;
      return { ...st, vault: vs };
    } catch (e) {
      return null;
    }
  }

  async function submitUnlock() {
    const password = ($("app-lock-password") || {}).value || "";
    const recovery = ($("app-lock-recovery") || {}).value || "";
    const autoUnlock = !!($("app-lock-auto") || {}).checked;
    const errBox = $("app-lock-error");
    try {
      // 金库优先：密码/恢复码都是为了解开 DEK
      const vs = await api("GET", "/api/vault/status");
      if (vs && vs.configured) {
        await api("POST", "/api/vault/unlock", {
          password: password || undefined,
          recovery_code: recovery || undefined,
        });
      } else {
        await api("POST", "/api/app-lock/unlock", { password, auto_unlock: autoUnlock });
      }
      if (errBox) errBox.textContent = "";
      showLockScreen(false);
      log("应用已解锁", "ok");
      await refreshAppLock();
    } catch (e) {
      if (errBox) errBox.textContent = e.error || e.message || "解锁失败";
    }
  }

  function openAppLockModal() {
    if (!lockModal) return;
    lockModal.style.display = "flex";
    const cur = $("app-lock-current");
    const neu = $("app-lock-new");
    if (cur) cur.value = "";
    if (neu) neu.value = "";
    refreshAppLock();
  }

  function closeAppLockModal() {
    if (lockModal) lockModal.style.display = "none";
  }

  async function saveAppLock() {
    const current = ($("app-lock-current") || {}).value || "";
    const next = ($("app-lock-new") || {}).value || "";
    const autoUnlock = !!($("app-lock-remember") || {}).checked;
    try {
      const vs = await api("GET", "/api/vault/status");
      if (vs && vs.configured) {
        if (!next) {
          log("请填写新密码（至少 6 位）", "warn");
          return;
        }
        await api("POST", "/api/vault/set-password", {
          password: next, current_password: current,
        });
        log("应用密码已更新", "ok");
        closeAppLockModal();
        refreshAppLock();
        return;
      }
      const st = await api("GET", "/api/app-lock/status");
      if (!next && st.password_set) {
        log("未填写新密码，仅更新自动解锁选项", "info");
        return;
      }
      if (!next && !st.password_set) {
        log("请填写至少 6 位的应用密码", "warn");
        return;
      }
      await api("POST", "/api/app-lock/set-password", {
        password: next, current_password: current, auto_unlock: autoUnlock,
      });
      log("应用密码已保存", "ok");
      closeAppLockModal();
      refreshAppLock();
    } catch (e) {
      log(e.error || e.message || "保存应用密码失败", "err");
    }
  }

  async function removeAppLock() {
    const current = ($("app-lock-current") || {}).value || "";
    try {
      await api("POST", "/api/app-lock/remove", { current_password: current });
      log("已移除应用密码", "warn");
      closeAppLockModal();
      refreshAppLock();
    } catch (e) {
      log(e.error || e.message || "移除失败", "err");
    }
  }

  if ($("app-lock-btn")) $("app-lock-btn").addEventListener("click", () => openSettings("security"));
  if ($("app-lock-close")) $("app-lock-close").addEventListener("click", closeAppLockModal);
  if ($("app-lock-unlock-btn")) $("app-lock-unlock-btn").addEventListener("click", submitUnlock);
  if ($("app-lock-password")) {
    $("app-lock-password").addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitUnlock();
    });
  }
  if ($("app-lock-save")) $("app-lock-save").addEventListener("click", saveAppLock);
  if ($("app-lock-remove")) $("app-lock-remove").addEventListener("click", removeAppLock);
  refreshAppLock();

  // ---------- 金库 / 恢复码 ----------
  function showVaultRecovery(code) {
    const modal = $("vault-recovery-modal");
    const codeEl = $("vault-recovery-code");
    const saved = $("vault-recovery-saved");
    const ok = $("vault-recovery-ok");
    if (!modal) return;
    if (codeEl) codeEl.textContent = code;
    if (saved) saved.checked = false;
    if (ok) ok.disabled = true;
    modal.style.display = "flex";
    if (saved && ok) saved.onchange = () => { ok.disabled = !saved.checked; };
    const copy = $("vault-recovery-copy");
    if (copy) copy.onclick = () => {
      if (navigator.clipboard && code) navigator.clipboard.writeText(code).catch(() => {});
    };
    if (ok) ok.onclick = async () => {
      try {
        await api("POST", "/api/vault/complete-onboarding", {});
        modal.style.display = "none";
        log("恢复码已确认保存，金库已上锁，请用密码解锁后使用", "ok");
        await refreshAppLock();
        showLockScreen(true);
      } catch (e) {
        log(e.error || e.message || "完成初始化失败", "err");
      }
    };
  }

  async function refreshVaultStatus() {
    try { return await api("GET", "/api/vault/status"); } catch (e) { return null; }
  }

  window.__biliFavVault = { showRecovery: showVaultRecovery, refreshStatus: refreshVaultStatus };

  async function maybeShowVaultOnboarding() {
    const st = await refreshVaultStatus();
    const modal = $("vault-onboarding-modal");
    if (!modal) return;
    if (st && st.configured) {
      modal.style.display = "none";
      return;
    }
    modal.style.display = "flex";
    const err = $("vault-onboard-error");
    const statusEl = $("vault-qr-status");
    const midInput = $("vault-onboard-mid");
    let qrTimer = null;

    function setMid(mid) {
      if (midInput) midInput.value = mid || "";
      if (statusEl) {
        statusEl.textContent = mid
          ? `✅ 已登录，账号 mid=${mid}，请设置应用密码`
          : "尚未登录，请扫码";
      }
    }

    async function syncMidFromCookie() {
      try {
        const me = await api("GET", "/api/login/status");
        let mid = me && me.mid;
        if (!mid) {
          const ck = await api("GET", "/api/cookie");
          mid = ck && ck.mid;
        }
        if (mid) {
          setMid(String(mid));
          return String(mid);
        }
      } catch (_) { /* ignore */ }
      return "";
    }

    async function generateVaultQr() {
      const btn = $("vault-qr-btn");
      const box = $("vault-qr-box");
      const img = $("vault-qr-img");
      if (statusEl) statusEl.textContent = "生成中 ...";
      if (btn) btn.disabled = true;
      try {
        const r = await api("POST", "/api/login/qr/generate", undefined, { timeoutMs: 20000 });
        if (!r || !r.ok) {
          if (statusEl) statusEl.textContent = "失败：" + ((r && r.error) || "无法生成二维码");
          return;
        }
        if (img) img.src = r.image;
        if (box) box.style.display = "flex";
        if (statusEl) statusEl.textContent = "请用手机 B 站 App 扫码登录";
        if (qrTimer) clearInterval(qrTimer);
        qrTimer = setInterval(async () => {
          try {
            const p = await api("GET", "/api/login/qr/poll", undefined, { timeoutMs: 15000 });
            if (p && p.status === "ok") {
              let mid = p.mid || "";
              if (!mid) mid = await syncMidFromCookie();
              if (mid) {
                if (qrTimer) clearInterval(qrTimer);
                if (box) box.style.display = "none";
                setMid(mid);
                return;
              }
              if (statusEl) statusEl.textContent = "登录成功，正在读取账号 ID…";
              return;
            }
            if (p && statusEl) statusEl.textContent = p.message || "等待扫码 ...";
            if (p && p.status === "expired") {
              if (qrTimer) clearInterval(qrTimer);
              if (btn) btn.disabled = false;
            }
          } catch (_) { /* 忽略单次轮询失败 */ }
        }, 1500);
      } catch (e) {
        if (statusEl) statusEl.textContent = e.error || e.message || "生成二维码失败";
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    const qrBtn = $("vault-qr-btn");
    if (qrBtn) qrBtn.onclick = generateVaultQr;

    // 已有 Cookie / 别处扫码成功：立刻同步 mid，并持续探测直到拿到
    await syncMidFromCookie();
    const midTimer = setInterval(async () => {
      if (midInput && midInput.value) {
        clearInterval(midTimer);
        return;
      }
      const mid = await syncMidFromCookie();
      if (mid) clearInterval(midTimer);
    }, 2000);

    if (submitHandlerBound) return;
    submitHandlerBound = true;
    const submit = $("vault-onboard-submit");
    if (submit) {
      submit.onclick = async () => {
        const mid = ($("vault-onboard-mid") || {}).value || "";
        const p1 = ($("vault-onboard-password") || {}).value || "";
        const p2 = ($("vault-onboard-password2") || {}).value || "";
        if (err) err.textContent = "";
        if (!mid) {
          if (err) err.textContent = "请先扫码登录 B 站账号";
          return;
        }
        if (p1.length < 6) {
          if (err) err.textContent = "应用密码至少 6 位";
          return;
        }
        if (p1 !== p2) {
          if (err) err.textContent = "两次输入的密码不一致";
          return;
        }
        try {
          const res = await api("POST", "/api/vault/onboarding", {
            bound_mid: String(mid).trim(), password: p1,
          });
          modal.style.display = "none";
          showVaultRecovery(res.recovery_code);
        } catch (e) {
          if (err) err.textContent = e.error || e.message || "初始化失败";
        }
      };
    }
  }
  let submitHandlerBound = false;
  maybeShowVaultOnboarding();

  function setUpdateStatus(message, state = "") {
    const box = $("update-status");
    box.className = `update-status ${state}`.trim();
    box.textContent = message;
  }

  // 下载阶段的「停止下载」按钮：只在下载中出现，进入替换阶段后收起（那时无法中止）。
  let updateStopButton = null;

  function renderDownloadProgress(text) {
    const box = $("update-status");
    box.className = "update-status";
    if (!updateStopButton) {
      updateStopButton = document.createElement("button");
      updateStopButton.type = "button";
      updateStopButton.className = "mini";
      updateStopButton.style.marginLeft = "6px";
      updateStopButton.textContent = "停止下载";
      updateStopButton.addEventListener("click", cancelUpdate);
    }
    box.replaceChildren(document.createTextNode(text), updateStopButton);
  }

  function clearUpdateStopButton() {
    updateStopButton = null;
  }

  async function cancelUpdate() {
    if (updateStopButton) updateStopButton.disabled = true;
    try {
      await api("POST", "/api/update/cancel", undefined, { timeoutMs: 10000 });
      log("已请求取消更新下载", "info");
    } catch (e) {
      const reason = e && (e.error || e.message || String(e));
      if (updateStopButton) updateStopButton.disabled = false;
      log(`取消更新失败：${reason}`, "err");
    }
  }

  async function loadAppVersion() {
    try {
      const info = await api("GET", "/api/version");
      const commit = info.commit ? ` (${info.commit})` : "";
      $("app-version").textContent = `版本 ${info.version || "dev"}${commit}`;
    } catch (_) { /* 服务连接状态另有提示 */ }
    // 上次更新失败的原因（写在固定文件里，重启后还能看到）
    try {
      const state = await api("GET", "/api/update/status");
      if (state && state.status === "error" && state.error) {
        setUpdateStatus(`上次更新失败：${state.error}`, "error");
        log(`上次更新失败：${state.error}`, "err");
      }
    } catch (_) { /* 没有失败记录时是 idle，忽略 */ }
  }

  async function watchUpdateProgress(expectedVersion) {
    for (let attempt = 0; attempt < 180; attempt++) {
      let state = null;
      try {
        state = await api("GET", "/api/update/status", undefined, { timeoutMs: 3000 });
      } catch (_) { /* 更新重启期间服务会暂时断开 */ }

      if (state && state.status === "error") {
        const message = state.error || "更新失败";
        clearUpdateStopButton();
        setUpdateStatus(`更新失败：${message}`, "error");
        log(`更新失败：${message}`, "err");
        return false;
      }
      if (state && state.status === "cancelled") {
        clearUpdateStopButton();
        setUpdateStatus("已取消更新下载", "ok");
        log("已取消更新下载", "info");
        return false;
      }
      if (state && state.status === "downloading") {
        const downloaded = Number(state.downloaded_bytes || 0);
        const total = Number(state.total_bytes || 0);
        const progress = total ? ` ${Math.min(100, Math.floor(downloaded * 100 / total))}%` : "";
        renderDownloadProgress(`正在下载更新${progress}…`);
      } else if (state && state.status === "installing") {
        clearUpdateStopButton();
        setUpdateStatus(`正在安装 ${expectedVersion}，应用即将重启…`);
      }

      try {
        const info = await api("GET", "/api/version", undefined, { timeoutMs: 3000 });
        if (info.version === expectedVersion) {
          clearUpdateStopButton();
          setUpdateStatus(`已更新到 ${expectedVersion}，正在重新载入…`, "ok");
          setTimeout(() => window.location.reload(), 800);
          return true;
        }
      } catch (_) { /* 继续等待服务重启 */ }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    clearUpdateStopButton();
    setUpdateStatus("等待更新重启超时，请检查程序窗口后重试", "error");
    log("等待更新程序重启超时", "err");
    return false;
  }

  async function checkForUpdates() {
    const button = $("check-updates");
    button.disabled = true;
    button.classList.add("checking");
    setUpdateStatus("正在检查更新…");
    let installing = false;
    try {
      const result = await api("POST", "/api/update/check", undefined, { timeoutMs: 30000 });
      const currentVersion = result.current_version || "dev";
      $("app-version").textContent = `版本 ${currentVersion}`;
      if (result.version_comparable === false) {
        setUpdateStatus(`当前是开发构建（${currentVersion}），不做版本比较`, "");
        log(`当前 ${currentVersion} 不是正式发布版本，跳过更新比较；如需正式版请手动下载 Release 包`, "info");
        return;
      }
      if (!result.update_available) {
        setUpdateStatus(`已是最新版本（${currentVersion}）`, "ok");
        log(`版本检查完成：当前 ${currentVersion}，已是最新版本`, "ok");
        return;
      }

      log(`发现新版本 ${result.latest_version}（当前 ${currentVersion}）`, "info");
      if (!result.supports_self_update) {
        const status = $("update-status");
        status.className = "update-status";
        status.replaceChildren(document.createTextNode(`发现 ${result.latest_version}。源码运行请 `));
        const link = document.createElement("a");
        link.href = result.release_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "前往下载";
        status.appendChild(link);
        return;
      }

      // 由用户决定是否更新：不再"点了检查就自动下载覆盖"。
      const modeNote = result.install_mode === "installed"
        ? "· 当前是安装版：会下载安装包并静默运行（全机安装时会弹一次 UAC 确认）。\n"
        : "";
      const agreed = confirm(
        `发现新版本 ${result.latest_version}（当前 ${currentVersion}）。\n\n` +
        "现在下载并安装？\n" +
        modeNote +
        "· 下载阶段可以点「停止下载」取消；\n" +
        "· 下载完成后会替换程序文件并自动重启，该阶段无法中止。");
      if (!agreed) {
        setUpdateStatus(`已跳过 ${result.latest_version}，可随时点 ↻ 重新检查`, "");
        log(`已跳过更新 ${result.latest_version}`, "info");
        return;
      }

      setUpdateStatus(`发现 ${result.latest_version}，正在准备更新…`);
      await api("POST", "/api/update/install", undefined, { timeoutMs: 10000 });
      installing = await watchUpdateProgress(result.latest_version);
    } catch (e) {
      const reason = e && (e.error || e.message || String(e));
      setUpdateStatus(`检查更新失败：${reason}`, "error");
      log(`检查更新失败：${reason}`, "err");
    } finally {
      if (!installing) {
        button.disabled = false;
        button.classList.remove("checking");
      }
    }
  }

  $("check-updates").addEventListener("click", checkForUpdates);

  // ---------- 连接状态 ----------
  async function checkConn() {
    try {
      await api("GET", "/api/status");
      $("conn-dot").className = "dot on";
      $("conn-text").textContent = "已连接";
      return true;
    } catch (e) {
      $("conn-dot").className = "dot off";
      $("conn-text").textContent = "未连接";
      return false;
    }
  }

  // ---------- 统计 ----------
  async function refreshStats() {
    try {
      const s = await api("GET", "/api/status");
      const st = s.stats || {};
      $("st-folders").textContent = st.folders ?? "-";
      $("st-videos").textContent = st.videos ?? "-";
      $("st-analyzed").textContent = st.analyzed ?? "-";
      $("st-planned").textContent = st.planned ?? "-";
      return s;
    } catch (e) { return null; }
  }

  async function refreshOrganizationReadiness() {
    const contentBox = $("content-profile-readiness");
    const mergeBox = $("merge-profile-readiness");
    try {
      const status = await api("GET", "/api/organization/readiness");
      const missingRows = (status.missing || []).slice(0, 8);
      const names = missingRows.map(row => row.name
        ? `${row.name}（${row.reason || "画像未就绪"}）` : "").filter(Boolean);
      const suffix = names.length
        ? ` · 待处理：${names.join("、")}${status.missing.length > names.length ? ` 等 ${status.missing.length} 个` : ""}`
        : "";
      const text = status.ready
        ? `画像就绪：${status.current_count}/${status.required_count} 个有内容的 active 收藏夹均已完成扫描并有当前画像。空收藏夹不作为整理目标。`
        : `${status.message || "画像尚未就绪。"}${suffix}`;
      for (const box of [contentBox, mergeBox]) {
        if (!box) continue;
        box.textContent = text;
        box.className = "profile-gate-status " + (status.ready ? "ready" : "blocked");
      }
      for (const id of ["analyze-start", "merge-ai", "merge-submit", "merge-start"]) {
        if ($(id)) $(id).disabled = !status.ready;
      }
      return status;
    } catch (e) {
      const text = "无法读取画像完整性状态；刷新页面或检查服务连接后重试。";
      for (const box of [contentBox, mergeBox]) if (box) box.textContent = text;
      for (const id of ["analyze-start", "merge-ai", "merge-submit", "merge-start"]) {
        if ($(id)) $(id).disabled = true;
      }
      return null;
    }
  }

  // ---------- 配置 / 供应商模型 ----------
  let providersCache = [];
  let modelsCache = [];
  let activeModelId = "";
  let manageSelectedProviderId = "";
  let editingProviderId = "";
  let editingModelId = "";
  let modelFormBaseline = null;
  let modelFormTouched = { ctx: false, out: false };
  let providerPresetsCache = [];
  let lastRefreshDiff = null;
  let selectedRefreshModels = new Map();
  let selectedRefreshPreset = "";

  function modelsOf(providerId) {
    return modelsCache.filter(m => m.provider_id === providerId);
  }

  function providerById(id) {
    return providersCache.find(p => p.id === id) || null;
  }

  function modelById(id) {
    return modelsCache.find(m => m.id === id) || null;
  }

  function fillProviderSelect(selectedId) {
    const sel = $("cfg-provider");
    const current = selectedId !== undefined ? selectedId : sel.value;
    sel.replaceChildren();
    if (!providersCache.length) {
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "（无供应商，请点「管理模型」新增）";
      sel.appendChild(o);
      return;
    }
    for (const p of providersCache) {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.name;
      sel.appendChild(o);
    }
    sel.value = providersCache.some(p => p.id === current) ? current : (providersCache[0] || {}).id || "";
  }

  function fillModelSelect(selectedId) {
    const sel = $("cfg-model");
    const providerId = $("cfg-provider").value;
    const current = selectedId !== undefined ? selectedId : sel.value;
    const list = providerId ? modelsOf(providerId) : [];
    sel.replaceChildren();
    if (!list.length) {
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "（该供应商下无模型）";
      sel.appendChild(o);
      return;
    }
    for (const m of list) {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.name;
      sel.appendChild(o);
    }
    sel.value = list.some(m => m.id === current) ? current
      : (list.find(m => m.id === activeModelId) || list[0] || {}).id || "";
  }

  function renderActiveModelInfo() {
    const box = $("active-model-info");
    const model = modelById($("cfg-model").value || activeModelId);
    const provider = providerById($("cfg-provider").value || (model && model.provider_id) || "");
    if (!model || !provider) {
      box.textContent = "选择供应商与模型后显示地址与参数。";
      return;
    }
    const bits = [
      `供应商：${provider.name}`,
      `地址：${provider.base_url}`,
      `API Key：${provider.api_key_set ? (provider.api_key_masked || "已配置") : "未配置"}`,
      `模型：${model.name}`,
      `上下文：${modelLimitLabel(model.context_tokens, model.context_source)} tokens`,
      `最大输出：${modelLimitLabel(model.max_output_tokens, model.output_source)} tokens`,
      `思考强度：${model.thinking_effort || "默认"}`,
      `测试：${({ ok: "通过", fail: "失败", testing: "测试中", untested: "未测试" })[model.test_status] || model.test_status}`,
    ];
    box.textContent = bits.join("  ·  ");
    box.classList.toggle("is-active", model.id === activeModelId);
  }

  async function loadConfig() {
    try {
      const c = await api("GET", "/api/config");
      providersCache = c.providers || [];
      modelsCache = c.models || [];
      activeModelId = c.active_model_id || "";
      fillProviderSelect(modelById(activeModelId) ? modelById(activeModelId).provider_id : undefined);
      if (activeModelId) {
        const am = modelById(activeModelId);
        if (am) fillModelSelect(am.id);
        else fillModelSelect();
      } else {
        fillModelSelect();
      }
      renderActiveModelInfo();
      if (c.scan_interval) $("scan-interval").value = String(c.scan_interval);
      if (c.write_interval) $("write-interval").value = String(c.write_interval);
      if (c.folder_merge_interval) $("merge-interval").value = String(c.folder_merge_interval);
      if (c.apply_batch) $("apply-batch").value = String(c.apply_batch);
      if (c.analyze_concurrency) $("analyze-concurrency").value = String(c.analyze_concurrency);
      if (c.analyze_batch) $("analyze-batch").value = String(c.analyze_batch);
      if (c.model_tpm_limit) $("model-tpm-limit").value = String(c.model_tpm_limit);
      if (c.profile_request_interval) $("profile-request-interval").value = String(c.profile_request_interval);
      renderManageLists();
      providerPresetsCache = c.provider_presets || [];
      renderPresetList(providerPresetsCache);
    } catch (e) { log("读取配置失败: " + e.message, "err"); }
  }

  $("cfg-provider").addEventListener("change", () => {
    fillModelSelect();
    renderActiveModelInfo();
  });
  $("cfg-model").addEventListener("change", () => {
    renderActiveModelInfo();
  });

  // 请求间隔改动即时保存
  $("scan-interval").addEventListener("change", async () => {
    const v = parseInt($("scan-interval").value, 10);
    try {
      await api("POST", "/api/config", { scan_interval: v });
      log(`请求间隔已设为 ${v} 秒`);
    } catch (e) { log("保存间隔失败: " + (e.error || e.message), "err"); }
  });

  // 写操作间隔改动即时保存
  $("write-interval").addEventListener("change", async () => {
    const v = parseInt($("write-interval").value, 10);
    try {
      await api("POST", "/api/config", { write_interval: v });
      log(`写操作间隔已设为 ${v} 秒`);
    } catch (e) { log("保存写操作间隔失败: " + (e.error || e.message), "err"); }
  });
  $("merge-interval").addEventListener("change", async () => {
    const v = parseInt($("merge-interval").value, 10);
    try { await api("POST", "/api/config", { folder_merge_interval: v }); log(`合并操作间隔已设为 ≥${v} 秒`); }
    catch (e) { log("保存合并间隔失败: " + e.message, "err"); }
  });

  // 批量执行单批条数
  $("apply-batch").addEventListener("change", async () => {
    const v = parseInt($("apply-batch").value, 10);
    try {
      await api("POST", "/api/config", { apply_batch: v });
      log(`执行批大小已设为 ${v} 条`);
    } catch (e) { log("保存批大小失败: " + (e.error || e.message), "err"); }
  });

  // 分析并发数改动即时保存
  $("analyze-concurrency").addEventListener("change", async () => {
    const v = parseInt($("analyze-concurrency").value, 10);
    try {
      await api("POST", "/api/config", { analyze_concurrency: v });
      log(`分析并发数已设为 ${v}（仅影响模型请求）`);
    } catch (e) { log("保存并发数失败: " + (e.error || e.message), "err"); }
  });

  // 分析批大小改动即时保存
  $("analyze-batch").addEventListener("change", async () => {
    const v = parseInt($("analyze-batch").value, 10);
    try {
      await api("POST", "/api/config", { analyze_batch: v });
      log(`分析批大小上限已设为 ${v} 条；超过上下文安全预算时会在发送前自动拆分`);
    } catch (e) { log("保存批大小失败: " + (e.error || e.message), "err"); }
  });

  // 上下文窗口与最大输出由「管理模型」里的模型规格决定，这里不再单独保存。
  $("profile-request-interval").addEventListener("change", async () => {
    const v = parseFloat($("profile-request-interval").value);
    if (!Number.isFinite(v) || v < 0.5) { log("画像请求间隔无效（需 ≥ 0.5 秒）", "warn"); return; }
    try {
      await api("POST", "/api/config", { profile_request_interval: v });
      log(`画像请求间隔已设为 ${v} 秒`);
    } catch (e) { log("保存画像请求间隔失败: " + (e.error || e.message), "err"); }
  });

  $("model-tpm-limit").addEventListener("change", async () => {
    const v = parseInt($("model-tpm-limit").value, 10);
    if (!v || v < 1000) { log("模型 TPM 上限无效（需 ≥ 1000）", "warn"); return; }
    try {
      await api("POST", "/api/config", { model_tpm_limit: v });
      log(`模型 TPM 上限已设为 ${v.toLocaleString("zh-CN")} tokens/分钟`);
    } catch (e) { log("保存模型 TPM 上限失败: " + (e.error || e.message), "err"); }
  });

  $("save-config").addEventListener("click", async () => {
    try {
      const modelId = $("cfg-model").value;
      const payload = {
        active_model_id: modelId || "",
      };
      await api("POST", "/api/config", payload);
      activeModelId = modelId || "";
      log(modelId ? "配置已保存（已切换激活模型）" : "配置已保存（未选择模型）");
      await loadConfig();
    } catch (e) { log("保存配置失败: " + (e.error || e.message), "err"); }
  });

  $("load-stats").addEventListener("click", () => refreshStats());

  // ---------- 进度条 ----------
  function setProgress(el, id, done, total) {
    const pct = total > 0 ? Math.min(100, Math.round(done / total * 100)) : 0;
    const fill = document.getElementById(id + "-fill");
    const text = document.getElementById(id + "-text");
    if (fill) fill.style.width = pct + "%";
    if (text) text.textContent = `${done}/${total} (${pct}%)`;
    if (el) el.style.display = "flex";
  }

  function setAnalyzeProgress(done, inflight, total) {
    const completedPct = total > 0 ? Math.min(100, done / total * 100) : 0;
    const sentPct = total > 0 ? Math.min(100, (done + inflight) / total * 100) : 0;
    $("analyze-fill").style.width = completedPct + "%";
    $("analyze-sent-fill").style.width = sentPct + "%";
    $("analyze-progress").style.display = "flex";
  }

  // ---------- 步骤1 扫描 ----------
  let scanIssues = [];
  let scanIssueId = "";
  let scanRecoveryBusy = false;
  let scanIssuesRunning = false;
  let scanIssueFocus = null;
  let lastScanIssuePrompt = "";
  const scanIssueModal = $("scan-issue-modal");

  function setScanRecoveryControls() {
    const busy = scanRecoveryBusy || scanIssuesRunning;
    const issue = scanIssues.find(row => row.media_id === scanIssueId);
    for (const id of ["scan-issue-retry", "scan-issue-refresh", "scan-issue-select",
                      "scan-issue-clean-consent"]) {
      $(id).disabled = busy;
    }
    $("scan-issue-clean-show").disabled = busy || !!(issue && issue.cleanup_unknown);
    $("scan-issue-clean").disabled = busy || !$("scan-issue-clean-consent").checked || !!(issue && issue.cleanup_unknown);
    $("scan-issue-link").setAttribute("aria-disabled", String(busy));
    scanIssueModal.setAttribute("aria-busy", String(busy));
  }

  function renderScanIssue() {
    const issue = scanIssues.find(row => row.media_id === scanIssueId) || scanIssues[0];
    if (!issue) return;
    const changed = scanIssueId !== issue.media_id;
    scanIssueId = issue.media_id;
    $("scan-issue-title").textContent = "收藏夹数量不一致";
    $("scan-issue-eyebrow").textContent = "扫描需要你的帮助";
    $("scan-issue-block-note").hidden = false;
    $("scan-issue-picker").hidden = scanIssues.length < 2;
    $("scan-issue-select").replaceChildren(...scanIssues.map(row => {
      const option = document.createElement("option");
      option.value = row.media_id;
      option.textContent = row.title;
      option.selected = row.media_id === scanIssueId;
      return option;
    }));
    $("scan-issue-description").textContent = `收藏夹「${issue.title}」的数量与本次扫描结果不一致。你可以选择以下方式处理。`;
    $("scan-issue-expected").textContent = issue.expected_count ?? "—";
    $("scan-issue-fetched").textContent = issue.fetched_count ?? "—";
    $("scan-issue-unique").textContent = issue.unique_count ?? "—";
    $("scan-issue-link").href = `https://www.bilibili.com/medialist/detail/ml${encodeURIComponent(issue.media_id)}`;
    $("scan-issue-options").hidden = false;
    $("scan-issue-foot-note").textContent = "关闭提示后，后续整理仍会暂停。";
    $("scan-issue-dismiss").textContent = "暂时关闭";
    $("scan-issue-result").className = "scan-issue-result";
    if (!scanRecoveryBusy) $("scan-issue-result").textContent = issue.cleanup_unknown
      ? "上次自动清理的结果不确定。请前往 B 站核对，再重试扫描；程序不会重复发送清理请求。"
      : (issue.last_error || "");
    if (changed) {
      $("scan-issue-clean-consent").checked = false;
      $("scan-issue-clean-confirm").hidden = true;
      $("scan-issue-clean-show").setAttribute("aria-expanded", "false");
      $("scan-issue-history").open = false;
    }
    const history = issue.history || [];
    $("scan-issue-history").hidden = !history.length;
    $("scan-issue-history-list").replaceChildren(...history.map(event => {
      const li = document.createElement("li");
      li.textContent = `${event.at} · ${event.text}`;
      return li;
    }));
    setScanRecoveryControls();
  }

  function showScanIssue() {
    if (!scanIssues.length) return;
    lastScanIssuePrompt = scanIssues.map(row => `${row.media_id}:${row.scan_run_id || ""}`).join(",");
    renderScanIssue();
    if (scanIssueModal.style.display !== "flex") {
      scanIssueFocus = document.activeElement;
      scanIssueModal.style.display = "flex";
      $("app").inert = true;
      $("scan-issue-close").focus();
    }
  }

  function closeScanIssue() {
    scanIssueModal.style.display = "none";
    $("app").inert = false;
    if (scanIssueFocus && scanIssueFocus.isConnected) scanIssueFocus.focus();
  }

  async function refreshScanIssues(open = false) {
    try {
      const state = await api("GET", "/api/scan/issues");
      scanIssues = state.issues || [];
      scanIssuesRunning = !!state.running;
      $("scan-issue-banner").hidden = !scanIssues.length;
      $("scan-issue-banner-text").textContent = `${scanIssues.length} 个收藏夹扫描异常，处理后才能继续整理。`;
      if (scanIssues.length) {
        if (open) showScanIssue();
        else if (scanIssueModal.style.display === "flex") renderScanIssue();
      }
      setScanRecoveryControls();
      return scanIssues;
    } catch (e) {
      log("读取扫描异常失败：" + e.message, "warn");
      throw e;
    }
  }

  async function recoverScanIssue(action) {
    if (scanRecoveryBusy || scanIssuesRunning) return;
    if (action === "clean" && !$("scan-issue-clean-consent").checked) return;
    const issue = scanIssues.find(row => row.media_id === scanIssueId);
    if (!issue) return;
    scanRecoveryBusy = true;
    $("scan-issue-clean-consent").checked = false;
    $("scan-issue-result").className = "scan-issue-result";
    $("scan-issue-result").textContent = action === "clean"
      ? "正在清理失效内容，完成后将刷新收藏夹并重新扫描…" : "正在重新读取收藏夹并扫描，请稍候…";
    setScanRecoveryControls();
    let recoveryError = "";
    try {
      const started = await api("POST", `/api/scan/issues/${encodeURIComponent(issue.media_id)}/recover`,
        { action, confirmed: action === "clean" });
      const deadline = Date.now() + 6 * 60 * 60 * 1000;
      let status;
      while (true) {
        status = await api("GET", "/api/scan/status");
        if (status.id === started.run_id && !status.running) break;
        if (Date.now() > deadline) throw new Error("扫描等待超时，请查看日志和当前扫描状态。");
        await sleep(1000);
      }
      const remaining = await refreshScanIssues();
      if (!remaining.some(row => row.media_id === issue.media_id)) {
        const message = `「${issue.title}」重新扫描验证通过。B 站数量由 ${issue.expected_count} 更新为 ${status.total} 条，本次读取 ${status.done} 条。`;
        log(message, "ok");
        if (!remaining.length) {
          $("scan-issue-options").hidden = true;
          $("scan-issue-result").className = "scan-issue-result resolved";
          $("scan-issue-result").textContent = message + " 可以继续整理。";
          $("scan-issue-description").textContent = "收藏夹数据已重新核对一致，扫描异常已解决。";
          $("scan-issue-title").textContent = "扫描异常已解决";
          $("scan-issue-eyebrow").textContent = "核对完成";
          $("scan-issue-block-note").hidden = true;
          $("scan-issue-expected").textContent = status.total;
          $("scan-issue-fetched").textContent = status.done;
          $("scan-issue-unique").textContent = status.done;
          $("scan-issue-history").hidden = true;
          $("scan-issue-foot-note").textContent = "验证通过，可以继续后续步骤。";
          $("scan-issue-dismiss").textContent = "关闭";
        } else {
          renderScanIssue();
          $("scan-issue-result").textContent = message + " 请继续处理其余收藏夹。";
        }
      } else {
        renderScanIssue();
        $("scan-issue-result").textContent = status.error || "数量仍不一致，请检查 B 站收藏夹后再重试。";
      }
      await refreshTree();
      await refreshStats();
      await loadFolderProfiles();
      await refreshOrganizationReadiness();
    } catch (e) {
      recoveryError = e.error || e.message;
      $("scan-issue-result").textContent = recoveryError;
      log("处理未完成：" + (e.error || e.message), "warn");
    } finally {
      scanRecoveryBusy = false;
      try { await refreshScanIssues(); } catch (_) { /* 保留错误说明供用户重试 */ }
      if (recoveryError) $("scan-issue-result").textContent = recoveryError;
      setScanRecoveryControls();
    }
  }

  $("scan-issue-open").addEventListener("click", () => refreshScanIssues(true).catch(() => {}));
  $("scan-issue-close").addEventListener("click", closeScanIssue);
  $("scan-issue-dismiss").addEventListener("click", closeScanIssue);
  $("scan-issue-select").addEventListener("change", () => {
    scanIssueId = "";
    const selected = $("scan-issue-select").value;
    scanIssues.sort((a, b) => Number(b.media_id === selected) - Number(a.media_id === selected));
    renderScanIssue();
  });
  $("scan-issue-link").addEventListener("click", event => {
    if (scanRecoveryBusy || scanIssuesRunning) event.preventDefault();
  });
  $("scan-issue-retry").addEventListener("click", () => recoverScanIssue("retry"));
  $("scan-issue-refresh").addEventListener("click", () => recoverScanIssue("refresh"));
  $("scan-issue-clean-show").addEventListener("click", () => {
    const panel = $("scan-issue-clean-confirm");
    panel.hidden = !panel.hidden;
    $("scan-issue-clean-show").setAttribute("aria-expanded", String(!panel.hidden));
    $("scan-issue-clean-consent").checked = false;
    setScanRecoveryControls();
  });
  $("scan-issue-clean-consent").addEventListener("change", setScanRecoveryControls);
  $("scan-issue-clean").addEventListener("click", () => recoverScanIssue("clean"));
  scanIssueModal.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); closeScanIssue(); }
    if (event.key !== "Tab") return;
    const controls = [...scanIssueModal.querySelectorAll("a[href], button, select, input, summary")]
      .filter(el => !el.disabled && el.getClientRects().length && el.getAttribute("aria-disabled") !== "true");
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  $("scan-btn").addEventListener("click", async () => {
    try {
      const directory = await api("GET", "/api/folders");
      await api("POST", "/api/scan", {
        folder_ids: directory.selected_ids || [],
        mode: $("scan-mode").value || "resume"
      });
      $("scan-progress").style.display = "flex";
      setProgress($("scan-progress"), "scan", 0, 0);
      await refreshOrganizationReadiness();
      log("扫描已启动；优先复用本地索引，缺失内容再读取 B 站元数据");
    } catch (e) {
      log("启动扫描失败: " + (e.error || e.message || ""), "err");
    }
  });

  $("folder-refresh").addEventListener("click", async () => {
    const btn = $("folder-refresh");
    if (btn.disabled) return;
    const label = btn.textContent;
    btn.disabled = true;
    btn.classList.add("busy");
    btn.setAttribute("aria-busy", "true");
    btn.textContent = "已发送请求，刷新中…";
    log("正在同步收藏夹目录…");
    try {
      const r = await api("POST", "/api/folders/refresh");
      log(`收藏夹目录已刷新，共 ${r.folders.length} 个`, "ok");
      await refreshTree();
      await loadFolderMerge();
      if (folderProfileListLoaded) await loadFolderProfiles();
      await refreshOrganizationReadiness();
    } catch (e) { log("刷新目录失败: " + (e.error || e.message), "err"); }
    finally {
      btn.disabled = false;
      btn.classList.remove("busy");
      btn.removeAttribute("aria-busy");
      btn.textContent = label;
    }
  });

  $("scan-stop").addEventListener("click", async () => {
    try {
      await api("POST", "/api/scan/stop");
      log("正在停止扫描（已扫内容已保存）...", "warn");
    } catch (e) { log("停止失败: " + e.message, "err"); }
  });

  // ---------- 项目数据：导出 / 读取 / 清除 ----------
  $("data-export-btn").addEventListener("click", () => {
    log("导出项目数据 ...（浏览器将下载 JSON 文件）");
    window.location.href = "/api/data/export";
  });

  $("data-import-btn").addEventListener("click", () => $("data-file").click());

  $("data-file").addEventListener("change", async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    try {
      const bundle = JSON.parse(await f.text());
      const n = (o) => (o && typeof o === "object") ? Object.keys(o).length : 0;
      const msg = `确定用「${f.name}」覆盖当前项目数据吗？\n\n` +
        `· 收藏夹 ${(bundle.folders || []).length}\n` +
        `· 视频 ${n(bundle.videos)}\n` +
        `· 分析 ${n(bundle.analysis)}\n` +
        `· 方案 ${n(bundle.plan)}\n` +
        `· 已扫完的收藏夹 ${(bundle.scan_done || []).length}`;
      if (!confirm(msg)) return;
      const r = await api("POST", "/api/data/import", { bundle });
      log(`已导入项目数据：${JSON.stringify(r.stats)}`, "ok");
      refreshStats();
    } catch (err) {
      log("导入失败: " + (err.error || err.message), "err");
    } finally {
      e.target.value = "";
    }
  });

  const clearModal = $("clear-modal");
  $("data-clear-btn").addEventListener("click", () => { clearModal.style.display = "flex"; });
  $("clear-close").addEventListener("click", () => { clearModal.style.display = "none"; });
  $("clear-all").addEventListener("click", () => document.querySelectorAll(".clear-options input").forEach(x => x.checked = true));
  $("clear-none").addEventListener("click", () => document.querySelectorAll(".clear-options input").forEach(x => x.checked = false));
  $("clear-confirm").addEventListener("click", async () => {
    const scopes = [...document.querySelectorAll(".clear-options input:checked")].map(x => x.value);
    if (!scopes.length) { log("请至少选择一类数据", "warn"); return; }
    if (!confirm("确定清除已选数据？清除前会自动备份。")) return;
    try {
      const r = await api("POST", "/api/data/clear", { scopes });
      log(`已清除：${r.cleared.join("、")}`, "warn"); clearModal.style.display = "none";
      refreshStats(); loadFolderMerge();
      if (scopes.includes("folder_profile")) {
        await loadFolderProfiles();
        await refreshOrganizationReadiness();
      }
    } catch (e) { log("清除失败: " + (e.error || e.message), "err"); }
  });

  // ---------- 扫码登录 ----------
  let qrTimer = null;

  async function refreshQrCode() {
    if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
    $("qr-box").style.display = "none";
    $("qr-status").textContent = "生成中 ...";
    $("qr-btn").disabled = true;
    try {
      const r = await api("POST", "/api/login/qr/generate", undefined, { timeoutMs: 20000 });
      if (!r.ok) {
        $("qr-status").textContent = "失败：" + r.error;
        log("二维码生成失败：" + r.error, "err");
        return;
      }
      $("qr-img").src = r.image;
      $("qr-box").style.display = "flex";
      $("qr-status").textContent = "请用手机 B站 App 扫码";
      log("二维码已生成，请用手机 B站 App 扫码");
      qrTimer = setInterval(pollQr, 2000);
    } catch (e) {
      const message = e.name === "AbortError"
        ? "二维码请求超时：请检查此电脑能否访问 B站登录服务，或改用手动输入 Cookie"
        : e.message;
      $("qr-status").textContent = "失败：" + message;
      log("二维码生成失败：" + message, "err");
    } finally {
      $("qr-btn").disabled = false;
    }
  }

  $("qr-btn").addEventListener("click", refreshQrCode);

  function setCookieMethod(method, refreshQr = false) {
    const useQr = method !== "manual";
    $("cookie-qr-panel").hidden = !useQr;
    $("cookie-manual-panel").hidden = useQr;
    if (!useQr && qrTimer) { clearInterval(qrTimer); qrTimer = null; }
    if (useQr && refreshQr) refreshQrCode();
  }
  $("cookie-method").addEventListener("change", () =>
    setCookieMethod($("cookie-method").value, $("cookie-method").value === "qr"));

  async function pollQr() {
    try {
      const r = await api("GET", "/api/login/qr/poll");
      if (r.status === "ok") {
        clearInterval(qrTimer); qrTimer = null;
        $("qr-box").style.display = "none";
        $("qr-status").textContent = "✅ 登录成功，Cookie 已保存";
        log("✅ 扫码登录成功，Cookie 已保存", "ok");
        loadCookieState().then(() => {
          $("qr-status").textContent = "✅ 登录成功，Cookie 已保存";
        });
      } else if (r.status === "expired") {
        clearInterval(qrTimer); qrTimer = null;
        $("qr-status").textContent = "二维码已过期，请重新生成";
        log("二维码已过期，请点「刷新二维码」重新获取", "warn");
      } else if (r.status === "scanned") {
        $("qr-status").textContent = "已扫码，请在手机上确认";
      } else if (r.status === "waiting") {
        $("qr-status").textContent = "等待扫码 ...";
      } else if (r.status === "error") {
        clearInterval(qrTimer); qrTimer = null;
        $("qr-status").textContent = "出错：" + (r.message || "");
        log("扫码登录出错：" + (r.message || ""), "err");
      }
    } catch (e) { /* 网络抖动继续轮询 */ }
  }

  // ---------- Cookie 粘贴 ----------
  async function loadCookieState() {
    try {
      const c = await api("GET", "/api/cookie");
      if (c.configured) {
        $("cfg-cookie").placeholder = `已保存 Cookie（长度 ${c.length}），重新粘贴可覆盖`;
        $("cookie-state").textContent = `已保存 Cookie（${c.masked || "已配置"}）`;
        $("qr-status").textContent = "已保存登录 Cookie；需要更换时可刷新二维码";
      } else {
        $("cookie-state").textContent = "尚未保存 Cookie";
        $("qr-status").textContent = "尚未登录，请扫码并在手机上确认";
      }
    } catch (e) { /* ignore */ }
  }

  $("save-cookie").addEventListener("click", async () => {
    const val = $("cfg-cookie").value.trim();
    if (!val) { log("Cookie 为空，未保存", "warn"); return; }
    try {
      await api("POST", "/api/cookie", { cookie_string: val });
      log("Cookie 已保存", "ok");
      loadCookieState();
    } catch (e) { log("保存 Cookie 失败: " + e.message, "err"); }
  });

  // ---------- 连接测试 ----------
  async function testCookie() {
    log("测试 Cookie 读取 ...");
    try {
      const r = await api("POST", "/api/test/cookie");
      if (r.ok) log("✅ " + r.message, "ok");
      else log("❌ Cookie 测试失败: " + r.error, "err");
    } catch (e) { log("❌ 请求失败: " + e.message, "err"); }
  }
  $("test-cookie-qr").addEventListener("click", testCookie);
  $("test-cookie-manual").addEventListener("click", testCookie);

  $("test-llm").addEventListener("click", async () => {
    log("测试模型连接 ...");
    try {
      const r = await api("POST", "/api/test/llm", { model_id: $("cfg-model").value || "" });
      if (r.ok) log("✅ " + r.message, "ok");
      else log("❌ 模型连接失败: " + r.error, "err");
    } catch (e) { log("❌ 请求失败: " + e.message, "err"); }
  });

  // ---------- 扫描状态弹窗 ----------
  const modal = $("tree-modal");
  let folderRows = [];
  $("scan-tree-btn").addEventListener("click", async () => {
    modal.style.display = "flex";
    await refreshTree();
  });
  $("tree-close").addEventListener("click", () => { modal.style.display = "none"; });
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.style.display = "none"; });

  async function refreshTree() {
    try {
      const t = await api("GET", "/api/scan/tree");
      $("tree-meta").textContent =
        `已完成收藏夹 ${t.done_count} / ${t.total_count} · 共 ${t.folders.length} 个`;
      const body = $("tree-body");
      body.innerHTML = "";
      const directory = await api("GET", "/api/folders");
      const selected = new Set(directory.selected_ids || []);
      folderRows = t.folders;
      t.folders.forEach(f => {
        const pct = f.expected > 0 ? Math.round(f.scanned / f.expected * 100) : 100;
        const cls = f.done ? "ok" : (f.scanned > 0 ? "partial" : "none");
        const ico = f.done ? "✅" : (f.status === "inconsistent" ? "⚠️" : (f.scanned > 0 ? "🔄" : "⬜"));
        const row = document.createElement("div");
        row.className = "tree-row";
        row.innerHTML = `<input class="folder-pick" type="checkbox" data-id="${f.media_id}">
          <span class="ico"></span>
          <span class="nm${f.done ? " done" : ""}"></span>
          <span class="cnt"></span>
          <span class="scan-state"></span>
          <span class="pct ${cls}"></span>`;
        row.querySelector(".ico").textContent = ico;
        row.querySelector(".folder-pick").checked = selected.has(String(f.media_id));
        row.querySelector(".nm").textContent = f.title;
        row.querySelector(".cnt").textContent = `${f.scanned} / ${f.expected}`;
        const strategyLabel = f.strategy === "indexed_ids" ? "索引复用" :
          f.strategy === "bulk_ids_infos" ? "批量" :
          f.strategy === "paged" ? "分页" :
          (f.strategy === "paged_fallback" || f.strategy === "paged_index_fallback") ? "回退分页" : "";
        const stateLabel = f.done ? `完成 · ${strategyLabel}` :
          f.status === "inconsistent" ? `数量不一致 · ${strategyLabel}` :
          f.status === "stale" ? "旧进度需校验" : f.status === "partial" ? `未完成 · ${strategyLabel}` : "未扫描";
        row.querySelector(".scan-state").textContent = stateLabel;
        row.querySelector(".pct").textContent = pct + "%";
        body.appendChild(row);
      });
      $("scan-selection-summary").textContent = `已选 ${selected.size || t.folders.length} / ${t.folders.length} 个`;
    } catch (e) { log("读取扫描状态失败: " + e.message, "err"); }
  }

  // ---------- 本地收藏内容浏览 ----------
  const libraryModal = $("library-modal");
  const libraryPageSize = 100;
  let libraryFolders = [];
  let librarySelectedFolder = null;
  let libraryOffset = 0;
  let libraryTotal = 0;
  let librarySearchTimer = null;
  let libraryRequestId = 0;
  const librarySortMode = $("library-sort-mode");
  const librarySortDirection = $("library-sort-direction");
  librarySortMode.value = localStorage.getItem("library-sort-mode") || "bili";
  librarySortDirection.value = localStorage.getItem("library-sort-direction") || "asc";

  $("library-view-btn").addEventListener("click", async () => {
    libraryModal.style.display = "flex";
    $("library-meta").textContent = "正在读取本地收藏夹和扫描状态…";
    try {
      const [tree, directory, profileData] = await Promise.all([
        api("GET", "/api/scan/tree"), api("GET", "/api/folders"),
        api("GET", "/api/folder-profiles"),
      ]);
      const scanById = new Map((tree.folders || []).map(folder => [String(folder.media_id), folder]));
      const profileById = new Map((profileData.folders || []).map(folder => [String(folder.media_id), folder]));
      libraryFolders = (directory.folders || []).map((folder, index) => ({
        ...folder,
        ...(scanById.get(String(folder.media_id)) || {}),
        ...folder,
        profile_data: profileById.get(String(folder.media_id)) || null,
        _biliOrder: index,
        expected: Number(folder.count || 0),
      }));
      $("library-meta").textContent =
        `本地收藏夹 ${tree.total_count} 个 · 已完成扫描 ${tree.done_count} 个 · 这里只读取本地已扫描数据`;
      renderLibraryFolders();
      if (libraryFolders.length) {
        const selected = libraryFolders.find(x => String(x.media_id) === String(librarySelectedFolder));
        selectLibraryFolder((selected || libraryFolders[0]).media_id);
      }
      else {
        $("library-items").textContent = "没有收藏夹目录，请先刷新收藏夹目录。";
        $("library-detail").textContent = "选择一条内容查看详情";
        $("library-folder-info").textContent = "没有收藏夹目录。";
      }
    } catch (e) {
      $("library-meta").textContent = "读取失败：" + e.message;
      $("library-folders").textContent = "";
      $("library-items").textContent = "";
    }
  });
  $("library-close").addEventListener("click", () => { libraryModal.style.display = "none"; });
  libraryModal.addEventListener("click", e => { if (e.target === libraryModal) libraryModal.style.display = "none"; });

  function renderLibraryFolders() {
    const container = $("library-folders");
    container.replaceChildren();
    const collator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });
    const direction = librarySortDirection.value === "desc" ? -1 : 1;
    const ordered = [...libraryFolders].sort((a, b) => {
      let compare = 0;
      if (librarySortMode.value === "count") {
        compare = Number(a.count || 0) - Number(b.count || 0);
      } else if (librarySortMode.value === "name") {
        compare = collator.compare(String(a.title || ""), String(b.title || ""));
      } else {
        compare = Number(a._biliOrder || 0) - Number(b._biliOrder || 0);
      }
      return compare * direction;
    });
    ordered.forEach(folder => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "library-folder" +
        (String(folder.media_id) === String(librarySelectedFolder) ? " active" : "");
      button.setAttribute("role", "treeitem");
      button.setAttribute("aria-selected", String(String(folder.media_id) === String(librarySelectedFolder)));
      const title = document.createElement("span");
      title.className = "library-folder-name";
      title.textContent = folder.title || "未命名收藏夹";
      const count = document.createElement("span");
      count.className = "library-folder-count";
      count.textContent = `${folder.scanned || 0} / ${folder.expected || 0}`;
      button.append(title, count);
      button.addEventListener("click", () => selectLibraryFolder(folder.media_id));
      container.appendChild(button);
    });
  }

  function selectLibraryFolder(mediaId) {
    librarySelectedFolder = String(mediaId);
    libraryOffset = 0;
    const folder = libraryFolders.find(x => String(x.media_id) === librarySelectedFolder);
    $("library-items-title").textContent = folder ? `${folder.title} · 内容` : "内容";
    renderLibraryFolderInfo(folder);
    renderLibraryFolderDetail(folder);
    renderLibraryFolders();
    loadLibraryItems();
  }

  function folderScanStatus(folder) {
    if (folder.scan_state === "inconsistent" || folder.status === "inconsistent") return "数量不一致，需复核";
    if (folder.scan_state === "stale" || folder.status === "stale") return "旧进度待校验";
    if (folder.done) return "已完成";
    if (folder.scanned_count || folder.scanned) return "部分扫描";
    return "尚未扫描";
  }

  function folderStrategy(folder) {
    const strategy = folder.scan_strategy || folder.strategy;
    return ({ indexed_ids: "ID 核对 + 本地索引", bulk_ids_infos: "批量元数据",
      paged: "分页扫描", paged_fallback: "批量失败后分页",
      paged_index_fallback: "索引路径失败后分页" })[strategy] || "—";
  }

  function renderLibraryFolderInfo(folder, localCount = null) {
    const box = $("library-folder-info");
    box.replaceChildren();
    if (!folder) { box.textContent = "选择收藏夹查看数量、扫描状态和画像状态。"; return; }
    const name = document.createElement("strong");
    name.className = "library-folder-info-name";
    name.textContent = folder.title || "未命名收藏夹";
    const stats = document.createElement("span");
    stats.textContent = `B 站内容 ${Number(folder.count || 0).toLocaleString("zh-CN")} 条`;
    const local = document.createElement("span");
    local.textContent = localCount === null
      ? `本地扫描记录 ${Number(folder.scanned_count || folder.scanned || 0).toLocaleString("zh-CN")} 条`
      : `本地记录 ${Number(localCount).toLocaleString("zh-CN")} 条`;
    const scan = document.createElement("span");
    scan.textContent = `扫描：${folderScanStatus(folder)} · ${folderStrategy(folder)}`;
    const profile = document.createElement("span");
    profile.className = "library-profile-state";
    const savedProfile = folder.profile_data && folder.profile_data.profile;
    profile.textContent = savedProfile
      ? `内容画像${folder.profile_data.profile_state === "current" ? "" : "（过期）"}：${savedProfile.summary || "已生成"}`
      : "内容画像：尚未生成";
    box.append(name, stats, local, scan, profile);
    if (folder.scanned_at) {
      const at = document.createElement("span");
      at.textContent = `最近扫描：${folder.scanned_at}`;
      box.appendChild(at);
    }
  }

  function renderLibraryFolderDetail(folder) {
    const detail = $("library-detail");
    $("library-detail-title").textContent = "收藏夹信息";
    detail.replaceChildren();
    if (!folder) { detail.textContent = "选择收藏夹查看信息"; return; }
    const fields = [
      ["名称", folder.title], ["收藏夹 ID", folder.media_id],
      ["B 站内容数量", Number(folder.count || 0).toLocaleString("zh-CN")],
      ["已扫描数量", Number(folder.scanned_count || folder.scanned || 0).toLocaleString("zh-CN")],
      ["扫描状态", folderScanStatus(folder)], ["扫描方式", folderStrategy(folder)],
      ["最近扫描", folder.scanned_at || "暂无"],
      ["画像状态", folder.profile_data ? (folder.profile_data.profile_state === "current" ? "最新" : "过期，建议重建") : "尚未生成"],
    ];
    const profile = folder.profile_data && folder.profile_data.profile;
    if (profile) {
      fields.push(["画像简介", profile.summary]);
      fields.push(["主题", (profile.topics || []).join("、")]);
      fields.push(["适合收纳", (profile.typical_content || []).join("；")]);
      fields.push(["明显范围外", (profile.out_of_scope || []).join("；")]);
      fields.push(["画像样本", `${profile.sample_count || 0} / ${profile.source_item_count || 0} 条 · ${profile.generated_at || profile.updated_at || ""}`]);
    }
    const list = document.createElement("dl");
    list.className = "library-detail-list";
    fields.forEach(([label, value]) => {
      const term = document.createElement("dt"); term.textContent = label;
      const description = document.createElement("dd"); description.textContent = String(value ?? "");
      list.append(term, description);
    });
    detail.appendChild(list);
  }

  async function loadLibraryItems() {
    if (!librarySelectedFolder) return;
    const requestId = ++libraryRequestId;
    const query = $("library-search").value.trim();
    const params = new URLSearchParams({
      q: query, offset: String(libraryOffset), limit: String(libraryPageSize),
    });
    $("library-items").textContent = "读取本地数据…";
    try {
      const result = await api("GET", `/api/library/folders/${encodeURIComponent(librarySelectedFolder)}/items?${params}`);
      if (requestId !== libraryRequestId) return;
      libraryTotal = result.total;
      const folder = libraryFolders.find(x => String(x.media_id) === librarySelectedFolder);
      renderLibraryFolderInfo(folder, query ? null : libraryTotal);
      renderLibraryItems(result.items || []);
      const page = libraryTotal ? Math.floor(libraryOffset / libraryPageSize) + 1 : 0;
      const pages = Math.ceil(libraryTotal / libraryPageSize);
      $("library-page-label").textContent = `${page} / ${pages || 0} 页 · ${libraryTotal} 条`;
      $("library-prev").disabled = libraryOffset <= 0;
      $("library-next").disabled = libraryOffset + libraryPageSize >= libraryTotal;
    } catch (e) {
      if (requestId !== libraryRequestId) return;
      $("library-items").textContent = "读取失败：" + e.message;
      $("library-page-label").textContent = "—";
      $("library-prev").disabled = true;
      $("library-next").disabled = true;
    }
  }

  function renderLibraryItems(items) {
    const container = $("library-items");
    container.replaceChildren();
    if (!items.length) {
      container.textContent = "没有匹配的本地内容。未扫描的收藏夹不会自动联网获取。";
      return;
    }
    items.forEach(item => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "library-item";
      const title = document.createElement("span");
      title.className = "library-item-title";
      title.textContent = item.title || item.bvid || item.resource_key || "未命名内容";
      const sub = document.createElement("span");
      sub.className = "library-item-sub";
      sub.textContent = [item.upper_name, item.bvid, formatLibraryTime(item.fav_time)].filter(Boolean).join(" · ");
      button.append(title, sub);
      const review = libraryProfileReview(item);
      if (review) {
        const reviewTag = document.createElement("span");
        reviewTag.className = "library-item-profile" + (review.startsWith("⚠️") ? " mismatch" : "");
        reviewTag.textContent = review;
        button.appendChild(reviewTag);
      }
      button.addEventListener("click", () => {
        container.querySelectorAll(".library-item.active").forEach(x => x.classList.remove("active"));
        button.classList.add("active");
        renderLibraryDetail(item);
      });
      container.appendChild(button);
    });
  }

  function formatLibraryTime(value) {
    if (value === null || value === undefined || value === "") return "";
    const numeric = Number(value);
    if (Number.isFinite(numeric) && numeric > 0) {
      const date = new Date(numeric < 1e12 ? numeric * 1000 : numeric);
      if (!Number.isNaN(date.getTime())) return date.toLocaleString("zh-CN", { hour12: false });
    }
    return String(value);
  }

  function libraryProfileReview(item) {
    const folder = libraryFolders.find(x => String(x.media_id) === String(librarySelectedFolder));
    if (!folder || !folder.profile_data || !folder.profile_data.profile) return "";
    if (folder.profile_data.profile_state !== "current") return "画像已过期";
    const analysis = item.analysis || {};
    const checked = new Set((analysis.profile_checked_folder_ids || []).map(String));
    const mismatched = new Set((analysis.profile_mismatch_folder_ids || []).map(String));
    const folderId = String(librarySelectedFolder);
    const currentRevision = String(folder.profile_data.profile.profile_revision ||
      folder.profile_data.profile.generated_at || folder.profile_data.profile.updated_at || "");
    const checkedRevision = String((analysis.profile_checked_versions || {})[folderId] || "");
    if (!checked.has(folderId) || !currentRevision || checkedRevision !== currentRevision) return "画像待核对";
    if (mismatched.has(folderId)) return "⚠️画像不符";
    return "画像核对通过";
  }

  function renderLibraryDetail(item) {
    const detail = $("library-detail");
    $("library-detail-title").textContent = "内容详情";
    detail.replaceChildren();
    const fields = [
      ["标题", item.title], ["BVID", item.bvid], ["AV 号", item.aid], ["资源 ID", item.id],
      ["资源类型", item.type], ["UP 主", item.upper_name], ["UP 主 UID", item.upper_mid],
      ["时长（秒）", item.duration], ["收藏时间", formatLibraryTime(item.fav_time)],
      ["发布时间", formatLibraryTime(item.pubtime)], ["简介", item.desc],
      ["标签", Array.isArray(item.tags) ? item.tags.join("、") : item.tags],
      ["属性值", item.attr], ["来源收藏夹 ID", item.source_folder_id], ["资源标识", item.resource_key],
    ];
    const review = libraryProfileReview(item);
    if (review) {
      const analysis = item.analysis || {};
      fields.push(["收藏夹画像核对", review +
        (review.startsWith("⚠️") && analysis.profile_mismatch_reason
          ? " · " + analysis.profile_mismatch_reason : "")]);
    }
    const list = document.createElement("dl");
    list.className = "library-detail-list";
    fields.forEach(([label, value]) => {
      if (value === null || value === undefined || value === "") return;
      const term = document.createElement("dt");
      term.textContent = label;
      const description = document.createElement("dd");
      description.textContent = String(value);
      if (label === "简介") description.classList.add("library-description");
      list.append(term, description);
    });
    detail.appendChild(list);
    if (item.bvid) {
      const link = document.createElement("a");
      link.href = `https://www.bilibili.com/video/${encodeURIComponent(item.bvid)}`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.className = "library-video-link";
      link.textContent = "在 B 站打开 ↗";
      detail.appendChild(link);
    }
  }

  $("library-search").addEventListener("input", () => {
    clearTimeout(librarySearchTimer);
    librarySearchTimer = setTimeout(() => { libraryOffset = 0; loadLibraryItems(); }, 250);
  });
  librarySortMode.addEventListener("change", () => {
    localStorage.setItem("library-sort-mode", librarySortMode.value);
    renderLibraryFolders();
  });
  librarySortDirection.addEventListener("change", () => {
    localStorage.setItem("library-sort-direction", librarySortDirection.value);
    renderLibraryFolders();
  });
  $("library-prev").addEventListener("click", () => {
    libraryOffset = Math.max(0, libraryOffset - libraryPageSize);
    loadLibraryItems();
  });
  $("library-next").addEventListener("click", () => {
    if (libraryOffset + libraryPageSize < libraryTotal) {
      libraryOffset += libraryPageSize;
      loadLibraryItems();
    }
  });

  $("tree-all").addEventListener("click", () => document.querySelectorAll(".folder-pick").forEach(x => x.checked = true));
  $("tree-none").addEventListener("click", () => document.querySelectorAll(".folder-pick").forEach(x => x.checked = false));
  $("tree-undone").addEventListener("click", () => document.querySelectorAll(".folder-pick").forEach((x, i) => x.checked = !folderRows[i].done));
  $("tree-default").addEventListener("click", () => document.querySelectorAll(".folder-pick").forEach((x, i) => x.checked = folderRows[i].title === "默认收藏夹"));
  $("tree-except-default").addEventListener("click", () => document.querySelectorAll(".folder-pick").forEach((x, i) => x.checked = folderRows[i].title !== "默认收藏夹"));
  $("tree-save").addEventListener("click", async () => {
    const ids = [...document.querySelectorAll(".folder-pick:checked")].map(x => x.dataset.id);
    if (!ids.length) { log("至少选择一个收藏夹", "warn"); return; }
    await api("PUT", "/api/scan/selection", { folder_ids: ids });
    $("scan-selection-summary").textContent = `已选 ${ids.length} / ${folderRows.length} 个`;
    modal.style.display = "none";
  });

  // ---------- 模式设置 / 快速模式 ----------
  const fastRun = {
    running: false,
    stop: false,
    biz: null,
    stepIndex: 0,
    steps: [],
  };

  function getFastBiz() {
    const el = document.querySelector('input[name="fast-biz"]:checked');
    return el ? el.value : "";
  }

  function setMode(mode) {
    const isFast = mode === "fast";
    $("mode-fast").classList.toggle("active", isFast);
    $("mode-manual").classList.toggle("active", !isFast);
    $("mode-fast").setAttribute("aria-selected", isFast ? "true" : "false");
    $("mode-manual").setAttribute("aria-selected", isFast ? "false" : "true");
    $("fast-mode-panel").hidden = !isFast;
    $("manual-mode-panel").hidden = isFast;
    const routeStack = document.querySelector(".route-stack");
    if (routeStack) {
      routeStack.inert = isFast;
      routeStack.setAttribute("aria-disabled", isFast ? "true" : "false");
      routeStack.classList.toggle("is-locked", isFast);
    }
    $("manual-route-lock").hidden = !isFast;
  }

  function updateFastRunButton() {
    const btn = $("fast-run");
    const biz = getFastBiz();
    btn.disabled = fastRun.running || !biz;
    btn.setAttribute("aria-pressed", fastRun.running ? "true" : "false");
    btn.classList.toggle("busy", fastRun.running);
    const activeBiz = fastRun.running ? fastRun.biz : biz;
    const updateOnly = activeBiz === "update";
    btn.textContent = fastRun.running
      ? `${updateOnly ? "更新目录" : "自动整理"}中… ${fastRun.stepIndex + 1}/${fastRun.steps.length}`
      : updateOnly ? "开始更新目录" : "开始自动整理";
    document.querySelectorAll('input[name="fast-biz"]').forEach(el => {
      el.disabled = fastRun.running;
    });
    $("fast-stop").hidden = !fastRun.running;
  }

  function renderFastSteps() {
    const box = $("fast-steps");
    if (!fastRun.steps.length) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    box.innerHTML = "";
    fastRun.steps.forEach((step, i) => {
      const li = document.createElement("li");
      li.textContent = step.label;
      if (step.state === "done") li.classList.add("step-done");
      else if (step.state === "fail") li.classList.add("step-fail");
      else if (step.state === "skip") li.classList.add("step-skip");
      else if (i === fastRun.stepIndex && fastRun.running) li.classList.add("step-active");
      box.appendChild(li);
    });
    updateFastRunButton();
  }

  function setFastStep(i, state) {
    if (fastRun.steps[i]) fastRun.steps[i].state = state;
    if (state === "active" || state === undefined) fastRun.stepIndex = i;
    renderFastSteps();
    $("fast-status").textContent = fastRun.steps[i]
      ? `${fastRun.steps[i].label}${state === "done" ? " · 已完成" : state === "fail" ? " · 失败" : ""}`
      : "";
  }

  function switchRoute(routeId) {
    document.querySelectorAll(".route-tab").forEach(x =>
      x.classList.toggle("active", x.dataset.route === routeId));
    document.querySelectorAll(".route-panel").forEach(x => {
      x.style.display = x.id === routeId ? "block" : "none";
    });
  }

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  async function waitUntil(check, label) {
    const started = Date.now();
    while (true) {
      if (fastRun.stop) throw new Error("已停止快速模式");
      if (Date.now() - started > 6 * 60 * 60 * 1000) throw new Error(`${label}超时`);
      const ok = await check();
      if (ok) return;
      await sleep(1500);
    }
  }

  async function waitJobIdle(statusUrl, label, extraFailCheck) {
    await waitUntil(async () => {
      const st = await api("GET", statusUrl);
      if (fastRun.stop) throw new Error("已停止快速模式");
      if (st && st.error) throw new Error(`${label}失败：${st.error}`);
      if (extraFailCheck) {
        const failMsg = extraFailCheck(st);
        if (failMsg) throw new Error(failMsg);
      }
      return st && st.running === false;
    }, label);
  }

  async function waitScanDone() {
    await waitUntil(async () => {
      const st = await api("GET", "/api/scan/status");
      if (fastRun.stop) throw new Error("已停止快速模式");
      if (!st || st.running !== false) return false;
      const issues = await refreshScanIssues();
      if (issues.length) {
        $("fast-status").textContent = "扫描需要处理，解决异常后自动继续";
        const promptKey = issues.map(row => `${row.media_id}:${row.scan_run_id || ""}`).join(",");
        if (scanIssueModal.style.display !== "flex" && promptKey !== lastScanIssuePrompt) showScanIssue();
        return false;
      }
      const err = String(st.error || "");
      if (err.includes("停止")) throw new Error(`扫描已停止：${err}`);
      if (err) throw new Error(`扫描失败：${err}`);
      return true;
    }, "扫描");
  }

  async function waitMergeAiDone() {
    await waitUntil(async () => {
      const r = await api("GET", "/api/folder-organize");
      if (fastRun.stop) throw new Error("已停止快速模式");
      const ai = (r && r.ai_run) || {};
      if (ai.error) throw new Error(`AI 合并建议失败：${ai.error}`);
      return ai.running === false;
    }, "AI 合并建议");
  }

  function showFastDone(title, text) {
    $("fast-done-title").textContent = title;
    $("fast-done-text").textContent = text;
    $("fast-done-modal").style.display = "flex";
  }

  function closeFastDone() {
    $("fast-done-modal").style.display = "none";
    fastRun.running = false;
    fastRun.stop = false;
    updateFastRunButton();
  }

  async function stopFastCurrentJob() {
    const step = fastRun.steps[fastRun.stepIndex] || {};
    const label = step.label || "";
    try {
      if (label.includes("扫描")) await api("POST", "/api/scan/stop");
      else if (label.includes("画像")) await api("POST", "/api/folder-profiles/stop");
      else if (label.includes("归类") || label.includes("分析")) await api("POST", "/api/analyze/stop");
      else if (label.includes("合并")) await api("POST", "/api/folder-organize/stop");
    } catch (_) { /* stop 尽力而为 */ }
  }

  async function runFastMode() {
    if (fastRun.running) return;
    const biz = getFastBiz();
    if (!biz) { log("请选择快速模式业务", "warn"); return; }
    const isUpdate = biz === "update";

    // 前置检查
    try {
      const login = await api("GET", "/api/login/status");
      if (!login || !login.configured) {
        log("快速模式需要先登录 B 站（扫码或手动 Cookie）", "err");
        return;
      }
      if (!isUpdate) {
        const cfg = await api("GET", "/api/config");
        if (!cfg || !cfg.active_model_id) {
          log("快速模式需要先在模型配置中激活一个模型", "err");
          return;
        }
      }
      const st = await api("GET", "/api/status");
      const runs = st || {};
      let busy = Boolean(
        (runs.scan && runs.scan.running) ||
        (runs.analyze && runs.analyze.running) ||
        (runs.apply && runs.apply.running)
      );
      if (!busy) {
        try {
          const prof = await api("GET", "/api/folder-profiles/status");
          if (prof && prof.running) busy = true;
        } catch (_) { /* 忽略 */ }
      }
      if (!busy) {
        try {
          const org = await api("GET", "/api/folder-organize");
          if ((org && org.ai_run && org.ai_run.running) ||
              (org && org.run && org.run.running)) busy = true;
        } catch (_) { /* 忽略 */ }
      }
      if (busy) {
        log("已有整理任务在运行，请先停止或等待完成", "warn");
        return;
      }
    } catch (e) {
      log("快速模式前置检查失败: " + (e.error || e.message || e), "err");
      return;
    }

    const isContent = biz === "content";
    const steps = [
      { label: "刷新收藏夹目录", state: "pending", run: async () => {
        const r = await api("POST", "/api/folders/refresh");
        if (!r || r.ok === false) throw new Error((r && r.error) || "刷新目录失败");
        await refreshTree();
        if (!isUpdate) await loadFolderProfiles();
      }},
      { label: "勾选全部收藏夹", state: "pending", run: async () => {
        const directory = await api("GET", "/api/folders");
        const ids = (directory.folders || []).map(f => String(f.media_id));
        if (!ids.length) throw new Error("收藏夹目录为空");
        await api("PUT", "/api/scan/selection", { folder_ids: ids });
        $("scan-selection-summary").textContent = `已选 ${ids.length} / ${ids.length} 个`;
      }},
      { label: "开始扫描（补齐未完成/变化）", state: "pending", run: async () => {
        if (!isUpdate) switchRoute("content-route");
        const directory = await api("GET", "/api/folders");
        await api("POST", "/api/scan", {
          folder_ids: directory.selected_ids || [],
          mode: "resume",
        });
      }},
      { label: "等待扫描完成", state: "pending", run: async () => {
        await waitScanDone();
      }},
    ];

    if (!isUpdate) steps.push(
      { label: "生成缺失 / 过期画像", state: "pending", run: async () => {
        switchRoute("profile-route");
        await loadFolderProfiles();
        // 全选 active（默认夹是收件箱，后端也会拒绝，这里先排除）
        document.querySelectorAll(".folder-profile-pick:not(:disabled)").forEach(x => {
          const card = x.closest(".folder-profile-card");
          const isDefault = card && card.classList.contains("is-default");
          if (!isDefault) x.checked = true;
        });
        await startFolderProfileGeneration(false);
      }},
      { label: "等待画像完成", state: "pending", run: async () => {
        await waitJobIdle("/api/folder-profiles/status", "画像生成");
        await refreshOrganizationReadiness();
      }},
    );

    if (isContent) {
      steps.push(
        { label: "LLM 归类分析开始", state: "pending", run: async () => {
          switchRoute("content-route");
          const r = await api("POST", "/api/analyze/start", {
            continuous: false,
            rebuild: $("analyze-rebuild").value || "incremental",
          });
          if (r && r.error) throw new Error(r.error);
          log(`快速模式分析已启动：候选 ${r.total || 0} 条`);
        }},
        { label: "等待分析完成", state: "pending", run: async () => {
          await waitJobIdle("/api/analyze/status", "LLM 归类分析", (st) => {
            if (st && st.stopped && (st.done || 0) < (st.total || 0)) {
              return "分析被中断";
            }
            return null;
          });
        }},
        { label: "加载分析结果", state: "pending", run: async () => {
          await loadPlan();
        }},
      );
    } else if (biz === "folder") {
      steps.push(
        { label: "生成 AI 合并建议", state: "pending", run: async () => {
          switchRoute("folder-route");
          await loadFolderMerge();
          await api("POST", "/api/folder-organize/suggest");
          log("快速模式：已开始 AI 收藏夹合并分析");
        }},
        { label: "等待合并建议完成", state: "pending", run: async () => {
          await waitMergeAiDone();
          await loadFolderMerge();
        }},
      );
    }

    fastRun.running = true;
    fastRun.stop = false;
    fastRun.biz = biz;
    fastRun.steps = steps;
    fastRun.stepIndex = 0;
    renderFastSteps();
    log(`快速模式已启动：${isUpdate ? "更新目录" : isContent ? "内容整理" : "收藏夹整理"}`);

    try {
      for (let i = 0; i < steps.length; i++) {
        if (fastRun.stop) throw new Error("已停止快速模式");
        setFastStep(i, "active");
        await steps[i].run();
        if (fastRun.stop) throw new Error("已停止快速模式");
        setFastStep(i, "done");
      }
      setFastStep(steps.length - 1, "done");
      setMode("manual");
      $("fast-status").textContent = isUpdate
        ? "目录更新和扫描已完成，已切换到手动模式"
        : "自动步骤已完成，已切换到手动模式，等待人工复核";
      if (isUpdate) {
        showFastDone(
          "快速模式 · 更新目录完成",
          "已完成：刷新收藏夹目录 → 全选收藏夹 → 扫描全部收藏夹。\n\n扫描结束后，快速模式已停止。"
        );
        log("目录更新和扫描已完成", "ok");
      } else if (isContent) {
        showFastDone(
          "快速模式 · 内容整理完成",
          "已完成：刷新目录 → 扫描 → 生成画像 → LLM 归类分析 → 加载分析结果。\n\n" +
          "请到「内容整理」查看预归类方案，逐条确认后提交方案，再手动点击「开始执行」。"
        );
        log("快速模式内容整理完成，请复核预归类方案后手动执行", "ok");
      } else {
        showFastDone(
          "快速模式 · 收藏夹整理完成",
          "已完成：刷新目录 → 扫描 → 生成画像 → 生成 AI 合并建议。\n\n" +
          "请到「收藏夹整理」查看合并组草稿，人工复核后提交合并任务，再手动执行。"
        );
        log("快速模式收藏夹整理完成，请复核合并建议后手动执行", "ok");
      }
    } catch (e) {
      const msg = e && (e.error || e.message || String(e));
      if (String(msg).includes("停止")) {
        setFastStep(fastRun.stepIndex, "fail");
        $("fast-status").textContent = "已停止";
        log("快速模式已停止", "warn");
      } else {
        setFastStep(fastRun.stepIndex, "fail");
        $("fast-status").textContent = "失败：" + msg;
        log("快速模式中断: " + msg, "err");
        showFastDone("快速模式已中断", `在「${(fastRun.steps[fastRun.stepIndex] || {}).label || "未知步骤"}」失败：\n${msg}\n\n可切换到手动模式继续处理，或修复后重新运行。`);
      }
    } finally {
      if ($("fast-done-modal").style.display !== "flex") {
        fastRun.running = false;
        fastRun.stop = false;
        updateFastRunButton();
      }
    }
  }

  $("mode-fast").addEventListener("click", () => setMode("fast"));
  $("mode-manual").addEventListener("click", () => setMode("manual"));
  document.querySelectorAll('input[name="fast-biz"]').forEach(el => {
    el.addEventListener("change", updateFastRunButton);
  });
  $("fast-run").addEventListener("click", () => {
    if (fastRun.running) return;
    runFastMode();
  });
  $("fast-stop").addEventListener("click", async () => {
    if (!fastRun.running) return;
    fastRun.stop = true;
    $("fast-status").textContent = "正在停止…";
    await stopFastCurrentJob();
  });
  $("fast-done-ok").addEventListener("click", closeFastDone);
  $("fast-done-close").addEventListener("click", closeFastDone);

  // ---------- 三路线标签（收藏夹画像 / 内容整理 / 收藏夹整理） ----------
  document.querySelectorAll(".route-tab").forEach(tab => tab.addEventListener("click", () => {
    document.querySelectorAll(".route-tab").forEach(x => x.classList.toggle("active", x === tab));
    document.querySelectorAll(".route-panel").forEach(x => {
      x.style.display = x.id === tab.dataset.route ? "block" : "none";
    });
    const stack = document.querySelector(".route-stack");
    if (stack && stack.scrollIntoView) stack.scrollIntoView({ block: "start", behavior: "smooth" });
    if (tab.dataset.route === "profile-route") loadFolderProfiles();
  }));

  // ---------- 收藏夹画像 ----------
  let folderProfileRows = [];
  let folderProfileListLoaded = false;
  let profileIntroFolder = null;
  let profileIntroRemote = null;
  let profileIntroRequest = 0;
  let profileIntroLoading = false;

  function updateProfileIntroCounter() {
    const value = $("profile-intro-edit").value || "";
    const overLimit = value.length > 200;
    const sameAsRemote = profileIntroRemote && value === String(profileIntroRemote.intro || "");
    $("profile-intro-count").textContent = `${value.length} / 200` + (overLimit ? "（超出上限）" : "");
    $("profile-intro-count").classList.toggle("over-limit", overLimit);
    const nameMatches = profileIntroFolder && profileIntroRemote &&
      String(profileIntroRemote.title || "").trim() === String(profileIntroFolder.title || "").trim();
    $("profile-intro-publish").disabled = profileIntroLoading || !profileIntroRemote ||
      !nameMatches || overLimit || sameAsRemote;
    $("profile-intro-publish").textContent = sameAsRemote ? "简介已同步" : "确认上传简介";
  }

  async function openProfileIntroModal(row) {
    const modal = $("profile-intro-modal");
    const requestId = ++profileIntroRequest;
    profileIntroFolder = row;
    profileIntroRemote = null;
    profileIntroLoading = true;
    $("profile-intro-title").textContent = `同步「${row.title || "未命名收藏夹"}」简介`;
    $("profile-intro-status").textContent = row.profile_state === "current"
      ? "正在读取 B 站当前简介…"
      : "本地画像已过期或未完成核对，请检查内容后再决定是否上传。正在读取 B 站当前简介…";
    $("profile-intro-remote").value = "";
    $("profile-intro-edit").value = String(row.profile?.summary || "");
    modal.style.display = "flex";
    updateProfileIntroCounter();
    try {
      const remote = await api("GET", `/api/folder-profiles/${encodeURIComponent(row.media_id)}/bilibili`);
      if (requestId !== profileIntroRequest) return;
      profileIntroRemote = remote;
      $("profile-intro-remote").value = String(remote.intro || "");
      const matches = String(remote.title || "").trim() === String(row.title || "").trim();
      const profileNotice = row.profile_state === "current" ? "" : "；注意：该画像不是当前有效版本";
      const privacyLabel = remote.privacy === null || remote.privacy === undefined
        ? "公开状态未知" : (remote.privacy ? "私密收藏夹" : "公开收藏夹");
      $("profile-intro-status").textContent = matches
        ? `已读取 B 站现有简介（${privacyLabel}）${profileNotice}`
        : `B 站当前名称为「${remote.title || "未命名收藏夹"}」，与本地目录不一致；请先刷新收藏夹目录${profileNotice}`;
      updateProfileIntroCounter();
    } catch (e) {
      if (requestId !== profileIntroRequest) return;
      profileIntroLoading = false;
      $("profile-intro-status").textContent = "读取 B 站简介失败：" + (e.error || e.message);
      updateProfileIntroCounter();
    } finally {
      if (requestId === profileIntroRequest) {
        profileIntroLoading = false;
        updateProfileIntroCounter();
      }
    }
  }

  $("profile-intro-edit").addEventListener("input", updateProfileIntroCounter);
  function closeProfileIntroModal() {
    profileIntroRequest++;
    profileIntroFolder = null;
    profileIntroRemote = null;
    profileIntroLoading = false;
    $("profile-intro-modal").style.display = "none";
  }
  $("profile-intro-close").addEventListener("click", closeProfileIntroModal);
  $("profile-intro-cancel").addEventListener("click", closeProfileIntroModal);
  $("profile-intro-modal").addEventListener("click", event => {
    if (event.target === $("profile-intro-modal")) closeProfileIntroModal();
  });
  $("profile-intro-publish").addEventListener("click", async () => {
    if (!profileIntroFolder || !profileIntroRemote) return;
    const intro = $("profile-intro-edit").value;
    if (intro.length > 200) return;
    if (!confirm(`将用本地画像简介覆盖 B 站「${profileIntroFolder.title}」的当前简介？\n\n` +
                 "上传时会保留收藏夹名称、公开状态和封面。")) return;
    const button = $("profile-intro-publish");
    button.disabled = true;
    button.textContent = "上传中…";
    try {
      const result = await api("POST",
        `/api/folder-profiles/${encodeURIComponent(profileIntroFolder.media_id)}/publish-intro`,
        { intro });
      profileIntroRemote = { ...profileIntroRemote, intro: result.intro ?? intro };
      $("profile-intro-remote").value = String(profileIntroRemote.intro || "");
      $("profile-intro-status").textContent = `已上传并收到 B 站成功响应；本地画像未修改。`;
      log(`「${profileIntroFolder.title}」收藏夹简介已上传到 B 站`, "ok");
      updateProfileIntroCounter();
    } catch (e) {
      const message = e.error || e.message;
      $("profile-intro-status").textContent = "上传失败：" + message;
      updateProfileIntroCounter();
      if (!button.disabled) button.textContent = "重试上传";
      log(`「${profileIntroFolder.title}」简介上传失败：${message}`, "err");
    }
  });

  function profileStateLabel(row) {
    if (row.is_default) return "未分拣收件箱";
    if (!row.scan_complete) return "扫描未完成";
    if (row.profile_state === "current") return "画像最新";
    if (row.profile_state === "stale") return "画像过期";
    return "尚无画像";
  }

  async function loadFolderProfiles() {
    folderProfileListLoaded = true;
    try {
      const response = await api("GET", "/api/folder-profiles");
      folderProfileRows = response.folders || [];
      renderFolderProfiles();
      const run = response.run || {};
      $("profile-status").textContent = run.running
        ? "生成中 " + (run.done || 0) + "/" + (run.total || 0) + " · " + (run.current || "")
        : folderProfileRows.length + " 个 active 收藏夹";
    } catch (e) {
      $("folder-profile-list").textContent = "读取画像列表失败：" + e.message;
      $("profile-status").textContent = "";
    }
  }

  function renderFolderProfiles() {
    const box = $("folder-profile-list");
    box.replaceChildren();
    if (!folderProfileRows.length) {
      box.textContent = "没有 active 收藏夹。点击「刷新收藏夹目录」同步 B 站目录。";
      return;
    }
    folderProfileRows.forEach(row => {
      const isDefault = Boolean(row.is_default);
      const card = document.createElement("article");
      card.className = "folder-profile-card" + (isDefault ? " is-default" : "");
      const head = document.createElement("div");
      head.className = "folder-profile-head";
      const pick = document.createElement("input");
      pick.type = "checkbox";
      pick.className = "folder-profile-pick";
      pick.value = String(row.media_id);
      const title = document.createElement("strong");
      title.textContent = row.title || "未命名收藏夹";
      const state = document.createElement("span");
      state.className = "folder-profile-state-badge " + (isDefault ? "inbox" : (row.profile_state || "missing"));
      state.textContent = profileStateLabel(row);
      const count = document.createElement("span");
      count.className = "muted";
      const localN = Number(row.local_count || 0);
      const remoteN = Number(row.count || 0);
      let countText = "内容 " + localN.toLocaleString("zh-CN") + " / " + remoteN.toLocaleString("zh-CN");
      if (localN !== remoteN) countText += "（目录待刷新）";
      count.textContent = countText;
      const del = document.createElement("button");
      del.type = "button";
      del.className = "folder-profile-del";
      del.textContent = "🗑";
      del.disabled = !row.profile;
      del.title = "删除该收藏夹的本地画像（不改动 B 站简介）";
      del.addEventListener("click", () => deleteFolderProfile(row));
      if (isDefault) {
        pick.disabled = true;
        pick.title = "默认收藏夹是未分拣收件箱，不生成画像";
        del.title = "删除默认收藏夹的本地画像（它不参与归类）";
      }
      head.append(pick, title, state, count, del);
      card.appendChild(head);

      const profile = row.profile;
      if (profile) {
        const summary = document.createElement("p");
        summary.className = "folder-profile-summary";
        summary.textContent = profile.summary || "画像没有简介";
        card.appendChild(summary);
        const traits = [];
        if (profile.topics && profile.topics.length) traits.push("主题：" + profile.topics.join("、"));
        if (profile.typical_content && profile.typical_content.length) traits.push("适合：" + profile.typical_content.join("；"));
        if (profile.out_of_scope && profile.out_of_scope.length) traits.push("范围外：" + profile.out_of_scope.join("；"));
        if (traits.length) {
          const notes = document.createElement("div");
          notes.className = "folder-profile-notes";
          notes.textContent = traits.join("　|　");
          card.appendChild(notes);
        }
        const meta = document.createElement("div");
        meta.className = "folder-profile-meta muted";
        meta.textContent = "样本 " + (profile.sample_count || 0) + " / " +
          (profile.source_item_count || 0) + " 条 · 一致性 " + (profile.coherence || "—") +
          " · 画像把握 " + Math.round(Number(profile.confidence || 0) * 100) + "% · 更新 " +
          (profile.generated_at || profile.updated_at || "—");
        card.appendChild(meta);
        const actions = document.createElement("div");
        actions.className = "folder-profile-actions";
        const upload = document.createElement("button");
        upload.type = "button";
        upload.className = "btn ghost mini-btn";
        upload.textContent = "上传简介到 B 站";
        upload.title = "先预览 B 站现有简介，再确认覆盖";
        upload.addEventListener("click", () => openProfileIntroModal(row));
        actions.appendChild(upload);
        card.appendChild(actions);
      } else {
        const empty = document.createElement("p");
        empty.className = "muted folder-profile-empty";
        if (isDefault) {
          empty.textContent = "未分拣收件箱，不生成画像。里面的内容按其余收藏夹的画像判定归属。";
        } else {
          empty.textContent = row.scan_complete ? "尚未生成画像。" : "先完成该收藏夹的扫描，再生成画像。";
        }
        card.appendChild(empty);
      }
      box.appendChild(card);
    });
  }

  async function deleteFolderProfile(row) {
    const name = row.title || "未命名收藏夹";
    const ok = window.confirm(
      "确认删除「" + name + "」的本地画像？\n\n" +
      "只删除本地保存的画像，不会改动 B 站收藏夹简介。\n" +
      "删除后如需恢复，请重新生成画像。");
    if (!ok) return;
    try {
      await api("DELETE", "/api/folder-profiles/" + encodeURIComponent(String(row.media_id)));
      log("已删除「" + name + "」的收藏夹画像", "warn");
      await loadFolderProfiles();
      await refreshOrganizationReadiness();
    } catch (e) { log("删除画像失败: " + (e.error || e.message), "err"); }
  }

  async function startFolderProfileGeneration(rebuild) {
    const defaultIds = new Set(folderProfileRows.filter(r => r.is_default).map(r => String(r.media_id)));
    const folderIds = [...document.querySelectorAll(".folder-profile-pick:checked")]
      .map(input => input.value)
      .filter(id => !defaultIds.has(String(id)));
    if (!folderIds.length) {
      const msg = "请先选择 active 收藏夹（默认收藏夹是收件箱，不生成画像）";
      log(msg, "warn");
      throw new Error(msg);
    }
    try {
      const result = await api("POST", "/api/folder-profiles/generate", { folder_ids: folderIds, rebuild });
      $("profile-status").textContent = "画像任务已启动，共 " + result.total + " 个收藏夹";
      $("profile-progress").style.display = "flex";
      setProgress($("profile-progress"), "profile", 0, result.total || 0);
      $("profile-text").textContent = "等待开始 0/" + (result.total || 0);
      return result;
    } catch (e) {
      const msg = (e && (e.error || e.message)) || String(e);
      log("启动画像任务失败: " + msg, "err");
      throw e instanceof Error ? e : new Error(msg);
    }
  }

  $("profile-refresh").addEventListener("click", loadFolderProfiles);
  $("profile-select-all").addEventListener("click", () =>
    document.querySelectorAll(".folder-profile-pick:not(:disabled)").forEach(x => { x.checked = true; }));
  $("profile-select-none").addEventListener("click", () =>
    document.querySelectorAll(".folder-profile-pick").forEach(x => { x.checked = false; }));
  $("profile-generate").addEventListener("click", () => {
    startFolderProfileGeneration(false).catch(() => {});
  });
  $("profile-rebuild").addEventListener("click", () => {
    startFolderProfileGeneration(true).catch(() => {});
  });
  $("profile-stop").addEventListener("click", async () => {
    try { await api("POST", "/api/folder-profiles/stop"); log("正在停止画像任务…", "warn"); }
    catch (e) { log("停止画像任务失败: " + e.message, "err"); }
  });

  // ---------- 收藏夹整理：可编辑合并组表 ----------
  let mergeFolders = [];
  let mergeGroups = [];
  let submittedMergeGroups = [];
  let draftTimer = null;
  let editingSourceIndex = -1;
  const folderById = () => Object.fromEntries(mergeFolders.map(f => [String(f.media_id), f]));
  function clearMergeProfileEvidence(group) {
    group.profile_context_ids = [];
    group.profile_context_versions = {};
    group.profile_evidence = "";
    group.profile_context_state = "none";
  }
  function saveMergeDraftSoon() {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(async () => {
      try { await api("PUT", "/api/folder-organize/draft", { groups: mergeGroups }); }
      catch (e) { log("自动保存合并草稿失败: " + (e.error || e.message), "err"); }
    }, 500);
  }
  function normalizeMergeNames(g) {
    const byId = folderById();
    g.target_name = (byId[String(g.target_id)] || {}).title || "";
    g.source_names = (g.source_ids || []).map(x => (byId[String(x)] || {}).title || x);
  }
  function renderMergePlan() {
    const box = $("merge-plan"); box.innerHTML = "";
    if (!mergeGroups.length) {
      box.innerHTML = '<span class="muted">尚未生成或添加合并组。</span>'; return;
    }
    const table = document.createElement("table"); table.className = "merge-table";
    table.innerHTML = "<thead><tr><th>合并后收藏夹</th><th>目标收藏夹</th><th>来源收藏夹（复选）</th><th>清空后删除来源夹</th><th>参考</th><th></th></tr></thead>";
    const tbody = document.createElement("tbody");
    mergeGroups.forEach((g, i) => {
      normalizeMergeNames(g);
      const tr = document.createElement("tr");
      const nameTd = document.createElement("td");
      const name = document.createElement("input"); name.value = g.final_name || g.target_name || "";
      name.addEventListener("input", () => {
        g.final_name = name.value.trim(); clearMergeProfileEvidence(g); saveMergeDraftSoon();
      }); nameTd.appendChild(name);

      const targetTd = document.createElement("td");
      const target = document.createElement("select");
      mergeFolders.forEach(f => {
        const o = document.createElement("option"); o.value = String(f.media_id); o.textContent = `${f.title} (${f.count || 0})`;
        target.appendChild(o);
      });
      target.value = String(g.target_id || "");
      target.addEventListener("change", () => {
        g.target_id = target.value;
        g.source_ids = (g.source_ids || []).filter(x => String(x) !== target.value);
        clearMergeProfileEvidence(g);
        normalizeMergeNames(g); renderMergePlan(); saveMergeDraftSoon();
      }); targetTd.appendChild(target);

      const sourcesTd = document.createElement("td");
      const cards = document.createElement("div"); cards.className = "source-cards";
      (g.source_ids || []).forEach(id => {
        const f = folderById()[String(id)]; if (!f) return;
        const card = document.createElement("span"); card.className = "source-card";
        card.appendChild(document.createTextNode(f.title));
        const remove = document.createElement("button"); remove.textContent = "×"; remove.title = "移除";
        remove.addEventListener("click", () => {
          g.source_ids = g.source_ids.filter(x => String(x) !== String(id));
          clearMergeProfileEvidence(g);
          normalizeMergeNames(g); renderMergePlan(); saveMergeDraftSoon();
        }); card.appendChild(remove); cards.appendChild(card);
      });
      const add = document.createElement("button"); add.className = "source-add"; add.textContent = "+"; add.title = "修改来源收藏夹";
      add.addEventListener("click", () => openSourceEditor(i)); cards.appendChild(add); sourcesTd.appendChild(cards);

      const deleteTd = document.createElement("td"); deleteTd.className = "merge-delete-source";
      const deleteLabel = document.createElement("label");
      const deleteCheck = document.createElement("input"); deleteCheck.type = "checkbox";
      deleteCheck.checked = g.delete_sources !== false;
      deleteCheck.addEventListener("change", () => { g.delete_sources = deleteCheck.checked; saveMergeDraftSoon(); });
      deleteLabel.append(deleteCheck, document.createTextNode(" 删除空夹")); deleteTd.appendChild(deleteLabel);

      const refTd = document.createElement("td"); refTd.className = "merge-reference";
      const levelName = g.level === "high" ? "高置信" : (g.level === "medium" ? "待人工判断" : "");
      refTd.textContent = (levelName ? `[ ${levelName} ] ` : "") + (g.reason || "手动添加");
      if (g.confidence) refTd.textContent += ` · ${Math.round(g.confidence * 100)}%`;
      if (g.risk) refTd.textContent += ` · 风险：${g.risk}`;
      const profileContextNames = (g.profile_context_ids || []).map(id =>
        (folderById()[String(id)] || {}).title || String(id));
      if (g.profile_basis_state === "stale") {
        refTd.textContent += " · 建议画像版本已过期，需重新生成 AI 建议";
      } else if (!profileContextNames.length && g.profile_basis_state === "current") {
        refTd.textContent += " · 人工调整（原建议画像版本仍有效）";
      } else if (profileContextNames.length) {
        const statusText = g.profile_context_state === "current" ? "画像最新"
          : (g.profile_context_state === "stale" ? "画像已过期" : "画像参考");
        refTd.textContent += ` · ${statusText}（${profileContextNames.join("、")}）`;
        if (g.profile_evidence) refTd.textContent += `：${g.profile_evidence}`;
        else if (g.profile_context_state === "stale") refTd.textContent += "；建议重新生成候选";
      } else if (g.profile_evidence) {
        refTd.textContent += ` · 画像依据：${g.profile_evidence}`;
      }
      if (g.status && g.status !== "pending") refTd.textContent += ` · ${g.status}`;
      const actTd = document.createElement("td");
      const del = document.createElement("button"); del.className = "mini"; del.textContent = "删除行";
      del.addEventListener("click", () => { mergeGroups.splice(i, 1); renderMergePlan(); saveMergeDraftSoon(); }); actTd.appendChild(del);
      tr.append(nameTd, targetTd, sourcesTd, deleteTd, refTd, actTd); tbody.appendChild(tr);
    });
    table.appendChild(tbody); box.appendChild(table);
  }
  async function loadFolderMerge() {
    try {
      const r = await api("GET", "/api/folder-organize");
      mergeFolders = r.folders || []; mergeGroups = r.draft || [];
      submittedMergeGroups = r.plan || [];
      renderMergePlan();
      const taskBox = $("merge-task-summary"); taskBox.innerHTML = "";
      if (submittedMergeGroups.length) {
        const title = document.createElement("div"); title.className = "merge-task-title";
        title.textContent = `已提交任务：${submittedMergeGroups.length} 组`;
        taskBox.appendChild(title);
        submittedMergeGroups.forEach((g, i) => {
          const row = document.createElement("div"); row.className = "merge-task-row";
          const values = [String(i + 1) + ".", (g.source_names || []).join(" + "), "→",
                          g.target_name || "", "→", (g.final_name || "") +
                          (g.delete_sources === false ? "（保留空来源夹）" : "（删除空来源夹）")];
          values.forEach(v => { const s = document.createElement("span"); s.textContent = v; row.appendChild(s); });
          taskBox.appendChild(row);
        });
      } else {
        taskBox.textContent = "尚未提交合并任务。上方表格为草稿，修改会自动保存并纳入项目导入/导出数据。";
      }
      const run = r.run || {};
      $("merge-status").textContent = run.running
        ? `执行中 ${run.done || 0}/${run.total || 0}`
        : (run.status ? `上次状态：${run.status}` : "");
      const ai = r.ai_run || {};
      $("merge-ai-status").textContent = ai.running ? "AI 分析中…" : (ai.error ? `失败：${ai.error}` : "");
    } catch (e) { log("读取收藏夹整理方案失败: " + e.message, "err"); }
  }
  $("merge-add").addEventListener("click", () => {
    const target = mergeFolders.find(f => f.title !== "默认收藏夹") || mergeFolders[0];
    if (!target) { log("请先刷新收藏夹目录", "warn"); return; }
    mergeGroups.push({ target_id: String(target.media_id), target_name: target.title, source_ids: [],
      source_names: [], final_name: target.title, delete_sources: true, status: "pending",
      reason: "", confidence: 0 });
    renderMergePlan(); saveMergeDraftSoon();
  });
  $("merge-ai").addEventListener("click", async () => {
    if (mergeGroups.length && !confirm("AI 建议会替换当前合并组表，继续？")) return;
    try {
      await api("POST", "/api/folder-organize/suggest");
      $("merge-ai-status").textContent = "AI 分析中…";
      log("已开始 AI 收藏夹合并分析");
    } catch (e) { log("启动 AI 合并分析失败: " + (e.error || e.message), "err"); }
  });
  $("merge-submit").addEventListener("click", async () => {
    try {
      await api("PUT", "/api/folder-organize/draft", { groups: mergeGroups });
      const r = await api("PUT", "/api/folder-organize/plan", { groups: mergeGroups });
      submittedMergeGroups = r.groups; await loadFolderMerge();
      log(`已提交 ${submittedMergeGroups.length} 组合并任务`, "ok");
    } catch (e) { log("提交合并任务失败: " + (e.error || e.message), "err"); }
  });
  $("merge-start").addEventListener("click", async () => {
    if (!submittedMergeGroups.length) { log("请先提交合并任务", "warn"); return; }
    if (!confirm(`确定执行已提交的 ${submittedMergeGroups.length} 组合并任务？`)) return;
    try {
      const r = await api("POST", "/api/folder-organize/start");
      $("merge-status").textContent = `执行中 0/${r.total}`; log("收藏夹合并已启动", "warn");
    } catch (e) { log("启动合并失败: " + (e.error || e.message), "err"); }
  });
  $("merge-stop").addEventListener("click", async () => {
    try { await api("POST", "/api/folder-organize/stop"); log("正在停止收藏夹合并…", "warn"); }
    catch (e) { log("停止失败: " + e.message, "err"); }
  });

  const sourceModal = $("source-modal");
  function openSourceEditor(index) {
    editingSourceIndex = index; const g = mergeGroups[index]; const list = $("source-modal-list"); list.innerHTML = "";
    mergeFolders.forEach(f => {
      const id = String(f.media_id); const label = document.createElement("label");
      const cb = document.createElement("input"); cb.type = "checkbox"; cb.value = id;
      cb.checked = (g.source_ids || []).map(String).includes(id); cb.disabled = id === String(g.target_id);
      label.append(cb, document.createTextNode(` ${f.title} (${f.count || 0})`)); list.appendChild(label);
    }); sourceModal.style.display = "flex";
  }
  $("source-close").addEventListener("click", () => { sourceModal.style.display = "none"; });
  $("source-none").addEventListener("click", () => document.querySelectorAll("#source-modal-list input:not(:disabled)").forEach(x => x.checked = false));
  $("source-confirm").addEventListener("click", () => {
    if (editingSourceIndex < 0) return;
    const g = mergeGroups[editingSourceIndex];
    g.source_ids = [...document.querySelectorAll("#source-modal-list input:checked")].map(x => x.value).filter(x => x !== String(g.target_id));
    clearMergeProfileEvidence(g);
    normalizeMergeNames(g); sourceModal.style.display = "none"; renderMergePlan(); saveMergeDraftSoon();
  });

  // ---------- 步骤2 分析 ----------
  $("analyze-start").addEventListener("click", async () => {
    try {
      const rebuild = $("analyze-rebuild").value || "incremental";
      const r = await api("POST", "/api/analyze/start", {
        continuous: $("analyze-continuous").checked,
        rebuild,
      });
      setAnalyzeProgress(0, 0, r.total || 0);
      log(`分析已启动：候选 ${r.total || 0} 条` +
          (typeof r.pending === "number" ? `，待发送 ${r.pending} 条` : ""));
    } catch (e) {
      log("启动分析失败: " + (e.error || e.message), "err");
    }
  });

  $("analyze-stop").addEventListener("click", async () => {
    await api("POST", "/api/analyze/stop");
    log("正在停止分析...", "warn");
  });
  $("analyze-continuous").addEventListener("change", async () => {
    await api("POST", "/api/analyze/continuous", { enabled: $("analyze-continuous").checked });
  });

  // ---------- 步骤3 方案 ----------
  let planRows = [];       // 展示行数据
  let foldersMap = {};     // title -> analysis

  let planFolders = [];
  let inboxNames = new Set();   // 未分拣收件箱（默认收藏夹）的名称，永远不是移入目标
  let planPage = 1;          // 当前页
  let planTab = "pending";   // pending | done
  let planFilter = { cur: "", target: "", action: "" };
  let planSel = new Set();   // 勾选的 bvid
  const PLAN_PAGE_SIZE = 100;

  async function loadPlan() {
    try {
      const p = await api("GET", "/api/plan");
      planFolders = (p.existing_folders || []).map(f => f.title);
      inboxNames = new Set((p.inbox_folder_names || []).map(String));
      const idToTitle = {};
      (p.existing_folders || []).forEach(f => { idToTitle[String(f.media_id)] = f.title; });
      const currentProfileIds = new Set((p.profile_current_ids || []).map(String));
      const staleProfileIds = new Set((p.profile_stale_ids || []).map(String));
      const currentProfileRevisions = p.profile_current_revisions || {};
      const videos = p.videos_by_bvid || {};
      const planMap = p.plan || {};
      const withMeta = (base, bvid) => {
        const v = videos[bvid] || {};
        const pl = planMap[bvid] || {};
        const planIsClassification = ["move_to_existing", "create_new"].includes(pl.action);
        const stalePlan = planIsClassification && pl.profile_context_current === false;
        const usePlan = !!planMap[bvid] && !stalePlan;
        return Object.assign({}, base, {
          _title: v.title || base.bvid || bvid,
          _upper: v.upper_name || "",
          _cur: idToTitle[String(v.source_folder_id || "")] || "",
          _profileCheckText: (() => {
            if (base.organization_profile_current === false) return "画像版本已变化，需重新分析";
            if (stalePlan && base._fromPlan) return "已提交方案的画像版本已过期，需重新分析";
            if (stalePlan) return "旧方案画像已过期，当前建议来自新分析";
            const memberships = new Set((v.folder_ids || []).map(String));
            if (v.source_folder_id) memberships.add(String(v.source_folder_id));
            const checked = new Set((base.profile_checked_folder_ids || []).map(String));
            const checkedVersions = base.profile_checked_versions || {};
            const mismatched = new Set((base.profile_mismatch_folder_ids || []).map(String));
            const freshMemberships = [...memberships].filter(id => currentProfileIds.has(id));
            const staleMemberships = [...memberships].filter(id => staleProfileIds.has(id));
            const freshChecked = freshMemberships.filter(id => checked.has(id) &&
              String(checkedVersions[id] || "") === String(currentProfileRevisions[id] || ""));
            const freshMismatched = freshChecked.filter(id => mismatched.has(id));
            if (freshMismatched.length) return "⚠️画像偏离：" + freshMismatched.map(id => idToTitle[id] || id).join("、");
            if (freshChecked.length) return "画像核对：未见明显偏离";
            if (staleMemberships.length) return "画像已过期，需重建：" + staleMemberships.map(id => idToTitle[id] || id).join("、");
            if (freshMemberships.length) return "画像待核对";
            return "";
          })(),
          _profileMismatchReason: (() => {
            const checked = new Set((base.profile_checked_folder_ids || []).map(String));
            const checkedVersions = base.profile_checked_versions || {};
            return (base.profile_mismatch_folder_ids || []).some(id =>
              currentProfileIds.has(String(id)) && checked.has(String(id)) &&
              String(checkedVersions[String(id)] || "") === String(currentProfileRevisions[String(id)] || ""))
              ? (base.profile_mismatch_reason || "") : "";
          })(),
          _status: ["done", "unknown"].includes(pl.status) ? pl.status
            : ((usePlan || base._fromPlan) ? (pl.status || "pending") : "pending"),
          _result: pl.result || "",
          _at: pl.at || "",
          // E：带上"已提交方案"的动作与目标，否则会显示回 LLM 建议
          _hasPlan: usePlan,
          _planAction: usePlan ? (pl.action || "") : "",
          _planTarget: usePlan ? (pl.target_folder || "") : "",
          _planNew: usePlan ? (pl.create_new_name || "") : "",
          _profileAnalysisStale: base.organization_profile_current === false,
          _profilePlanStale: stalePlan,
          organization_profile_versions: base.organization_profile_versions ||
            pl.organization_profile_versions || {},
        });
      };
      planRows = (p.analysis || []).map(a => withMeta(a, a.bvid));
      // 只在 plan 里、没有分析记录的条目（如批量标记删除的失效视频）也要能管理
      const seen = new Set(planRows.map(r => r.bvid));
      Object.values(planMap).forEach(pl => {
        if (!seen.has(pl.bvid)) {
          planRows.push(withMeta(Object.assign({ _fromPlan: true }, pl), pl.bvid));
        }
      });
      renderSummary();
      $("plan-detail").disabled = planRows.length === 0;
      const invCount = p.invalid_count || 0;
      $("invalid-info").textContent = `已失效视频：${invCount} 条`;
      $("mark-invalid").disabled = (invCount === 0);
      // 确认执行卡片概览（已提交方案 / 待操作 / 已完成）
      const nP = planRows.filter(r => r._status !== "done").length;
      const nD = planRows.filter(r => r._status === "done").length;
      const planned = Object.keys(planMap).length;
      $("apply-meta").textContent = planned
        ? `已提交方案 ${planned} 条（待操作 ${nP} · 已完成 ${nD}）`
        : "尚未提交方案";
      log(`已加载 ${planRows.length} 条待处理记录（另有 ${invCount} 条已失效视频）` +
          (p.stale_analysis_count ? `；${p.stale_analysis_count} 条分析基于旧画像，需重新分析` : ""));
    } catch (e) { log("加载方案失败: " + e.message, "err"); }
  }
  $("plan-load").addEventListener("click", loadPlan);

  // 一键标记失效视频为删除（防火墙：仅标题恰为「已失效视频」的条目）
  $("mark-invalid").addEventListener("click", async () => {
    if (!confirm("把所有标题为「已失效视频」的视频标记为删除？\n\n" +
                 "标记后需到「确认执行」点「开始执行」才会真正删除。")) return;
    try {
      const r = await api("POST", "/api/plan/mark_invalid");
      log(`已标记 ${r.count} 条失效视频为删除（到「确认执行」点「开始执行」生效）`, "warn");
    } catch (e) { log("标记失败: " + (e.error || e.message), "err"); }
  });

  // 统计摘要（分类卡片默认展示）
  function renderSummary() {
    const wrap = $("plan-summary");
    wrap.innerHTML = "";
    if (!planRows.length) {
      wrap.innerHTML = '<div class="muted">暂无分析结果，请先点「LLM 归类分析」中的「开始分析」。</div>';
      $("plan-meta").textContent = "";
      return;
    }
    // 只统计待操作（未完成）条目，避免与「已完成」混在一起造成误会
    const pendingRows = planRows.filter(r => r._status !== "done");
    let nKeep = 0, nExisting = 0, nNew = 0, nSkip = 0;
    const byTarget = new Map();
    pendingRows.forEach(r => {
      const rec = (r.recommended || "").trim();
      const isExisting = planFolders.includes(rec);
      let mode;
      if (!rec || r.action === "skip") mode = "skip";
      else if (r._cur && rec === r._cur) mode = "keep";      // 已在目标夹 → 无需移动
      else if (inboxNames.has(rec)) mode = "skip";          // 默认夹不可作为移入目标
      else if (isExisting) mode = "existing";
      else mode = "new";
      if (mode === "keep") nKeep++;
      else if (mode === "existing") nExisting++;
      else if (mode === "new") nNew++;
      else nSkip++;
      // Top 目标只统计会产生实际动作的去向；留在原夹和跳过不属于整理目标。
      if (mode === "existing" || mode === "new") {
        const key = mode === "new" ? "（新建）" + rec : rec;
        byTarget.set(key, (byTarget.get(key) || 0) + 1);
      }
    });
    $("plan-meta").textContent =
      `待操作 ${pendingRows.length} 条 · ${planFolders.length} 个现有收藏夹`;

    const stat = document.createElement("div");
    stat.className = "plan-stat";
    stat.innerHTML =
      `<span class="muted">待操作：</span>` +
      `<span class="s-keep">无需移动 <b>${nKeep}</b></span>` +
      `<span>移入现有收藏夹 <b>${nExisting}</b></span>` +
      `<span class="s-new">建议新建 <b>${nNew}</b></span>` +
      `<span class="s-skip">跳过 <b>${nSkip}</b></span>`;
    wrap.appendChild(stat);

    const top = [...byTarget.entries()].sort((a, b) => b[1] - a[1]).slice(0, 15);
    const tops = document.createElement("div");
    tops.className = "plan-top";
    tops.appendChild(document.createTextNode("Top 目标（待操作中实际移动/新建）："));
    if (!top.length) {
      const empty = document.createElement("span");
      empty.className = "muted";
      empty.textContent = "暂无实际移动或新建目标";
      tops.appendChild(empty);
    }
    top.forEach(([k, n]) => {
      const s = document.createElement("span");
      s.className = "tag" + (k.startsWith("（新建）") ? " new" : "");
      s.textContent = `${k} ${n}`;
      tops.appendChild(s);
    });
    wrap.appendChild(tops);
  }

  // 方案详情弹窗（预归类方案 与 确认执行 共用同一个）
  const planModal = $("plan-modal");
  async function openPlanModal() {
    planModal.style.display = "flex";
    await loadPlan();
    renderPlan(planFolders);
  }
  $("plan-detail").addEventListener("click", openPlanModal);
  $("plan-view-apply").addEventListener("click", openPlanModal);
  $("plan-close").addEventListener("click", () => { planModal.style.display = "none"; });
  planModal.addEventListener("click", (e) => {
    if (e.target === planModal) planModal.style.display = "none";
  });

  // ---------- 方案详情：分组渲染 ----------
  async function moveBack(bvids) {
    if (!bvids || !bvids.length) return;
    if (!confirm(`把 ${bvids.length} 条移回「待操作」？它们会重新进入执行清单。`)) return;
    try {
      await api("POST", "/api/plan/set_status", { bvids, status: "pending" });
      log(`已把 ${bvids.length} 条移回待操作`, "warn");
      await loadPlan();
      renderPlan(planFolders);
    } catch (e) { log("移回失败: " + (e.error || e.message), "err"); }
  }

  // 条目的"默认目标"（渲染与提交共用 → 未渲染的条目行为也一致）
  function defaultTarget(row, folders) {
    folders = folders || planFolders;
    if (row._target) return row._target;
    // E：已提交的方案优先，否则会把用户改过的目标显示回 LLM 建议
    if (row._hasPlan) {
      const a = row._planAction;
      if (a === "delete_invalid") return { mode: "delete" };
      if (a === "create_new") return { mode: "new", name: row._planNew || "" };
      if (a === "move_to_existing") return { mode: "existing", name: row._planTarget || "" };
      return { mode: "skip" };
    }
    const rec = (row.recommended || "").trim();
    // 失效视频：默认不动（不自动移动也不自动删除），删除需显式选择
    if ((row._title || "").trim() === "已失效视频") return { mode: "skip" };
    if (!rec || row.action === "skip") return { mode: "skip" };
    if (inboxNames.has(rec) && !inboxNames.has(row._cur)) return { mode: "skip" };
    if (folders.includes(rec)) return { mode: "existing", name: rec };
    return { mode: "new", name: rec };
  }

  // 排序优先级：删除(0) > 新建(1) > 移动(2) > 不移动(3) > 跳过(4)
  function rowRank(row) {
    const dt = defaultTarget(row);
    if (dt.mode === "delete") return 0;
    if (dt.mode === "new") return 1;
    if (dt.mode === "existing") return (row._cur && dt.name === row._cur) ? 3 : 2;
    return 4;
  }

  function sortPending(rows) {
    return rows.slice().sort((a, b) => rowRank(a) - rowRank(b));
  }

  // ---- 标签页 / 筛选 辅助 ----
  const ACTION_NAMES = ["删除", "新建", "移动", "不移动", "跳过"];

  function targetLabel(row) {
    const dt = defaultTarget(row);
    if (dt.mode === "existing") return dt.name;
    if (dt.mode === "new") return "（新建）" + (dt.name || "");
    if (dt.mode === "delete") return "（删除）";
    return "（跳过）";
  }

  function actionLabel(row) { return ACTION_NAMES[rowRank(row)]; }

  function baseRows() {
    return planRows.filter(r =>
      planTab === "done" ? r._status === "done" : r._status !== "done");
  }

  function visibleRows() {
    return rowsMatchingFilters(baseRows());
  }

  // 每个下拉选项按其他筛选条件计算计数；改变“操作”时，当前夹和目标夹的数量会同步刷新。
  function rowsMatchingFilters(rows, exceptKey = "") {
    return rows.filter(r => {
      if (exceptKey !== "cur" && planFilter.cur && (r._cur || "未知") !== planFilter.cur) return false;
      if (exceptKey !== "target" && planFilter.target && targetLabel(r) !== planFilter.target) return false;
      if (exceptKey !== "action" && planFilter.action && actionLabel(r) !== planFilter.action) return false;
      return true;
    });
  }

  function countedOptions(rows, key, labels) {
    const counts = new Map(labels.map(label => [label, 0]));
    rowsMatchingFilters(rows, key).forEach(row => {
      const label = key === "cur" ? (row._cur || "未知")
        : key === "target" ? targetLabel(row) : actionLabel(row);
      counts.set(label, (counts.get(label) || 0) + 1);
    });
    return labels.map(value => ({ value, count: counts.get(value) || 0 }))
      .sort((a, b) => (a.count === 0) - (b.count === 0) || b.count - a.count || a.value.localeCompare(b.value, "zh-CN"));
  }

  function sortRows(rows) {
    if (planTab === "done") {
      return rows.slice().sort((a, b) => (b._at || "").localeCompare(a._at || ""));
    }
    return sortPending(rows);
  }

  // 收藏夹下拉选项：构建一次、克隆复用（避免几千行 × 90 选项的 DOM 爆炸）
  function buildOptionFragment(folders) {
    const frag = document.createDocumentFragment();
    const mk = (v, t) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = t;
      frag.appendChild(o);
    };
    mk("__skip__", "（跳过）");
    folders.forEach(f => mk(f, f));
    mk("__new__", "➕ 新建...");
    mk("__delete__", "🗑 删除（已失效）");
    return frag;
  }

  function buildEditableRow(wrap, row, folders, optFrag) {
    const item = document.createElement("div");
    item.className = "plan-item";

    // 勾选框（批量操作）
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.title = "勾选后可批量操作";
    cb.checked = planSel.has(row.bvid);
    cb.addEventListener("change", () => {
      if (cb.checked) planSel.add(row.bvid); else planSel.delete(row.bvid);
      const n = $("plan-sel-count");
      if (n) n.textContent = `已选 ${planSel.size} 条`;
    });
    item.appendChild(cb);

    const t = document.createElement("div");
    t.className = "t";
    t.innerHTML = `<div class="title"></div><div class="meta"></div><div class="reason"></div>`;
    t.querySelector(".title").textContent = row._title;
    const stTxt = row._status === "failed" ? "❌ 上次失败："
                : (row._status === "partial" ? "⚠️ 上次半完成："
                   : (row._status === "unknown" ? "❓ 结果待人工复核：" : ""));
    t.querySelector(".meta").textContent =
      `当前：${row._cur || "未知"}` +
      (row._upper ? " · UP: " + row._upper : "") +
      (row._result ? ` · ${stTxt}${row._result}` : "") +
      (row.reason ? " · " + row.reason : "") +
      (row._profileCheckText ? " · " + row._profileCheckText : "");
    if (row.reason || row._profileMismatchReason) {
      t.querySelector(".reason").textContent =
        (row.reason ? "建议: " + row.reason : "") +
        (row._profileMismatchReason ? (row.reason ? " · " : "") + "画像依据: " + row._profileMismatchReason : "");
    }

    const rec = row.recommended || "";
    const isExisting = folders.includes(rec);
    const isInvalid = (row._title || "").trim() === "已失效视频";

    const sel = document.createElement("select");
    sel.appendChild(optFrag.cloneNode(true));
    // 防火墙：只有标题恰为「已失效视频」的行才保留"删除"选项
    if (!isInvalid) {
      const del = sel.querySelector('option[value="__delete__"]');
      if (del) del.remove();
    }

    // 默认目标（与提交共用同一函数，未渲染的条目行为一致）
    const dt = defaultTarget(row, folders);
    row._target = dt;
    sel.value = dt.mode === "delete" ? "__delete__"
      : dt.mode === "existing" ? (folders.includes(dt.name) ? dt.name : "__skip__")
        : dt.mode === "new" ? "__new__" : "__skip__";
    const defaultTargetOption = sel.querySelector('option[value="默认收藏夹"]');
    if (defaultTargetOption) defaultTargetOption.disabled = true;

    sel.addEventListener("change", () => {
      const v = sel.value;
      if (v === "__delete__") {
        row._target = { mode: "delete" }; showBadge(item, "del", "删除");
      } else if (v === "__new__") {
        const name = prompt("输入新收藏夹名称：", rec || row.create_new_name || "");
        if (name && name.trim()) {
          if (name.trim() === "默认收藏夹") {
            log("默认收藏夹只能移出，不能作为新建或移入目标", "warn");
            sel.value = "__skip__";
            row._target = { mode: "skip" };
            showBadge(item, "skip", "跳过");
          } else {
            row._target = { mode: "new", name: name.trim() };
            showBadge(item, "new", name.trim());
          }
        } else { sel.value = "__skip__"; row._target = { mode: "skip" }; showBadge(item, "skip", "跳过"); }
      } else if (v === "__skip__") {
        row._target = { mode: "skip" }; showBadge(item, "skip", "跳过");
      } else {
        row._target = { mode: "existing", name: v };
        if (row._cur && v === row._cur) showBadge(item, "keep", "无需移动");
        else showBadge(item, "existing", v);
      }
      planPage = 1;
      renderPlan(planFolders);
    });

    const conf = document.createElement("span");
    conf.className = "conf";
    conf.textContent = row.confidence ? row.confidence.toFixed(2) : "";

    item.appendChild(t);
    item.appendChild(sel);
    item.appendChild(conf);
    wrap.appendChild(item);

    // 初始化徽标
    if (dt.mode === "delete") showBadge(item, "del", "删除");
    else if (dt.mode === "new") showBadge(item, "new", dt.name || "新建");
    else if (dt.mode === "existing") {
      if (row._cur && dt.name === row._cur) showBadge(item, "keep", "无需移动");
      else showBadge(item, "existing", dt.name);
    } else showBadge(item, "skip", "跳过");
  }

  function buildDoneRow(wrap, row) {
    const item = document.createElement("div");
    item.className = "plan-item is-done";
    const t = document.createElement("div");
    t.className = "t";
    t.innerHTML = `<div class="title"></div><div class="meta"></div>`;
    t.querySelector(".title").textContent = "✅ " + row._title;
    t.querySelector(".meta").textContent =
      `当前：${row._cur || "未知"} · ${row._result || "已完成"}` +
      (row._at ? ` · ${row._at}` : "");
    const b = document.createElement("button");
    b.className = "btn mini-btn";
    b.textContent = "↩ 移回待操作";
    b.addEventListener("click", () => moveBack([row.bvid]));
    item.appendChild(t);
    item.appendChild(b);
    wrap.appendChild(item);
  }

  function buildPager(total, page, pages, onGo) {
    const bar = document.createElement("div");
    bar.className = "plan-pager";
    const mk = (label, target, disabled) => {
      const b = document.createElement("button");
      b.className = "mini"; b.textContent = label; b.disabled = !!disabled;
      b.addEventListener("click", () => onGo(target));
      return b;
    };
    bar.appendChild(mk("« 首页", 1, page <= 1));
    bar.appendChild(mk("‹ 上一页", page - 1, page <= 1));

    const inp = document.createElement("input");
    inp.type = "number"; inp.min = "1"; inp.max = String(pages);
    inp.value = String(page); inp.className = "num-input page-input";
    const jump = () => {
      let v = parseInt(inp.value, 10);
      if (!v || v < 1) v = 1;
      if (v > pages) v = pages;
      onGo(v);
    };
    inp.addEventListener("change", jump);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") jump(); });

    const info = document.createElement("span");
    info.textContent = `/ ${pages} 页 · 共 ${total} 条 · 每页 ${PLAN_PAGE_SIZE}`;
    bar.appendChild(inp); bar.appendChild(info);
    bar.appendChild(mk("下一页 ›", page + 1, page >= pages));
    bar.appendChild(mk("末页 »", pages, page >= pages));
    return bar;
  }

  // ---- 标签页 ----
  function buildTabs(nPending, nDone) {
    const bar = document.createElement("div");
    bar.className = "plan-tabs";
    [["pending", `待操作 ${nPending}`], ["done", `已完成 ${nDone}`]].forEach(([k, label]) => {
      const b = document.createElement("button");
      b.className = "tab" + (planTab === k ? " active" : "");
      b.textContent = label;
      b.addEventListener("click", () => {
        planTab = k; planPage = 1;           // 切标签回到第 1 页（筛选保留）
        renderPlan(planFolders);
      });
      bar.appendChild(b);
    });
    return bar;
  }

  // ---- 筛选栏 ----
  function buildFilters() {
    const bar = document.createElement("div");
    bar.className = "plan-filters";
    const base = baseRows();
    const curs = [...new Set(base.map(r => r._cur || "未知"))];
    const targets = [...new Set(base.map(r => targetLabel(r)))];
    const acts = [...ACTION_NAMES];
    if (planFilter.cur && !curs.includes(planFilter.cur)) curs.push(planFilter.cur);
    if (planFilter.target && !targets.includes(planFilter.target)) targets.push(planFilter.target);
    const mkSel = (label, key, options) => {
      const w = document.createElement("label");
      w.className = "inline-label";
      w.appendChild(document.createTextNode(label));
      const s = document.createElement("select");
      const o0 = document.createElement("option");
      o0.value = ""; o0.textContent = `全部（${rowsMatchingFilters(base, key).length}）`;
      s.appendChild(o0);
      countedOptions(base, key, options).forEach(({ value, count }) => {
        const o = document.createElement("option");
        o.value = value; o.textContent = `${value}（${count}）`;
        s.appendChild(o);
      });
      s.value = planFilter[key] || "";
      s.addEventListener("change", () => {
        planFilter[key] = s.value; planPage = 1; renderPlan(planFolders);
      });
      w.appendChild(s);
      return w;
    };
    bar.appendChild(mkSel("当前所在夹 ", "cur", curs));
    bar.appendChild(mkSel("目标夹 ", "target", targets));
    bar.appendChild(mkSel("操作 ", "action", acts));
    const rb = document.createElement("button");
    rb.className = "mini"; rb.textContent = "重置筛选";
    rb.addEventListener("click", () => {
      planFilter = { cur: "", target: "", action: "" }; planPage = 1;
      renderPlan(planFolders);
    });
    bar.appendChild(rb);
    return bar;
  }

  // ---- 批量操作（待操作标签页） ----
  function batchSet(fn, label) {
    if (!planSel.size) { log("请先勾选条目", "warn"); return; }
    let n = 0;
    planRows.forEach(r => { if (planSel.has(r.bvid)) { r._target = fn(r); n++; } });
    log(`已把 ${n} 条改为「${label}」（点「确认方案」后写入并执行）`, "warn");
    renderPlan(planFolders);
  }

  function buildBatchBar(rows) {
    const bar = document.createElement("div");
    bar.className = "plan-filters";
    const info = document.createElement("span");
    info.className = "muted"; info.id = "plan-sel-count";
    info.textContent = `已选 ${planSel.size} 条`;

    const all = document.createElement("button");
    all.className = "mini"; all.textContent = `全选(${rows.length})`;
    all.addEventListener("click", () => { rows.forEach(r => planSel.add(r.bvid)); renderPlan(planFolders); });
    const clr = document.createElement("button");
    clr.className = "mini"; clr.textContent = "清空";
    clr.addEventListener("click", () => { planSel.clear(); renderPlan(planFolders); });

    const fsel = document.createElement("select");
    const o0 = document.createElement("option");
    o0.value = ""; o0.textContent = "目标收藏夹...";
    fsel.appendChild(o0);
    planFolders.filter(f => !inboxNames.has(f)).forEach(f => {
      const o = document.createElement("option"); o.value = f; o.textContent = f;
      fsel.appendChild(o);
    });
    const setBtn = document.createElement("button");
    setBtn.className = "mini"; setBtn.textContent = "设为该夹";
    setBtn.addEventListener("click", () => {
      if (!fsel.value) { log("请先选择目标收藏夹", "warn"); return; }
      batchSet(() => ({ mode: "existing", name: fsel.value }), fsel.value);
    });

    const skip = document.createElement("button");
    skip.className = "mini"; skip.textContent = "跳过";
    skip.addEventListener("click", () => batchSet(() => ({ mode: "skip" }), "跳过"));

    const del = document.createElement("button");
    del.className = "mini"; del.textContent = "删除失效";
    del.addEventListener("click", () => {
      const sel = planRows.filter(r => planSel.has(r.bvid));
      if (!sel.length) { log("请先勾选条目", "warn"); return; }
      // 防火墙：只要有一条不是「已失效视频」，整体拒绝
      const bad = sel.filter(r => (r._title || "").trim() !== "已失效视频");
      if (bad.length) {
        log(`拒绝批量删除：选中的 ${sel.length} 条里有 ${bad.length} 条不是「已失效视频」`, "err");
        return;
      }
      batchSet(() => ({ mode: "delete" }), "删除（已失效）");
    });

    const rm = document.createElement("button");
    rm.className = "mini"; rm.textContent = "移除";
    rm.addEventListener("click", async () => {
      const ids = [...planSel];
      if (!ids.length) { log("请先勾选条目", "warn"); return; }
      if (!confirm(`把勾选的 ${ids.length} 条从方案中彻底移除？（只删本地方案，不动 B站）`)) return;
      try {
        const r = await api("POST", "/api/plan/remove", { bvids: ids });
        log(`已从方案中移除 ${r.count} 条`, "warn");
        planSel.clear();
        await loadPlan();
        renderPlan(planFolders);
      } catch (e) { log("移除失败: " + (e.error || e.message), "err"); }
    });

    [info, all, clr, fsel, setBtn, skip, del, rm].forEach(el => bar.appendChild(el));
    return bar;
  }

  // ---- 批量操作（已完成标签页） ----
  function buildDoneBar(rows) {
    const bar = document.createElement("div");
    bar.className = "plan-filters";
    const info = document.createElement("span");
    info.className = "muted";
    info.textContent = `共 ${rows.length} 条已完成（只读）`;
    const back = document.createElement("button");
    back.className = "mini";
    back.textContent = `移回待操作(${rows.length})`;
    back.disabled = rows.length === 0;
    back.addEventListener("click", () => moveBack(rows.map(r => r.bvid)));
    bar.appendChild(info); bar.appendChild(back);
    return bar;
  }

  function renderPlan(folders) {
    const wrap = $("plan-list");
    const toolbar = $("plan-toolbar");
    wrap.innerHTML = "";
    toolbar.innerHTML = "";
    const nPending = planRows.filter(r => r._status !== "done").length;
    const nDone = planRows.filter(r => r._status === "done").length;
    const rows = sortRows(visibleRows());
    const optFrag = buildOptionFragment(folders);

    // 固定工具栏（在滚动区之外）：标签页 + 筛选 + 分页 + 批量
    toolbar.appendChild(buildTabs(nPending, nDone));
    toolbar.appendChild(buildFilters());
    const pages = Math.max(1, Math.ceil(rows.length / PLAN_PAGE_SIZE));
    if (planPage > pages) planPage = pages;
    if (planPage < 1) planPage = 1;
    toolbar.appendChild(buildPager(rows.length, planPage, pages,
                                   p => { planPage = p; renderPlan(planFolders); }));
    toolbar.appendChild(planTab === "pending" ? buildBatchBar(rows) : buildDoneBar(rows));

    const start = (planPage - 1) * PLAN_PAGE_SIZE;
    const pageRows = rows.slice(start, start + PLAN_PAGE_SIZE);
    if (!pageRows.length) {
      const empty = document.createElement("div");
      empty.className = "muted";
      empty.style.padding = "16px";
      empty.textContent = planTab === "pending" ? "（当前筛选下没有待操作条目）" : "（当前筛选下没有已完成条目）";
      wrap.appendChild(empty);
    } else if (planTab === "pending") {
      pageRows.forEach(r => buildEditableRow(wrap, r, folders, optFrag));
    } else {
      pageRows.forEach(r => buildDoneRow(wrap, r));
    }

    $("plan-modal-meta").textContent =
      `共 ${planRows.length} 条 · 待操作 ${nPending} · 已完成 ${nDone} · ` +
      `当前显示 ${rows.length} 条 · ${folders.length} 个现有收藏夹`;
    $("plan-meta").textContent =
      `待操作 ${nPending} · 已完成 ${nDone} · 共 ${planRows.length} 条`;
  }

  function showBadge(item, mode, text) {
    let badge = item.querySelector(".badge");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "badge";
      item.appendChild(badge);
    }
    badge.className = "badge " + mode;
    badge.textContent = text;
  }

  async function confirmPlan() {
    // 已完成的不提交（保持原状，避免被重置为待办）；按 删除>新建>移动>不移动>跳过 排序提交
    const pend = sortPending(planRows.filter(r => r._status !== "done"));
    const staleRows = pend.filter(r =>
      ((r._profileAnalysisStale && !r._hasPlan) || (r._profilePlanStale && r._fromPlan)) &&
      (["move_to_existing", "create_new"].includes(r.action) ||
       ["move_to_existing", "create_new"].includes(r._planAction)));
    if (staleRows.length) {
      log(`${staleRows.length} 条归类结果对应的画像已变化，请先重新分析再确认方案`, "warn");
      return;
    }
    const applyList = pend.map(r => {
      // 未渲染（超出显示上限）的条目也用同一套默认目标，避免被静默跳过
      const tg = defaultTarget(r);
      // 目标就是当前所在夹 → 不移动（跳过）
      const same = (tg.mode === "existing" && r._cur && tg.name === r._cur);
      return {
        bvid: r.bvid,
        action: tg.mode === "delete" ? "delete_invalid"
          : (same ? "skip"
             : (tg.mode === "new" ? "create_new"
                : (tg.mode === "existing" ? "move_to_existing" : "skip"))),
        target_folder: tg.name || "",
        create_new_name: tg.name || "",
        organization_profile_versions: r.organization_profile_versions || {},
      };
    });
    try {
      const r = await api("POST", "/api/plan/apply", { apply_list: applyList });
      log(`已确认方案：本次写入 ${r.planned} 条待操作` +
          (r.kept_done ? `，另有 ${r.kept_done} 条已完成状态保留` : ""));
      $("plan-modal").style.display = "none";
    } catch (e) { log("确认方案失败: " + (e.error || e.message), "err"); }
  }
  $("plan-save").addEventListener("click", confirmPlan);

  // ---------- 步骤4 执行 ----------
  $("apply-start").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/apply/start");
      log(`批量执行已启动，共 ${r.total || 0} 条，单批最多 ${r.batch_size || 1000} 条` +
          (r.held_unknown ? `；另有 ${r.held_unknown} 条待人工复核` : ""));
    } catch (e) { log("启动执行失败: " + (e.error || e.message), "err"); }
  });
  $("apply-stop").addEventListener("click", async () => {
    await api("POST", "/api/apply/stop");
    log("正在停止执行...", "warn");
  });

  // ---------- 事件流（SSE：事件驱动，空闲时不产生任何更新） ----------
  let lastEvtId = 0;

  function connectEvents() {
    const es = new EventSource("/api/events/stream?since=" + lastEvtId);
    es.onmessage = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch (_) { return; }
      if (ev.id) lastEvtId = Math.max(lastEvtId, ev.id);
      // level=progress 为静默事件：只用于推进度条，不写日志
      if (ev.level !== "progress") log(ev.text, ev.level || "info");

      // 扫描：仅事件到达时更新进度
      if (ev.kind === "scan_start" || ev.kind === "scan_progress") {
        if (ev.kind === "scan_start") refreshOrganizationReadiness();
        const box = $("scan-progress");
        box.style.display = "flex";
        if (typeof ev.done === "number") {
          setProgress(box, "scan", ev.done, ev.total || 0);
          const parts = [`已抓取 ${ev.done}/${ev.total} 条`];
          if (typeof ev.unique === "number") parts.push(`去重 ${ev.unique}`);
          if (ev.ftotal) parts.push(`收藏夹 ${ev.fdone}/${ev.ftotal}`);
          if (ev.current) parts.push(`当前：${ev.current}`);
          if (ev.strategy) parts.push(ev.strategy === "indexed_ids" ? "ID 核对 + 本地索引" :
            (ev.strategy === "bulk_ids_infos" ? "批量元数据" : "分页明细"));
          $("scan-text").textContent = parts.join(" · ");
        }
        if (modal.style.display === "flex") refreshTree();
        refreshStats();
      }
      if (ev.kind === "scan_end") {
        $("scan-progress").style.display = "none";
        refreshStats();
        if (folderProfileListLoaded) loadFolderProfiles();
        refreshOrganizationReadiness();
      }
      if (ev.kind === "scan_idle") refreshScanIssues(true).catch(() => {});

      // 分析
      if (ev.phase === "analyze" && typeof ev.done === "number") {
        const b = $("analyze-progress");
        b.style.display = "flex";
        const inflight = Number(ev.inflight || 0);
        setAnalyzeProgress(ev.done, inflight, ev.total || 0);
        const parts = [`已完成 ${ev.done}/${ev.total}`];
        if (typeof ev.pending === "number") parts.push(`待发送 ${ev.pending}`);
        if (inflight) parts.push(`等待响应 ${inflight}`);
        if (ev.waiting) parts.push("等待新扫描内容");
        if (typeof ev.failed === "number" && ev.failed) parts.push(`失败 ${ev.failed}`);
        parts.push(`批 ${$("analyze-batch").value} × 并发 ${$("analyze-concurrency").value}`);
        $("analyze-text").textContent = parts.join(" · ");
        if (ev.kind !== "analyze_progress") refreshStats();   // 静默进度不触发取数
      }
      if (ev.kind === "analyze_end") {
        $("analyze-progress").style.display = "none";
        $("analyze-continuous").checked = false;
        refreshStats();
        loadPlan();
      }
      if (ev.phase === "folder_merge" || ev.phase === "folder_merge_ai") loadFolderMerge();

      if (ev.phase === "folder_profile") {
        if (typeof ev.done === "number" && typeof ev.total === "number") {
          const box = $("profile-progress");
          box.style.display = "flex";
          setProgress(box, "profile", ev.done, ev.total);
          $("profile-text").textContent = "已处理 " + ev.done + "/" + ev.total +
            (ev.current ? " · " + ev.current : "") +
            " · 生成 " + (ev.generated || 0) + " · 跳过 " + (ev.skipped || 0) + " · 失败 " + (ev.failed || 0);
        }
        if (ev.kind === "folder_profile_end") {
          $("profile-progress").style.display = "none";
          $("profile-status").textContent = "生成 " + (ev.generated || 0) +
            " · 跳过 " + (ev.skipped || 0) + " · 失败 " + (ev.failed || 0);
          if (folderProfileListLoaded) loadFolderProfiles();
          refreshOrganizationReadiness();
        }
      }

      // 执行
      if (ev.phase === "apply" && typeof ev.done === "number") {
        const b = $("apply-progress");
        b.style.display = "flex";
        setProgress(b, "apply", ev.done, ev.total || 0);
        refreshStats();
      }
      if (ev.kind === "apply_end") {
        $("apply-progress").style.display = "none";
        refreshStats();
        // 执行完成后服务端会自动刷新收藏夹目录，这里同步重载画像列表与就绪状态
        if (folderProfileListLoaded) loadFolderProfiles();
        refreshOrganizationReadiness();
      }
    };
    es.onerror = () => {
      es.close();
      setTimeout(connectEvents, 3000);   // 断线自动重连，从上次事件继续
    };
  }

  // ---------- 管理供应商 / 模型 ----------
  const manageModal = $("manage-modal");
  const refreshModal = $("refresh-modal");
  const testModal = $("test-modal");

  function setManageStatus(text) {
    const el = $("manage-status");
    if (el) el.textContent = text || "";
  }

  function renderPresetList(presets) {
    const select = $("preset-select");
    if (!select) return;
    select.replaceChildren();
    const other = document.createElement("option");
    other.value = "custom";
    other.textContent = "其他";
    select.appendChild(other);
    for (const preset of presets) {
      if (preset.key === "custom") continue;
      const option = document.createElement("option");
      option.value = preset.key;
      option.textContent = preset.name;
      option.title = preset.base_url || "需手动填写地址";
      select.appendChild(option);
    }
    select.value = "custom";
  }

  async function addSelectedProviderPreset() {
    const key = $("preset-select").value || "custom";
    const preset = providerPresetsCache.find(item => item.key === key);
    let name = preset ? preset.name : "";
    let base = preset ? (preset.base_url || "") : "";
    if (key === "custom" || !preset) {
      name = (prompt("输入供应商名称：") || "").trim();
      if (!name) return;
      base = (prompt("输入供应商 base_url：") || "").trim();
    } else if (!base) {
      base = (prompt(`输入「${name}」的 base_url：`) || "").trim();
    }
    if (!base) return;
    try {
      let selected = providersCache.find(x => x.name === name && x.base_url === base);
      if (!selected) {
        const result = await api("POST", "/api/providers", { name, base_url: base, api_key: "" });
        selected = result.provider;
        setManageStatus(`已添加预设「${name}」，请填写 API Key`);
        await loadConfig();
      } else {
        setManageStatus(`供应商「${name}」已存在，打开设置`);
      }
      manageSelectedProviderId = selected.id;
      openProviderForm(selected.id);
      renderManageLists({ resetModelScroll: true });
    } catch (e) { log("添加预设失败: " + (e.error || e.message), "err"); }
  }

  function openProviderForm(providerId) {
    editingProviderId = providerId || "";
    const form = $("provider-form");
    form.hidden = false;
    const p = providerId ? providerById(providerId) : null;
    $("pf-name").value = p ? p.name : "";
    $("pf-url").value = p ? p.base_url : "";
    $("pf-key").value = "";
    $("pf-clear-key").checked = false;
    $("pf-clear-key-wrap").hidden = !(p && p.api_key_set);
    $("pf-key").placeholder = p && p.api_key_set
      ? `已配置（${p.api_key_masked}）— 留空不修改`
      : "sk-…（仅本地保存，界面保密显示）";
    $("pf-name").focus();
  }

  function closeProviderForm() {
    editingProviderId = "";
    $("provider-form").hidden = true;
    $("pf-clear-key").checked = false;
  }

  function openModelForm(modelId) {
    editingModelId = modelId || "";
    const form = $("model-form");
    form.hidden = false;
    const m = modelId ? modelById(modelId) : null;
    $("mf-name").value = m ? m.name : "";
    $("mf-ctx").value = String(m && Number(m.context_tokens) > 0 ? m.context_tokens : "");
    $("mf-out").value = String(m && Number(m.max_output_tokens) > 0 ? m.max_output_tokens : "");
    modelFormBaseline = m ? {
      context_source: m.context_source || "legacy_unknown",
      output_source: m.output_source || "legacy_unknown",
    } : null;
    modelFormTouched = { ctx: false, out: false };
    $("mf-think").value = m ? (m.thinking_effort || "") : "";
    $("mf-activate").hidden = !m;
  }

  function closeModelForm() {
    editingModelId = "";
    $("model-form").hidden = true;
    $("mf-activate").hidden = true;
    modelFormBaseline = null;
  }

  function modelLimitLabel(value, source) {
    if (!(Number(value) > 0)) return "未知（API未提供）";
    const origin = {
      api: "API",
      manual: "手动",
      documented_cap: "API / 供应商上限",
      legacy_unknown: "来源未记录",
      legacy_config: "旧配置",
      unknown: "来源未知",
    }[source || "unknown"] || "来源未知";
    return `${Number(value).toLocaleString("zh-CN")}（${origin}）`;
  }

  function statusDot(status) {
    const map = { ok: "ok", fail: "fail", testing: "testing", untested: "untested" };
    return map[status] || "untested";
  }

  function modelIconButton(label, svgMarkup) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "mini icon-btn";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.innerHTML = svgMarkup;
    return button;
  }

  function renderManageLists(options = {}) {
    const pbox = $("provider-list");
    if (!pbox) return;
    const providerScrollTop = pbox.scrollTop;
    const mbox = $("model-list");
    const modelScrollTop = mbox.scrollTop;
    pbox.replaceChildren();
    if (!providersCache.length) {
      const empty = document.createElement("div");
      empty.className = "muted manage-empty";
      empty.textContent = "尚无供应商，可用下方预设快速添加。";
      pbox.appendChild(empty);
    }
    for (const p of providersCache) {
      const row = document.createElement("div");
      row.className = "manage-row provider-row" + (p.id === manageSelectedProviderId ? " active" : "");
      const main = document.createElement("button");
      main.type = "button";
      main.className = "manage-row-main";
      main.innerHTML = `<div class="mr-name"></div><div class="mr-sub"></div><div class="mr-key"></div>`;
      main.querySelector(".mr-name").textContent = p.name;
      main.querySelector(".mr-sub").textContent = p.base_url;
      main.querySelector(".mr-key").textContent = p.api_key_set
        ? `Key ${p.api_key_masked || "****"}` : "Key 未配置";
      main.addEventListener("click", () => {
        manageSelectedProviderId = p.id;
        closeModelForm();
        renderManageLists({ resetModelScroll: true });
      });
      const acts = document.createElement("div");
      acts.className = "row-acts";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "mini";
      edit.textContent = "编辑";
      edit.addEventListener("click", (ev) => {
        ev.stopPropagation();
        manageSelectedProviderId = p.id;
        openProviderForm(p.id);
        renderManageLists();
      });
      const del = document.createElement("button");
      del.type = "button";
      del.className = "mini danger-mini";
      del.textContent = "删除";
      del.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删除供应商「${p.name}」及其全部模型？`)) return;
        try {
          await api("DELETE", "/api/providers/" + p.id);
          if (manageSelectedProviderId === p.id) manageSelectedProviderId = "";
          closeModelForm();
          setManageStatus("已删除供应商");
          await loadConfig();
        } catch (e) { log("删除供应商失败: " + (e.error || e.message), "err"); }
      });
      acts.append(edit, del);
      row.append(main, acts);
      pbox.appendChild(row);
    }

    const hasProvider = Boolean(manageSelectedProviderId && providerById(manageSelectedProviderId));
    $("model-refresh").disabled = !hasProvider;
    $("model-test").disabled = !hasProvider;
    $("model-add-btn").disabled = !hasProvider;

    mbox.replaceChildren();
    if (!hasProvider) {
      const empty = document.createElement("div");
      empty.className = "muted manage-empty";
      empty.textContent = "选择左侧供应商后查看模型。";
      mbox.appendChild(empty);
      $("model-pane-status").textContent = "";
      pbox.scrollTop = providerScrollTop;
      mbox.scrollTop = options.resetModelScroll ? 0 : modelScrollTop;
      return;
    }
    const list = modelsOf(manageSelectedProviderId);
    $("model-pane-status").textContent = `${list.length} 个模型`;
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "muted manage-empty";
      empty.textContent = "该供应商下暂无模型；点「刷新」从远程拉取，或「新增模型」手动添加。";
      mbox.appendChild(empty);
      pbox.scrollTop = providerScrollTop;
      mbox.scrollTop = options.resetModelScroll ? 0 : modelScrollTop;
      return;
    }
    for (const m of list) {
      const row = document.createElement("div");
      row.className = "manage-row model-row" + (m.id === editingModelId ? " selected" : "");
      row.dataset.modelId = m.id;
      const main = document.createElement("button");
      main.type = "button";
      main.className = "manage-row-main model-main";
      const dot = document.createElement("span");
      dot.className = "test-dot " + statusDot(m.test_status);
      dot.title = m.test_status + (m.test_message ? "：" + m.test_message : "");
      const body = document.createElement("span");
      body.className = "model-body";
      body.innerHTML = `<div class="mr-name"></div><div class="mr-sub"></div>`;
      body.querySelector(".mr-name").textContent = m.name + (m.id === activeModelId ? "（激活）" : "");
      body.querySelector(".mr-sub").textContent =
        `上下文 ${modelLimitLabel(m.context_tokens, m.context_source)} · 输出 ${modelLimitLabel(m.max_output_tokens, m.output_source)} · 思考 ${m.thinking_effort || "默认"}`;
      main.append(dot, body);
      main.addEventListener("click", () => {
        openModelForm(m.id);
        for (const modelRow of mbox.querySelectorAll(".model-row")) {
          modelRow.classList.toggle("selected", modelRow.dataset.modelId === m.id);
        }
        setManageStatus(`正在编辑「${m.name}」；保存后如需切换请点「激活」`);
      });
      const acts = document.createElement("div");
      acts.className = "row-acts";
      const copy = modelIconButton("复制模型名称", '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="3.5" width="12" height="13" rx="1.5"></rect><path d="M16 7.5H5.5A1.5 1.5 0 0 0 4 9v10a1.5 1.5 0 0 0 1.5 1.5H15a1.5 1.5 0 0 0 1.5-1.5V8"></path></svg>');
      copy.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        try {
          await navigator.clipboard.writeText(m.name);
          setManageStatus("已复制模型名称");
        } catch (_) {
          prompt("复制模型名称：", m.name);
        }
      });
      const edit = modelIconButton("编辑模型", '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 16.5-.8 4.3 4.3-.8L19.8 7.7a2.1 2.1 0 0 0-3-3L4 16.5Z"></path><path d="m14.9 6.6 3 3"></path></svg>');
      edit.addEventListener("click", (ev) => {
        ev.stopPropagation();
        openModelForm(m.id);
        for (const modelRow of mbox.querySelectorAll(".model-row")) {
          modelRow.classList.toggle("selected", modelRow.dataset.modelId === m.id);
        }
      });
      const del = modelIconButton("删除模型", '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"></path></svg>');
      del.classList.add("danger-mini");
      del.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删除模型「${m.name}」？`)) return;
        try {
          await api("DELETE", "/api/models/" + m.id);
          if (editingModelId === m.id) closeModelForm();
          setManageStatus("已删除模型");
          await loadConfig();
        } catch (e) { log("删除模型失败: " + (e.error || e.message), "err"); }
      });
      acts.append(copy, edit, del);
      row.append(main, acts);
      mbox.appendChild(row);
    }
    pbox.scrollTop = providerScrollTop;
    if (options.scrollToActive) {
      const activeRow = [...mbox.querySelectorAll(".model-row")]
        .find(row => row.dataset.modelId === activeModelId);
      if (activeRow) {
        const rowTop = activeRow.getBoundingClientRect().top - mbox.getBoundingClientRect().top + mbox.scrollTop;
        mbox.scrollTop = Math.max(0, rowTop - (mbox.clientHeight - activeRow.offsetHeight) / 2);
      } else {
        mbox.scrollTop = options.resetModelScroll ? 0 : modelScrollTop;
      }
    } else {
      mbox.scrollTop = options.resetModelScroll ? 0 : modelScrollTop;
    }
  }

  $("manage-models-btn").addEventListener("click", async () => {
    manageModal.style.display = "flex";
    setManageStatus("");
    try { await loadConfig(); } catch (_) { /* loadConfig already logs */ }
    const activeModel = modelById(activeModelId);
    manageSelectedProviderId = activeModel ? activeModel.provider_id : (providersCache[0] || {}).id || "";
    if (activeModel) openModelForm(activeModel.id);
    else closeModelForm();
    renderManageLists({ resetModelScroll: true, scrollToActive: Boolean(activeModel) });
  });
  $("manage-close").addEventListener("click", () => { manageModal.style.display = "none"; });
  $("manage-done").addEventListener("click", () => { manageModal.style.display = "none"; });
  $("provider-add-btn").addEventListener("click", () => openProviderForm(""));
  $("preset-add-btn").addEventListener("click", addSelectedProviderPreset);
  $("pf-cancel").addEventListener("click", closeProviderForm);
  $("pf-save").addEventListener("click", async () => {
    const name = $("pf-name").value.trim();
    const base = $("pf-url").value.trim();
    const key = $("pf-key").value.trim();
    if (!name || !base) { setManageStatus("名称与 base_url 必填"); return; }
    try {
      if (editingProviderId) {
        const body = { name, base_url: base };
        if ($("pf-clear-key").checked) body.api_key = "";
        else if (key) body.api_key = key;
        await api("PUT", "/api/providers/" + editingProviderId, body);
        setManageStatus("供应商已更新");
      } else {
        const r = await api("POST", "/api/providers", { name, base_url: base, api_key: key });
        manageSelectedProviderId = r.provider.id;
        setManageStatus("供应商已创建");
      }
      closeProviderForm();
      await loadConfig();
      renderManageLists();
    } catch (e) { setManageStatus(e.error || e.message); }
  });

  $("model-add-btn").addEventListener("click", () => openModelForm(""));
  $("mf-ctx").addEventListener("input", () => { modelFormTouched.ctx = true; });
  $("mf-out").addEventListener("input", () => { modelFormTouched.out = true; });
  $("mf-cancel").addEventListener("click", closeModelForm);
  function currentModelFormValues() {
    const name = $("mf-name").value.trim();
    const ctx = parseInt($("mf-ctx").value, 10);
    const out = parseInt($("mf-out").value, 10);
    const think = $("mf-think").value;
    if (!name) throw new Error("模型名称不能为空");
    const values = { name, context_tokens: Number.isFinite(ctx) ? ctx : 0,
      max_output_tokens: Number.isFinite(out) ? out : 0, thinking_effort: think };
    if (modelFormBaseline) {
      if (!modelFormTouched.ctx) {
        values.context_source = modelFormBaseline.context_source;
      }
      if (!modelFormTouched.out) {
        values.output_source = modelFormBaseline.output_source;
      }
    }
    return values;
  }

  async function saveCurrentModelForm() {
    const values = currentModelFormValues();
    let modelId = editingModelId;
    if (modelId) {
      await api("PUT", "/api/models/" + modelId, values);
      setManageStatus("模型已更新");
    } else {
      const result = await api("POST", "/api/models", {
        provider_id: manageSelectedProviderId, ...values,
      });
      modelId = result.model && result.model.id || "";
      setManageStatus("模型已添加");
    }
    return modelId;
  }

  $("mf-save").addEventListener("click", async () => {
    try {
      await saveCurrentModelForm();
      closeModelForm();
      await loadConfig();
      renderManageLists();
    } catch (e) { setManageStatus(e.error || e.message); }
  });
  $("mf-activate").addEventListener("click", async () => {
    const modelId = editingModelId;
    if (!modelId) return;
    try {
      await saveCurrentModelForm();
      await api("POST", "/api/config", { active_model_id: modelId });
      activeModelId = modelId;
      await loadConfig();
      openModelForm(modelId);
      renderManageLists();
      setManageStatus(`已保存并激活「${modelById(modelId)?.name || "模型"}」`);
    } catch (e) { setManageStatus(e.error || e.message); }
  });

  $("model-refresh").addEventListener("click", async () => {
    if (!manageSelectedProviderId) return;
    $("refresh-meta").textContent = "正在从供应商读取模型…";
    $("refresh-diff").replaceChildren();
    $("refresh-modal").style.display = "flex";
    lastRefreshDiff = null;
    selectedRefreshModels = new Map();
    selectedRefreshPreset = "";
    $("sync-save-selected").disabled = true;
    try {
      const r = await api("POST", `/api/providers/${manageSelectedProviderId}/remote-models`);
      lastRefreshDiff = { ...r, provider_id: manageSelectedProviderId };
      const add = (r.added || []).length, rem = (r.removed || []).length, com = (r.common || []).length;
      $("refresh-meta").textContent =
        `远程 ${r.remote.length} · 本地 ${r.local.length} · 新增 ${add} · 失效 ${rem} · 共有 ${com} · 已选 0`;
      const box = $("refresh-diff");
      box.replaceChildren();
      if ((r.metadata_warnings || []).length) {
        const warning = document.createElement("div");
        warning.className = "hint warning";
        warning.textContent = `规格读取提示：${r.metadata_warnings.join("；")}。未从 API 返回的上下文/输出限制会显示为未知，可手动填写。`;
        box.appendChild(warning);
      }
      const remoteByName = new Map((r.remote_models || []).map(model => [model.name, model]));
      const sections = [
        { key: "added", cls: "new", title: "新增（远程有、本地无）" },
        { key: "removed", cls: "removed", title: "失效（本地有、远程无）" },
        { key: "common", cls: "common", title: "共有" },
      ];
      for (const sec of sections) {
        const names = r[sec.key] || [];
        if (!names.length) continue;
        const h = document.createElement("div");
        h.className = "diff-head " + sec.cls;
        h.textContent = `${sec.title}（${names.length}）`;
        box.appendChild(h);
        for (const n of names) {
          const line = document.createElement("div");
          const selectable = true;
          line.className = "diff-line " + sec.cls + (selectable ? " selectable" : "");
          if (selectable) {
            line.dataset.modelName = n;
            line.dataset.category = sec.key;
            line.setAttribute("role", "button");
            line.setAttribute("aria-pressed", "false");
            line.tabIndex = 0;
            line.title = sec.key === "added"
              ? "点击选择增加；再次点击取消选择"
              : "点击选择移除；再次点击取消选择";
          }
          const remoteModel = remoteByName.get(n) || {};
          const localModel = modelsCache.find(model => model.provider_id === manageSelectedProviderId && model.name === n);
          const details = [];
          if (Number(remoteModel.context_tokens) > 0) {
            details.push(`API 上下文 ${Number(remoteModel.context_tokens).toLocaleString("zh-CN")}`);
          } else if (localModel && Number(localModel.context_tokens) > 0) {
            details.push(`本地上下文 ${modelLimitLabel(localModel.context_tokens, localModel.context_source)}`);
          } else details.push("API未提供上下文");
          if (Number(remoteModel.max_output_tokens) > 0) {
            details.push(`API 输出 ${Number(remoteModel.max_output_tokens).toLocaleString("zh-CN")}`);
          } else if (localModel && Number(localModel.max_output_tokens) > 0) {
            details.push(`本地输出 ${modelLimitLabel(localModel.max_output_tokens, localModel.output_source)}`);
          } else details.push("API未提供输出上限");
          if (remoteModel.thinking_effort) details.push(`API 思考 ${remoteModel.thinking_effort}`);
          else if (localModel?.thinking_effort) details.push(`本地思考 ${localModel.thinking_effort}`);
          line.textContent = details.length ? `${n} · ${details.join(" · ")}` : n;
          if (selectable) {
            const toggleSelection = () => {
              if (selectedRefreshPreset) {
                selectedRefreshPreset = "";
                for (const [name, action] of selectedRefreshModels) {
                  if (action === "keep") selectedRefreshModels.delete(name);
                }
              }
              if (selectedRefreshModels.has(n)) selectedRefreshModels.delete(n);
              else selectedRefreshModels.set(n, sec.key === "added" ? "add" : "remove");
              updateRefreshSelectionSummary(r);
            };
            line.addEventListener("click", toggleSelection);
            line.addEventListener("keydown", (ev) => {
              if (ev.key === "Enter" || ev.key === " ") {
                ev.preventDefault();
                toggleSelection();
              }
            });
          }
          box.appendChild(line);
        }
      }
      if (!box.children.length) {
        box.textContent = "没有可对比的模型。";
      }
      updateRefreshSelectionSummary(r);
    } catch (e) {
      $("refresh-meta").textContent = "刷新失败：" + (e.error || e.message);
    }
  });
  $("refresh-close").addEventListener("click", () => { refreshModal.style.display = "none"; });

  const refreshPresetLabels = {
    overwrite: "覆盖列表",
    remove_stale: "移除失效模型",
    add_new: "增加新模型",
    remove_all: "移除全部本地模型",
  };
  function updateRefreshSelectionSummary(diff = lastRefreshDiff) {
    if (!diff) return;
    let adds = 0, removes = 0, keeps = 0;
    for (const action of selectedRefreshModels.values()) {
      if (action === "add") adds++;
      else if (action === "remove") removes++;
      else if (action === "keep") keeps++;
    }
    for (const line of document.querySelectorAll("#refresh-diff .diff-line.selectable")) {
      const action = selectedRefreshModels.get(line.dataset.modelName) || "";
      line.classList.toggle("is-selected", !!action);
      line.classList.toggle("is-selected-add", action === "add");
      line.classList.toggle("is-selected-remove", action === "remove");
      line.classList.toggle("is-selected-keep", action === "keep");
      line.setAttribute("aria-pressed", String(!!action));
    }
    const preset = selectedRefreshPreset ? ` · 快速选择：${refreshPresetLabels[selectedRefreshPreset]}` : "";
    $("refresh-meta").textContent =
      `远程 ${(diff.remote || []).length} · 本地 ${(diff.local || []).length} · 新增 ${(diff.added || []).length} · 失效 ${(diff.removed || []).length} · 共有 ${(diff.common || []).length} · 已选 ${selectedRefreshModels.size}（增加 ${adds} / 移除 ${removes} / 保留 ${keeps}）${preset}`;
    const canSavePreset = selectedRefreshPreset === "overwrite" ? (diff.remote || []).length > 0
      : selectedRefreshPreset === "remove_all" ? (diff.local || []).length > 0
        : selectedRefreshPreset === "remove_stale" ? (diff.removed || []).length > 0
          : selectedRefreshPreset === "add_new" ? (diff.added || []).length > 0 : false;
    $("sync-save-selected").disabled = !canSavePreset && selectedRefreshModels.size === 0;
  }
  function quickSelectRefreshPreset(mode) {
    if (!lastRefreshDiff) return;
    selectedRefreshPreset = mode;
    selectedRefreshModels.clear();
    if (mode === "overwrite") {
      for (const name of lastRefreshDiff.added || []) selectedRefreshModels.set(name, "add");
      for (const name of lastRefreshDiff.removed || []) selectedRefreshModels.set(name, "remove");
      for (const name of lastRefreshDiff.common || []) selectedRefreshModels.set(name, "keep");
    } else if (mode === "remove_stale") {
      for (const name of lastRefreshDiff.removed || []) selectedRefreshModels.set(name, "remove");
    } else if (mode === "add_new") {
      for (const name of lastRefreshDiff.added || []) selectedRefreshModels.set(name, "add");
    } else if (mode === "remove_all") {
      for (const name of [...(lastRefreshDiff.removed || []), ...(lastRefreshDiff.common || [])]) {
        selectedRefreshModels.set(name, "remove");
      }
    }
    updateRefreshSelectionSummary();
  }

  async function applySync() {
    if (!lastRefreshDiff) return;
    const mode = selectedRefreshPreset;
    if (mode === "remove_all") {
      const count = (lastRefreshDiff.local || []).length;
      if (!count) { setManageStatus("该供应商下没有本地模型"); return; }
      if (!confirm(`移除该供应商下全部 ${count} 个本地模型？供应商和 API Key 会保留；如果激活模型在其中，也会取消激活。`)) return;
      try {
        const result = await api("DELETE", `/api/providers/${lastRefreshDiff.provider_id}/models`);
        if (editingModelId) closeModelForm();
        refreshModal.style.display = "none";
        setManageStatus(`已全部移除 ${result.removed || 0} 个模型`);
        await loadConfig();
      } catch (e) {
        const message = e.error || e.message;
        $("refresh-meta").textContent = "保存失败：" + message;
        setManageStatus(message);
      }
      return;
    }
    if (mode === "overwrite") {
      if (!confirm("用远程列表完整覆盖本地模型列表？远程不存在的本地模型会被移除，同名模型保留本地 ID、参数和测试状态。")) return;
    } else if (!mode && !selectedRefreshModels.size) {
      setManageStatus("请先选择模型，或使用快速选择按钮");
      return;
    } else if (!mode && [...selectedRefreshModels.values()].includes("remove")) {
      const count = [...selectedRefreshModels.values()].filter(action => action === "remove").length;
      if (!confirm(`将移除所选 ${count} 个本地模型？若其中包含当前激活模型，会取消激活。`)) return;
    }
    let addedBeforeFailure = 0;
    try {
      const request = {
        provider_id: lastRefreshDiff.provider_id,
        names: lastRefreshDiff.remote || [],
        remote_models: lastRefreshDiff.remote_models || [],
      };
      if (mode) {
        await api("POST", "/api/models/sync", { ...request, mode });
        const action = { overwrite: "已覆盖完整远程列表",
          remove_stale: "已移除全部失效模型", add_new: "已增加全部新模型" };
        setManageStatus(action[mode] || "模型列表已保存");
      } else {
        const addNames = [...selectedRefreshModels]
          .filter(([, action]) => action === "add").map(([name]) => name);
        const removeNames = [...selectedRefreshModels]
          .filter(([, action]) => action === "remove").map(([name]) => name);
        if (addNames.length) {
          await api("POST", "/api/models/sync", {
            ...request, mode: "add_selected", selected_names: addNames,
          });
          addedBeforeFailure = addNames.length;
        }
        if (removeNames.length) {
          // An empty remote-name list makes remove_selected apply to any selected local model,
          // including common models, and is supported by older running server versions too.
          await api("POST", "/api/models/sync", {
            provider_id: lastRefreshDiff.provider_id,
            mode: "remove_selected",
            names: [],
            remote_models: [],
            selected_names: removeNames,
          });
        }
        setManageStatus(`已保存所选模型（增加 ${addNames.length} · 移除 ${removeNames.length}）`);
      }
      refreshModal.style.display = "none";
      await loadConfig();
      renderManageLists();
    } catch (e) {
      const detail = e.error || e.message;
      const message = addedBeforeFailure
        ? `已增加 ${addedBeforeFailure} 个模型，但移除操作失败：${detail}` : detail;
      $("refresh-meta").textContent = "保存失败：" + message;
      setManageStatus(message);
    }
  }
  $("sync-overwrite").addEventListener("click", () => quickSelectRefreshPreset("overwrite"));
  $("sync-remove").addEventListener("click", () => quickSelectRefreshPreset("remove_stale"));
  $("sync-add").addEventListener("click", () => quickSelectRefreshPreset("add_new"));
  $("sync-remove-all").addEventListener("click", () => quickSelectRefreshPreset("remove_all"));
  $("sync-save-selected").addEventListener("click", async () => {
    const button = $("sync-save-selected");
    if (button.disabled) return;
    button.disabled = true;
    await applySync();
    if ($("refresh-modal").style.display !== "none") updateRefreshSelectionSummary();
  });

  $("model-test").addEventListener("click", () => {
    if (!manageSelectedProviderId) return;
    const list = modelsOf(manageSelectedProviderId);
    const box = $("test-list");
    box.replaceChildren();
    for (const m of list) {
      const label = document.createElement("label");
      label.className = "test-item";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = m.id;
      cb.checked = true;
      const span = document.createElement("span");
      span.className = "test-dot " + statusDot(m.test_status);
      span.title = m.test_status;
      const name = document.createElement("span");
      name.textContent = m.name;
      label.append(cb, span, name);
      box.appendChild(label);
    }
    testModal.style.display = "flex";
  });
  $("test-close").addEventListener("click", () => { testModal.style.display = "none"; });
  $("test-all").addEventListener("click", () => {
    document.querySelectorAll("#test-list input").forEach(x => { x.checked = true; });
  });
  $("test-none").addEventListener("click", () => {
    document.querySelectorAll("#test-list input").forEach(x => { x.checked = false; });
  });
  $("test-run").addEventListener("click", async () => {
    const ids = [...document.querySelectorAll("#test-list input:checked")].map(x => x.value);
    if (!ids.length) { setManageStatus("请选择要测试的模型"); return; }
    testModal.style.display = "none";
    setManageStatus(`测试中 0/${ids.length}`);
    let ok = 0, fail = 0;
    for (let i = 0; i < ids.length; i++) {
      // 先标橙色
      const model = modelById(ids[i]);
      if (model) model.test_status = "testing";
      renderManageLists();
      try {
        const r = await api("POST", `/api/models/${ids[i]}/test`);
        if (r.ok) ok++; else fail++;
        if (r.model) {
          const idx = modelsCache.findIndex(x => x.id === r.model.id);
          if (idx >= 0) modelsCache[idx] = r.model;
        }
      } catch (e) { fail++; }
      setManageStatus(`测试中 ${i + 1}/${ids.length} · 成功 ${ok} · 失败 ${fail}`);
      renderManageLists();
    }
    setManageStatus(`测试完成：成功 ${ok} · 失败 ${fail}`);
    await loadConfig();
    renderManageLists();
  });

  // ---------- 初始化 ----------
  function init() {
    setMode("fast");
    updateFastRunButton();
    loadAppVersion();
    checkConn();
    loadConfig();
    loadCookieState().then(() => {
      if ($("cookie-method").value === "qr") refreshQrCode();
    });
    refreshStats();
    refreshOrganizationReadiness();
    refreshScanIssues(true).catch(() => {});
    refreshTree();
    loadFolderMerge();
    loadFolderProfiles();
    connectEvents();
    setInterval(checkConn, 10000);   // 仅更新连接指示灯，不写日志
    log("系统就绪。请先扫码登录（或手动输入 Cookie）→ 扫描 → 分析 → 定案 → 执行。");
  }

  // ---------- 设置中心 ----------
  function openSettings(tab) {
    const panel = $("settings-panel");
    if (!panel) return;
    panel.hidden = false;
    if (tab) selectSettingsTab(tab);
    refreshSettingsData();
  }
  function closeSettings() {
    const panel = $("settings-panel");
    if (panel) panel.hidden = true;
  }
  function selectSettingsTab(name) {
    document.querySelectorAll(".settings-nav-item").forEach((el) => {
      el.classList.toggle("active", el.getAttribute("data-tab") === name);
    });
    document.querySelectorAll(".settings-tab").forEach((el) => {
      const on = el.getAttribute("data-tab-panel") === name;
      el.hidden = !on;
    });
  }

  async function refreshSettingsData() {
    // 账号
    try {
      const st = await api("GET", "/api/login/status");
      const box = $("settings-account-info");
      if (box) {
        box.innerHTML = st && st.configured
          ? `<div>已登录 · B 站 <b>mid=${st.mid || "未知"}</b></div>`
          : "<div>尚未登录 B 站账号</div>";
      }
    } catch (e) {
      const box = $("settings-account-info");
      if (box) box.textContent = e.error || e.message || "读取账号失败";
    }
    // 模型概览
    try {
      const cfg = await api("GET", "/api/config");
      const box = $("settings-model-overview");
      if (box) {
        box.innerHTML = `
          <div>当前模型：<b>${cfg.model || "未配置"}</b></div>
          <div class="muted">${cfg.base_url || ""}</div>
          <div class="muted">输出上限 ${cfg.analyze_max_tokens || "-"} · 上下文 ${cfg.active_context_tokens || cfg.model_context_tokens || "-"}</div>`;
      }
    } catch (e) {
      const box = $("settings-model-overview");
      if (box) box.textContent = e.error || e.message || "读取模型配置失败";
    }
    // 安全
    try {
      const vs = await api("GET", "/api/vault/status");
      const box = $("settings-security-status");
      if (box) {
        box.textContent = vs && vs.configured
          ? (vs.unlocked ? "金库：已设置应用密码 · 已解锁" : "金库：已设置应用密码 · 未解锁")
          : "金库：尚未初始化";
      }
    } catch (_) {}
    // 高级默认值
    loadPromptFields();
  }

  async function loadPromptFields() {
    const map = [
      ["prompt-profile", "prompt_profile"],
      ["prompt-analyze", "prompt_analyze"],
      ["prompt-merge", "prompt_merge"],
      ["conf-profile", "confidence_profile_min"],
      ["conf-analyze", "confidence_analyze_min"],
      ["conf-merge", "confidence_merge_min"],
    ];
    try {
      const [cfg, defRes] = await Promise.all([
        api("GET", "/api/config"),
        api("GET", "/api/prompts/defaults"),
      ]);
      const defaults = (defRes && defRes.defaults) || {};
      for (const [elId, key] of map) {
        const el = $(elId);
        if (!el) continue;
        const hasUser = cfg[key] !== undefined && cfg[key] !== null && cfg[key] !== "";
        el.value = hasUser ? cfg[key] : (defaults[key] ?? el.value);
      }
    } catch (_) {}
  }

  if ($("settings-btn")) $("settings-btn").addEventListener("click", () => openSettings());
  if ($("settings-close")) $("settings-close").addEventListener("click", closeSettings);
  if ($("settings-nav")) {
    $("settings-nav").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-tab]");
      if (btn) selectSettingsTab(btn.getAttribute("data-tab"));
    });
  }
  if ($("settings-manage-models")) {
    $("settings-manage-models").addEventListener("click", () => {
      const open = $("manage-models-btn");
      if (open) open.click();
    });
  }
  if ($("settings-open-library")) {
    $("settings-open-library").addEventListener("click", () => {
      const open = $("open-library") || $("library-open") || document.querySelector("[id*=library]");
      // 优先走既有查看本地内容按钮
      const btn = document.querySelector('button[id*="library"], button[title*="本地"]');
      if (btn && btn.id !== "settings-open-library") btn.click();
      else log("请使用主界面「查看本地内容」按钮", "info");
    });
  }
  if ($("settings-sec-reset")) {
    $("settings-sec-reset").addEventListener("click", async () => {
      const cur = ($("settings-sec-current") || {}).value || "";
      const p1 = ($("settings-sec-new") || {}).value || "";
      const p2 = ($("settings-sec-new2") || {}).value || "";
      if (p1.length < 6 || p1 !== p2) {
        log("新密码至少 6 位且两次一致", "warn");
        return;
      }
      try {
        await api("POST", "/api/vault/set-password", {
          password: p1, current_password: cur,
        });
        log("应用密码已重置", "ok");
        if ($("settings-sec-current")) $("settings-sec-current").value = "";
        if ($("settings-sec-new")) $("settings-sec-new").value = "";
        if ($("settings-sec-new2")) $("settings-sec-new2").value = "";
        refreshSettingsData();
      } catch (e) {
        log(e.error || e.message || "重置密码失败", "err");
      }
    });
  }
  if ($("settings-sec-hello")) {
    $("settings-sec-hello").addEventListener("click", async () => {
      try {
        const vs = await api("GET", "/api/vault/status");
        if (!vs || !vs.configured) {
          log("请先设置应用密码", "warn");
          return;
        }
        if (!vs.unlocked) {
          log("请先解锁金库再绑定 Windows Hello", "warn");
          return;
        }
        await api("POST", "/api/vault/bind-device", {});
        log("已绑定本机验证（Windows Hello / DPAPI）", "ok");
        refreshSettingsData();
      } catch (e) {
        log(e.error || e.message || "绑定失败", "err");
      }
    });
  }
  if ($("settings-save-advanced")) {
    $("settings-save-advanced").addEventListener("click", async () => {
      try {
        await api("POST", "/api/config", {
          prompt_profile: ($("prompt-profile") || {}).value || "",
          prompt_analyze: ($("prompt-analyze") || {}).value || "",
          prompt_merge: ($("prompt-merge") || {}).value || "",
          confidence_profile_min: Number(($("conf-profile") || {}).value || 0),
          confidence_analyze_min: Number(($("conf-analyze") || {}).value || 0),
          confidence_merge_min: Number(($("conf-merge") || {}).value || 0),
        });
        if ($("settings-advanced-state")) $("settings-advanced-state").textContent = "已保存";
        log("高级设置已保存", "ok");
      } catch (e) {
        log(e.error || e.message || "保存失败", "err");
      }
    });
  }
  async function saveOnePrompt(kind) {
    const pKey = kind === "profile" ? "prompt_profile" : kind === "analyze" ? "prompt_analyze" : "prompt_merge";
    const cKey = kind === "profile" ? "confidence_profile_min" : kind === "analyze" ? "confidence_analyze_min" : "confidence_merge_min";
    const pEl = $(kind === "profile" ? "prompt-profile" : kind === "analyze" ? "prompt-analyze" : "prompt-merge");
    const cEl = $(kind === "profile" ? "conf-profile" : kind === "analyze" ? "conf-analyze" : "conf-merge");
    try {
      await api("POST", "/api/config", {
        [pKey]: (pEl && pEl.value) || "",
        [cKey]: Number((cEl && cEl.value) || 0),
      });
      if ($("settings-advanced-state")) $("settings-advanced-state").textContent = "本项已保存";
    } catch (e) {
      log(e.error || e.message || "保存失败", "err");
    }
  }
  async function resetOnePrompt(kind) {
    try {
      const defRes = await api("GET", "/api/prompts/defaults");
      const d = (defRes && defRes.defaults) || {};
      const pEl = $(kind === "profile" ? "prompt-profile" : kind === "analyze" ? "prompt-analyze" : "prompt-merge");
      const cEl = $(kind === "profile" ? "conf-profile" : kind === "analyze" ? "conf-analyze" : "conf-merge");
      const pKey = kind === "profile" ? "prompt_profile" : kind === "analyze" ? "prompt_analyze" : "prompt_merge";
      const cKey = kind === "profile" ? "confidence_profile_min" : kind === "analyze" ? "confidence_analyze_min" : "confidence_merge_min";
      if (pEl) pEl.value = d[pKey] || "";
      if (cEl) cEl.value = d[cKey] ?? 0.5;
      await api("POST", "/api/config", { [pKey]: "", [cKey]: d[cKey] ?? 0.5 });
      log("本项已恢复默认", "ok");
    } catch (e) {
      log(e.error || e.message || "恢复默认失败", "err");
    }
  }
  if ($("save-prompt-profile")) $("save-prompt-profile").onclick = () => saveOnePrompt("profile");
  if ($("save-prompt-analyze")) $("save-prompt-analyze").onclick = () => saveOnePrompt("analyze");
  if ($("save-prompt-merge")) $("save-prompt-merge").onclick = () => saveOnePrompt("merge");
  if ($("reset-prompt-profile")) $("reset-prompt-profile").onclick = () => resetOnePrompt("profile");
  if ($("reset-prompt-analyze")) $("reset-prompt-analyze").onclick = () => resetOnePrompt("analyze");
  if ($("reset-prompt-merge")) $("reset-prompt-merge").onclick = () => resetOnePrompt("merge");
  if ($("settings-reset-all-prompts")) {
    $("settings-reset-all-prompts").onclick = async () => {
      await resetOnePrompt("profile");
      await resetOnePrompt("analyze");
      await resetOnePrompt("merge");
      if ($("settings-advanced-state")) $("settings-advanced-state").textContent = "已全部恢复默认";
    };
  }
  // 数据目录
  if ($("settings-data-open")) {
    $("settings-data-open").addEventListener("click", async () => {
      try {
        await api("POST", "/api/data/open-folder");
      } catch (e) {
        log(e.error || e.message || "无法打开目录", "err");
      }
    });
  }
  async function refreshDataPath() {
    try {
      const p = await api("GET", "/api/data/paths");
      const el = $("settings-data-path");
      if (el) el.value = p.data_dir || "";
    } catch (_) {}
  }
  if ($("settings-data-import")) $("settings-data-import").addEventListener("click", () => {
    const b = $("data-import-btn"); if (b) b.click();
  });
  if ($("settings-data-export")) $("settings-data-export").addEventListener("click", () => {
    const b = $("data-export-btn"); if (b) b.click();
  });
  if ($("settings-data-clear")) $("settings-data-clear").addEventListener("click", () => {
    const b = $("data-clear-btn"); if (b) b.click();
  });
  if ($("settings-theme-mode")) {
    $("settings-theme-mode").addEventListener("change", () => {
      const v = $("settings-theme-mode").value;
      const top = $("theme-mode");
      if (top) top.value = v;
      top && top.dispatchEvent(new Event("change"));
    });
  }
  const _openSettings = openSettings;
  openSettings = function (tab) {
    _openSettings(tab);
    refreshDataPath();
  };
  if ($("settings-cookie-method")) {
    $("settings-cookie-method").addEventListener("change", () => {
      const qr = $("settings-cookie-method").value === "qr";
      const a = $("settings-qr-panel");
      const b = $("settings-manual-panel");
      if (a) a.hidden = !qr;
      if (b) b.hidden = qr;
    });
  }
  if ($("lock-now-btn")) {
    $("lock-now-btn").addEventListener("click", async () => {
      try {
        await api("POST", "/api/vault/lock", {});
        await refreshAppLock();
        showLockScreen(true);
        log("已锁定", "warn");
      } catch (e) {
        log(e.error || e.message || "锁定失败", "err");
      }
    });
  }

  // 设置-账号：独立二维码/手动 Cookie（复用登录 API）
  if ($("settings-qr-btn")) {
    $("settings-qr-btn").addEventListener("click", async () => {
      const status = $("settings-qr-status");
      const box = $("settings-qr-box");
      const img = $("settings-qr-img");
      if (status) status.textContent = "生成中…";
      try {
        const r = await api("POST", "/api/login/qr/generate", undefined, { timeoutMs: 20000 });
        if (!r || !r.ok) throw new Error((r && r.error) || "生成失败");
        if (img) img.src = r.image;
        if (box) box.style.display = "flex";
        if (status) status.textContent = "请用手机 B 站 App 扫码";
      } catch (e) {
        if (status) status.textContent = e.error || e.message || "生成失败";
      }
    });
  }
  if ($("settings-save-cookie")) {
    $("settings-save-cookie").addEventListener("click", async () => {
      try {
        await api("POST", "/api/cookie", { cookie_string: ($("settings-cookie") || {}).value || "" });
        if ($("settings-cookie-state")) $("settings-cookie-state").textContent = "已保存";
        refreshSettingsData();
      } catch (e) {
        if ($("settings-cookie-state")) $("settings-cookie-state").textContent = e.error || e.message || "保存失败";
      }
    });
  }
  if ($("settings-test-cookie")) {
    $("settings-test-cookie").addEventListener("click", async () => {
      try {
        const r = await api("POST", "/api/test/cookie");
        log((r && r.ok) ? "Cookie 可用" : (r && r.error) || "Cookie 测试失败", (r && r.ok) ? "ok" : "err");
        refreshSettingsData();
      } catch (e) {
        log(e.error || e.message || "Cookie 测试失败", "err");
      }
    });
  }
  if ($("settings-save-account")) {
    $("settings-save-account").addEventListener("click", async () => {
      try {
        const r = await api("POST", "/api/folders/refresh");
        log("已刷新收藏夹目录：" + ((r && r.total != null) ? r.total : ""), "ok");
        refreshSettingsData();
        if (typeof refreshStats === "function") refreshStats();
      } catch (e) {
        log("刷新目录失败: " + (e.error || e.message), "err");
      }
    });
  }

  init();
})();
