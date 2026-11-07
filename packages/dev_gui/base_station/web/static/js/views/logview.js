// The event log. Rows come from the engine in batches ("rows after N"); only a capped window is kept
// and drawn. The full record is the CSV on disk.
import { h, setText, show } from "../dom.js";
import { nodeName } from "../nodes.js";

const KEEP = 400;      // rows held in memory
const DRAW = 200;      // rows drawn
const TYPES = ["EVENT", "COMMAND", "DISCOVERY", "BNC", "SYNC", "WARNING", "EXPERIMENT", "ERROR"];
const COLS = ["Time", "Node", "Dir", "Type", "Event", "ID", "Data", "Details"];

export function createLog(ctx) {
  const m = { rows: [], lastSeq: 0, fetching: false, filterNode: "all", filterType: "all", heartbeats: false, hb: [], hbTimer: null, drawn: new Set(), dirty: true };

  const nodeSel = h("select", { class: "select", "aria-label": "Show node", onChange: (e) => { m.filterNode = e.target.value; m.dirty = true; draw(); } });
  const typeSel = h("select", { class: "select", "aria-label": "Show event type", onChange: (e) => { m.filterType = e.target.value; m.dirty = true; draw(); } },
    h("option", { value: "all" }, "All types"), ...TYPES.map((t) => h("option", { value: t }, t)));
  const hbBox = h("input", { type: "checkbox", id: "log-hb", onChange: (e) => { m.heartbeats = e.target.checked; m.dirty = true; if (m.heartbeats) pollHeartbeats(); draw(); } });
  const count = h("span", { class: "small muted", role: "status" });
  const tbody = h("tbody", {});
  const latest = h("span", { class: "muted", style: { fontWeight: 400 } });
  const el = h("details", { class: "logbox", onToggle: () => { if (el.open) { m.dirty = true; draw(); } } },
    h("summary", {}, h("span", {}, "Event log"), latest),
    h("div", { class: "logtools" },
      h("label", { class: "small" }, "Node "), nodeSel, typeSel,
      h("label", { class: "check small", for: "log-hb", style: { minHeight: "36px" } }, hbBox, "Show heartbeats"),
      h("div", { class: "grow" }), count,
      h("button", { class: "btn", type: "button", onClick: clear }, "Clear"),
      h("button", { class: "btn", type: "button", onClick: exportRows }, "Export")),
    h("div", { class: "logscroll" },
      h("table", { class: "log" }, h("thead", {}, h("tr", {}, ...COLS.map((c) => h("th", {}, c)))), tbody)));

  async function clear() {
    const r = await ctx.command("log_clear");
    if (!r.ok) { ctx.toast(r.error, "error"); return; }
    m.rows = []; m.hb = []; m.dirty = true; draw();
  }

  async function exportRows() {
    const r = await ctx.command("log_export");
    ctx.toast(r.ok ? `Exported to ${r.path}` : r.error, r.ok ? "info" : "error");
  }

  async function fetchNew() {
    if (m.fetching) return;
    m.fetching = true;
    try {
      // Keep asking while a full batch comes back, so a burst isn't left behind.
      for (let i = 0; i < 5; i++) {
        const r = await ctx.command("log_rows", { after: m.lastSeq, limit: 500 });
        if (!r.ok) return;
        if (r.rows.length) {
          m.rows.push(...r.rows);
          if (m.rows.length > KEEP) m.rows.splice(0, m.rows.length - KEEP);
          m.lastSeq = r.rows[r.rows.length - 1].seq;
          m.dirty = true;
        } else if (r.last_seq < m.lastSeq) { m.lastSeq = r.last_seq; }
        if (r.rows.length < 500) break;
      }
    } finally { m.fetching = false; }
    draw();
  }

  async function pollHeartbeats() {
    clearTimeout(m.hbTimer);
    if (!m.heartbeats) return;
    const r = await ctx.command("heartbeat_rows");
    if (r.ok) { m.hb = r.rows; m.dirty = true; draw(); }
    m.hbTimer = setTimeout(pollHeartbeats, 2000);
  }

  function visibleRows() {
    const node = m.filterNode === "all" ? null : Number(m.filterNode);
    const rows = (m.heartbeats ? m.rows.concat(m.hb) : m.rows)
      .filter((r) => (node === null || r.node === node) && (m.filterType === "all" || r.type === m.filterType));
    if (m.heartbeats) rows.sort((a, b) => a.ts - b.ts);
    return rows;
  }

  function rowEl(r) {
    const tr = h("tr", { class: r.type === "WARNING" || r.type === "ERROR" ? "warn" : "" },
      h("td", { class: "dim" }, r.time), h("td", {}, r.node === 0 ? "All" : nodeNameById(r.node)), h("td", {}, r.dir),
      h("td", { class: "dim" }, r.type), h("td", {}, r.event), h("td", { class: "dim" }, r.id), h("td", { class: "dim" }, r.data), h("td", {}, r.details));
    return tr;
  }
  const nodeNameById = (id) => { const n = ctx.engine.get().state?.nodes.find((x) => x.id === id); return n ? nodeName(n) : `Node ${id}`; };

  function draw() {
    const rows = visibleRows();
    const last = m.rows[m.rows.length - 1];
    setText(latest, last ? `${last.time} · ${last.event}` : "");
    if (!el.open) return;
    if (m.dirty) {
      m.dirty = false;
      const shown = rows.slice(-DRAW).reverse();       // newest first
      tbody.replaceChildren(...shown.map(rowEl));
      setText(count, `Showing ${shown.length} of ${rows.length}${m.rows.length >= KEEP ? " (older rows are in the CSV)" : ""}`);
    }
  }

  function update() {
    const state = ctx.engine.get().state;
    if (!state) return;
    // Node filter options follow the nodes.
    const want = ["all", ...state.nodes.map((n) => String(n.id)), "0"];
    if (nodeSel.options.length !== want.length || [...nodeSel.options].some((o, i) => o.value !== want[i])) {
      nodeSel.replaceChildren(h("option", { value: "all" }, "All nodes"),
        ...state.nodes.map((n) => h("option", { value: String(n.id) }, nodeName(n))), h("option", { value: "0" }, "Base station"));
      nodeSel.value = want.includes(m.filterNode) ? m.filterNode : "all";
    }
    if (state.log.last_seq !== m.lastSeq && state.log.last_seq > m.lastSeq) fetchNew();
    else if (state.log.last_seq < m.lastSeq) { m.lastSeq = state.log.last_seq; }
    draw();
  }

  return { el, update, recentFor: (id, n = 12) => m.rows.filter((r) => r.node === id).slice(-n).reverse(), latest: () => m.rows[m.rows.length - 1] };
}
