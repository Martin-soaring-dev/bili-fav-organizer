/* Progressive task-center UI. Read-only, isolated from legacy app.js. */
import { createApiClient } from "./api-client.js";
import { createTaskStore } from "./task-store.js";

export function mountTaskCenter({ documentRef = document, intervalMs = 4000 } = {}) {
  const doc = documentRef;
  if (!doc.body || doc.getElementById("bfo-task-center")) return () => {};
  const request = createApiClient();
  const store = createTaskStore();
  const root = doc.createElement("section");
  root.id = "bfo-task-center";
  root.setAttribute("aria-label", "任务中心");
  root.style.cssText = "position:fixed;bottom:18px;right:18px;z-index:2000;font:13px system-ui,sans-serif;color:var(--text,#ddd)";
  const toggle = doc.createElement("button");
  toggle.type = "button";
  toggle.textContent = "任务中心";
  toggle.style.cssText = "padding:9px 14px;border-radius:12px;border:1px solid #888;background:var(--bg2,#222);color:inherit;cursor:pointer";
  const panel = doc.createElement("div");
  panel.hidden = true;
  panel.style.cssText = "width:min(360px,85vw);max-height:45vh;overflow:auto;padding:12px;margin-bottom:8px;border-radius:12px;border:1px solid #777;background:var(--bg2,#222);box-shadow:0 8px 28px #0006";
  const title = doc.createElement("strong");
  title.textContent = "统一任务中心（只读）";
  const list = doc.createElement("ul");
  list.style.cssText = "padding-left:18px;line-height:1.7";
  panel.append(title,list);
  root.append(panel,toggle);
  doc.body.append(root);
  let disposed = false;
  let busy = false;
  let interval = null;
  const render = () => {
    list.replaceChildren();
    const tasks = store.all();
    if (!tasks.length) {
      const li = doc.createElement("li");
      li.textContent = "暂无任务记录";
      list.append(li);
    }
    for (const task of tasks.slice(0, 30)) {
      const li = doc.createElement("li");
      li.textContent = `${task.kind || "task"} · ${task.state || "unknown"} · ${String(task.id || "").slice(0, 8)}`;
      list.append(li);
    }
  };
  const unsubscribe = store.subscribe(render);
  const refresh = async () => {
    if (disposed || busy || panel.hidden) return;
    busy = true;
    try {
      const data = await request("GET", "/api/tasks", undefined, { timeoutMs: 10000 });
      if (!disposed) store.replace(data?.tasks || []);
    } catch {
      if (!disposed) {
        list.replaceChildren();
        const li = doc.createElement("li");
        li.textContent = "任务状态暂不可用（可能需要解锁）";
        list.append(li);
      }
    } finally { busy = false; }
  };
  toggle.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) void refresh();
  });
  interval = setInterval(() => { void refresh(); }, intervalMs);
  return () => {
    disposed = true;
    clearInterval(interval);
    unsubscribe();
    root.remove();
  };
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => mountTaskCenter(), { once: true });
  } else {
    mountTaskCenter();
  }
}
