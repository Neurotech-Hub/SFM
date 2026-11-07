// Talks to the gateway: commands over POST, state over a WebSocket.

const HEADERS = { "Content-Type": "application/json", "X-SFM-Request": "1" };
const COMMAND_TIMEOUT_MS = 35000;

/** Run an engine command. Never throws: always {ok, ...} with `error` set when ok is false. */
export async function command(name, args = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), COMMAND_TIMEOUT_MS);
  try {
    const res = await fetch(`/api/cmd/${encodeURIComponent(name)}`, {
      method: "POST", headers: HEADERS, body: JSON.stringify(args), signal: ctl.signal,
    });
    const body = await res.json().catch(() => null);
    if (body && typeof body === "object") return body;
    return { ok: false, error: `The gateway answered ${res.status}.` };
  } catch (err) {
    return { ok: false, error: err.name === "AbortError" ? `${name} did not answer in time.` : "The gateway is not reachable." };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Keep a WebSocket to the gateway open. `onMessage({engine, state})` for each push,
 * `onLink(bool)` when the link to the gateway itself goes up or down.
 * Reconnects with a growing pause (0.5 s up to 5 s).
 */
export function watchState(onMessage, onLink) {
  let attempt = 0, closed = false, ws = null, timer = null;
  const open = () => {
    if (closed) return;
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => { attempt = 0; onLink(true); };
    ws.onmessage = (e) => {
      try { const m = JSON.parse(e.data); if (m && m.type === "state") onMessage(m); } catch { /* ignore a bad frame */ }
    };
    ws.onclose = () => {
      onLink(false);
      if (closed) return;
      timer = setTimeout(open, Math.min(5000, 500 * 2 ** attempt++));
    };
    ws.onerror = () => ws.close();
  };
  open();
  return () => { closed = true; clearTimeout(timer); if (ws) ws.close(); };
}
