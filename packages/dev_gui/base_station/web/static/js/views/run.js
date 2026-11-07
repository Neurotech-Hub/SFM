// Step 3: run the session. Header controls, banners, the map, the event log and the side panel.
import { h, setAttr, setDisabled, setText, show, reconcile } from "../dom.js";
import { createStore } from "../store.js";
import { fmtElapsed, words } from "../format.js";
import { nodeName } from "../nodes.js";
import { currentSetup } from "./experiment.js";
import { createMap } from "./map.js";
import { createLog } from "./logview.js";
import { createPanel } from "./panel.js";

const CAL_ICON = "M12 3v4M12 17v4M3 12h4M17 12h4M12 12h.01";

export function createRun(ctx) {
  // What this step is showing: selected node, tab, whether nodes are being arranged, calibration progress.
  const rs = createStore({ selected: null, tab: "node", arranging: false, dev: false, cal: { phase: "idle" }, setupView: null });
  const map = createMap(ctx, rs), log = createLog(ctx), panel = createPanel(ctx, rs, log);

  const title = h("strong", { class: "title" }), sub = h("span", { class: "small muted" });
  const stat = (label) => { const dd = h("dd", {}); return { dd, el: h("div", {}, h("dt", {}, label), dd) }; };
  const sElapsed = stat("Elapsed"), sTrial = stat("Trial"), sOnline = stat("Nodes online"), sPellets = stat("Taken / presented");
  const stats = h("dl", { class: "stats" }, sElapsed.el, sTrial.el, sOnline.el, sPellets.el);

  const calSvgPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
  const calBtn = h("button", { class: "btn", type: "button", onClick: () => rs.set({ cal: { phase: "confirm" } }) },
    h("svg", { svg: true, width: 16, height: 16, viewBox: "0 0 24 24", "aria-hidden": "true", style: { fill: "none", stroke: "currentColor", strokeWidth: 2.4 } }), "Calibrate presence");
  calBtn.querySelector("svg").append(calSvgPath); calSvgPath.setAttribute("d", CAL_ICON);
  const devBtn = h("button", { class: "btn", type: "button", "aria-pressed": "false", onClick: () => rs.set({ dev: !rs.get().dev, tab: !rs.get().dev ? "dev" : "node" }) }, "Developer");
  const arrangeBtn = h("button", { class: "btn", type: "button", "aria-pressed": "false", onClick: () => rs.set({ arranging: !rs.get().arranging }) }, "Arrange nodes");
  const autoBtn = h("button", { class: "btn", type: "button", onClick: autoArrange }, "Auto-arrange");
  const dispAll = h("button", { class: "btn", type: "button", onClick: () => broadcast("dispense") }, "Dispense all");
  const recAll = h("button", { class: "btn", type: "button", onClick: () => broadcast("recover") }, "Recover all");
  const pingAll = h("button", { class: "btn", type: "button", onClick: () => broadcast("ping") }, "Ping all");
  const armBtn = h("button", { class: "btn", type: "button", onClick: toggleArm }, "Arm start");
  const runBtn = h("button", { class: "btn primary", type: "button", onClick: toggleRun }, "Start session");

  const header = h("header", { class: "runhead" },
    h("div", { class: "col", style: { gap: 0 } }, title, sub), stats, h("div", { class: "grow" }),
    calBtn, devBtn, arrangeBtn, autoBtn, dispAll, recAll, pingAll, armBtn, runBtn);

  // Banners
  const calTitle = h("strong", {}), calMsg = h("span", { class: "msg" }), calResults = h("ul", { class: "col tight", style: { flex: "1 1 100%" } });
  const calCancel = h("button", { class: "btn", type: "button", onClick: () => rs.set({ cal: { phase: "idle" } }) }, "Cancel");
  const calGo = h("button", { class: "btn primary", type: "button", onClick: doCalibrate }, "Cage is empty, calibrate");
  const calBanner = h("div", { class: "banner warn", role: "status" }, calTitle, calMsg, calCancel, calGo, calResults);
  const armBanner = h("div", { class: "banner info", role: "status" }, h("strong", {}, "Armed"),
    h("span", { class: "msg" }, "Waiting for a start pulse on a BNC input. It starts this session once, then you re-arm by hand."),
    h("button", { class: "btn", type: "button", onClick: toggleArm }, "Disarm"));
  const faultBox = h("div", { id: "fault-box", class: "col tight", role: "alert" }), faultStore = new Map();
  const startErr = h("div", { class: "banner err", role: "alert", hidden: true }, h("strong", {}, "Could not start"), h("span", { class: "msg", id: "start-err" }));

  const el = h("div", { id: "view-run", class: "col", style: { flex: "1 1 auto", minWidth: 0, gap: "14px" } },
    header, startErr, calBanner, armBanner, faultBox,
    h("div", { class: "runbody" }, h("main", { class: "runmain" }, map.el, log.el), panel.el));

  // ---- actions ----
  async function broadcast(cmd) { const r = await ctx.command(cmd, { node: "all" }); if (!r.ok) ctx.toast(r.error, "error"); }

  async function autoArrange() {
    const ok = await ctx.confirm({ title: "Auto-arrange nodes?", body: "This forgets every saved position and spaces the nodes out evenly.", confirm: "Auto-arrange" });
    if (!ok) return;
    const r = await ctx.command("clear_layout");
    if (!r.ok) ctx.toast(r.error, "error"); else map.resetLayoutOverrides();
  }

  function setup() {
    const s = currentSetup(ctx);
    if (!s) { ctx.toast("No experiment is available.", "error"); return null; }
    if (!sanitized(s.sessionName)) { ctx.toast("Enter a session name in step 2 first.", "warning"); ctx.go("experiment"); return null; }
    if (Object.keys(s.errors).length) { ctx.toast("Fix the values in step 2 first.", "warning"); ctx.go("experiment"); return null; }
    return s;
  }
  const sanitized = (n) => String(n || "").trim().replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[._-]+|[._-]+$/g, "");

  async function toggleRun() {
    const state = ctx.engine.get().state;
    if (state.session.running) {
      const ok = await ctx.confirm({ title: "Stop the session?", body: "The log is kept. You can start again from this page.", confirm: "Stop session", danger: true });
      if (!ok) return;
      const r = await ctx.command("stop_session");
      if (!r.ok) ctx.toast(r.error, "error");
      return;
    }
    const s = setup(); if (!s) return;
    setText(document.getElementById("start-err"), ""); show(startErr, false);
    map.resetTrack();
    const r = await ctx.command("start_session", { experiment: s.exp.name, session_name: s.sessionName, values: s.send });
    if (!r.ok) { setText(document.getElementById("start-err"), r.error); show(startErr, true); }
  }

  async function toggleArm() {
    const state = ctx.engine.get().state;
    if (state.session.armed) { const r = await ctx.command("disarm_start"); if (!r.ok) ctx.toast(r.error, "error"); return; }
    const s = setup(); if (!s) return;
    const r = await ctx.command("arm_start", { experiment: s.exp.name, session_name: s.sessionName, values: s.send });
    if (!r.ok) { setText(document.getElementById("start-err"), r.error); show(startErr, true); }
    else show(startErr, false);
  }

  async function doCalibrate() {
    const r = await ctx.command("calibrate_presence", { cage_empty: true });
    if (!r.ok) { ctx.toast(r.error, "error"); rs.set({ cal: { phase: "idle" } }); return; }
    rs.set({ cal: { phase: "running", at: Date.now() } });
    setTimeout(() => { if (rs.get().cal.phase === "running") rs.set({ cal: { phase: "done" } }); }, 6500);
  }

  // ---- update ----
  function update() {
    const state = ctx.engine.get().state;
    if (!state) return;
    const running = state.session.running;
    // The setup shown on the map and panel: what the session was started with, else what step 2 holds.
    const live = state.session.setup;
    const exps = ctx.ui.get().experiments;
    const exp = exps.find((e) => e.name === (live ? live.experiment : ctx.draft.experiment)) || exps[0] || null;
    const cs = exp && !live ? currentSetup(ctx) : null;
    const view = exp ? { exp, values: live ? live.values : (cs ? cs.values : {}) } : null;
    if (JSON.stringify(view) !== JSON.stringify(rs.get().setupView)) rs.set({ setupView: view });
    if (rs.get().selected == null || !state.nodes.some((n) => n.id === rs.get().selected)) {
      if (state.nodes.length) rs.set({ selected: state.nodes[0].id });
    }

    const sess = state.session;
    setText(title, sess.name || sanitized(ctx.draft.sessionName) || "No session");
    setText(sub, `${exp ? exp.label : "No experiment"} · ${state.interface || ""}`);
    setText(sElapsed.dd, fmtElapsed(running || sess.elapsed_s != null ? sess.elapsed_s : 0));
    setText(sTrial.dd, running ? String(sess.trial || 0) : "—");
    setText(sOnline.dd, `${state.nodes_online}/${state.nodes.length}`);
    setText(sPellets.dd, sess.pellets_presented == null ? "—" : `${sess.pellets_taken ?? 0}/${sess.pellets_presented}`);

    const cal = rs.get().cal;
    setDisabled(calBtn, running || cal.phase === "running");
    setAttr(calBtn, "title", running ? "Stop the session to recalibrate." : "Recalibrate the presence pads on all nodes.");
    setAttr(devBtn, "aria-pressed", rs.get().dev ? "true" : "false");
    setAttr(arrangeBtn, "aria-pressed", rs.get().arranging ? "true" : "false");
    setText(arrangeBtn, rs.get().arranging ? "Done arranging" : "Arrange nodes");
    show(autoBtn, rs.get().arranging);
    setText(runBtn, running ? "Stop session" : "Start session");
    runBtn.className = running ? "btn danger" : "btn primary";
    setText(armBtn, sess.armed ? "Disarm" : "Arm start");
    setDisabled(armBtn, running);
    setDisabled(runBtn, !state.nodes.length);
    for (const b of [dispAll, recAll, pingAll]) setDisabled(b, !state.nodes.length);

    show(armBanner, !!sess.armed && !running);
    // Calibration is optional: this banner appears only when asked for.
    show(calBanner, cal.phase !== "idle");
    setText(calTitle, cal.phase === "running" ? "Calibrating presence" : cal.phase === "done" ? "Calibration finished" : "Calibrate presence");
    setText(calMsg, cal.phase === "running" ? "Sampling the idle pads on every node for about 5 seconds. Keep every cage empty and undisturbed."
      : cal.phase === "done" ? "Latest result reported by each node:"
      : "Every cage must be empty and undisturbed for about 5 seconds. Calibrating with an animal present sets the threshold above real readings, and that node then stops detecting the animal until it is recalibrated.");
    show(calGo, cal.phase === "confirm"); show(calCancel, cal.phase === "confirm");
    show(calResults, cal.phase === "done");
    if (cal.phase === "done") {
      const rows = state.nodes.map((n) => [nodeName(n), n.presence_cal_ok === true ? `calibrated, threshold ${n.presence_threshold}` : n.presence_cal_ok === false ? "failed" : "no result", n.presence_cal_ok === true]);
      calResults.replaceChildren(...rows.map(([name, text, ok]) => h("li", { class: "small" }, h("strong", {}, name + ": "), h("span", { class: ok ? "ok-text" : "note" }, text)),
        h("button", { class: "btn", type: "button", onClick: () => rs.set({ cal: { phase: "idle" } }) }, "Done")));
    }

    const faulted = state.nodes.filter((n) => n.status === "FAULT" || (n.fault && n.fault !== "Ok"));
    show(faultBox, faulted.length > 0);
    reconcile(faultBox, faulted, (n) => n.id, (n) => {
      const msg = h("span", { class: "msg" }), b = h("button", { class: "btn danger", type: "button", onClick: async () => { const r = await ctx.command("recover", { node: n.id }); if (!r.ok) ctx.toast(r.error, "error"); } });
      return { el: h("div", { class: "banner err" }, h("strong", {}, "Fault"), msg, b),
        update: (nn) => { setText(msg, `${nodeName(nn)}: ${words(nn.fault)}`); setText(b, running ? `Recover ${nodeName(nn)} and resume` : `Recover ${nodeName(nn)}`); } };
    }, faultStore);

    map.update(); log.update(); panel.update();
  }
  rs.subscribe(() => { if (!el.hidden) update(); });

  return { el, update };
}
