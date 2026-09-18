/* 事件证据链审计台 —— 原生前端（无框架、无 CDN） */
"use strict";

let STATE = null;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------------ API -- */
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

async function loadState() {
  STATE = await api("/api/state");
  render();
}

async function patchSource(id, body) {
  const state = await api(`/api/sources/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  STATE = state;
  render();
}

/* -------------------------------------------------------------- helpers -- */
function fmtTime(iso) {
  if (!iso) return "—";
  return iso.replace("T", " ").replace("Z", " UTC");
}
function fmtHM(iso) {
  if (!iso) return "";
  return iso.slice(11, 23);
}
function confClass(v) {
  if (v == null) return "lo";
  if (v >= 0.75) return "hi";
  if (v >= 0.5) return "mid";
  return "lo";
}
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = `toast ${kind}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 2600);
}
function busy(flag) {
  $$(".btn").forEach((b) => { b.disabled = flag; });
}

/* --------------------------------------------------------------- stats -- */
function renderStats() {
  const s = STATE.stats;
  const span = s.span.start ? `${fmtHM(s.span.start)} ~ ${fmtHM(s.span.end)}` : "—";
  $("#statbar").innerHTML = `
    <div class="stat"><div class="k">事件数</div><div class="v">${s.event_count}</div></div>
    <div class="stat"><div class="k">来源数</div><div class="v">${s.source_count}</div></div>
    <div class="stat ${s.conflict_count ? "bad" : "ok"}">
      <div class="k">冲突链</div><div class="v">${s.conflict_count}</div></div>
    <div class="stat ${s.impossible_edge_count ? "bad" : "ok"}">
      <div class="k">不可能顺序边</div><div class="v">${s.impossible_edge_count}</div></div>
    <div class="stat"><div class="k">存疑顺序边</div><div class="v">${s.ambiguous_edge_count}</div></div>
    <div class="stat ${s.avg_confidence != null && s.avg_confidence < 0.5 ? "bad" : "ok"}">
      <div class="k">平均可信度</div><div class="v">${s.avg_confidence ?? "—"}</div></div>
    <div class="stat"><div class="k">参考时间跨度</div><div class="v" style="font-size:13px">${esc(span)}</div></div>
  `;
  $("#conflict-count").textContent = s.conflict_count;
  $("#conflict-count").className = `badge ${s.conflict_count ? "badge-red" : "badge-green"}`;
}

/* ------------------------------------------------------------- sources -- */
function renderSources() {
  const box = $("#sources");
  if (!STATE.sources.length) {
    box.innerHTML = `<p class="hint" style="padding:8px">尚未导入任何来源。</p>`;
    return;
  }
  box.innerHTML = "";
  for (const s of STATE.sources) {
    const card = document.createElement("div");
    card.className = "source-card" + (s.conflict_events > 0 ? " highlight" : "");
    card.dataset.sourceId = s.id;
    card.innerHTML = `
      <div class="sc-head">
        <span class="sc-name">${esc(s.name)}</span>
        <span>
          ${s.conflict_events > 0
            ? `<span class="badge badge-red" title="该来源有事件处于冲突链中">${s.conflict_events} 冲突</span> `
            : ""}
          <span class="confidence ${confClass(s.trust)}" title="来源信任度">${s.trust.toFixed(2)}</span>
        </span>
      </div>
      <div class="sc-grid">
        <div class="sc-field"><label>时钟偏差 offset(秒)</label>
          <input type="number" step="any" data-k="offset_seconds" value="${s.offset_seconds}"></div>
        <div class="sc-field"><label>漂移 drift(ppm)</label>
          <input type="number" step="any" data-k="drift_ppm" value="${s.drift_ppm}"></div>
        <div class="sc-field"><label>不确定半径(秒)</label>
          <input type="number" step="any" min="0" data-k="uncertainty_seconds" value="${s.uncertainty_seconds}"></div>
        <div class="sc-field"><label>来源信任 0~1</label>
          <input type="number" step="0.01" min="0" max="1" data-k="trust" value="${s.trust}"></div>
      </div>
      <div class="sc-foot">
        <label class="switch">
          <input type="checkbox" data-k="monotonic" ${s.monotonic ? "checked" : ""}>
          假设时钟单调（同源按序）
        </label>
        <span><span class="dirty-dot hidden">● 未保存</span>
        <button class="btn sc-save btn-primary">应用并重算</button></span>
      </div>
      ${s.note ? `<div class="sc-note">${esc(s.note)}</div>` : ""}
    `;
    const inputs = $$("input", card);
    inputs.forEach((inp) => inp.addEventListener("input", () => {
      $(".dirty-dot", card).classList.remove("hidden");
    }));
    $(".sc-save", card).addEventListener("click", async () => {
      const body = {};
      inputs.forEach((inp) => {
        const k = inp.dataset.k;
        if (k === "monotonic") body[k] = inp.checked;
        else body[k] = parseFloat(inp.value);
      });
      try {
        busy(true);
        await patchSource(s.id, body);
        toast(`来源 ${s.name} 的偏差假设已应用并完成重算`, "ok");
      } catch (e) {
        toast(e.message, "err");
      } finally { busy(false); }
    });
    box.appendChild(card);
  }
}

/* ------------------------------------------------------------ conflicts -- */
function renderConflicts() {
  const box = $("#conflicts");
  if (!STATE.conflicts.length) {
    box.innerHTML = `<div class="no-conflict">✓ 当前偏差假设下没有发现不可能顺序或冲突链。</div>`;
    return;
  }
  box.innerHTML = "";
  for (const c of STATE.conflicts) {
    const card = document.createElement("div");
    card.className = "conflict-card";
    const chain = c.chain.map((id) => `<span class="ev">${esc(id)}</span>`).join('<span class="arrow">→</span>');
    card.innerHTML = `
      <div class="conflict-kind">${c.kind === "cycle" ? "⟳ 不可能顺序（约束闭环）" : "⚠ 时间区间冲突（被约束挤压为空）"}</div>
      <div class="conflict-chain">${chain}</div>
      <div class="conflict-detail">${esc(c.detail)}</div>`;
    card.addEventListener("click", () => {
      locateChain(c.chain_db_ids);
    });
    box.appendChild(card);
  }
}

/** 滚动并闪烁冲突链上的事件条；先清除筛选，确保整条链可见 */
function locateChain(ids) {
  $("#f-text").value = "";
  $("#f-source").value = "";
  $("#f-type").value = "";
  $("#f-conflict").checked = false;
  $("#f-lowconf").checked = false;
  renderTimeline();
  const first = ids[0];
  const row = document.querySelector(`.tl-row[data-event-id="${first}"]`);
  if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
  ids.forEach((id) => {
    const bar = document.querySelector(`.tl-bar[data-event-id="${id}"]`);
    if (bar) {
      bar.classList.remove("flash");
      void bar.offsetWidth;
      bar.classList.add("flash");
    }
  });
}

/* -------------------------------------------------------------- timeline -- */
function getFilters() {
  return {
    text: $("#f-text").value.trim().toLowerCase(),
    source: $("#f-source").value,
    type: $("#f-type").value,
    conflictOnly: $("#f-conflict").checked,
    lowConfOnly: $("#f-lowconf").checked,
  };
}

function filteredEvents() {
  const f = getFilters();
  return STATE.events.filter((e) => {
    if (f.source && e.source !== f.source) return false;
    if (f.type && e.type !== f.type) return false;
    if (f.conflictOnly && !e.in_conflict) return false;
    if (f.lowConfOnly && (e.confidence == null || e.confidence >= 0.5)) return false;
    if (f.text) {
      const hay = `${e.ext_id} ${e.type} ${e.summary} ${e.source}`.toLowerCase();
      if (!hay.includes(f.text)) return false;
    }
    return true;
  });
}

function renderTimeline() {
  const evs = filteredEvents();
  $("#empty").classList.toggle("hidden", evs.length > 0);
  const tl = $("#timeline");
  tl.innerHTML = "";
  if (!evs.length) return;

  // 时间域基于全量事件，保证筛选后比例不变
  const all = STATE.events.length ? STATE.events : evs;
  const tMin = Math.min(...all.map((e) => e.ref_lower_epoch));
  const tMax = Math.max(...all.map((e) => e.ref_upper_epoch));
  const span = Math.max(tMax - tMin, 1e-3);
  // 两侧留 1% 边距
  const pad = span * 0.01;
  const domMin = tMin - pad, domMax = tMax + pad;
  const domSpan = domMax - domMin;
  const pct = (t) => ((t - domMin) / domSpan) * 100;

  // ---- 时间轴（与泳道列对齐：70px gutter + 1fr lane）----
  const axis = document.createElement("div");
  axis.className = "tl-row";
  axis.style.gridTemplateColumns = "70px 1fr";
  const axisGutter = document.createElement("div");
  axisGutter.className = "tl-gutter";
  axisGutter.innerHTML = `<span class="src" style="font-size:10px">UTC</span>`;
  const axisLane = document.createElement("div");
  axisLane.className = "tl-lane tl-timeaxis";
  const TICKS = 8;
  axisLane.style.gridTemplateColumns = `repeat(${TICKS}, 1fr)`;
  for (let i = 0; i < TICKS; i++) {
    const t = domMin + (domSpan * i) / (TICKS - 1);
    const iso = new Date(t * 1000).toISOString();
    const tick = document.createElement("div");
    tick.className = "tl-tick";
    tick.textContent = fmtHM(iso);
    axisLane.appendChild(tick);
  }
  axis.appendChild(axisGutter);
  axis.appendChild(axisLane);
  tl.appendChild(axis);

  // ---- 按来源分泳道，泳道内按 ordinal 排序 ----
  const bySource = new Map();
  for (const e of evs) {
    if (!bySource.has(e.source)) bySource.set(e.source, []);
    bySource.get(e.source).push(e);
  }

  const eventRowEls = new Map();

  for (const [sourceName, list] of bySource) {
    list.sort((a, b) => a.ordinal - b.ordinal);

    const laneHeader = document.createElement("div");
    laneHeader.className = "tl-row";
    laneHeader.innerHTML = `
      <div class="tl-gutter" style="border-bottom:1px solid var(--line)">
        <span class="src">${esc(sourceName)}</span><span>${list.length} 条</span>
      </div>
      <div class="tl-lane" style="height:14px;border-bottom:1px solid var(--line);
        background:repeating-linear-gradient(90deg,transparent 0,transparent calc(12.5% - 1px),rgba(42,53,80,.5) calc(12.5% - 1px),rgba(42,53,80,.5) 12.5%)"></div>`;
    tl.appendChild(laneHeader);

    for (const e of list) {
      const row = document.createElement("div");
      row.className = "tl-row";
      row.dataset.eventId = e.id;

      const left = pct(e.ref_lower_epoch);
      const width = Math.max(pct(e.ref_upper_epoch) - left, 0.35);

      row.innerHTML = `
        <div class="tl-gutter">
          <span class="src">${esc(e.ext_id || "#" + e.id)}</span>
          <span>${esc(e.type || "event")} · ±${(e.ref_upper_epoch - e.ref_lower_epoch).toFixed(1)}s</span>
        </div>
        <div class="tl-lane tl-events">
          <div class="tl-bar ${e.in_conflict ? "in-conflict" : ""} ${e.confidence != null && e.confidence < 0.5 ? "lowconf" : ""}"
               data-event-id="${e.id}"
               title="${esc(e.summary)}&#10;参考区间: ${fmtTime(e.ref_lower)} ~ ${fmtTime(e.ref_upper)}&#10;可信度: ${e.confidence ?? "—"}"
               style="left:${left}%; width:${width}%">
            <span class="evid">${esc(e.type || e.ext_id || "#" + e.id)}</span>
            <span class="confidence ${confClass(e.confidence)}">${e.confidence ?? "—"}</span>
          </div>
        </div>`;
      row.querySelector(".tl-bar").addEventListener("click", () => openEvent(e.id));
      tl.appendChild(row);
      eventRowEls.set(e.id, row);
    }
  }

  // ---- 约束箭头层（SVG），仅画当前可见事件之间的边 ----
  requestAnimationFrame(() => drawLinks(tl, evs, eventRowEls));
}

function drawLinks(tl, evs, rowEls) {
  const old = tl.querySelector(".tl-links-layer");
  if (old) old.remove();

  const visible = new Set(evs.map((e) => e.id));
  const edges = STATE.constraints.filter((c) => visible.has(c.from_event) && visible.has(c.to_event));
  if (!edges.length) return;

  const tlRect = tl.getBoundingClientRect();
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("class", "tl-links-layer");
  svg.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible;";
  svg.innerHTML = `
    <defs>
      <marker id="arrow-green" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
        <path d="M0,0 L10,5 L0,10 z" fill="#37c97a"/></marker>
      <marker id="arrow-amber" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
        <path d="M0,0 L10,5 L0,10 z" fill="#f2b84b"/></marker>
      <marker id="arrow-red" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
        <path d="M0,0 L10,5 L0,10 z" fill="#ef5f6b"/></marker>
    </defs>`;

  for (const c of edges) {
    const fromBar = document.querySelector(`.tl-bar[data-event-id="${c.from_event}"]`);
    const toBar = document.querySelector(`.tl-bar[data-event-id="${c.to_event}"]`);
    if (!fromBar || !toBar) continue;
    const a = fromBar.getBoundingClientRect();
    const b = toBar.getBoundingClientRect();
    // 源：from 条中点底部；目标：to 条中点顶部（若同 row 则画右侧小弧）
    const x1 = a.left - tlRect.left + a.width / 2;
    const y1 = a.top - tlRect.top + a.height;
    const x2 = b.left - tlRect.left + b.width / 2;
    const y2 = b.top - tlRect.top;
    const path = document.createElementNS(svgNS, "path");
    let d;
    if (Math.abs(y2 - y1) < 2) {
      const sx = a.right - tlRect.left + 4, ex = b.left - tlRect.left - 4;
      const yy = a.top - tlRect.top + a.height / 2;
      d = `M ${sx} ${yy} C ${sx + 14} ${yy - 16}, ${ex - 14} ${yy - 16}, ${ex} ${yy}`;
    } else {
      const my = (y1 + y2) / 2;
      d = `M ${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2 - 2}`;
    }
    path.setAttribute("d", d);
    path.setAttribute("class", `tl-linkpath ${c.status}`);
    path.setAttribute("marker-end",
      c.status === "impossible" ? "url(#arrow-red)"
      : c.status === "ambiguous" ? "url(#arrow-amber)" : "url(#arrow-green)");
    const originLabel = c.origin === "monotonic" ? "同源单调" : "显式因果";
    path.innerHTML = `<title>${esc(c.from_ext)} → ${esc(c.to_ext)}（${originLabel}，${
      c.status === "forced" ? "顺序确定" : c.status === "ambiguous" ? "区间重叠存疑" : "不可能顺序"}）</title>`;
    svg.appendChild(path);
  }
  tl.style.position = "relative";
  tl.appendChild(svg);
}

/* -------------------------------------------------------- event detail -- */
function openEvent(id) {
  const e = STATE.events.find((x) => x.id === id);
  if (!e) return;
  $("#ev-title").textContent = `${e.ext_id || "#" + e.id} · ${e.type || "event"}`;
  const incoming = STATE.constraints.filter((c) => c.to_event === id);
  const outgoing = STATE.constraints.filter((c) => c.from_event === id);
  const relRow = (c, dir) => `<div>
      <span class="confidence ${c.status === "impossible" ? "lo" : c.status === "forced" ? "hi" : "mid"}">
      ${c.status === "forced" ? "确定" : c.status === "ambiguous" ? "存疑" : "不可能"}</span>
      ${c.origin === "monotonic" ? "（同源单调）" : "（显式）"}
      ${dir === "in" ? "← " : " → "}
      <a href="#" data-jump="${dir === "in" ? c.from_event : c.to_event}">
        ${esc(dir === "in" ? c.from_ext : c.to_ext)}</a>
    </div>`;
  // 原始记录在 export 中才有；这里用可从 state 还原的字段展示
  const rawLike = {
    id: e.ext_id, source: e.source, type: e.type, summary: e.summary,
    raw_timestamp: e.raw_ts, jitter_seconds: e.jitter,
  };
  $("#ev-body").innerHTML = `
    <dl class="kv">
      <dt>来源</dt><dd>${esc(e.source)}（同源序号 ${e.seq}）</dd>
      <dt>源时钟时间</dt><dd>${fmtTime(e.raw_ts)}</dd>
      <dt>参考区间</dt><dd class="${e.in_conflict ? "" : ""}">
        ${fmtTime(e.ref_lower)}<br>～ ${fmtTime(e.ref_upper)}
        ${e.in_conflict ? '<strong style="color:var(--red)">（区间冲突）</strong>' : ""}</dd>
      <dt>区间半宽</dt><dd>${((e.ref_upper_epoch - e.ref_lower_epoch) / 2).toFixed(2)} 秒</dd>
      <dt>可信度</dt><dd><span class="confidence ${confClass(e.confidence)}">${e.confidence ?? "—"}</span></dd>
      <dt>摘要</dt><dd>${esc(e.summary || "—")}</dd>
      <dt>前置关系</dt><dd>${incoming.length ? incoming.map((c) => relRow(c, "in")).join("") : "—"}</dd>
      <dt>后继关系</dt><dd>${outgoing.length ? outgoing.map((c) => relRow(c, "out")).join("") : "—"}</dd>
    </dl>
    <details open><summary class="hint">事件关键字段（原始 JSON 可在导出结果中查看）</summary>
      <pre class="rawjson">${esc(JSON.stringify(rawLike, null, 2))}</pre></details>`;
  $("#modal-event").classList.remove("hidden");
  $$("#ev-body [data-jump]").forEach((a) => a.addEventListener("click", (ev) => {
    ev.preventDefault();
    openEvent(parseInt(a.dataset.jump, 10));
  }));
}

/* ---------------------------------------------------------------- render -- */
function populateFilterOptions() {
  const srcSel = $("#f-source");
  const typeSel = $("#f-type");
  const curSrc = srcSel.value, curType = typeSel.value;
  const sources = [...new Set(STATE.events.map((e) => e.source))].sort();
  const types = [...new Set(STATE.events.map((e) => e.type).filter(Boolean))].sort();
  srcSel.innerHTML = `<option value="">全部来源</option>` +
    sources.map((s) => `<option ${s === curSrc ? "selected" : ""}>${esc(s)}</option>`).join("");
  typeSel.innerHTML = `<option value="">全部类型</option>` +
    types.map((t) => `<option ${t === curType ? "selected" : ""}>${esc(t)}</option>`).join("");
}

function render() {
  renderStats();
  renderSources();
  renderConflicts();
  populateFilterOptions();
  renderTimeline();
}

/* ------------------------------------------------------------- modals -- */
function openModal(id) { $("#" + id).classList.remove("hidden"); }
function closeModal(id) { $("#" + id).classList.add("hidden"); }
$$("[data-close]").forEach((b) => b.addEventListener("click", () => closeModal(b.dataset.close)));
$$(".modal-mask").forEach((m) => m.addEventListener("click", (e) => {
  if (e.target === m) m.classList.add("hidden");
}));

$("#btn-import").addEventListener("click", () => {
  $("#import-msg").innerHTML = "";
  openModal("modal-import");
});

const dz = $("#dropzone");
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) $("#import-file").files = e.dataTransfer.files;
});

$("#import-go").addEventListener("click", async () => {
  const mode = $("#import-mode").value;
  const file = $("#import-file").files[0];
  const text = $("#import-paste").value.trim();
  const msg = $("#import-msg");
  if (!file && !text) {
    msg.innerHTML = `<div class="err">请选择文件或粘贴 JSON 文本</div>`;
    return;
  }
  const fd = new FormData();
  fd.append("mode", mode);
  if (file) fd.append("file", file);
  else fd.append("payload", text);
  try {
    busy(true);
    const state = await api("/api/import", { method: "POST", body: fd });
    STATE = state;
    render();
    const imp = state.import || {};
    const warns = (imp.warnings || []).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join("");
    msg.innerHTML = `<div class="ok">导入 ${imp.imported} 条，跳过 ${imp.skipped} 条；冲突 ${state.stats.conflict_count} 条。</div>${warns}`;
    toast(`导入完成：${imp.imported} 条事件`, "ok");
    if (!warns) setTimeout(() => closeModal("modal-import"), 700);
  } catch (e) {
    msg.innerHTML = `<div class="err">导入失败：${esc(e.message)}</div>`;
    toast(e.message, "err");
  } finally { busy(false); }
});

/* ------------------------------------------------------------- buttons -- */
$("#btn-sample").addEventListener("click", async () => {
  try {
    busy(true);
    STATE = await api("/api/sample?mode=replace", { method: "POST" });
    render();
    toast("示例数据已载入，可尝试把 srv-db 偏差调到 +30 秒", "ok");
  } catch (e) { toast(e.message, "err"); } finally { busy(false); }
});

$("#btn-recompute").addEventListener("click", async () => {
  try {
    busy(true);
    STATE = await api("/api/recompute", { method: "POST" });
    render();
    toast("已基于当前偏差假设重新计算", "ok");
  } catch (e) { toast(e.message, "err"); } finally { busy(false); }
});

$("#btn-export").addEventListener("click", () => {
  window.location.href = "/api/export";
});

$("#btn-reset").addEventListener("click", async () => {
  if (!confirm("确定清空当前审计工作区的全部来源、事件与冲突结果？")) return;
  try {
    busy(true);
    STATE = await api("/api/reset", { method: "POST" });
    render();
    toast("工作区已清空", "ok");
  } catch (e) { toast(e.message, "err"); } finally { busy(false); }
});

$("#toggle-conflicts").addEventListener("click", () => {
  const box = $("#conflicts");
  const hidden = box.classList.toggle("hidden");
  $("#toggle-conflicts").textContent = hidden ? "展开" : "收起";
});

/* ------------------------------------------------------------- filters -- */
let filterTimer;
["#f-text", "#f-source", "#f-type", "#f-conflict", "#f-lowconf"].forEach((sel) => {
  $(sel).addEventListener("input", () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(renderTimeline, 120);
  });
});

window.addEventListener("resize", () => {
  if (STATE) renderTimeline();
});

/* ------------------------------------------------------------------ go -- */
loadState().catch((e) => toast("初始化加载失败：" + e.message, "err"));
