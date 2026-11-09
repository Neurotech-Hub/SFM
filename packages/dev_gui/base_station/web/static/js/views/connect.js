// Step 1: choose the bus and where logs go, connect, and see which nodes answered.
import { h, s, setAttr, setDisabled, setText, show, reconcile } from "../dom.js";
import { nodeName } from "../nodes.js";

const CHECK = "M5 12.5l4.5 4.5L19 7.5", CROSS = "M7 7l10 10M17 7L7 17";
const IFACES = ["can0", "vcan0"], DEFAULT_IFACE = "can0", RESCAN_MS = 3000;

export function createConnect(ctx) {
  const model = { iface: DEFAULT_IFACE, numNodes: 2, logDir: "", autoSave: true, scanned: [], connected: false, checkMsg: null, busy: false, seeded: false };

  const ifaceList = h("div", { class: "opts", role: "radiogroup", "aria-labelledby": "can-h" });
  const checkStatus = h("span", { role: "status", class: "small" });
  const bitrateRow = h("p", { class: "small muted" });
  const countOut = h("output", { class: "mono", style: { minWidth: "40px", textAlign: "center", fontSize: "20px", fontWeight: "500" }, "aria-live": "polite" });
  const minus = h("button", { class: "btn square", type: "button", "aria-label": "Fewer nodes", onClick: () => bump(-1) }, "−");
  const plus = h("button", { class: "btn square", type: "button", "aria-label": "More nodes", onClick: () => bump(+1) }, "+");
  const connectBtn = h("button", { class: "btn primary", type: "button", onClick: doConnect }, "Connect");
  const checkBtn = h("button", { class: "btn", type: "button", onClick: doCheck }, "Check");
  const rescanBtn = h("button", { class: "btn", type: "button", onClick: rescan }, "Re-scan interfaces");
  const changeBtn = h("button", { class: "btn", type: "button", onClick: doChange }, "Change connection");
  const connectNote = h("p", { class: "small", role: "status" });

  const connectedBanner = h("div", { class: "banner info", role: "status" },
    h("strong", {}, "Connected"), h("span", { class: "msg", id: "conn-msg" }), changeBtn);
  const nodeCount = h("div", { class: "row", style: { justifyContent: "space-between", padding: "10px 12px", background: "var(--surface)", borderRadius: "10px" } },
    h("div", { class: "col", style: { gap: "2px" } },
      h("span", { id: "count-l", style: { fontWeight: 600 } }, "Number of nodes"),
      h("span", { class: "small muted" }, "Feeder nodes on this rig, 1 to 12. Connecting waits for each one to answer.")),
    h("div", { class: "row tight", role: "group", "aria-labelledby": "count-l" }, minus, countOut, plus));

  const found = h("ul", { class: "col tight", "aria-label": "Nodes" });
  const foundStore = new Map();
  const foundSummary = h("p", { class: "small", role: "status" });
  const rediscoverBtn = h("button", { class: "btn", type: "button", onClick: doRediscover }, "Re-discover nodes");
  const gpioLine = h("p", { class: "small" });
  const discoveryBox = h("div", { class: "col" }, foundSummary, found, h("div", { class: "row" }, rediscoverBtn), gpioLine);

  const logDir = h("input", { class: "input mono", id: "log-dir", onInput: (e) => { model.logDir = e.target.value; } });
  const useDrive = h("button", { class: "btn", type: "button", onClick: () => { model.logDir = ctx.ui.get().defaults?.log_dir || ""; logDir.value = model.logDir; } }, "Use external drive");
  const autoSave = h("input", { type: "checkbox", id: "auto-save", onChange: (e) => { model.autoSave = e.target.checked; } });
  autoSave.checked = true;

  const next = h("button", { class: "btn primary big", type: "button", onClick: () => ctx.go("experiment") }, "Continue to experiment");

  const el = h("main", { id: "view-connect", class: "page", style: { maxWidth: "920px" } },
    h("div", {}, h("h1", {}, "Connect"), h("p", { class: "muted" }, "Pick the CAN interface, choose where the log is saved, and check which nodes answer.")),
    h("section", { class: "card col", "aria-labelledby": "can-h" },
      h("div", { class: "row", style: { justifyContent: "space-between" } }, h("h2", { id: "can-h" }, "CAN interface"), rescanBtn),
      ifaceList,
      h("div", { class: "row" }, checkBtn, checkStatus),
      bitrateRow, nodeCount,
      h("div", { class: "row" }, connectBtn, connectNote),
      connectedBanner, discoveryBox),
    h("section", { class: "card col", "aria-labelledby": "log-h" },
      h("h2", { id: "log-h" }, "Log directory"),
      h("div", { class: "field" }, h("label", { class: "lab", for: "log-dir" }, "Folder"),
        h("div", { class: "row", style: { flexWrap: "wrap" } }, h("div", { style: { flex: "1 1 320px" } }, logDir), useDrive)),
      h("label", { class: "check", for: "auto-save" }, autoSave, "Save the log to CSV automatically"),
      h("p", { class: "small muted" }, "Each session is written to its own file in this folder. The name is chosen in step 2.")),
    h("div", { class: "row", style: { justifyContent: "flex-end" } }, next),
  );

  function bump(d) { model.numNodes = Math.min(ctx.ui.get().defaults?.max_nodes || 12, Math.max(1, model.numNodes + d)); update(); }

  async function rescan() {
    const r = await ctx.command("list_interfaces");
    if (!r.ok) { model.checkMsg = { ok: false, text: r.error }; update(); return; }
    model.scanned = r.interfaces;
    update();
  }

  async function doCheck() {
    if (!model.iface) { model.checkMsg = { ok: false, text: "Choose an interface first." }; update(); return; }
    const r = await ctx.command("check_interface", { interface: model.iface });
    model.checkMsg = r.ok && r.up ? { ok: true, text: `${model.iface} is up.` }
      : { ok: false, text: r.ok ? `${model.iface} was not found or is down. See deploy/README.md to bring up the CAN HAT.` : r.error };
    update();
  }

  async function doConnect() {
    if (!model.iface) { model.checkMsg = { ok: false, text: "Choose an interface first." }; update(); return; }
    model.busy = true; update();
    const r = await ctx.command("connect", {
      interface: model.iface, bitrate: ctx.ui.get().defaults?.bitrate || 250000,
      num_nodes: model.numNodes, log_dir: model.logDir, auto_save: model.autoSave,
    });
    model.busy = false;
    if (!r.ok) { model.checkMsg = { ok: false, text: r.error }; update(); return; }
    model.checkMsg = null;
    ctx.command("set_gui_settings", { interface: model.iface, num_nodes: model.numNodes, log_dir: model.logDir, auto_save: model.autoSave });
    update();
  }

  async function doChange() {
    const ok = await ctx.confirm({
      title: "Change the connection?",
      body: ["This closes the CAN bus and the log so you can pick another interface, number of nodes or log folder.",
        "Node IDs and saved positions are kept. You connect again afterwards."],
      confirm: "Change connection",
    });
    if (!ok) return;
    const r = await ctx.command("disconnect");
    if (!r.ok) ctx.toast(r.error, "error");
  }

  async function doRediscover() {
    const ok = await ctx.confirm({
      title: "Re-discover nodes?",
      body: ["This clears the saved ID map and tells every node to forget its ID, then assigns IDs again from 1.",
        "A board can end up with a different ID. Saved map positions follow each board, not its ID."],
      confirm: "Re-discover", danger: true,
    });
    if (!ok) return;
    const r = await ctx.command("rediscover");
    if (!r.ok) ctx.toast(r.error, "error");
  }

  function seed() {
    const settings = ctx.ui.get().settings || {}, defaults = ctx.ui.get().defaults;
    if (model.seeded || !defaults) return;
    model.seeded = true;
    model.numNodes = settings.num_nodes || 2;
    model.logDir = settings.log_dir || defaults.log_dir || "";
    model.autoSave = settings.auto_save !== false;
    if (IFACES.includes(settings.interface)) model.iface = settings.interface;
    logDir.value = model.logDir;
    autoSave.checked = model.autoSave;
    rescan();
    setInterval(() => { const { state, up } = ctx.engine.get(); if (up && !(state && state.connected) && !document.hidden) rescan(); }, RESCAN_MS);
  }

  function update() {
    seed();
    const { state, up } = ctx.engine.get();
    const connected = model.connected = !!(up && state && state.connected);
    const defaults = ctx.ui.get().defaults;

    if (connected) {
      model.iface = state.interface || model.iface;
      model.numNodes = state.nodes.length;
    }
    // Interface choices: always can0 and vcan0, marked by what the last scan found.
    const options = IFACES.map((name) => model.scanned.find((i) => i.name === name)
      || { name, virtual: name.startsWith("v"), missing: true });
    reconcile(ifaceList, options, (i) => i.name, (i) => {
      const name = h("strong", { class: "mono" }, i.name), desc = h("span", { class: "small muted" });
      const b = h("button", { class: "opt", type: "button", role: "radio", onClick: () => { if (!model.connected) { model.iface = i.name; update(); } } }, name, desc);
      return { el: b, update: (it) => {
        setAttr(b, "aria-checked", model.iface === it.name ? "true" : "false");
        setText(desc, (it.virtual ? "Virtual bus, for testing without hardware" : "Hardware CAN")
          + (it.missing ? " · not found" : `${it.bitrate ? " · " + it.bitrate / 1000 + " kbit/s" : ""}${it.up ? " · up" : " · down"}`));
        setDisabled(b, model.connected);
      } };
    }, ifaceStore);
    const cur = model.scanned.find((i) => i.name === model.iface);
    const rate = cur && cur.bitrate ? cur.bitrate : defaults?.bitrate;
    setText(bitrateRow, rate ? `Bitrate ${rate / 1000} kbit/s, set by the system and shown here read-only.` : "");

    setText(countOut, model.numNodes);
    setDisabled(minus, connected || model.numNodes <= 1);
    setDisabled(plus, connected || model.numNodes >= (defaults?.max_nodes || 12));
    setDisabled(checkBtn, connected);
    setDisabled(rescanBtn, connected);
    setDisabled(connectBtn, connected || model.busy || !up);
    setText(connectBtn, model.busy ? "Connecting…" : "Connect");
    show(connectBtn, !connected);

    const msg = model.checkMsg;
    setText(checkStatus, msg ? msg.text : "");
    checkStatus.className = "small " + (msg ? (msg.ok ? "ok-text" : "err-text") : "muted");
    setText(connectNote, msg && !msg.ok && !checkStatus.textContent ? msg.text : "");

    setDisabled(logDir, connected); setDisabled(useDrive, connected); setDisabled(autoSave, connected);
    show(connectedBanner, connected);
    show(discoveryBox, connected);
    show(checkBtn.parentElement, !connected);
    show(next, connected);
    if (connected) {
      setText(document.getElementById("conn-msg"), `${state.interface} · ${state.nodes.length} ${state.nodes.length === 1 ? "node" : "nodes"} expected. Use Change connection to pick another interface, node count or log folder.`);
      if (document.activeElement !== logDir) { logDir.value = model.logDir; }
      const online = state.nodes.filter((n) => n.online).length;
      setText(foundSummary, `${online} of ${state.nodes.length} nodes answered`);
      foundSummary.className = "small " + (online === state.nodes.length ? "ok-text" : "note");
      reconcile(found, state.nodes, (n) => n.id, (n) => {
        const icon = s("path", {}), svg = s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, icon);
        const name = h("strong", { style: { fontWeight: 600 } }), detail = h("span", { class: "mono small" });
        return { el: h("li", { class: "found" }, svg, name, detail), update: (nn) => {
          const ok = nn.online || !!nn.mac;
          setAttr(icon, "d", nn.online ? CHECK : CROSS);
          svg.style.stroke = nn.online ? "var(--teal)" : "var(--amber)";
          setText(name, nodeName(nn));
          setText(detail, `Node ID ${nn.id} · ${nn.mac || "no answer yet"}${ok && !nn.online ? " · offline" : ""}`);
          detail.style.color = nn.online ? "var(--muted)" : "var(--amber)";
        } };
      }, foundStore);
      const gb = state.gpio_backend;
      if (gb === "gpiod") { setText(gpioLine, "BNC I/O: gpiod, with kernel edge timestamps."); gpioLine.className = "small ok-text"; }
      else if (gb) { setText(gpioLine, `BNC I/O: ${gb}. Edge times are stamped in software, so timing is less precise.`); gpioLine.className = "small note"; }
      else { setText(gpioLine, "BNC I/O is simulated: no GPIO was found, so signals will not reach the BNC connectors."); gpioLine.className = "small err-text"; }
    }
  }
  const ifaceStore = new Map();

  return { el, update };
}
