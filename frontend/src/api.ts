export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch("/api" + url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  if (r.status === 401) window.dispatchEvent(new Event("subsync:unauth"));
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ }
    throw new ApiError(r.status, msg);
  }
  return r.json() as Promise<T>;
}
/** Envia bytes (um ficheiro) como corpo do pedido; os parâmetros vão no URL. */
export async function uploadFile<T>(url: string, file: File): Promise<T> {
  const r = await fetch("/api" + url, { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" }, credentials: "same-origin" });
  if (r.status === 401) window.dispatchEvent(new Event("subsync:unauth"));
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ }
    throw new ApiError(r.status, msg);
  }
  return r.json() as Promise<T>;
}
export const api = {
  get: <T,>(u: string) => req<T>("GET", u),
  post: <T,>(u: string, b?: unknown) => req<T>("POST", u, b ?? {}),
  put: <T,>(u: string, b: unknown) => req<T>("PUT", u, b),
  patch: <T,>(u: string, b: unknown) => req<T>("PATCH", u, b),
  del: <T,>(u: string) => req<T>("DELETE", u),
};
export const qs = (o: Record<string, unknown>) => {
  const p = new URLSearchParams();
  Object.entries(o).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") p.set(k, String(v)); });
  const s = p.toString();
  return s ? "?" + s : "";
};

export interface Item {
  id: number; kind: "movie" | "episode"; title: string; year: number | null; series_title: string | null;
  season: number | null; episode: number | null; episode_title: string | null; file_name: string; path: string;
  duration: number | null; width: number | null; height: number | null; video_codec: string | null; fps: number | null;
  has_poster: boolean; fallback?: Record<string, string>; covered: Record<string, "external" | "embedded">; missing: string[];
  n_external: number; n_embedded: number; suspect: boolean; unverified: boolean; unknown_lang: number; library_id: number; imdb_rating?: number | null; imdb_votes?: number | null; added_at?: string | null; radarr_id?: number | null; file_title?: string | null;
}
export interface Issue { code: string; severity: "info" | "warning" | "error"; message: string }
export interface Sub {
  id: number; kind: "external" | "embedded"; path: string; stream_index: number | null; format: string; lang: string | null;
  lang_source: string; detected_lang: string | null; forced: number; hi: number; text_based: number; size: number | null;
  cue_count: number | null; last_end_ms: number | null; status: string; issues: Issue[]; association: string;
  sync_status: string; sync_detail: string | null;
}
export interface Factor { label: string; points: number; kind: string }
export interface Evaluation {
  score: number; compat: "user" | "hash" | "probable" | "weak" | "incompatible"; compat_label: string;
  sync: "validated_reference" | "not_confirmed" | "suspect" | "inconclusive"; sync_label: string;
  factors: Factor[]; duration_diff_s: number | null; notes: string[]; disclaimer: string;
  alignment: null | { reference: string; offset_ms: number; match_ratio: number; drift_ms: number; events_compared: number };
  writable_error?: string | null; translation?: { engine: string; source_lang: string | null; target: string; characters: number }; existing?: string[]; encoding?: string; library?: string; cue_count?: number;
}
export interface SubResult {
  provider: string; file_id: string; language: string | null; release: string; file_name: string; hash_match: boolean;
  hearing_impaired: boolean; forced: boolean; fps: number | null; download_count: number | null; rating: number | null;
  from_trusted: boolean; ai_translated: boolean; machine_translated: boolean; uploader: string | null; upload_date: string | null;
  feature_title: string | null; season: number | null; episode: number | null; page_url: string | null; found_by?: string | null; via?: string | null; extra?: { provider_label?: string; bazarr_score?: number; matches?: string[]; dont_matches?: string[] } | null; [k: string]: unknown;
}
export interface Candidate { result: SubResult; evaluation: Evaluation }
export interface Download {
  id: number; media_id: number; media_title: string | null; provider: string; lang: string | null; release: string | null;
  file_name: string | null; dest_path: string | null; status: string; error: string | null; score: number | null;
  evaluation: Evaluation | null; conflict: string | null; created_at: string; installed_at: string | null;
}
export interface Job {
  id: number; type: string; title: string; status: string; progress: number; total: number; current: string | null;
  message: string | null; error: string | null; created_at: string; started_at: string | null; finished_at: string | null;
  result: Record<string, unknown> | null; params: Record<string, unknown>;
}
export interface Library { id: number; name: string; type: "movies" | "series"; path: string; writable: number; exists: boolean; os_writable: boolean; effective_write: boolean; last_scan_at: string | null; counts: { media: number } }
export interface Meta { version: string; languages: { code: string; name: string }[]; auth_required: boolean; ffprobe: boolean; allowed_roots: string[]; writable_roots: string[] }
export type Settings = Record<string, any>;
