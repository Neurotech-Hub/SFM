// One small store. `engine` is what the engine reports; `ui` is what this page is showing.

export function createStore(initial) {
  let value = initial;
  const subs = new Set();
  return {
    get: () => value,
    set(patch) {
      const next = { ...value, ...patch };
      let changed = false;
      for (const k of Object.keys(patch)) if (next[k] !== value[k]) { changed = true; break; }
      if (!changed) return;
      value = next;
      for (const fn of [...subs]) fn(value);
    },
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
  };
}

export const engine = createStore({ link: false, up: false, state: null, version: 0 });

// The state without its clock, so a push that changed nothing doesn't repaint anything.
export function fingerprint(state) {
  if (!state) return "";
  const { time, ...rest } = state;
  return JSON.stringify(rest);
}

let lastPrint = "";
export function acceptState(msg) {
  const print = fingerprint(msg.state) + "|" + msg.engine;
  if (print === lastPrint) return false;
  lastPrint = print;
  engine.set({ up: !!msg.engine, state: msg.state, version: engine.get().version + 1 });
  return true;
}

// Draft values the operator typed (experiment, session name, form values), kept across reloads.
const KEY = "sfm.setup.v1";
export function loadDraft() {
  try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; }
}
export function saveDraft(draft) {
  try { localStorage.setItem(KEY, JSON.stringify(draft)); } catch { /* storage may be blocked */ }
}
