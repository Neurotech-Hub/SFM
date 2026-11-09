// Boot: build the frame, start the state feed, and switch between the three steps.
import { h, setAttr, setDisabled, setText, show } from "./dom.js";
import { command, watchState } from "./api.js";
import { acceptState, createStore, engine, loadDraft, saveDraft } from "./store.js";
import { confirmDialog, mountToasts, toast } from "./ui.js";
import { createConnect } from "./views/connect.js";
import { createExperiment } from "./views/experiment.js";
import { createRun } from "./views/run.js";

const STEPS = [["connect", "Connect"], ["experiment", "Experiment"], ["run", "Run"]];

const ui = createStore({ step: null, defaults: null, experiments: [], settings: {}, linkNodes: 0 });
let draft = loadDraft();

export const ctx = {
  engine, ui, command, toast,
  confirm: confirmDialog,
  get draft() { return draft; },
  setDraft(patch) { draft = { ...draft, ...patch }; saveDraft(draft); },
  go,
  stateNow: () => engine.get().state,
};

const app = document.getElementById("app");
const root = h("div", { class: "app" });
app.append(root);
mountToasts(document.body);

const stepBtns = {};
const nav = h("nav", { class: "nav", "aria-label": "Session steps" },
  ...STEPS.map(([key, label], i) => {
    const b = h("button", { class: "step", type: "button", onClick: () => go(key) },
      h("span", { class: "n" }, String(i + 1)), label);
    stepBtns[key] = b;
    return b;
  }),
  h("div", { class: "grow" }),
  h("span", { class: "small muted", id: "step-hint", role: "status" }),
);
const engineBanner = h("div", { class: "banner err", role: "alert", hidden: true },
  h("strong", {}, "Engine not running"),
  h("span", { class: "msg", id: "engine-msg" }, "The page can't reach the base-station engine, so nothing here is live. It reconnects by itself."));

const views = { connect: createConnect(ctx), experiment: createExperiment(ctx), run: createRun(ctx) };
root.append(nav, engineBanner, ...Object.values(views).map((v) => v.el));

/** Move to a step. Leaving a running session asks first. */
async function go(step) {
  const { state, up } = engine.get();
  if (step !== "connect" && !(up && state && state.connected)) {
    toast("Connect to the bus first.", "warning");
    step = "connect";
  }
  const current = ui.get().step;
  if (current === "run" && step !== "run" && state && state.session.running) {
    const ok = await confirmDialog({
      title: "Stop the running session?",
      body: ["Going back to the setup steps stops the session that is running now.", "The log is kept. You can start again from step 2."],
      confirm: "Stop session and go back", danger: true,
    });
    if (!ok) return;
    const res = await command("stop_session");
    if (!res.ok) { toast(res.error, "error"); return; }
  }
  ui.set({ step });
}

function render() {
  const { up, state, link } = engine.get();
  const { step } = ui.get();
  show(engineBanner, !up);
  setText(document.getElementById("engine-msg"), link
    ? "The gateway is running but the engine isn't. Start sfm-engine; this page reconnects by itself."
    : "This page can't reach the gateway. It keeps trying.");
  const connected = !!(up && state && state.connected);
  for (const [key] of STEPS) {
    const b = stepBtns[key];
    setAttr(b, "aria-current", key === step ? "step" : null);
    b.classList.toggle("done", STEPS.findIndex(([k]) => k === step) > STEPS.findIndex(([k]) => k === key));
    setDisabled(b, key !== "connect" && !connected);
  }
  const hint = step === "run"
    ? `${state && state.session.running ? "Session running" : "Not running"}${state && state.interface ? " · " + state.interface : ""}`
    : `Step ${STEPS.findIndex(([k]) => k === step) + 1} of 3`;
  setText(document.getElementById("step-hint"), hint + (state && state.nodes ? ` · ${state.nodes.length} ${state.nodes.length === 1 ? "node" : "nodes"}` : ""));
  for (const [key] of STEPS) show(views[key].el, key === step);
  if (step) views[step].update();
}

// What the forms are built from. Fetched when the engine comes up, and again when the node count changes.
async function loadStatics() {
  const [defaults, settings, experiments] = await Promise.all([
    command("get_defaults"), command("get_gui_settings"), command("list_experiments"),
  ]);
  ui.set({
    defaults: defaults.ok ? defaults : ui.get().defaults,
    settings: settings.ok ? settings.settings : ui.get().settings,
    experiments: experiments.ok ? experiments.experiments : ui.get().experiments,
  });
}

let wasUp = false, wasConnected = false, lastNodeCount = -1, started = false, lastNotice = null;
engine.subscribe(({ up, state }) => {
  if (up && !wasUp) loadStatics();
  wasUp = up;
  // Connected again, maybe with another number of nodes: the forms are built for the new setup.
  const isConnected = !!(up && state && state.connected);
  if (isConnected && !wasConnected && started) loadStatics();
  wasConnected = isConnected;
  const count = state && state.nodes ? state.nodes.length : 0;
  if (up && count && count !== lastNodeCount) {
    if (lastNodeCount !== -1) command("list_experiments").then((r) => r.ok && ui.set({ experiments: r.experiments }));
    lastNodeCount = count;
  }
  // Engine notices (a rejected pulse, a settings file that couldn't be saved) become toasts.
  // The first state after a page load only records the latest one, so old news isn't replayed.
  if (state && state.notice) {
    if (lastNotice !== null && state.notice.seq !== lastNotice) toast(state.notice.text, state.notice.level === "warning" ? "warning" : "info");
    lastNotice = state.notice.seq;
  } else if (state && lastNotice === null) { lastNotice = 0; }
  if (!started && up && state) {
    started = true;
    ui.set({ step: state.connected ? (state.session.running ? "run" : "experiment") : "connect" });
  }
  if (!state || !state.connected) lastNodeCount = -1;
  // The engine is up but no longer on the bus (it restarted): the later steps mean nothing, so go back to Connect.
  if (up && state && !state.connected && ui.get().step && ui.get().step !== "connect") {
    toast("The engine is not connected to the bus. It may have restarted, so connect again.", "warning");
    ui.set({ step: "connect" });
  }
  render();
});
ui.subscribe(render);

ui.set({ step: "connect" });
watchState((msg) => acceptState(msg), (link) => engine.set({ link, ...(link ? {} : { up: false }) }));
