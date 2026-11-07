// Tiny DOM helpers. No framework: views build their elements once and update
// only what changed, so a 10 Hz state push doesn't redraw the page.

const SVG_NS = "http://www.w3.org/2000/svg";

/** Create an element. Props: class, style (object), on<Event> handlers, aria and data attributes, and plain attributes. */
export function h(tag, props = {}, ...kids) {
  const svg = tag === "svg" || props.svg;
  const el = svg ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false || k === "svg") continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "value" && !svg) el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, kids);
  return el;
}

/** An SVG element whose children are also SVG. */
export function s(tag, props = {}, ...kids) {
  return h(tag, { ...props, svg: true }, ...kids.map((k) => k));
}

function append(el, kids) {
  for (const k of kids.flat()) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}

/** Set text only when it changed. */
export function setText(el, value) {
  const t = value == null ? "" : String(value);
  if (el.textContent !== t) el.textContent = t;
}

/** Set an attribute only when it changed. null/false removes it. */
export function setAttr(el, name, value) {
  if (value == null || value === false) {
    if (el.hasAttribute(name)) el.removeAttribute(name);
    return;
  }
  const v = value === true ? "" : String(value);
  if (el.getAttribute(name) !== v) el.setAttribute(name, v);
}

export function setStyle(el, name, value) {
  if (el.style.getPropertyValue(name) !== String(value)) el.style.setProperty(name, String(value));
}

export function setValue(el, value) {
  const v = value == null ? "" : String(value);
  if (el.value !== v && document.activeElement !== el) el.value = v;
}

/** Show or hide an element. Uses the attribute, which also works on SVG elements. */
export function show(el, on) {
  if (el.hasAttribute("hidden") === !on) return;
  el.toggleAttribute("hidden", !on);
}

export function setDisabled(el, on) {
  const want = !!on;
  if (el.disabled !== want) el.disabled = want;
}

/**
 * Keep `container`'s children in step with `items`, keyed. `make(item)` returns
 * {el, update(item)}. Existing entries are updated and reordered in place.
 */
export function reconcile(container, items, keyOf, make, store) {
  const live = new Map();
  for (const item of items) {
    const key = keyOf(item);
    let entry = store.get(key);
    if (!entry) { entry = make(item); store.set(key, entry); }
    entry.update(item);
    live.set(key, entry);
  }
  for (const [key, entry] of store) {
    if (!live.has(key)) { entry.el.remove(); store.delete(key); }
  }
  let prev = null;
  for (const entry of live.values()) {
    const want = prev ? prev.nextSibling : container.firstChild;
    if (entry.el !== want) container.insertBefore(entry.el, want);
    prev = entry.el;
  }
}
