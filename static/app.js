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
  async function api(method, url, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    const res = await fetch(url, opt);
    const txt = await res.text();
    let data = null;
    try { data = txt ? JSON.parse(txt) : null; } catch (_) { /* 非 JSON */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || ("HTTP " + res.status));
      err.error = data && data.error;
      throw err;
    }
    return data;
  }

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

  // ---------- 配置 ----------
  async function loadConfig() {
    try {
      const c = await api("GET", "/api/config");
      $("cfg-base-url").value = c.base_url || "";
      $("cfg-model").value = c.model || "";
      $("cfg-key").value = "";   // P0-1：不回显凭据
      $("cfg-key").placeholder = c.api_key_set
        ? `已配置（${c.api_key_masked}）— 留空表示不修改`
        : "（本地无鉴权模型可留空）";
      if (c.browser) $("cfg-browser").value = c.browser;
      if (c.scan_interval) $("scan-interval").value = String(c.scan_interval);
      if (c.write_interval) $("write-interval").value = String(c.write_interval);
      if (c.folder_merge_interval) $("merge-interval").value = String(c.folder_merge_interval);
      if (c.apply_batch) $("apply-batch").value = String(c.apply_batch);
      if (c.analyze_concurrency) $("analyze-concurrency").value = String(c.analyze_concurrency);
      if (c.analyze_batch) $("analyze-batch").value = String(c.analyze_batch);
      if (c.analyze_max_tokens) $("analyze-max-tokens").value = String(c.analyze_max_tokens);
    } catch (e) { log("读取配置失败: " + e.message, "err"); }
  }

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
      log(`写操作间隔已设为 ≥${v} 秒（另加 0~1.5 秒随机抖动）`);
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
      log(`分析批大小已设为 ${v}（每次请求带 ${v} 条视频）`);
    } catch (e) { log("保存批大小失败: " + (e.error || e.message), "err"); }
  });

  // 最大输出改动即时保存
  $("analyze-max-tokens").addEventListener("change", async () => {
    const v = parseInt($("analyze-max-tokens").value, 10);
    if (!v || v < 256) { log("最大输出无效（需 ≥ 256）", "warn"); return; }
    try {
      await api("POST", "/api/config", { analyze_max_tokens: v });
      log(`最大输出已设为 ${v} tokens`);
    } catch (e) { log("保存最大输出失败: " + (e.error || e.message), "err"); }
  });

  $("save-config").addEventListener("click", async () => {
    try {
      const payload = {
        base_url: $("cfg-base-url").value.trim(),
        model: $("cfg-model").value.trim(),
        browser: $("cfg-browser").value,
      };
      // P0-1：留空表示不修改已保存的 key
      const k = $("cfg-key").value.trim();
      if (k) payload.api_key = k;
      await api("POST", "/api/config", payload);
      log(k ? "配置已保存（含新的 API Key）" : "配置已保存（API Key 未改动）");
      await loadConfig();
    } catch (e) { log("保存配置失败: " + (e.error || e.message), "err"); }
  });

  $("load-stats").addEventListener("click", () => refreshStats());

  // ---------- 进度条 ----------
  function setProgress(el, id, done, total) {
    const pct = total > 0 ? Math.min(100, Math.round(done / total * 100)) : 0;
    document.getElementById(id + "-fill").style.width = pct + "%";
    document.getElementById(id + "-text").textContent = `${done}/${total} (${pct}%)`;
    el.style.display = "flex";
  }

  function setAnalyzeProgress(done, inflight, total) {
    const completedPct = total > 0 ? Math.min(100, done / total * 100) : 0;
    const sentPct = total > 0 ? Math.min(100, (done + inflight) / total * 100) : 0;
    $("analyze-fill").style.width = completedPct + "%";
    $("analyze-sent-fill").style.width = sentPct + "%";
    $("analyze-progress").style.display = "flex";
  }

  // ---------- 步骤1 扫描 ----------
  $("scan-btn").addEventListener("click", async () => {
    try {
      await api("POST", "/api/scan");
      $("scan-progress").style.display = "flex";
      setProgress($("scan-progress"), "scan", 0, 0);
      log("扫描已启动（具体进度见下方事件日志）");
    } catch (e) {
      log("启动扫描失败: " + (e.error || e.message || ""), "err");
    }
  });

  $("folder-refresh").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/folders/refresh");
      log(`收藏夹目录已刷新，共 ${r.folders.length} 个`, "ok");
      await refreshTree();
      await loadFolderMerge();
    } catch (e) { log("刷新目录失败: " + (e.error || e.message), "err"); }
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
    } catch (e) { log("清除失败: " + (e.error || e.message), "err"); }
  });

  // ---------- 扫码登录 ----------
  let qrTimer = null;

  $("qr-btn").addEventListener("click", async () => {
    $("qr-status").textContent = "生成中 ...";
    try {
      const r = await api("POST", "/api/login/qr/generate");
      if (!r.ok) { $("qr-status").textContent = "失败：" + r.error; return; }
      $("qr-img").src = r.image;
      $("qr-box").style.display = "flex";
      $("qr-status").textContent = "请用手机 B站 App 扫码";
      log("二维码已生成，请用手机 B站 App 扫码");
      if (qrTimer) clearInterval(qrTimer);
      qrTimer = setInterval(pollQr, 2000);
    } catch (e) { $("qr-status").textContent = "失败：" + e.message; }
  });

  async function pollQr() {
    try {
      const r = await api("GET", "/api/login/qr/poll");
      if (r.status === "ok") {
        clearInterval(qrTimer); qrTimer = null;
        $("qr-box").style.display = "none";
        $("qr-status").textContent = "✅ 登录成功，Cookie 已保存";
        log("✅ 扫码登录成功，Cookie 已保存", "ok");
        loadCookieState();
      } else if (r.status === "expired") {
        clearInterval(qrTimer); qrTimer = null;
        $("qr-status").textContent = "二维码已过期，请重新生成";
        log("二维码已过期，请点「显示登录二维码」刷新", "warn");
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
  $("test-cookie").addEventListener("click", async () => {
    log("测试 Cookie 读取 ...");
    try {
      const r = await api("POST", "/api/test/cookie");
      if (r.ok) log("✅ " + r.message, "ok");
      else log("❌ Cookie 测试失败: " + r.error, "err");
    } catch (e) { log("❌ 请求失败: " + e.message, "err"); }
  });

  $("test-llm").addEventListener("click", async () => {
    log("测试模型连接 ...");
    try {
      const r = await api("POST", "/api/test/llm");
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
        const ico = f.done ? "✅" : (f.scanned > 0 ? "🔄" : "⬜");
        const row = document.createElement("div");
        row.className = "tree-row";
        row.innerHTML = `<input class="folder-pick" type="checkbox" data-id="${f.media_id}">
          <span class="ico"></span>
          <span class="nm${f.done ? " done" : ""}"></span>
          <span class="cnt"></span>
          <span class="pct ${cls}"></span>`;
        row.querySelector(".ico").textContent = ico;
        row.querySelector(".folder-pick").checked = selected.has(String(f.media_id));
        row.querySelector(".nm").textContent = f.title;
        row.querySelector(".cnt").textContent = `${f.scanned} / ${f.expected}`;
        row.querySelector(".pct").textContent = pct + "%";
        body.appendChild(row);
      });
      $("scan-selection-summary").textContent = `已选 ${selected.size || t.folders.length} / ${t.folders.length} 个`;
    } catch (e) { log("读取扫描状态失败: " + e.message, "err"); }
  }

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

  // ---------- 双路线标签 ----------
  document.querySelectorAll(".route-tab").forEach(tab => tab.addEventListener("click", () => {
    document.querySelectorAll(".route-tab").forEach(x => x.classList.toggle("active", x === tab));
    document.querySelectorAll(".route-panel").forEach(x => { x.style.display = x.id === tab.dataset.route ? "block" : "none"; });
  }));

  // ---------- 收藏夹整理：可编辑合并组表 ----------
  let mergeFolders = [];
  let mergeGroups = [];
  let submittedMergeGroups = [];
  let draftTimer = null;
  let editingSourceIndex = -1;
  const folderById = () => Object.fromEntries(mergeFolders.map(f => [String(f.media_id), f]));
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
      name.addEventListener("input", () => { g.final_name = name.value.trim(); saveMergeDraftSoon(); }); nameTd.appendChild(name);

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
    normalizeMergeNames(g); sourceModal.style.display = "none"; renderMergePlan(); saveMergeDraftSoon();
  });

  // ---------- 步骤2 分析 ----------
  $("analyze-start").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/analyze/start", { continuous: $("analyze-continuous").checked });
      setAnalyzeProgress(0, 0, r.total || 0);
      log(`分析已启动，共 ${r.total || 0} 条`);
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
  let planPage = 1;          // 当前页
  let planTab = "pending";   // pending | done
  let planFilter = { cur: "", target: "", action: "" };
  let planSel = new Set();   // 勾选的 bvid
  const PLAN_PAGE_SIZE = 100;

  async function loadPlan() {
    try {
      const p = await api("GET", "/api/plan");
      planFolders = (p.existing_folders || []).map(f => f.title);
      const idToTitle = {};
      (p.existing_folders || []).forEach(f => { idToTitle[String(f.media_id)] = f.title; });
      const videos = p.videos_by_bvid || {};
      const planMap = p.plan || {};
      const withMeta = (base, bvid) => {
        const v = videos[bvid] || {};
        const pl = planMap[bvid] || {};
        return Object.assign({}, base, {
          _title: v.title || base.bvid || bvid,
          _upper: v.upper_name || "",
          _cur: idToTitle[String(v.source_folder_id || "")] || "",
          _status: pl.status || "pending",
          _result: pl.result || "",
          _at: pl.at || "",
          // E：带上"已提交方案"的动作与目标，否则会显示回 LLM 建议
          _hasPlan: !!planMap[bvid],
          _planAction: pl.action || "",
          _planTarget: pl.target_folder || "",
          _planNew: pl.create_new_name || "",
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
      // ❹ 卡片概览（已提交方案 / 待操作 / 已完成）
      const nP = planRows.filter(r => r._status !== "done").length;
      const nD = planRows.filter(r => r._status === "done").length;
      const planned = Object.keys(planMap).length;
      $("apply-meta").textContent = planned
        ? `已提交方案 ${planned} 条（待操作 ${nP} · 已完成 ${nD}）`
        : "尚未提交方案";
      log(`已加载 ${planRows.length} 条待处理记录（另有 ${invCount} 条已失效视频）`);
    } catch (e) { log("加载方案失败: " + e.message, "err"); }
  }
  $("plan-load").addEventListener("click", loadPlan);

  // 一键标记失效视频为删除（防火墙：仅标题恰为「已失效视频」的条目）
  $("mark-invalid").addEventListener("click", async () => {
    if (!confirm("把所有标题为「已失效视频」的视频标记为删除？\n\n" +
                 "标记后需到阶段❹点「开始执行」才会真正删除。")) return;
    try {
      const r = await api("POST", "/api/plan/mark_invalid");
      log(`已标记 ${r.count} 条失效视频为删除（到阶段❹点「开始执行」生效）`, "warn");
    } catch (e) { log("标记失败: " + (e.error || e.message), "err"); }
  });

  // 统计摘要（分类卡片默认展示）
  function renderSummary() {
    const wrap = $("plan-summary");
    wrap.innerHTML = "";
    if (!planRows.length) {
      wrap.innerHTML = '<div class="muted">暂无分析结果，请先执行阶段❷「开始分析」。</div>';
      $("plan-meta").textContent = "";
      return;
    }
    let nKeep = 0, nExisting = 0, nNew = 0, nSkip = 0;
    const byTarget = new Map();
    planRows.forEach(r => {
      const rec = (r.recommended || "").trim();
      const isExisting = planFolders.includes(rec);
      let mode;
      if (!rec || r.action === "skip") mode = "skip";
      else if (r._cur && rec === r._cur) mode = "keep";      // 已在目标夹 → 无需移动
      else if (isExisting) mode = "existing";
      else mode = "new";
      if (mode === "keep") nKeep++;
      else if (mode === "existing") nExisting++;
      else if (mode === "new") nNew++;
      else nSkip++;
      const key = mode === "skip" ? "（跳过）"
                : (mode === "new" ? "（新建）" + rec : rec);
      byTarget.set(key, (byTarget.get(key) || 0) + 1);
    });
    $("plan-meta").textContent =
      `共 ${planRows.length} 条 · ${planFolders.length} 个现有收藏夹`;

    const stat = document.createElement("div");
    stat.className = "plan-stat";
    stat.innerHTML =
      `<span class="s-keep">无需移动 <b>${nKeep}</b></span>` +
      `<span>移入现有收藏夹 <b>${nExisting}</b></span>` +
      `<span class="s-new">建议新建 <b>${nNew}</b></span>` +
      `<span class="s-skip">跳过 <b>${nSkip}</b></span>`;
    wrap.appendChild(stat);

    const top = [...byTarget.entries()].sort((a, b) => b[1] - a[1]).slice(0, 15);
    const tops = document.createElement("div");
    tops.className = "plan-top";
    tops.appendChild(document.createTextNode("Top 目标："));
    top.forEach(([k, n]) => {
      const s = document.createElement("span");
      s.className = "tag" + (k.startsWith("（新建）") ? " new" : "");
      s.textContent = `${k} ${n}`;
      tops.appendChild(s);
    });
    wrap.appendChild(tops);
  }

  // 方案详情弹窗（❸ 与 ❹ 共用同一个）
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
    return baseRows().filter(r => {
      if (planFilter.cur && (r._cur || "未知") !== planFilter.cur) return false;
      if (planFilter.target && targetLabel(r) !== planFilter.target) return false;
      if (planFilter.action && actionLabel(r) !== planFilter.action) return false;
      return true;
    });
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
      (row.reason ? " · " + row.reason : "");
    if (row.reason) t.querySelector(".reason").textContent = "建议: " + row.reason;

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

    sel.addEventListener("change", () => {
      const v = sel.value;
      if (v === "__delete__") {
        row._target = { mode: "delete" }; showBadge(item, "del", "删除");
      } else if (v === "__new__") {
        const name = prompt("输入新收藏夹名称：", rec || row.create_new_name || "");
        if (name && name.trim()) {
          row._target = { mode: "new", name: name.trim() };
          showBadge(item, "new", name.trim());
        } else { sel.value = "__skip__"; row._target = { mode: "skip" }; showBadge(item, "skip", "跳过"); }
      } else if (v === "__skip__") {
        row._target = { mode: "skip" }; showBadge(item, "skip", "跳过");
      } else {
        row._target = { mode: "existing", name: v };
        if (row._cur && v === row._cur) showBadge(item, "keep", "无需移动");
        else showBadge(item, "existing", v);
      }
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
    const curs = [...new Set(base.map(r => r._cur || "未知"))].sort();
    const targets = [...new Set(base.map(r => targetLabel(r)))].sort();
    const acts = ACTION_NAMES.filter(a => base.some(r => actionLabel(r) === a));
    const mkSel = (label, key, options) => {
      const w = document.createElement("label");
      w.className = "inline-label";
      w.appendChild(document.createTextNode(label));
      const s = document.createElement("select");
      const o0 = document.createElement("option");
      o0.value = ""; o0.textContent = "全部";
      s.appendChild(o0);
      options.forEach(v => {
        const o = document.createElement("option");
        o.value = v; o.textContent = v;
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
    planFolders.forEach(f => {
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
        const box = $("scan-progress");
        box.style.display = "flex";
        if (typeof ev.done === "number") {
          setProgress(box, "scan", ev.done, ev.total || 0);
          const parts = [`已抓取 ${ev.done}/${ev.total} 条`];
          if (typeof ev.unique === "number") parts.push(`去重 ${ev.unique}`);
          if (ev.ftotal) parts.push(`收藏夹 ${ev.fdone}/${ev.ftotal}`);
          if (ev.current) parts.push(`当前：${ev.current}`);
          $("scan-text").textContent = parts.join(" · ");
        }
        if (modal.style.display === "flex") refreshTree();
        refreshStats();
      }
      if (ev.kind === "scan_end") {
        $("scan-progress").style.display = "none";
        refreshStats();
      }

      // 分析
      if (ev.phase === "analyze" && typeof ev.done === "number") {
        const b = $("analyze-progress");
        b.style.display = "flex";
        const inflight = Number(ev.inflight || 0);
        setAnalyzeProgress(ev.done, inflight, ev.total || 0);
        const parts = [`已完成 ${ev.done}/${ev.total}`];
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
      }
      if (ev.phase === "folder_merge" || ev.phase === "folder_merge_ai") loadFolderMerge();

      // 执行
      if (ev.phase === "apply" && typeof ev.done === "number") {
        const b = $("apply-progress");
        b.style.display = "flex";
        setProgress(b, "apply", ev.done, ev.total || 0);
        refreshStats();
      }
      if (ev.kind === "apply_end") $("apply-progress").style.display = "none";
    };
    es.onerror = () => {
      es.close();
      setTimeout(connectEvents, 3000);   // 断线自动重连，从上次事件继续
    };
  }

  // ---------- 初始化 ----------
  function init() {
    checkConn();
    loadConfig();
    loadCookieState();
    refreshStats();
    refreshTree();
    loadFolderMerge();
    connectEvents();
    setInterval(checkConn, 10000);   // 仅更新连接指示灯，不写日志
    log("系统就绪。请先扫码登录（或填 Cookie）→ 扫描 → 分析 → 定案 → 执行。");
  }
  init();
})();
