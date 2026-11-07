// Toasts and confirm dialogs.
import { h } from "./dom.js";

let host = null;
export function mountToasts(parent) {
  host = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
  parent.append(host);
}

export function toast(text, level = "info") {
  if (!host || !text) return;
  const el = h("div", { class: `toast ${level}` }, text);
  host.append(el);
  while (host.children.length > 4) host.firstChild.remove();
  setTimeout(() => el.remove(), level === "info" ? 5000 : 10000);
}

/** Ask a yes/no question in a modal dialog. Resolves true or false. */
export function confirmDialog({ title, body, confirm = "OK", cancel = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { "aria-labelledby": "dlg-title" },
      h("h2", { id: "dlg-title" }, title),
      h("div", { class: "col small" }, ...(Array.isArray(body) ? body : [body]).map((t) => h("p", {}, t))),
      h("div", { class: "actions" },
        h("button", { class: "btn", type: "button", onClick: () => dlg.close("no") }, cancel),
        h("button", { class: `btn ${danger ? "danger" : "primary"}`, type: "button", onClick: () => dlg.close("yes") }, confirm),
      ),
    );
    dlg.addEventListener("close", () => { const yes = dlg.returnValue === "yes"; dlg.remove(); resolve(yes); });
    document.body.append(dlg);
    dlg.showModal();
  });
}
