export function fmtElapsed(seconds) {
  if (seconds == null) return "00:00";
  const t = Math.max(0, Math.floor(seconds));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}

/** "ServiceStatus name" -> words: PelletLost -> "pellet lost". */
export function words(camel) {
  return String(camel || "").replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase();
}

export function plural(n, one, many) { return `${n} ${n === 1 ? one : many || one + "s"}`; }

export function seconds(n) {
  if (n == null) return "";
  return `${Number(n.toFixed(3))} s`;
}
