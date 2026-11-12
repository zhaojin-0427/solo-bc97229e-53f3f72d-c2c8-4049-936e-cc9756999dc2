/* 改名迁移页逻辑（原生 JS，无外部依赖）。 */

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

async function api(url, options = {}) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await resp.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!resp.ok || !data.ok) {
    throw new Error(data.error || `HTTP ${resp.status}`);
  }
  return data;
}

function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

// ------------------------------------------------------------ 全局状态

const state = {
  schemaText: "",
  tables: [],          // [{name, columns:[{name, sensitive}]}]
  views: [],
  migrations: [],
  preview: null,       // 最近一次成功预览
  editingId: null,     // 弹窗正在编辑的视图 id
};

// 解析源表文本为下拉选项（容错：解析失败时返回空列表，不阻塞页面）
function parseSchemaTables(text) {
  const tables = [];
  for (const raw of (text || "").splitlines()) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const body = line.endsWith(";") ? line.slice(0, -1) : line;
    const lp = body.indexOf("(");
    const rp = body.lastIndexOf(")");
    if (lp <= 0 || rp !== body.length - 1) continue;
    const name = body.slice(0, lp).trim();
    const cols = body.slice(lp + 1, rp).split(",").map((p) => {
      let c = p.trim();
      let sensitive = false;
      if (c.endsWith("*")) { sensitive = true; c = c.slice(0, -1).trim(); }
      return { name: c, sensitive };
    }).filter((c) => c.name);
    if (name && cols.length) tables.push({ name, columns: cols });
  }
  return tables;
}

// ------------------------------------------------------------ 渲染

function renderTableSelect() {
  const sel = $("sel-table");
  const prev = sel.value;
  sel.innerHTML = state.tables
    .map((t) => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
  if (state.tables.some((t) => t.name === prev)) sel.value = prev;
  renderColumnSelect();
}

function renderColumnSelect() {
  const sel = $("sel-old");
  const table = state.tables.find((t) => t.name === $("sel-table").value);
  sel.innerHTML = table
    ? table.columns.map((c) =>
        `<option value="${esc(c.name)}">${esc(c.name)}${c.sensitive ? " 🔒" : ""}</option>`).join("")
    : "";
}

function renderViewList() {
  $("view-list").innerHTML = state.views.map((v) => `
    <div class="view-item">
      <div class="view-meta">
        <div class="view-name">${esc(v.name)}</div>
        <div class="view-desc">${esc(v.description || "（无说明）")}</div>
      </div>
      <button type="button" class="secondary" data-edit="${v.id}">编辑 / 溯源</button>
    </div>`).join("") || '<div class="empty-note">暂无视图，点击下方按钮新增。</div>';
  document.querySelectorAll("[data-edit]").forEach((btn) => {
    btn.addEventListener("click", () => openModal(Number(btn.dataset.edit)));
  });
}

function renderHistory() {
  $("history-list").innerHTML = state.migrations.map((m) => `
    <div class="history-item">
      <span>${esc(m.table_name)}.${esc(m.old_column)}
        <span class="arrow-tx">→</span> ${esc(m.new_column)}</span>
      <span class="history-time">#${m.id} · ${esc(m.created_at)}</span>
    </div>`).join("") || '<div class="empty-note">暂无迁移记录。</div>';
}

function highlightNewSql(sql, oldName, newName) {
  // 仅高亮标识符形态的新列名，避免误伤字符串内容
  const escaped = esc(sql);
  const pat = new RegExp("(?<![A-Za-z0-9_$])" + newName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
    + "(?![A-Za-z0-9_$])", "g");
  return escaped.replace(pat, (m) => `<span class="tok-new">${m}</span>`);
}

function renderPreview(preview) {
  $("preview-meta").className = "preview-summary";
  const affected = preview.views.filter((v) => v.affected).length;
  $("preview-meta").innerHTML = `
    <b>预览已生成（尚未保存）</b><br>
    目标：${esc(preview.table)}.${esc(preview.old_column)}
    → <b>${esc(preview.new_column)}</b>；
    共 ${preview.views.length} 个视图，其中 ${affected} 个引用了目标源列。<br>
    全部视图已通过改名前后溯源等价校验：输出列名、顺序、值/条件来源与敏感标记一致。
    预览后若编辑任一输入，确认将被拒绝，需重新预览。`;

  $("preview-result").innerHTML = preview.views.map((v) => {
    const notes = (v.notes || []).map((n) => `<div class="note-line">${esc(n)}</div>`).join("");
    const starHint = v.expanded_stars.length
      ? `<div class="change-hint">星号展开 ${v.expanded_stars.length} 处，仅目标列改名，列顺序保持不变。</div>`
      : "";
    return `
    <div class="view-preview ${v.affected ? "" : "unaffected"}">
      <div class="vp-head">
        <span class="vp-name">${esc(v.view_name)}</span>
        <span class="vp-badge ${v.affected ? "changed" : "unchanged"}">
          ${v.affected ? `改写 ${v.changed_refs.length + v.expanded_stars.length} 处` : "不受影响"}
        </span>
        <span class="vp-actions">
          <button type="button" class="secondary" data-copy="${esc(v.view_name)}">复制新 SQL</button>
        </span>
      </div>
      <div class="sql-grid">
        <div class="sql-pane">
          <div class="sql-pane-head">改名前（当前库中）</div>
          <pre class="sql-code">${esc(v.old_sql)}</pre>
        </div>
        <div class="sql-pane">
          <div class="sql-pane-head new">改名后（待部署新 SQL）</div>
          <pre class="sql-code">${highlightNewSql(v.new_sql, preview.old_column, preview.new_column)}</pre>
        </div>
      </div>
      ${starHint}${notes}
    </div>`;
  }).join("");

  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const view = preview.views.find((x) => x.view_name === btn.dataset.copy);
      try {
        await navigator.clipboard.writeText(view.new_sql);
        toast("新 SQL 已复制到剪贴板");
      } catch (_e) {
        // 剪贴板权限受限时的兜底
        const ta = document.createElement("textarea");
        ta.value = view.new_sql;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
        toast("新 SQL 已复制（兼容模式）");
      }
    });
  });
}

function invalidatePreview(reason) {
  state.preview = null;
  $("btn-confirm").disabled = true;
  if (reason) {
    $("mig-warn").hidden = false;
    $("mig-warn").textContent = reason;
  }
}

function showError(boxId, msg) {
  const box = $(boxId);
  box.hidden = false;
  box.textContent = msg;
}

// ------------------------------------------------------------ 数据加载

async function loadState() {
  const data = await api("/api/state");
  state.schemaText = data.schema.schema_text;
  state.views = data.views;
  state.migrations = data.migrations;
  state.tables = parseSchemaTables(state.schemaText);
  $("schema-edit").value = state.schemaText;
  renderTableSelect();
  renderViewList();
  renderHistory();
}

// ------------------------------------------------------------ 改名流程

$("sel-table").addEventListener("change", renderColumnSelect);

$("btn-preview").addEventListener("click", async () => {
  $("mig-error").hidden = true;
  $("mig-warn").hidden = true;
  const table = $("sel-table").value;
  const oldColumn = $("sel-old").value;
  const newColumn = $("inp-new").value.trim();
  if (!table || !oldColumn || !newColumn) {
    showError("mig-error", "请完整选择源表、旧列并填写新列名。");
    return;
  }
  $("mig-status").textContent = "正在分析并预览整批视图…";
  try {
    const data = await api("/api/rename/preview", {
      method: "POST",
      body: { table, old_column: oldColumn, new_column: newColumn },
    });
    state.preview = data.preview;
    $("btn-confirm").disabled = false;
    $("mig-status").textContent = "预览成功：可核对后确认，或复制新 SQL 先行部署。";
    renderPreview(state.preview);
  } catch (e) {
    state.preview = null;
    $("btn-confirm").disabled = true;
    $("preview-result").innerHTML = "";
    $("preview-meta").className = "preview-meta empty-note";
    $("preview-meta").textContent = "预览被拒绝（整批未做任何修改）。";
    showError("mig-error", "整批拒绝：" + e.message);
    $("mig-status").textContent = "预览失败";
  }
});

$("btn-confirm").addEventListener("click", async () => {
  if (!state.preview) return;
  const p = state.preview;
  if (!window.confirm(
      `确认将 ${p.table}.${p.old_column} 改名为 ${p.new_column}？\n\n`
      + "源表说明与全部视图将在单个事务内一起保存；确认前若任一输入被编辑过，请求会被拒绝。")) {
    return;
  }
  $("mig-error").hidden = true;
  $("mig-status").textContent = "正在原子提交…";
  try {
    const data = await api("/api/rename/confirm", {
      method: "POST",
      body: {
        table: p.table,
        old_column: p.old_column,
        new_column: p.new_column,
        token: p.token,
      },
    });
    state.schemaText = data.schema.schema_text;
    state.views = data.views;
    state.migrations = data.migrations;
    state.tables = parseSchemaTables(state.schemaText);
    $("schema-edit").value = state.schemaText;
    renderTableSelect();
    renderViewList();
    renderHistory();
    state.preview = null;
    $("btn-confirm").disabled = true;
    $("preview-result").innerHTML = "";
    $("preview-meta").className = "preview-meta empty-note";
    $("preview-meta").textContent = "迁移已提交并持久化，可在迁移历史中查看。";
    $("mig-status").textContent = "迁移成功：源表与全部视图已原子保存。";
    $("inp-new").value = "";
    toast("改名迁移已整批保存");
  } catch (e) {
    showError("mig-error", "确认被拒绝，数据库未发生任何修改：" + e.message);
    $("mig-status").textContent = "确认失败（无半迁移）";
    invalidatePreview();
  }
});

// 任何输入编辑都会使既有预览失效
["schema-edit", "inp-new"].forEach((id) => {
  $(id).addEventListener("input", () =>
    invalidatePreview("检测到输入已编辑，此前预览已失效，请重新预览。"));
});
$("sel-table").addEventListener("change", () => invalidatePreview());
$("sel-old").addEventListener("change", () => invalidatePreview());

// ------------------------------------------------------------ 源表保存

$("btn-save-schema").addEventListener("click", async () => {
  $("mig-error").hidden = true;
  try {
    const data = await api("/api/schema", {
      method: "PUT",
      body: { schema_text: $("schema-edit").value },
    });
    state.schemaText = data.schema.schema_text;
    state.tables = parseSchemaTables(state.schemaText);
    renderTableSelect();
    invalidatePreview("源表说明已更新，此前预览已失效，请重新预览。");
    toast("源表说明已保存");
  } catch (e) {
    showError("mig-error", "源表保存失败：" + e.message);
  }
});

// ------------------------------------------------------------ 视图编辑弹窗

function openModal(id) {
  state.editingId = id;
  $("modal-error").hidden = true;
  if (id == null) {
    $("modal-title").textContent = "新增视图";
    $("modal-name").value = "";
    $("modal-desc").value = "";
    $("modal-sql").value = "SELECT 1";
    $("modal-delete").hidden = true;
  } else {
    const v = state.views.find((x) => x.id === id);
    if (!v) return;
    $("modal-title").textContent = "编辑视图：" + v.name;
    $("modal-name").value = v.name;
    $("modal-desc").value = v.description || "";
    $("modal-sql").value = v.sql_text;
    $("modal-delete").hidden = false;
  }
  $("modal-mask").hidden = false;
}

function closeModal() {
  $("modal-mask").hidden = true;
  state.editingId = null;
}

$("btn-new-view").addEventListener("click", () => openModal(null));
$("modal-close").addEventListener("click", closeModal);
$("modal-mask").addEventListener("click", (e) => {
  if (e.target === $("modal-mask")) closeModal();
});

$("modal-save").addEventListener("click", async () => {
  $("modal-error").hidden = true;
  const body = {
    name: $("modal-name").value,
    description: $("modal-desc").value,
    sql_text: $("modal-sql").value,
  };
  try {
    if (state.editingId == null) {
      await api("/api/views", { method: "POST", body });
    } else {
      await api(`/api/views/${state.editingId}`, { method: "PUT", body });
    }
    const data = await api("/api/state");
    state.views = data.views;
    renderViewList();
    closeModal();
    invalidatePreview("视图已变更，此前预览已失效，请重新预览。");
    toast("视图已保存（已通过溯源校验）");
  } catch (e) {
    showError("modal-error", "保存失败（视图未入库）：" + e.message);
  }
});

$("modal-delete").addEventListener("click", async () => {
  if (state.editingId == null) return;
  if (!window.confirm("确认删除该视图？")) return;
  try {
    await api(`/api/views/${state.editingId}`, { method: "DELETE" });
    const data = await api("/api/state");
    state.views = data.views;
    renderViewList();
    closeModal();
    invalidatePreview("视图已删除，此前预览已失效，请重新预览。");
    toast("视图已删除");
  } catch (e) {
    showError("modal-error", "删除失败：" + e.message);
  }
});

// 弹窗内「溯源」：新标签页打开原溯源页，便于继续使用原功能
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("modal-mask").hidden) closeModal();
});

// ------------------------------------------------------------ 启动

window.addEventListener("DOMContentLoaded", async () => {
  try {
    await loadState();
  } catch (e) {
    showError("mig-error", "初始状态加载失败：" + e.message);
  }
});
