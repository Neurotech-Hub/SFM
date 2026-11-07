// What a node looks like, and where it goes on the map. Pure functions: no DOM.

const ACCENT = "#2D4BD8";

// status (the engine's NodeState.status_label) -> [text, colour, dispense progress %]
export const STATES = {
  OFFLINE:    ["Offline", "#566070", 0],
  IDLE:       ["Idle", "#566070", 0],
  SEEKING:    ["Homing", ACCENT, 25],
  LOWERING:   ["Lowering", ACCENT, 25],
  LOADING:    ["Loading", ACCENT, 50],
  DWELLING:   ["Dwelling", ACCENT, 50],
  DOMEHOLD:   ["Waiting for dome", ACCENT, 60],
  RAISING:    ["Raising", ACCENT, 75],
  RETRACTING: ["Retracting", ACCENT, 75],
  LOADED:     ["Pellet ready", "#006A87", 100],
  EMPTY:      ["Empty plate up", "#8A5A00", 100],
  FAULT:      ["Fault", "#B3261E", 100],
};

export function describeNode(node) {
  const [text, color, progress] = STATES[node.status] || [node.status || "Unknown", "#566070", 0];
  return { text, color, progress, fault: node.status === "FAULT" || (node.fault && node.fault !== "Ok"), offline: !node.online };
}

export function nodeName(node) {
  return node.label && node.label.trim() ? node.label.trim() : `Node ${node.id}`;
}

/** Slot centres, as percentages of the map, for n nodes. */
export function autoSlots(n) {
  const cols = n <= 3 ? Math.max(1, n) : n <= 4 ? 2 : n <= 6 ? 3 : n <= 8 ? 4 : n <= 9 ? 3 : 4;
  const rows = Math.max(1, Math.ceil(n / cols));
  const out = [];
  for (let i = 0; i < n; i++) {
    const r = Math.floor(i / cols), c = i % cols;
    const inRow = Math.min(cols, n - r * cols);           // centre a short last row
    const x = ((c + 0.5 + (cols - inRow) / 2) / cols) * 100;
    // Rows are spaced for a node's height (ring plus labels), not spread evenly.
    const top = rows >= 3 ? 17 : rows === 2 ? 30 : 46, bottom = rows >= 3 ? 76 : rows === 2 ? 66 : 46;
    const y = rows === 1 ? top : top + (r / (rows - 1)) * (bottom - top);
    out.push([clamp(x, 10, 90), y]);
  }
  return out;
}

export function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

/** Keep a dragged position inside the map. */
export function clampPosition(x, y) { return [clamp(x, 6, 94), clamp(y, 10, 88)]; }

/**
 * Where each node sits: its saved position if it has one, else a free automatic slot.
 * Returns {id: [x, y]}.
 */
export function placeNodes(nodes, overrides = {}) {
  const saved = {};
  const loose = [];
  for (const n of nodes) {
    const pos = overrides[n.id] || (n.position ? [n.position.x * 100, n.position.y * 100] : null);
    if (pos) saved[n.id] = pos; else loose.push(n.id);
  }
  const slots = autoSlots(nodes.length);
  const near = (p) => Object.values(saved).some((q) => Math.abs(q[0] - p[0]) < 14 && Math.abs(q[1] - p[1]) < 18);
  const free = slots.filter((p) => !near(p));
  const rest = slots.filter((p) => near(p));
  const pool = free.concat(rest);
  const out = { ...saved };
  loose.forEach((id, i) => { out[id] = pool[i] || pool[pool.length - 1] || [50, 50]; });
  return out;
}

/** Ring scale, so twelve nodes still fit. The text keeps a readable size. */
export function nodeScale(n) { return n <= 4 ? 1 : n <= 8 ? 0.72 : 0.56; }

/** Remembers which node a mouse stood on last, and the one before it. */
export class PadTracker {
  constructor() { this.last = null; this.prev = null; this.seen = new Map(); }
  update(nodes) {
    for (const n of nodes) {
      const was = this.seen.get(n.id) || false;
      if (n.presence && !was && n.id !== this.last) { this.prev = this.last; this.last = n.id; }
      this.seen.set(n.id, !!n.presence);
    }
  }
  reset() { this.last = null; this.prev = null; this.seen.clear(); }
}

/** The role a node plays in the chosen experiment, for the chip on the map. */
export function roleOf(experiment, values, nodeId) {
  if (!experiment) return "";
  for (const p of experiment.parameters) {
    if (!["nodes", "node_number", "node_choice"].includes(p.type)) continue;
    const map = effectiveNodeValues(p, values && values[p.key], nodeId);
    if (map === undefined) return "";
    if (p.type === "nodes") return map ? "" : "Not in task";
    if (p.type === "node_choice") return String(map);
    if (p.type === "node_number") return map > 0 ? `${map}%` : "Not in task";
  }
  return "";
}

function effectiveNodeValues(param, raw, nodeId) {
  const defaults = param.node_defaults || {};
  let v = defaults[String(nodeId)];
  if (raw != null) {
    if (Array.isArray(raw)) v = raw.map(Number).includes(Number(nodeId));
    else if (raw[String(nodeId)] !== undefined) v = raw[String(nodeId)];
  }
  return v;
}
