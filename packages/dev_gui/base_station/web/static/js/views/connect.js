// Step 1: choose the bus and where logs go, connect, and see which nodes answered.
import { h, s, setAttr, setDisabled, setText, show, reconcile } from "../dom.js";
import { nodeName } from "../nodes.js";

const CHECK = "M5 12.5l4.5 4.5L19 7.5", CROSS = "M7 7l10 10M17 7L7 17";
const IFACES = ["can0", "vcan0"], DEFAULT_IFACE = "can0", RESCAN_MS = 3000;
const FAULTS = [["PelletLost", "Pellet lost"], ["FeedTimeout", "Feed timeout"], ["ActuatorTimeout", "Actuator timeout"]];
const stripSlash = (p) => String(p || "").trim().replace(/\/+$/, "");

export function createConnect(ctx) {
  const model = { iface: DEFAULT_IFACE, numNodes: 2, logDir: "", autoSave: true, scanned: [], connected: false, checkMsg: null, busy: false, seeded: false,
    dirTouched: false, dirMsg: null, notify: { loaded: false, touched: false, msg: null } };

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

  const logDir = h("input", { class: "input mono", id: "log-dir", onInput: (e) => { model.logDir = e.target.value; model.dirTouched = true; model.dirMsg = null; update(); } });
  const useDrive = h("button", { class: "btn", type: "button", onClick: () => { model.logDir = ctx.ui.get().defaults?.log_dir || ""; logDir.value = model.logDir; model.dirTouched = true; model.dirMsg = null; update(); } }, "Use external drive");
  const applyDir = h("button", { class: "btn primary", type: "button", onClick: doApplyDir }, "Apply");
  const dirNote = h("p", { class: "small", role: "status", id: "log-dir-note" });
  const autoNote = h("p", { class: "small muted" }, "Saving the activity log automatically is chosen when you connect. Use Change connection to change it.");
  const autoSave = h("input", { type: "checkbox", id: "auto-save", onChange: (e) => { model.autoSave = e.target.checked; } });
  autoSave.checked = true;

  // ---- phone notifications (ntfy) ----
  const nEnabled = h("input", { type: "checkbox", id: "notify-enabled" });
  const nServer = h("input", { class: "input mono", id: "notify-server", autocomplete: "off", spellcheck: "false", placeholder: "https://ntfy.sh" });
  const nTopic = h("input", { class: "input mono", id: "notify-topic", autocomplete: "off", spellcheck: "false", placeholder: "e.g. sfm-lab-a-7Qx2kP9wLm" });
  const nStation = h("input", { class: "input", id: "notify-station", autocomplete: "off", maxlength: "64", placeholder: "e.g. Lab A rig 1" });
  const nFaults = FAULTS.map(([key, label]) => {
    const box = h("input", { type: "checkbox", id: `notify-fault-${key}` });
    return { key, box, el: h("label", { class: "check", for: `notify-fault-${key}` }, box, label) };
  });
  const nSave = h("button", { class: "btn primary", type: "button", id: "notify-save", onClick: () => saveNotify(false) }, "Save");
  const nTest = h("button", { class: "btn", type: "button", id: "notify-test", onClick: () => saveNotify(true) }, "Send test");
  const nRandom = h("button", { class: "btn", type: "button", id: "notify-random", onClick: makeTopic }, "Make a random topic");
  const nStatus = h("p", { class: "small", role: "status", id: "notify-status" });
  const nHint = h("p", { class: "small", id: "notify-hint" });
  const nKept = h("p", { class: "tiny muted", id: "notify-kept" });
  for (const c of [nEnabled, nServer, nTopic, nStation, ...nFaults.map((f) => f.box)]) {
    c.addEventListener("input", () => { model.notify.touched = true; });
    c.addEventListener("change", () => { model.notify.touched = true; });
  }

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
        h("div", { class: "row", style: { flexWrap: "wrap" } }, h("div", { style: { flex: "1 1 320px" } }, logDir), useDrive, applyDir)),
      dirNote,
      h("label", { class: "check", for: "auto-save" }, autoSave, "Save the log to CSV automatically"),
      autoNote,
      h("p", { class: "small muted" }, "Each session is written to its own file in this folder. The name is chosen in step 2. Changing the folder moves new sessions and the daily log there; files already written stay where they are.")),
    notifyCard(),
    h("div", { class: "row", style: { justifyContent: "flex-end" } }, next),
  );

  function notifyCard() {
    return h("section", { class: "card col", "aria-labelledby": "notify-h" },
      h("h2", { id: "notify-h" }, "Phone notifications"),
      h("p", { class: "small muted" }, "Get an alert on your phone when a node faults. Install the free ntfy app (iOS or Android) and subscribe to your topic. The topic is the password: pick a long, random one, and a different one for each lab. Nothing is sent until a topic is saved."),
      h("label", { class: "check", for: "notify-enabled" }, nEnabled, "Send phone alerts"),
      h("div", { class: "field" }, h("label", { class: "lab", for: "notify-topic" }, "Topic"),
        h("div", { class: "row", style: { flexWrap: "wrap" } }, h("div", { style: { flex: "1 1 320px" } }, nTopic), nRandom),
        h("p", { class: "tiny muted" }, "Letters, digits, - and _ only.")),
      h("div", { class: "field" }, h("label", { class: "lab", for: "notify-station" }, "User name"), nStation,
        h("p", { class: "tiny muted" }, "Shown in the title of each alert, so you can tell rigs apart.")),
      h("fieldset", { style: { border: 0, padding: 0, margin: 0 } },
        h("legend", { class: "lab" }, "Send an alert for"), h("div", { class: "row" }, ...nFaults.map((f) => f.el)),
        h("p", { class: "tiny muted" }, "The same fault on the same node is sent at most once a minute.")),
      h("div", { class: "field" }, h("label", { class: "lab", for: "notify-server" }, "Server"), nServer,
        h("p", { class: "tiny muted" }, "Leave empty to use https://ntfy.sh. Only change it if your lab runs its own ntfy server.")),
      h("div", { class: "row" }, nSave, nTest), nStatus, nHint, nKept);
  }

  function gatherNotify() {
    return { enabled: nEnabled.checked, server: nServer.value, topic: nTopic.value, station_name: nStation.value,
      faults: nFaults.filter((f) => f.box.checked).map((f) => f.key) };
  }

  async function loadNotify() {
    const r = await ctx.command("get_notify_settings");
    if (!r.ok) { model.notify.msg = { ok: false, text: r.error }; update(); return; }
    model.notify.loaded = true;
    const kept = r.kept || { keys: [], faults: [] }, parts = [...kept.keys, ...kept.faults.map((f) => `fault ${f}`)];
    setText(nKept, parts.length ? `The settings file also holds ${parts.join(", ")}. This page doesn't show ${parts.length === 1 ? "it" : "them"}, and saving keeps ${parts.length === 1 ? "it" : "them"} as they are.` : "");
    if (!model.notify.touched) {                       // never overwrite what is being typed
      const st = r.settings;
      nEnabled.checked = st.enabled; nServer.value = st.server === "https://ntfy.sh" ? "" : st.server;
      nTopic.value = st.topic; nStation.value = st.station_name;
      for (const f of nFaults) f.box.checked = st.faults.includes(f.key);
    }
    update();
  }

  async function saveNotify(thenTest) {
    const startedAt = Date.now() / 1000;                // before anything is sent, so the result can't look older than the click
    const saved = await ctx.command("set_notify_settings", gatherNotify());
    if (!saved.ok) { model.notify.msg = { ok: false, text: saved.error }; update(); return; }
    model.notify.touched = false;
    if (!thenTest) {
      model.notify.msg = { ok: true, text: saved.has_topic ? "Saved." : "Saved. Add a topic to send alerts." };
      update(); return;
    }
    const test = await ctx.command("send_test_notification");
    // The send runs in the background; its result arrives in the engine state a moment later.
    model.notify.msg = test.ok ? { ok: null, text: "Sending test…", at: startedAt } : { ok: false, text: test.error };
    update();
  }

  function makeTopic() {
    const slug = nStation.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 20) || "lab";
    const chars = "abcdefghijklmnopqrstuvwxyz0123456789", bytes = new Uint8Array(12);
    crypto.getRandomValues(bytes);
    nTopic.value = `sfm-${slug}-${[...bytes].map((b) => chars[b % chars.length]).join("")}`;
    model.notify.touched = true; model.notify.msg = null;
    update();
  }

  function updateNotify(state, up) {
    if (!up) model.notify.loaded = false;
    else if (!model.notify.loaded) { model.notify.loaded = "loading"; loadNotify(); }
    for (const c of [nEnabled, nServer, nTopic, nStation, nSave, nTest, nRandom, ...nFaults.map((f) => f.box)]) setDisabled(c, !up);
    // What to say: the latest save/test note, or the result of the test that was just sent.
    const n = state && state.notify, note = model.notify.msg;
    let text = note ? note.text : "", ok = note ? note.ok : null;
    if (note && note.at && n && n.last && n.last.when >= note.at) {
      const when = new Date(n.last.when * 1000).toLocaleTimeString([], { hour12: false });
      text = n.last.ok ? `Test sent at ${when}. Check your phone.` : `Test failed at ${when}: ${n.last.detail}`;
      ok = n.last.ok;
    }
    setText(nStatus, text);
    nStatus.className = "small " + (ok === true ? "ok-text" : ok === false ? "err-text" : "muted");
    const hint = !up ? "Waiting for the engine." : n && !n.configured ? "No topic is saved, so no alerts are sent yet." : n && !n.enabled ? "Alerts are switched off." : "";
    setText(nHint, hint);
    nHint.className = "small " + (!up ? "muted" : "note");
  }

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

  async function doApplyDir() {
    const r = await ctx.command("set_log_dir", { path: model.logDir });
    if (!r.ok) { model.dirMsg = { ok: false, text: r.error }; update(); return; }
    model.dirTouched = false;
    model.dirMsg = { ok: true, text: `Logging to ${r.log_dir} from now on.` };
    ctx.command("set_gui_settings", { log_dir: r.log_dir });
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
      // Show the folder the engine is really writing to, unless the operator is editing it.
      if (!model.dirTouched && state.log && state.log.dir && model.logDir !== state.log.dir) { model.logDir = state.log.dir; logDir.value = model.logDir; }
    } else { model.dirMsg = null; }
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

    // The folder stays editable once connected, except while a session runs (its file is open in the old folder).
    const running = !!(connected && state.session.running);
    setDisabled(logDir, running); setDisabled(useDrive, running); setDisabled(autoSave, connected);
    const active = connected && state.log ? state.log.dir : "";
    show(applyDir, connected && !running && stripSlash(model.logDir) !== stripSlash(active));
    show(autoNote, connected);
    const dm = model.dirMsg;
    setText(dirNote, running ? "Stop the session to change the log folder." : dm ? dm.text : connected && active ? `Writing to ${active}` : "");
    dirNote.className = "small " + (running ? "note" : dm ? (dm.ok ? "ok-text" : "err-text") : "muted");
    updateNotify(state, up);
    show(connectedBanner, connected);
    show(discoveryBox, connected);
    show(checkBtn.parentElement, !connected);
    show(next, connected);
    if (connected) {
      setText(document.getElementById("conn-msg"), `${state.interface || "The bus"} · ${state.nodes.length} ${state.nodes.length === 1 ? "node" : "nodes"} expected. Use Change connection to pick another interface, node count or log folder.`);
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
