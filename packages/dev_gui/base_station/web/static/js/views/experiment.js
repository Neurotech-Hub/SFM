// Step 2: pick a template, fill its form (built from the template's JSON), name the session.
import { h, setAttr, setDisabled, setText, show, reconcile } from "../dom.js";
import { nodeName } from "../nodes.js";

const NODE_TYPES = ["nodes", "node_number", "node_choice"];

/** Same rule as the engine's param_visible: every condition must hold. */
export function visible(param, scalars) {
  const cond = param.visible_when;
  if (!cond) return true;
  return Object.entries(cond).every(([key, wanted]) => (Array.isArray(wanted) ? wanted : [wanted]).includes(scalars[key]));
}

/** What the session name becomes as a file name (the engine's sanitize_session_name). */
export function sanitizeName(name) {
  return String(name || "").trim().replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[._-]+|[._-]+$/g, "").slice(0, 64);
}

/** The values a form holds for an experiment, filled in with defaults. */
export function initialValues(exp) {
  const v = {};
  for (const p of exp.parameters) v[p.key] = NODE_TYPES.includes(p.type) ? { ...(p.node_defaults || {}) } : p.default;
  return v;
}

/** Saved form values laid over the defaults. Per-node maps merge node by node: a node that
 *  appeared after the values were saved gets its default, and one that is gone is dropped. */
export function mergeValues(exp, saved) {
  const base = initialValues(exp);
  for (const p of exp.parameters) {
    if (!(p.key in (saved || {}))) continue;
    if (!NODE_TYPES.includes(p.type)) { base[p.key] = saved[p.key]; continue; }
    // Only nodes in this setup: a node that was there before a reconnect must not linger.
    for (const id of Object.keys(base[p.key])) if (id in (saved[p.key] || {})) base[p.key][id] = saved[p.key][id];
  }
  return base;
}

/** The values to send: only parameters that are visible, so a stale hidden value can't be rejected. */
export function visibleValues(exp, values) {
  const scalars = {};
  for (const p of exp.parameters) if (!NODE_TYPES.includes(p.type)) scalars[p.key] = values[p.key];
  const out = {};
  for (const p of exp.parameters) if (visible(p, scalars) && values[p.key] !== undefined) out[p.key] = values[p.key];
  return out;
}

/** Problems with the numbers in a form, by parameter key. */
export function validate(exp, values) {
  const errors = {};
  const scalars = {};
  for (const p of exp.parameters) if (!NODE_TYPES.includes(p.type)) scalars[p.key] = values[p.key];
  for (const p of exp.parameters) {
    if (!visible(p, scalars)) continue;
    const v = values[p.key];
    if (p.type === "int" || p.type === "float") {
      const n = Number(v);
      if (v === "" || v === null || Number.isNaN(n)) errors[p.key] = "Enter a number.";
      else if (p.type === "int" && !Number.isInteger(n)) errors[p.key] = "Enter a whole number.";
      else if (p.min != null && n < p.min) errors[p.key] = `At least ${p.min}.`;
      else if (p.max != null && n > p.max) errors[p.key] = `At most ${p.max}.`;
    } else if (p.type === "node_number") {
      const lo = p.min ?? 0, hi = p.max ?? 100;
      for (const [id, x] of Object.entries(v || {})) {
        const n = Number(x);
        if (x === "" || Number.isNaN(n) || !Number.isInteger(n) || n < lo || n > hi) { errors[p.key] = `Each node needs a whole number from ${lo} to ${hi}.`; break; }
      }
    }
  }
  return errors;
}

export function createExperiment(ctx) {
  const model = { exp: null, values: {}, formKey: "", preview: null, previewFor: null, timer: null, startNote: "" };
  const cards = new Map();
  const picker = h("div", { class: "col tight", role: "radiogroup", "aria-labelledby": "exp-h" });
  const title = h("h2", { class: "display", style: { fontSize: "22px", fontWeight: 700 } });
  const desc = h("p", { class: "small muted" });
  const form = h("div", { class: "col" });
  const nameInput = h("input", { class: "input mono", id: "session-name", autocomplete: "off", spellcheck: "false",
    onInput: (e) => { ctx.setDraft({ sessionName: e.target.value }); schedulePreview(); update(); } });
  const pathLine = h("p", { class: "small muted wrap-any", role: "status" });
  const resumeLine = h("p", { class: "small note", role: "status" });
  const note = h("p", { class: "small note", role: "status" });
  const back = h("button", { class: "btn big", type: "button", onClick: () => ctx.go("connect") }, "Back");
  const nextBtn = h("button", { class: "btn primary big", type: "button", onClick: () => ctx.go("run") }, "Next");

  const el = h("main", { id: "view-experiment", class: "page", style: { maxWidth: "1240px", flexDirection: "row", flexWrap: "wrap", alignItems: "flex-start" } },
    h("section", { class: "card col", style: { flex: "1 1 340px", minWidth: 0 } },
      h("h1", { id: "exp-h", style: { fontSize: "24px", margin: "0 0 6px" } }, "Choose experiment"), picker),
    h("section", { class: "card col", style: { flex: "1 1 460px", minWidth: 0 } },
      title, desc, form, h("div", { class: "sep" }),
      h("div", { class: "field" },
        h("label", { class: "lab", for: "session-name" }, "Session name"), nameInput, pathLine, resumeLine),
      note,
      h("div", { class: "row", style: { justifyContent: "flex-end" } }, back, nextBtn)),
  );

  function select(name) {
    const exp = ctx.ui.get().experiments.find((e) => e.name === name);
    if (!exp) return;
    model.exp = exp;
    const saved = (ctx.draft.values || {})[name];
    model.values = mergeValues(exp, saved);
    model.formKey = "";
    ctx.setDraft({ experiment: name });
    ctx.command("set_gui_settings", { last_experiment: name });
    update();
  }

  function schedulePreview() {
    clearTimeout(model.timer);
    model.timer = setTimeout(async () => {
      const name = sanitizeName(nameInput.value);
      if (!name) { model.preview = null; update(); return; }
      const r = await ctx.command("preview_session", { session_name: nameInput.value });
      if (sanitizeName(nameInput.value) === name) { model.preview = r.ok ? r : null; model.previewFor = name; update(); }
    }, 250);
  }

  function field(param) {
    const id = `f-${param.key}`;
    const err = h("p", { class: "small err-text", role: "alert" });
    const help = param.help ? h("p", { class: "small muted" }, param.help) : null;
    const set = (v) => { model.values[param.key] = v; ctx.setDraft({ values: { ...(ctx.draft.values || {}), [model.exp.name]: model.values } }); update(); };
    let control, sync;

    if (param.type === "bool") {
      const box = h("input", { type: "checkbox", id, onChange: (e) => set(e.target.checked) });
      control = h("label", { class: "check", for: id }, box, param.label);
      sync = () => { box.checked = !!model.values[param.key]; };
    } else if (param.type === "int" || param.type === "float") {
      const input = h("input", { class: "input num", id, type: "number", inputmode: "decimal", step: param.step ?? (param.type === "int" ? "1" : "any"),
        min: param.min ?? undefined, max: param.max ?? undefined, onInput: (e) => set(e.target.value === "" ? "" : Number(e.target.value)) });
      control = h("div", { class: "field" }, h("label", { class: "lab", for: id }, param.label,
        param.min != null || param.max != null ? h("span", { class: "small muted" }, ` (${param.min ?? ""}–${param.max ?? ""})`) : null), input);
      sync = () => { if (document.activeElement !== input) input.value = model.values[param.key] ?? ""; setAttr(input, "aria-invalid", model.errors[param.key] ? "true" : null); };
    } else if (param.type === "choice") {
      const group = h("div", { class: "seg", role: "group", "aria-labelledby": id }), btns = param.options.map((o) =>
        h("button", { class: "btn", type: "button", "aria-pressed": "false", onClick: () => set(o) }, o));
      group.append(...btns);
      control = h("div", { class: "field" }, h("span", { class: "lab", id }, param.label), group);
      sync = () => btns.forEach((b, i) => setAttr(b, "aria-pressed", model.values[param.key] === param.options[i] ? "true" : "false"));
    } else if (param.type === "str") {
      const input = h("input", { class: "input", id, onInput: (e) => set(e.target.value) });
      control = h("div", { class: "field" }, h("label", { class: "lab", for: id }, param.label), input);
      sync = () => { if (document.activeElement !== input) input.value = model.values[param.key] ?? ""; };
    } else {
      // nodes / node_number / node_choice: one control per node
      const per = h("div", { class: "col tight" }), store = new Map();
      const tools = param.type === "nodes" ? h("div", { class: "row tight" },
        h("button", { class: "btn", type: "button", onClick: () => setAll(true) }, "All"),
        h("button", { class: "btn", type: "button", onClick: () => setAll(false) }, "None")) : null;
      const setAll = (v) => { const m = {}; for (const n of nodesNow()) m[n.id] = v; set(m); };
      control = h("div", { class: "field" }, h("span", { class: "lab", id }, param.label), per, tools);
      sync = () => {
        reconcile(per, nodesNow(), (n) => n.id, (n) => {
          const lab = h("span", { style: { fontWeight: 600 } });
          let ctl, upd;
          if (param.type === "nodes") {
            ctl = h("button", { class: "btn", type: "button", "aria-pressed": "false", onClick: () => set({ ...model.values[param.key], [n.id]: !model.values[param.key][n.id] }) }, "In task");
            upd = (v) => setAttr(ctl, "aria-pressed", v ? "true" : "false");
          } else if (param.type === "node_number") {
            ctl = h("input", { class: "input num", type: "number", step: "1", min: param.min ?? 0, max: param.max ?? 100, "aria-label": `${param.label} for node ${n.id}`,
              onInput: (e) => set({ ...model.values[param.key], [n.id]: e.target.value === "" ? "" : Number(e.target.value) }) });
            upd = (v) => { if (document.activeElement !== ctl) ctl.value = v ?? ""; };
          } else {
            ctl = h("div", { class: "seg", role: "group", "aria-label": `${param.label} for node ${n.id}` });
            const bs = param.options.map((o) => h("button", { class: "btn", type: "button", "aria-pressed": "false", onClick: () => set({ ...model.values[param.key], [n.id]: o }) }, o));
            ctl.append(...bs);
            upd = (v) => bs.forEach((b, i) => setAttr(b, "aria-pressed", v === param.options[i] ? "true" : "false"));
          }
          return { el: h("div", { class: "row", style: { justifyContent: "space-between" } }, lab, ctl),
            update: (node) => { setText(lab, nodeName(node)); upd((model.values[param.key] || {})[node.id]); } };
        }, store);
      };
    }
    const wrap = h("div", { class: "col tight" }, control, help, err);
    return { el: wrap, key: param.key, update() { sync(); setText(err, model.errors[param.key] || ""); } , param };
  }

  const nodesNow = () => (ctx.engine.get().state?.nodes || []);
  let fields = [];

  function rebuild() {
    form.replaceChildren();
    fields = model.exp.parameters.map(field);
    form.append(...fields.map((f) => f.el));
  }

  function update() {
    const { experiments } = ctx.ui.get();
    const state = ctx.engine.get().state;
    // The picker.
    reconcile(picker, experiments, (e) => e.name, (e) => {
      const label = h("strong", {}), d = h("span", { class: "small muted" });
      const b = h("button", { class: "opt", type: "button", role: "radio", onClick: () => select(e.name) }, label, d);
      return { el: b, update: (it) => { setText(label, it.label); setText(d, it.description); setAttr(b, "aria-checked", model.exp && model.exp.name === it.name ? "true" : "false"); } };
    }, cards);

    if (experiments.length && (!model.exp || !experiments.some((e) => e.name === model.exp.name))) {
      const want = ctx.draft.experiment || ctx.ui.get().settings.last_experiment;
      return select(experiments.some((e) => e.name === want) ? want : experiments[0].name);
    }
    if (!model.exp) return;
    // A refreshed schema (node count changed) keeps what was typed.
    const fresh = experiments.find((e) => e.name === model.exp.name);
    if (fresh !== model.exp) { model.exp = fresh; model.values = mergeValues(fresh, model.values); model.formKey = ""; }

    const key = model.exp.name + "|" + nodesNow().map((n) => n.id).join(",");
    if (key !== model.formKey) { model.formKey = key; rebuild(); }
    setText(title, model.exp.label);
    setText(desc, model.exp.description);

    model.errors = validate(model.exp, model.values);
    const scalars = {};
    for (const p of model.exp.parameters) if (!NODE_TYPES.includes(p.type)) scalars[p.key] = model.values[p.key];
    for (const f of fields) { show(f.el, visible(f.param, scalars)); f.update(); }

    if (document.activeElement !== nameInput && nameInput.value !== (ctx.draft.sessionName || "")) nameInput.value = ctx.draft.sessionName || "";
    const name = sanitizeName(nameInput.value);
    const pv = model.preview && model.previewFor === name ? model.preview : null;
    setText(pathLine, name ? (pv ? `Saves to ${pv.path}` : `Saves as ${name}.csv`) : "Enter a name. It becomes the log file name.");
    const resumed = pv && !pv.will_create;
    setText(resumeLine, resumed ? `A session with this name already exists (${pv.row_count} rows). This becomes run ${pv.next_run_id} in the same file${pv.resume && pv.resume.from_run ? ", and pellet counts continue from the earlier runs" : ""}.` : "");

    const bad = Object.keys(model.errors).length > 0;
    const noNodes = !state || !state.nodes.length;
    setText(note, bad ? "Fix the highlighted values to continue." : "");
    setDisabled(nextBtn, bad || !name || noNodes);
    setAttr(nextBtn, "title", !name ? "Enter a session name first." : null);
  }
  ctx.ui.subscribe(() => { if (!el.hidden) update(); });
  model.errors = {};

  return { el, update };
}

/** What the Run step sends to start a session. */
export function currentSetup(ctx) {
  const exp = ctx.ui.get().experiments.find((e) => e.name === ctx.draft.experiment) || ctx.ui.get().experiments[0];
  if (!exp) return null;
  const values = mergeValues(exp, (ctx.draft.values || {})[exp.name]);
  return { exp, values, sessionName: ctx.draft.sessionName || "", send: visibleValues(exp, values), errors: validate(exp, values) };
}
