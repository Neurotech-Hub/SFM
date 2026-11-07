// The side panel: Node, Experiment, I/O and Developer tabs.
import { h, setAttr, setDisabled, setText, setValue, show, reconcile } from "../dom.js";
import { describeNode, nodeName, roleOf } from "../nodes.js";
import { seconds, words } from "../format.js";

const MODE_NAMES = { edge: "Edge", pulse: "Pulse", level: "Level", log_only: "Log only" };
const MODE_HELP = {
  edge: "Each edge runs its own action as soon as it arrives.",
  pulse: "A signal that stays active for at least the minimum width counts as one pulse and runs the action. Shorter ones are rejected and logged.",
  level: "The line itself is the signal: going active runs the action, going inactive runs the release action. Arming is refused while the line is already active.",
  log_only: "Every edge is logged with its kernel timestamp and nothing else happens. Use it to wire in an external sync signal.",
};
const OUT_NAMES = {
  legacy: "Default: 500 ms sync pulse at session start",
  off: "Off", session_start_sync: "Session start sync", session_start_stop: "Session start and stop",
  trial: "Trial start and end", node_event: "A node event (from any node)", periodic: "Periodic sync",
};
const OUT_HELP = {
  legacy: "One pulse as a session starts, as the base station always did.",
  off: "BNC OUT stays low. A manual pulse still works.",
  session_start_sync: "One pulse as the session starts. Line it up with the other recorder's clock.",
  session_start_stop: "A pulse at the start, and one twice as wide at the end.",
  trial: "A pulse at the start of each trial, and a double-width one when the session ends.",
  node_event: "A pulse whenever any node sends the chosen event. The time is when the frame reached the base station.",
  periodic: "A pulse at the start and then every interval, so clock drift can be corrected later.",
};

const btn = (label, onClick, cls = "btn", extra = {}) => h("button", { class: cls, type: "button", onClick, ...extra }, label);

export function createPanel(ctx, rs, log) {
  const tabs = [["node", "Node"], ["experiment", "Experiment"], ["io", "I/O"], ["dev", "Developer"]];
  const tabBtns = {};
  const tablist = h("div", { class: "tabs", role: "tablist", "aria-label": "Panel" },
    ...tabs.map(([k, l]) => (tabBtns[k] = h("button", { class: "tab", type: "button", role: "tab", onClick: () => rs.set({ tab: k }) }, l))));
  const panels = { node: nodeTab(), experiment: expTab(), io: ioTab(), dev: devTab() };
  const el = h("aside", { class: "runside" }, tablist, ...Object.values(panels).map((p) => p.el));

  function nodeTab() {
    const name = h("h2", { class: "display", style: { fontSize: "24px", fontWeight: 700 } });
    const stateText = h("span", { style: { fontWeight: 600 } });
    const labelInput = h("input", { class: "input", id: "node-label", maxlength: "32", "aria-label": "Label",
      onChange: async (e) => { const n = sel(); if (!n) return; const r = await ctx.command("set_label", { node: n.id, label: e.target.value }); if (!r.ok) ctx.toast(r.error, "error"); } });
    const pills = h("dl", { class: "pillrow" });
    const pillStore = new Map();
    const dispense = btn("Dispense", () => act("dispense"), "btn primary"), recover = btn("Recover", () => act("recover")), ping = btn("Ping", () => act("ping"));
    const role = h("p", { class: "small" });
    const heartbeat = h("p", { class: "small", id: "node-hb" }), warn = h("p", { class: "small note", role: "status" });
    const recent = h("ol", { class: "recent" }), hw = h("dl", { class: "kv" });
    const empty = h("p", { class: "muted" }, "Select a node on the map.");
    const body = h("div", { class: "col" },
      h("div", {}, name, stateText, heartbeat, warn),
      h("div", { class: "field" }, h("label", { class: "lab small", for: "node-label" }, "Label"), labelInput),
      pills,
      h("div", { style: { display: "grid", gridTemplateColumns: "2fr 1fr 1fr", gap: "8px" } }, dispense, recover, ping),
      role, h("div", { class: "sep" }),
      h("div", { class: "col tight" }, h("h3", {}, "Recent activity"), recent),
      h("details", { class: "more" }, h("summary", {}, "Hardware details"), hw));
    const el = h("div", { class: "tabpanel", role: "tabpanel" }, empty, body);

    const sel = () => ctx.engine.get().state?.nodes.find((n) => n.id === rs.get().selected);
    async function act(cmd) { const n = sel(); if (!n) return; const r = await ctx.command(cmd, { node: n.id }); if (!r.ok) ctx.toast(r.error, "error"); }

    return { el, update() {
      const n = sel();
      show(empty, !n); show(body, !!n);
      if (!n) return;
      const d = describeNode(n);
      setText(name, nodeName(n)); setText(stateText, n.fault && n.fault !== "Ok" ? `${d.text}: ${words(n.fault)}` : d.text); stateText.style.color = d.color;
      setValue(labelInput, n.label || "");
      // Heartbeat age, with the old tile's thresholds: late after 1.5 intervals, overdue after 3.
      const every = Math.max(ctx.engine.get().state.heartbeat_interval_s || 60, 0.1);
      const age = n.heartbeat_age_s;
      setText(heartbeat, age == null ? "No heartbeat yet" : `Last heartbeat ${age} s ago`);
      heartbeat.className = "small " + (age == null || age > every * 3 ? "err-text" : age > every * 1.5 ? "note" : "muted");
      setText(warn, n.dome_open_warning ? "The dome has been open for more than 30 seconds." : "");
      const sensors = [
        ["Presence", n.presence, "On pad", "Clear"], ["Dome", n.dome_open, "Open", "Closed"],
        ["Pellet", n.pellet, "On plate", "None"], ["Load position", n.load_position, "At load", "Away"],
        ["Pellets presented", null, n.pellets_presented ?? "—"], ["Pellets taken", null, n.pellets_taken ?? "—"],
      ].map(([label, on, a, b]) => ({ label, on: on === null ? false : !!on, value: on === null ? String(a) : on ? a : b }));
      reconcile(pills, sensors, (x) => x.label, () => {
        const dt = h("dt", {}), dd = h("dd", {}), box = h("div", { class: "pill" }, dt, dd);
        return { el: box, update: (x) => { setText(dt, x.label); setText(dd, x.value); box.classList.toggle("on", x.on); } };
      }, pillStore);
      setDisabled(dispense, !n.online); setDisabled(recover, !n.online); setDisabled(ping, false);
      const { exp, values } = rs.get().setupView || {};
      const r = exp ? roleOf(exp, values, n.id) : "";
      setText(role, exp && r ? `Role in ${exp.label}: ${r}` : "");
      const rows = log.recentFor(n.id);
      const sig = rows.map((x) => x.seq).join(",");
      if (recent.dataset.sig !== sig) {
        recent.dataset.sig = sig;
        recent.replaceChildren(...(rows.length ? rows.map((x) => h("li", {}, h("span", { class: "t" }, x.time), h("span", {}, x.event))) : [h("li", { class: "muted" }, "Nothing yet")]));
      }
      const info = [["Node ID", n.id], ["MAC", n.mac || "Shown once discovered"], ["Firmware", n.firmware || "Unknown"], ["Discovery", n.discovery],
        ["Presence threshold", n.presence_threshold ?? "Not reported"]];
      const sig2 = JSON.stringify(info);
      if (hw.dataset.sig !== sig2) {
        hw.dataset.sig = sig2;
        hw.replaceChildren(...info.map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", { class: "mono" }, String(v)))));
      }
    } };
  }

  function expTab() {
    const title = h("h3", { class: "display", style: { fontSize: "20px", fontWeight: 700, color: "var(--text)" } });
    const list = h("dl", { class: "kv" });
    const change = btn("Change experiment or parameters", () => ctx.go("experiment"));
    const hint = h("p", { class: "tiny muted" }, "Goes back to step 2 and stops the session.");
    const el = h("div", { class: "tabpanel", role: "tabpanel" }, title, list, change, hint);
    return { el, update() {
      const { exp, values } = rs.get().setupView || {};
      const running = !!ctx.engine.get().state?.session.running;
      show(hint, running);
      if (!exp) { setText(title, "No experiment chosen"); list.replaceChildren(); return; }
      setText(title, exp.label);
      const rows = exp.parameters.map((p) => {
        let v = (values || {})[p.key];
        if (v && typeof v === "object" && !Array.isArray(v)) v = Object.entries(v).map(([id, x]) => `${nodeLabel(id)}: ${p.type === "nodes" ? (x ? "in" : "out") : x}`).join(", ");
        return [p.label, v === undefined || v === "" ? "—" : String(v)];
      });
      const sig = JSON.stringify(rows);
      if (list.dataset.sig !== sig) { list.dataset.sig = sig; list.replaceChildren(...rows.map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", { class: "mono small wrap-any" }, v)))); }
    } };
  }
  const nodeLabel = (id) => { const n = ctx.engine.get().state?.nodes.find((x) => String(x.id) === String(id)); return n ? nodeName(n) : `Node ${id}`; };

  function ioTab() {
    const logPath = h("span", { class: "mono small muted wrap-any" });
    const lockNote = h("p", { class: "small note", role: "status" }, "BNC settings are locked while a session runs.");
    const gpio = h("p", { class: "small" });
    const armLine = h("p", { class: "small", role: "status" });

    const inputs = [0, 1].map(inputCard);
    const out = outCard();
    const pulse = btn("Manual pulse", async () => { const r = await ctx.command("bnc_pulse"); if (!r.ok) ctx.toast(r.error, "error"); });
    const cal = btn("Calibrate presence", () => rs.set({ cal: { phase: "confirm" } }));
    const rediscover = btn("Re-discover nodes", async () => {
      const ok = await ctx.confirm({ title: "Re-discover nodes?", body: ["This clears the saved ID map and reassigns IDs from 1. A board can end up with a different ID."], confirm: "Re-discover", danger: true });
      if (!ok) return;
      const r = await ctx.command("rediscover"); if (!r.ok) ctx.toast(r.error, "error");
    });
    const el = h("div", { class: "tabpanel", role: "tabpanel" },
      h("div", { class: "panelrow", style: { flexDirection: "column", alignItems: "flex-start", gap: "2px" } }, h("span", {}, "Log file"), logPath),
      gpio, lockNote, armLine, ...inputs.map((i) => i.el), out.el, pulse, h("div", { class: "sep" }), cal, rediscover);

    function inputCard(index) {
      const which = index === 0 ? "IN1" : "IN2";
      const dot = h("span", { style: { width: "10px", height: "10px", borderRadius: "50%", display: "inline-block", background: "#C8CDD6" } });
      const levelText = h("span", { class: "mono small muted" });
      const enabled = h("input", { type: "checkbox", id: `bnc-en-${which}`, onChange: (e) => set({ enabled: e.target.checked }) });
      const mode = h("select", { class: "select", "aria-label": "Mode", onChange: (e) => set({ mode: e.target.value }) });
      const level = h("div", { class: "seg", role: "group", "aria-label": "Active level" },
        btn("Active high", () => set({ active_high: true }), "btn", { "aria-pressed": "false" }),
        btn("Active low", () => set({ active_high: false }), "btn", { "aria-pressed": "false" }));
      const width = h("input", { class: "input num", type: "number", min: "0.1", step: "any", "aria-label": "Minimum width in milliseconds",
        onChange: (e) => set({ min_width_ms: Number(e.target.value) }) });
      const actionSel = (key, label) => {
        const sel = h("select", { class: "select", "aria-label": label, onChange: (e) => set({ [key]: e.target.value }) });
        return { sel, key, wrap: h("div", { class: "field" }, h("label", { class: "lab small" }, label), sel) };
      };
      const labelIn = h("input", { class: "input", "aria-label": "Label", maxlength: "32", onChange: (e) => set({ label: e.target.value }) });
      const act = actionSel("action", "Action when active"), rel = actionSel("release_action", "Action when it goes inactive");
      const rise = actionSel("rising_action", "On rising edge"), fall = actionSel("falling_action", "On falling edge");
      const widthWrap = h("div", { class: "field" }, h("label", { class: "lab small" }, "Minimum pulse width (ms)"), width);
      const err = h("p", { class: "small err-text", role: "alert" });
      const modeHelp = h("p", { class: "tiny muted" });
      let optionsFor = null;
      async function set(patch) {
        const r = await ctx.command("set_bnc_input", { which, ...patch });
        setText(err, r.ok ? "" : r.error);
        if (!r.ok) ctx.toast(r.error, "error");
      }
      const el = h("section", { class: "panel card col", style: { padding: "12px", boxShadow: "none", border: "1px solid var(--line)" }, "aria-label": `BNC input ${index}` },
        h("div", { class: "row", style: { justifyContent: "space-between" } }, h("strong", { class: "label" }), h("span", { class: "row tight" }, dot, levelText)),
        h("label", { class: "check", for: `bnc-en-${which}` }, enabled, "Enabled"),
        h("div", { class: "field" }, h("label", { class: "lab small" }, "Label"), labelIn),
        h("div", { class: "field" }, h("label", { class: "lab small" }, "Mode"), mode, modeHelp),
        level, widthWrap, act.wrap, rel.wrap, rise.wrap, fall.wrap, err);
      const title = el.querySelector(".label");
      return { el, update(cfg, locked, defaults) {
        if (!cfg) return;
        if (optionsFor !== defaults) {
          optionsFor = defaults;
          mode.replaceChildren(...defaults.bnc.input_modes.map((m) => h("option", { value: m }, MODE_NAMES[m] || m)));
          for (const a of [act, rel, rise, fall]) a.sel.replaceChildren(...defaults.bnc.input_actions.map((v) => h("option", { value: v }, v === "(none)" ? "(none)" : v.replaceAll("_", " "))));
        }
        setText(title, cfg.label || `BNC IN ${index}`);
        setValue(labelIn, cfg.label || "");
        dot.style.background = cfg.level === null ? "#C8CDD6" : cfg.level ? "#006A87" : "#8A94A4";
        setText(levelText, cfg.level === null ? "no GPIO" : cfg.level ? "high" : "low");
        enabled.checked = cfg.enabled; setValue(mode, cfg.mode); setText(modeHelp, MODE_HELP[cfg.mode] || "");
        const m = cfg.mode;
        show(level, m === "pulse" || m === "level"); show(widthWrap, m === "pulse");
        show(act.wrap, m === "pulse" || m === "level"); show(rel.wrap, m === "level");
        show(rise.wrap, m === "edge"); show(fall.wrap, m === "edge");
        [...level.children].forEach((b, i) => setAttr(b, "aria-pressed", (i === 0) === cfg.active_high ? "true" : "false"));
        setValue(width, cfg.min_width_ms);
        for (const a of [act, rel, rise, fall]) setValue(a.sel, cfg[a.key] || "(none)");
        for (const c of [enabled, labelIn, mode, width, act.sel, rel.sel, rise.sel, fall.sel, ...level.children]) setDisabled(c, locked);
      } };
    }

    function outCard() {
      const mapping = h("select", { class: "select", "aria-label": "BNC OUT signals", onChange: () => { draft.mapping = mapping.value; sync(); } });
      const help = h("p", { class: "small muted" });
      const trigger = h("select", { class: "select", "aria-label": "Node event" });
      const width = h("input", { class: "input num", type: "number", step: "any", min: "0.001", "aria-label": "Pulse width in ms" });
      const sync_w = h("input", { class: "input num", type: "number", step: "any", min: "0.001", "aria-label": "Sync pulse width in ms" });
      const interval = h("input", { class: "input num", type: "number", step: "any", min: "0.5", "aria-label": "Interval in seconds" });
      const wrap = (label, c) => h("div", { class: "field" }, h("label", { class: "lab small" }, label), c);
      const wT = wrap("Node event", trigger), wW = wrap("Pulse width (ms)", width), wS = wrap("Sync pulse width (ms)", sync_w), wI = wrap("Interval (s)", interval);
      const err = h("p", { class: "small err-text", role: "alert" });
      const apply = btn("Apply", async () => {
        const args = { mapping: mapping.value };
        if (!wT.hidden) args.trigger = trigger.value;
        if (!wW.hidden) args.width_us = Math.round(Number(width.value) * 1000);
        if (!wS.hidden) args.sync_width_us = Math.round(Number(sync_w.value) * 1000);
        if (!wI.hidden) args.interval_s = Number(interval.value);
        const r = await ctx.command("set_bnc_out", args);
        setText(err, r.ok ? "" : r.error);
        if (r.ok) { draft = {}; ctx.toast("BNC OUT saved.", "info"); } else ctx.toast(r.error, "error");
      }, "btn primary");
      let draft = {}, optionsFor = null, triggersFor = null, lastCfg = null;
      const el = h("section", { class: "card col", style: { padding: "12px", boxShadow: "none", border: "1px solid var(--line)" }, "aria-label": "BNC output" },
        h("strong", {}, "BNC OUT"), h("div", { class: "field" }, h("label", { class: "lab small" }, "Signals"), mapping), help, wT, wW, wS, wI, err, apply);

      function sync() {
        const m = mapping.value;
        setText(help, OUT_HELP[m] || "");
        show(wT, m === "node_event");
        show(wW, ["session_start_stop", "trial", "node_event", "periodic"].includes(m));
        show(wS, m === "session_start_sync" || m === "legacy");
        show(wI, m === "periodic");
      }
      return { el, sync, update(cfg, locked, defaults, triggers) {
        if (optionsFor !== defaults || lastCfg === null || (lastCfg.mapping === "legacy") !== (cfg.mapping === "legacy")) {
          optionsFor = defaults;
          const maps = defaults.bnc.out_mappings.filter((x) => x !== "legacy" || cfg.mapping === "legacy");
          mapping.replaceChildren(...maps.map((x) => h("option", { value: x }, OUT_NAMES[x] || x)));
        }
        if (triggers && triggersFor !== triggers) { triggersFor = triggers; trigger.replaceChildren(...triggers.map((t) => h("option", { value: t }, t))); }
        const changed = JSON.stringify(cfg) !== JSON.stringify(lastCfg);
        lastCfg = cfg;
        if (changed) { setValue(mapping, cfg.mapping); setValue(trigger, cfg.trigger); setValue(width, cfg.pulse_width_us / 1000); setValue(sync_w, cfg.sync_width_us / 1000); setValue(interval, cfg.periodic_interval_s); }
        sync();
        for (const c of [mapping, trigger, width, sync_w, interval, apply]) setDisabled(c, locked);
      } };
    }

    let triggers = null;
    return { el, update() {
      const state = ctx.engine.get().state, defaults = ctx.ui.get().defaults;
      if (!state || !defaults) return;
      const locked = state.session.running;
      show(lockNote, locked);
      setText(logPath, state.log.csv_path || "No log file open");
      const gb = state.gpio_backend;
      setText(gpio, gb === "gpiod" ? "BNC I/O: gpiod, kernel edge timestamps." : gb ? `BNC I/O: ${gb} (software edge times).` : "BNC I/O is simulated: no GPIO found.");
      gpio.className = "small " + (gb === "gpiod" ? "ok-text" : gb ? "note" : "err-text");
      setText(armLine, state.session.armed ? "Armed: waiting for the start pulse." : "Not armed. A BNC input can start a session only after you arm it.");
      state.bnc.inputs.forEach((cfg, i) => inputs[i].update(cfg, locked, defaults));
      if (!triggers) { triggers = []; ctx.command("bnc_out_triggers").then((r) => { if (r.ok) { triggers = r.triggers; out.update(state.bnc.out, locked, defaults, triggers); } }); }
      out.update(state.bnc.out, locked, defaults, triggers);
      setDisabled(pulse, locked); setDisabled(cal, locked);
    } };
  }

  function devTab() {
    const factorNum = h("input", { class: "input num", type: "number", step: "0.1", id: "dev-factor", "aria-label": "Presence factor" });
    const factorRange = h("input", { type: "range", step: "0.1", "aria-label": "Presence factor slider" });
    const sync = (from, to) => () => { to.value = from.value; };
    factorRange.addEventListener("input", sync(factorRange, factorNum)); factorNum.addEventListener("input", sync(factorNum, factorRange));
    const applyFactor = btn("Apply to all nodes", async () => {
      const r = await ctx.command("set_presence_factor", { factor: Number(factorNum.value) });
      ctx.toast(r.ok ? `Presence factor ${r.presence_factor} sent to all nodes.` : r.error, r.ok ? "info" : "error");
    }, "btn primary");
    const hbInput = h("input", { class: "input num", type: "number", step: "any", id: "dev-hb", "aria-label": "Heartbeat interval in seconds" });
    const hbSummary = h("p", { class: "tiny muted" });
    const applyHb = btn("Apply heartbeat to all nodes", async () => {
      const r = await ctx.command("set_heartbeat", { seconds: Number(hbInput.value) });
      ctx.toast(r.ok ? `Heartbeat set to ${seconds(r.heartbeat_interval_s)}.` : r.error, r.ok ? "info" : "error");
    });
    const rangeHint = h("p", { class: "tiny muted" });
    const ids = h("div", { class: "col tight" }), idStore = new Map();
    const el = h("div", { class: "tabpanel", role: "tabpanel" },
      h("div", {}, h("h2", { class: "display", style: { fontSize: "20px", fontWeight: 700 } }, "Developer parameters"),
        h("span", { class: "small muted" }, "Low-level node settings. Changes go out as CAN commands and show in the event log.")),
      h("div", { class: "col tight" },
        h("div", { class: "row", style: { justifyContent: "space-between" } }, h("label", { class: "lab", for: "dev-factor" }, "Presence factor"), factorNum),
        factorRange, h("span", { class: "tiny mono muted" }, "threshold = mean + factor × std_dev"), rangeHint, applyFactor),
      h("div", { class: "sep" }),
      h("div", { class: "col tight" },
        h("h3", {}, "Heartbeat"),
        h("div", { class: "row", style: { justifyContent: "space-between" } }, h("label", { class: "lab", for: "dev-hb" }, "Interval (seconds)"), hbInput),
        hbSummary, applyHb),
      h("div", { class: "sep" }),
      h("div", { class: "col tight" }, h("h3", {}, "Assign node ID"), ids));
    let seeded = false;
    // A late answer from the engine must not replace something the operator has already typed.
    const touched = new Set();
    for (const [name, c] of [["factor", factorNum], ["factor", factorRange], ["hb", hbInput]]) c.addEventListener("input", () => touched.add(name));
    hbInput.addEventListener("input", () => refresh());

    return { el, update() {
      const state = ctx.engine.get().state, defaults = ctx.ui.get().defaults;
      if (!state || !defaults) return;
      if (!seeded) {
        seeded = true;
        const [lo, hi] = defaults.presence_factor_range;
        for (const c of [factorNum, factorRange]) { setAttr(c, "min", lo); setAttr(c, "max", hi); }
        ctx.command("get_dev_settings").then((r) => {
          if (!r.ok) return;
          if (!touched.has("factor")) factorNum.value = factorRange.value = r.presence_factor;
          if (!touched.has("hb")) hbInput.value = r.heartbeat_interval_s;
          refresh();
        });
        setText(rangeHint, `Range ${lo} to ${hi}.`);
      }
      refresh();
      const locked = state.session.running;
      reconcile(ids, state.nodes, (n) => n.id, (n) => {
        const input = h("input", { class: "input", style: { width: "64px" }, inputmode: "numeric", "aria-label": `New ID for node ${n.id}`, value: String(n.id) });
        const name = h("label", {}), send = btn("Send", async () => {
          const r = await ctx.command("assign_node_id", { node: n.id, new_id: Number(input.value) });
          ctx.toast(r.ok ? `Asked node ${n.id} to take ID ${input.value}. Re-discover or restart the session to see it.` : r.error, r.ok ? "info" : "error");
        });
        return { el: h("div", { class: "row", style: { justifyContent: "space-between" } }, name, h("span", { class: "row tight" }, input, send)),
          update: (nn) => { setText(name, nodeName(nn)); setDisabled(send, locked || !nn.mac); setAttr(send, "title", !nn.mac ? "The MAC is not known yet." : locked ? "Not while a session runs." : null); } };
      }, idStore);
    } };
    function refresh() {
      const v = Number(hbInput.value);
      setText(hbSummary, Number.isFinite(v) && v > 0 ? `Nodes report every ${seconds(v)}. A node is marked offline after 3 missed beats (${seconds(v * 3)}). Heartbeats can't be turned off.` : "");
    }
  }

  return { el, update() {
    const { tab, dev } = rs.get();
    const visibleTabs = tabs.filter(([k]) => k !== "dev" || dev);
    tablist.style.gridTemplateColumns = `repeat(${visibleTabs.length}, minmax(0, 1fr))`;
    const active = visibleTabs.some(([k]) => k === tab) ? tab : "node";
    for (const [k] of tabs) {
      show(tabBtns[k], k !== "dev" || dev);
      setAttr(tabBtns[k], "aria-selected", k === active ? "true" : "false");
      show(panels[k].el, k === active);
    }
    panels[active].update();
  } };
}
