export const fmtDate = (s?: string | null) => {
  if (!s) return "—";
  const d = new Date(s);
  return d.toLocaleString("pt-PT", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
};
export const fmtDur = (s?: number | null) => {
  if (!s) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
};
export const fmtSize = (b?: number | null) => {
  if (b == null) return "—";
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b / 1024).toFixed(0) + " KB";
  return (b / 1048576).toFixed(1) + " MB";
};
export const epCode = (i: { season: number | null; episode: number | null }) =>
  i.season != null && i.episode != null ? `S${String(i.season).padStart(2, "0")}E${String(i.episode).padStart(2, "0")}` : "";
export const cx = (...a: (string | false | null | undefined)[]) => a.filter(Boolean).join(" ");
export const baseName = (p: string) => p.split("/").pop() || p;
export const langLabel = (c: string | null | undefined, langs?: { code: string; name: string }[]) =>
  !c ? "Desconhecido" : langs?.find((l) => l.code === c)?.name ?? c;
export const resolution = (h: number | null) => (!h ? "" : h >= 2000 ? "4K" : h >= 1000 ? "1080p" : h >= 700 ? "720p" : `${h}p`);
export const fmtTs = (ms: number) => {
  const neg = ms < 0; const t = Math.abs(Math.round(ms));
  const h = Math.floor(t / 3600000), m = Math.floor((t % 3600000) / 60000), sec = Math.floor((t % 60000) / 1000), r = t % 1000;
  return `${neg ? "-" : ""}${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")},${String(r).padStart(3, "0")}`;
};
