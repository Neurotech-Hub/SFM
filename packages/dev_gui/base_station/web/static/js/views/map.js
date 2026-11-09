// The cage map: one ring per node, draggable when arranging.
import { h, s, setAttr, setStyle, setText, show } from "../dom.js";
import { PadTracker, clampPosition, describeNode, nodeName, nodeScale, placeNodes, roleOf } from "../nodes.js";

const CIRC = 2 * Math.PI * 50;

function ring() {
  const halo = s("circle", { class: "halo", cx: 56, cy: 56, r: 52, fill: "none", stroke: "#B5561A", "stroke-width": 3 });
  const base = s("circle", { cx: 56, cy: 56, r: 50, fill: "none", stroke: "#DDE1E8", "stroke-width": 9 });
  const arc = s("circle", { cx: 56, cy: 56, r: 50, fill: "none", stroke: "#566070", "stroke-width": 9, "stroke-dasharray": `0 ${CIRC}`, transform: "rotate(-90 56 56)" });
  const disc = s("circle", { cx: 56, cy: 56, r: 46, fill: "#FFFFFF", stroke: "#E4E7EC", "stroke-width": 1, class: "sel" });
  const plate = s("circle", { cx: 56, cy: 56, r: 29, fill: "#E9ECF1", stroke: "transparent", "stroke-width": 3 });
  const pellet = s("circle", { cx: 56, cy: 56, r: 9, fill: "#1A2030", opacity: 0 });
  const svg = s("svg", { class: "ring", viewBox: "0 0 112 112", "aria-hidden": "true" }, halo, base, arc, disc, plate, pellet);
  return { svg, halo, base, arc, plate, pellet };
}

export function createMap(ctx, rs) {
  const tracker = new PadTracker();
  const overrides = {};
  const store = new Map();
  let drag = null, warnedUnsaved = false;

  const trailLine = s("line", { x1: "0%", y1: "0%", x2: "0%", y2: "0%" });
  const trail = s("svg", { class: "trail", "aria-hidden": "true" }, trailLine);
  const chip = h("div", { class: "mousechip", hidden: true });
  const empty = h("div", { class: "empty-map" }, "No nodes yet. Connect to the bus first.");
  const legend = h("div", { class: "legend" },
    h("span", {}, h("i", { style: { border: "3px solid #B5561A" } }), "Mouse"),
    h("span", {}, h("i", { style: { background: "#1A2030" } }), "Pellet on plate"),
    h("span", {}, h("i", { style: { border: "2px solid #1A2030" } }), "Dome open"),
    h("span", {}, h("i", { style: { background: "conic-gradient(#2D4BD8 60%, #DDE1E8 0)" } }), "Dispense cycle progress"));
  const el = h("div", { class: "map", role: "group", "aria-label": "Rig map" }, trail, empty, chip, legend);

  function posOf(e) {
    const r = el.getBoundingClientRect();
    return clampPosition(((e.clientX - r.left) / r.width) * 100, ((e.clientY - r.top) / r.height) * 100);
  }

  async function drop(id) {
    const pos = overrides[id];
    if (!pos) return;
    const r = await ctx.command("set_node_position", { node: id, x: pos[0] / 100, y: pos[1] / 100 });
    if (!r.ok) { ctx.toast(r.error, "error"); delete overrides[id]; update(); return; }
    if (r.saved === false && !warnedUnsaved) {
      warnedUnsaved = true;
      ctx.toast("This node's position is kept until the page closes. It is saved once the node's MAC is known.", "warning");
    }
  }

  function make(node) {
    const r = ring();
    const name = h("span", { class: "name" }), state = h("span", { class: "state" }), role = h("span", { class: "role" });
    const scale = h("span", { class: "scale" }, r.svg, h("span", { class: "col", style: { alignItems: "center", gap: "0" } }, name, state), role);
    const btn = h("button", { class: "node", type: "button", "aria-pressed": "false",
      onClick: () => { if (!drag) rs.set({ selected: node.id, tab: "node" }); },
      onPointerDown: (e) => {
        rs.set({ selected: node.id, tab: "node" });
        if (!rs.get().arranging) return;
        btn.setPointerCapture(e.pointerId);
        drag = { id: node.id, pid: e.pointerId };
        btn.classList.add("drag");
        e.preventDefault();
      },
      onPointerMove: (e) => {
        if (!drag || drag.id !== node.id) return;
        overrides[node.id] = posOf(e);
        update();
      },
      onPointerUp: (e) => {
        if (!drag || drag.id !== node.id) return;
        btn.releasePointerCapture(e.pointerId);
        btn.classList.remove("drag");
        const id = drag.id;
        setTimeout(() => { drag = null; }, 0);   // swallow the click that follows a drop
        drop(id);
      },
      onKeyDown: (e) => {
        if (!rs.get().arranging) return;
        const step = { ArrowLeft: [-2, 0], ArrowRight: [2, 0], ArrowUp: [0, -2], ArrowDown: [0, 2] }[e.key];
        if (!step) return;
        e.preventDefault();
        const cur = currentPos[node.id] || [50, 50];
        overrides[node.id] = clampPosition(cur[0] + step[0], cur[1] + step[1]);
        update();
        clearTimeout(btn._t); btn._t = setTimeout(() => drop(node.id), 400);
      },
    }, scale);
    return { el: btn, update: (n, pos, exp, values) => {
      const d = describeNode(n);
      setStyle(btn, "left", `${pos[0]}%`); setStyle(btn, "top", `${pos[1]}%`);
      setAttr(btn, "aria-pressed", rs.get().selected === n.id ? "true" : "false");
      setAttr(btn, "aria-label", `${nodeName(n)}, ${d.text}${n.presence ? ", mouse on pad" : ""}`);
      setText(name, nodeName(n)); setText(state, d.text); state.style.color = d.color;
      setText(role, roleOf(exp, values, n.id));
      show(r.halo, !!n.presence);
      const full = d.fault;
      setAttr(r.arc, "stroke", full ? "#B3261E" : d.color);
      setAttr(r.arc, "stroke-dasharray", full ? `${CIRC} 0` : `${(d.progress / 100) * CIRC} ${CIRC}`);
      setAttr(r.base, "stroke", d.offline ? "#E4E7EC" : "#DDE1E8");
      setAttr(r.plate, "fill", d.offline ? "#F2F3F6" : "#E9ECF1");
      setAttr(r.plate, "stroke", n.dome_open ? "#1A2030" : "transparent");
      setAttr(r.pellet, "opacity", n.pellet ? 1 : 0);
    } };
  }

  const currentPos = {};
  function update() {
    const { state } = ctx.engine.get();
    const nodes = state ? state.nodes : [];
    show(empty, nodes.length === 0);
    el.classList.toggle("arranging", !!rs.get().arranging);
    el.classList.toggle("dense", nodes.length > 4);
    setStyle(el, "--s", nodeScale(nodes.length));
    tracker.update(nodes);
    // An override has done its job once the saved position matches it.
    for (const n of nodes) {
      const o = overrides[n.id];
      if (o && n.position && Math.abs(n.position.x * 100 - o[0]) < 0.6 && Math.abs(n.position.y * 100 - o[1]) < 0.6) delete overrides[n.id];
    }
    const pos = placeNodes(nodes, overrides);
    Object.assign(currentPos, pos);
    const { exp, values } = rs.get().setupView || {};
    reconcileNodes(nodes, pos, exp, values);

    // Where the mouse is (between every pad that senses it), or where it stood
    // last, and the line from the one before.
    const last = nodes.find((n) => n.id === tracker.last), prev = nodes.find((n) => n.id === tracker.prev);
    const onPads = nodes.filter((n) => n.presence && pos[n.id]);
    show(chip, onPads.length > 0 || !!last);
    if (onPads.length || last) {
      const at = onPads.length ? onPads.map((n) => pos[n.id]) : [pos[last.id]];
      const p = [0, 1].map((k) => at.reduce((sum, q) => sum + q[k], 0) / at.length);
      // Sit just above the node's block (ring plus labels), whatever size the ring is.
      setStyle(chip, "left", `${p[0]}%`); setStyle(chip, "top", `calc(${p[1]}% - ${Math.round((112 * nodeScale(nodes.length) + 62) / 2) + 2}px)`);
      setText(chip, onPads.length ? "Mouse" : "Mouse last here");
    }
    const a = last && pos[last.id], b = prev && pos[prev.id];
    show(trail, !!(a && b));
    if (a && b) { setAttr(trailLine, "x1", `${b[0]}%`); setAttr(trailLine, "y1", `${b[1]}%`); setAttr(trailLine, "x2", `${a[0]}%`); setAttr(trailLine, "y2", `${a[1]}%`); }
  }

  function reconcileNodes(nodes, pos, exp, values) {
    // Nodes are children of the map, before the legend, in id order.
    for (const n of nodes) {
      let entry = store.get(n.id);
      if (!entry) { entry = make(n); store.set(n.id, entry); el.insertBefore(entry.el, chip); }
      entry.update(n, pos[n.id], exp, values);
    }
    for (const [id, entry] of [...store]) if (!nodes.some((n) => n.id === id)) { entry.el.remove(); store.delete(id); }
  }

  return { el, update, resetTrack: () => tracker.reset(), resetLayoutOverrides: () => { for (const k of Object.keys(overrides)) delete overrides[k]; } };
}
