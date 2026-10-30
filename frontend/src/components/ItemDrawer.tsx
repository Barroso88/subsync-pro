import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowDownToLine, FileText, Layers, Loader2, RefreshCw, Search, ShieldAlert, Star, Download as DlIcon, Info, FolderOpen, Languages, Wrench, Upload } from "lucide-react";
import { api, uploadFile, Candidate, Download, Evaluation, Item, Issue, Sub } from "../api";
import { baseName, epCode, fmtDur, fmtSize, providerLabel, resolution, cx } from "../lib";
import { useLangFilter, useMeta } from "../hooks";
import { Badge, Drawer, Empty, Modal, Skeleton, Tabs, useToast } from "./ui";
import { CompatBadge, Factors, LangChips, RatingBadge, ScoreBar, SyncBadge, langName } from "./subs";
import { EditHistory, SubToolsModal } from "./SubTools";

interface Detail extends Item {
  dir: string; size: number; radarr_id?: number | null; subtitles: Sub[]; downloads: Download[]; moviehash: string | null; library_writable: boolean; probe_status: string;
}
interface SearchRes { results: Candidate[]; errors: { provider: string; message: string; auth?: boolean; rate_limited?: boolean; retry_after?: number }[]; query: { languages: string[]; hash_used: boolean; tried?: string[] } }

export function ItemDrawer({ id, onClose, initialTab = "subs" }: { id: number | null; onClose: () => void; initialTab?: "subs" | "search" }) {
  const [tab, setTab] = useState<"subs" | "search">(initialTab);
  useEffect(() => setTab(initialTab), [id, initialTab]);
  const q = useQuery({ queryKey: ["media", id], queryFn: () => api.get<Detail>(`/media/${id}`), enabled: id != null });
  const meta = useMeta();
  const qc = useQueryClient();
  const toast = useToast();
  const rescan = useMutation({
    mutationFn: () => api.post<{ job_id: number }>(`/media/${id}/rescan`),
    onSuccess: () => { toast("info", "Reanálise iniciada"); setTimeout(() => qc.invalidateQueries({ queryKey: ["media", id] }), 2500); },
  });
  const d = q.data;
  return (
    <Drawer open={id != null} onClose={onClose} title={d ? (d.kind === "episode" ? `${d.series_title} · ${epCode(d)}` : `${d.title}${d.year ? ` (${d.year})` : ""}`) : "A carregar…"}>
      {q.isLoading && <div className="p-5 space-y-3"><Skeleton className="h-6 w-2/3" /><Skeleton className="h-24" /><Skeleton className="h-40" /></div>}
      {d && (
        <div className="p-5 space-y-5">
          <div className="flex gap-4">
            <div className="w-24 shrink-0 aspect-[2/3] rounded-lg overflow-hidden bg-s2 border border-line grid place-items-center">
              {d.has_poster ? <img src={`/api/media/${d.id}/poster`} alt="" className="w-full h-full object-cover" loading="lazy" /> : <FileText className="text-dim" />}
            </div>
            <div className="min-w-0 space-y-2 text-sm">
              <div className="font-medium break-words">{d.kind === "episode" ? (d.episode_title || d.title) : d.title}</div>
              {d.kind === "movie" && d.file_title && d.file_title !== d.title && <div className="text-xs text-dim -mt-1">Título original (ficheiro): {d.file_title}</div>}
              <div className="flex flex-wrap gap-1.5">
                {d.video_codec && <Badge>{d.video_codec.toUpperCase()}</Badge>}
                {d.height && <Badge>{resolution(d.height, d.width)}</Badge>}
                {d.fps && <Badge>{d.fps} fps</Badge>}
                {d.imdb_rating ? <RatingBadge rating={d.imdb_rating} votes={d.imdb_votes} className="!h-6 !text-xs" /> : null}
                <Badge>{fmtDur(d.duration)}</Badge>
                <Badge>{fmtSize(d.size).replace(" MB", " MB")}</Badge>
              </div>
              <LangChips item={d} />
              <div className="text-xs text-dim break-all flex gap-1.5"><FolderOpen size={13} className="shrink-0 mt-0.5" />{d.path}</div>
              {!d.library_writable && <div className="text-xs text-warn flex gap-1.5"><ShieldAlert size={13} className="shrink-0 mt-0.5" />Biblioteca só de leitura: não é possível instalar legendas aqui (Definições → Bibliotecas).</div>}
              {d.probe_status !== "ok" && <div className="text-xs text-warn flex gap-1.5"><Info size={13} className="shrink-0 mt-0.5" />Sem dados do FFprobe: duração e legendas embutidas desconhecidas.</div>}
            </div>
          </div>
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <Tabs value={tab} onChange={setTab} items={[{ id: "subs", label: "Legendas", count: d.subtitles.length }, { id: "search", label: "Procurar online" }]} />
            <button className="btn-quiet" onClick={() => rescan.mutate()} disabled={rescan.isPending}><RefreshCw size={14} className={cx(rescan.isPending && "animate-spin")} />Reanalisar</button>
          </div>
          {tab === "subs" ? <SubsList d={d} onSearch={() => setTab("search")} /> : <SearchPanel d={d} meta={meta.data} />}
          <EditHistory mediaId={d.id} />
        </div>
      )}
    </Drawer>
  );
}

// Traduzir uma faixa de legenda de TEXTO embutida no vídeo (extração com ffmpeg só quando o utilizador pede; tarefa em segundo plano)
function EmbeddedTranslate({ d, sub }: { d: Detail; sub: Sub }) {
  const qc = useQueryClient();
  const toast = useToast();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api.get<Record<string, any>>("/settings") });
  const tr = useQuery({ queryKey: ["translation"], queryFn: () => api.get<any>("/translation"), enabled: !!settings.data?.translation?.enabled });
  const baseLangs: string[] = settings.data ? settings.data.languages.map((l: any) => l.code) : [];
  const targets = baseLangs.filter((c) => ["pt-PT", "pt-BR", "es", "fr", "de", "it", "nl"].includes(c) && c !== sub.lang);
  const [target, setTarget] = useState("");
  const [ask, setAsk] = useState(false);
  const [job, setJob] = useState<number | null>(null);
  const [prepared, setPrepared] = useState<Download | null>(null);
  useEffect(() => { if (!target && targets.length) setTarget(targets[0]); }, [targets.join(",")]);   // eslint-disable-line react-hooks/exhaustive-deps
  const engine = tr.data?.engine === "gemini" ? "Gemini" : "DeepL";
  const start = useMutation({
    mutationFn: () => api.post<{ job_id: number }>(`/media/${d.id}/translate-embedded/start`, { subtitle_id: sub.id, target }),
    onSuccess: (r) => setJob(r.job_id), onError: (e: Error) => { toast("bad", e.message); setAsk(false); },
  });
  const jq = useQuery({ queryKey: ["translate-embedded-job", job], enabled: job != null,
    queryFn: () => api.get<{ queue_position: number | null; status: string; progress: number; total: number; message: string | null; current: string | null; error: string | null; result: Download | null }>(`/jobs/${job}`),
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 1000 : false) });
  const running = job != null && (!jq.data || ["queued", "running"].includes(jq.data.status));
  useEffect(() => {
    const j = jq.data;
    if (!j || job == null) return;
    if (j.status === "done" && j.result) { setPrepared(j.result); setAsk(false); setJob(null); qc.invalidateQueries({ queryKey: ["downloads"] }); }
    else if (["failed", "cancelled", "interrupted"].includes(j.status)) { toast(j.status === "failed" ? "bad" : "warn", j.status === "failed" ? (j.error || "A tradução falhou.") : "Tradução cancelada."); setAsk(false); setJob(null); }
  }, [jq.data?.status]);   // eslint-disable-line react-hooks/exhaustive-deps
  const cancel = useMutation({ mutationFn: () => api.post(`/jobs/${job}/cancel`), onError: (e: Error) => toast("bad", e.message) });
  if (!settings.data?.translation?.enabled || !tr.data?.configured || !targets.length || !sub.text_based || !!sub.forced) return null;
  const pct = jq.data?.total ? Math.min(100, (100 * jq.data.progress) / jq.data.total) : 0;
  return (
    <>
      <div className="flex items-center gap-2 flex-wrap">
        <Languages size={14} className="text-mute" /><span className="text-xs text-mute">Traduzir esta faixa para</span>
        <select className="input !h-8 !w-auto !text-xs" value={target} onChange={(e) => setTarget(e.target.value)}>{targets.map((t) => <option key={t}>{t}</option>)}</select>
        <button className="btn-ghost !h-8" onClick={() => setAsk(true)}>Traduzir…</button>
      </div>
      <Modal open={ask} onClose={() => setAsk(false)} title={`Traduzir faixa embutida para ${target} (${engine})`}
        footer={running ? (<><button className="btn-ghost" onClick={() => setAsk(false)}>Fechar (continua em segundo plano)</button>
          <button className="btn-danger" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Cancelar tradução</button></>) : (<><button className="btn-ghost" onClick={() => setAsk(false)}>Cancelar</button>
          <button className="btn-primary" disabled={start.isPending} onClick={() => start.mutate()}>{start.isPending && <Loader2 size={14} className="animate-spin" />}Extrair e traduzir</button></>)}>
        <div className="space-y-3 text-sm">
          {running && (
            <div className="card bg-s2 p-3.5 space-y-2">
              <div className="flex items-center gap-2 font-medium"><Loader2 size={15} className="animate-spin text-accent" />{jq.data?.status === "queued" ? `Em fila de espera${jq.data.queue_position ? ` (${jq.data.queue_position} à frente)` : " (a seguir)"}` : jq.data?.current || "A iniciar…"}</div>
              <div className="h-2 rounded-full bg-s3 overflow-hidden"><div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${jq.data?.total ? pct : 4}%` }} /></div>
              <div className="flex justify-between text-xs text-mute"><span>{jq.data?.status === "queued" ? "As traduções correm uma de cada vez, por causa dos limites do serviço" : jq.data?.message || "A preparar…"}</span><span className="tabular-nums">{jq.data?.total ? `${Math.round(pct)} %` : ""}</span></div>
              <div className="text-xs text-dim">Pode fechar esta janela: continua em segundo plano (Tarefas e atividade) e a legenda fica em «Histórico de downloads» por confirmar.</div>
            </div>
          )}
          <div>Vai traduzir a faixa embutida <b>#{sub.stream_index}</b> ({sub.lang ?? "idioma desconhecido"}) para <b>{target}</b> e gravar um ficheiro de legenda externo ao lado do vídeo, mantendo os tempos da faixa.</div>
          <div className="card bg-s2 p-3 text-xs text-mute space-y-1">
            <div>• O ffmpeg lê o vídeo para extrair <b>só esta faixa</b> (só leitura). Em ficheiros grandes na rede pode demorar de alguns segundos a alguns minutos.</div>
            <div>• O texto é enviado ao {tr.data?.engine === "gemini" ? "Gemini (Google)" : "DeepL"}.</div>
            <div>• Fica «por confirmar» e assinalada como <b>tradução automática</b>; reveja a pré-visualização antes de instalar. O vídeo nunca é alterado.</div>
          </div>
        </div>
      </Modal>
      <PreviewModal dl={prepared} onClose={() => setPrepared(null)} writable={d.library_writable} onDone={() => { qc.invalidateQueries({ queryKey: ["media", d.id] }); qc.invalidateQueries(); }} />
    </>
  );
}

// Enviar uma legenda do próprio utilizador: valida, compara com uma referência (se existir) e deixa em «por confirmar»
function UploadSubtitle({ d }: { d: Detail }) {
  const qc = useQueryClient();
  const toast = useToast();
  const meta = useMeta();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api.get<Record<string, any>>("/settings") });
  const [file, setFile] = useState<File | null>(null);
  const [lang, setLang] = useState("auto");
  const [forced, setForced] = useState(false);
  const [hi, setHi] = useState(false);
  const [prepared, setPrepared] = useState<Download | null>(null);
  const langs: { code: string; name: string }[] = meta.data?.languages ?? [];
  const prio: string[] = settings.data ? settings.data.languages.map((l: any) => l.code) : [];
  const ordered = [...langs.filter((l) => prio.includes(l.code)), ...langs.filter((l) => !prio.includes(l.code))];
  const send = useMutation({
    mutationFn: () => uploadFile<Download>(`/media/${d.id}/upload` + `?filename=${encodeURIComponent(file!.name)}&lang=${encodeURIComponent(lang)}&forced=${forced}&hi=${hi}`, file!),
    onSuccess: (r) => { setPrepared(r); setFile(null); qc.invalidateQueries({ queryKey: ["downloads"] }); },
    onError: (e: Error) => toast("bad", e.message),
  });
  return (
    <>
      <label className={cx("btn-ghost cursor-pointer", !d.library_writable && "opacity-50 pointer-events-none")} title={d.library_writable ? "" : "Biblioteca só de leitura"}>
        <Upload size={14} />Carregar legenda
        <input type="file" className="hidden" accept=".srt,.ass,.ssa,.vtt,.zip" onChange={(e) => { const f = e.target.files?.[0]; if (f) { setFile(f); setLang("auto"); setForced(false); setHi(false); } e.target.value = ""; }} />
      </label>
      <Modal open={!!file} onClose={() => setFile(null)} title="Carregar legenda"
        footer={<><button className="btn-ghost" onClick={() => setFile(null)}>Cancelar</button>
          <button className="btn-primary" disabled={send.isPending} onClick={() => send.mutate()}>{send.isPending && <Loader2 size={14} className="animate-spin" />}Enviar e pré-visualizar</button></>}>
        <div className="space-y-3 text-sm">
          <div>Ficheiro: <b className="break-all font-mono text-xs">{file?.name}</b> <span className="text-dim">({file ? fmtSize(file.size) : ""})</span></div>
          <div className="flex items-center gap-2 flex-wrap"><span className="text-mute">Idioma</span>
            <select className="input !w-auto" value={lang} onChange={(e) => setLang(e.target.value)}>
              <option value="auto">Detetar automaticamente</option>{ordered.map((l) => <option key={l.code} value={l.code}>{l.name} ({l.code})</option>)}</select></div>
          <div className="flex gap-4 flex-wrap">
            <label className="flex items-center gap-2 cursor-pointer"><input type="checkbox" className="accent-[rgb(229,160,13)]" checked={forced} onChange={(e) => setForced(e.target.checked)} />Forçada (só partes estrangeiras)</label>
            <label className="flex items-center gap-2 cursor-pointer"><input type="checkbox" className="accent-[rgb(229,160,13)]" checked={hi} onChange={(e) => setHi(e.target.checked)} />Para surdos (SDH/HI)</label>
          </div>
          {send.isPending && <div className="card bg-s2 p-3 text-xs text-mute flex gap-2"><Loader2 size={14} className="animate-spin shrink-0 text-accent" />A validar e a comparar os tempos. Se o filme só tiver uma faixa embutida como referência, a app tem de a extrair do vídeo (ffmpeg): em ficheiros grandes na rede pode demorar até alguns minutos. O resto da app continua a funcionar.</div>}
          <div className="card bg-s2 p-3 text-xs text-mute space-y-1">
            <div>• O ficheiro é validado (SRT, ASS, SSA, VTT ou um .zip com uma legenda) e <b>não é gravado na biblioteca</b> até confirmar.</div>
            <div>• Se existir outra legenda do filme, compara os tempos e diz se coincidem; se já existir uma no mesmo idioma, <b>nunca é substituída em silêncio</b>.</div>
            <div>• O nome final segue o padrão do Plex (<span className="font-mono">Filme.pt-PT.srt</span>).</div>
          </div>
        </div>
      </Modal>
      <PreviewModal dl={prepared} onClose={() => setPrepared(null)} writable={d.library_writable} onDone={() => { qc.invalidateQueries({ queryKey: ["media", d.id] }); qc.invalidateQueries(); }} />
    </>
  );
}

const ISSUE_TONE: Record<string, "bad" | "warn" | "neutral"> = { error: "bad", warning: "warn", info: "neutral" };

function SubsList({ d, onSearch }: { d: Detail; onSearch: () => void }) {
  const meta = useMeta();
  const [tools, setTools] = useState<Sub | null>(null);
  const lf = useLangFilter();
  const origins = useQuery({ queryKey: ["origins", d.id], queryFn: () => api.get<{ subtitle_id: number; origin: string; provider: string | null; provider_label: string | null; detail: string | null; at: string | null }[]>(`/media/${d.id}/origins`), staleTime: 60_000 });
  const originOf = (id: number) => origins.data?.find((o) => o.subtitle_id === id);
  const [showAll, setShowAll] = useState(false);
  const needsTr = d.missing.length > 0;                          // falta idioma: as de origem de tradução (ex.: inglês) também aparecem
  const shown = showAll ? d.subtitles : d.subtitles.filter((s) => lf.visible(s.lang, needsTr));
  const hiddenN = d.subtitles.length - d.subtitles.filter((s) => lf.visible(s.lang, needsTr)).length;
  if (!d.subtitles.length) return <Empty icon={<FileText size={20} />} title="Sem legendas" hint="Não foram encontradas legendas externas nem embutidas para este vídeo." action={<div className="flex gap-2 flex-wrap justify-center"><button className="btn-primary" onClick={onSearch}><Search size={14} />Procurar online</button><UploadSubtitle d={d} /></div>} />;
  return (
    <div className="space-y-2.5">
      <div className="flex justify-end items-center gap-3 flex-wrap">
        {hiddenN > 0 && <button className="text-xs text-mute hover:text-fg underline" onClick={() => setShowAll(!showAll)}>{showAll ? "Esconder as de outros idiomas e sem idioma" : `Mostrar ${hiddenN} legendas noutros idiomas ou sem idioma (escondidas)`}</button>}
        <UploadSubtitle d={d} /></div>
      {shown.length === 0 && <div className="text-sm text-mute card p-3.5">Todas as legendas deste filme estão noutros idiomas ou sem idioma conhecido (escondidas). Use «Mostrar» acima ou ajuste em Definições → Idiomas.</div>}
      {shown.map((s) => (
        <div key={s.id} className="card p-3.5 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            {s.kind === "embedded" ? <Layers size={15} className="text-mute" /> : <FileText size={15} className="text-mute" />}
            <span className="font-medium text-sm">{langName(meta.data, s.lang)}</span>
            {s.lang && <Badge>{s.lang}</Badge>}
            <Badge tone={s.kind === "embedded" ? "info" : "neutral"}>{s.kind === "embedded" ? "Embutida" : "Externa"}</Badge>
            {!!s.forced && <Badge tone="warn">Forçada</Badge>}
            {!!s.hi && <Badge tone="neutral">SDH/HI</Badge>}
            {!s.lang && <Badge tone="warn">Idioma desconhecido</Badge>}
            {s.kind === "embedded" && !s.text_based && <Badge title="Legenda gráfica (PGS/VobSub): não analisável">Imagem</Badge>}
            {s.kind === "external" && (() => {
              const o = originOf(s.id);
              if (!o) return origins.isLoading ? null : <Badge title="Só se conhece a origem das legendas instaladas pela app, pedidas ao Bazarr (pelo histórico dele), enviadas por si ou traduzidas aqui.">Origem desconhecida</Badge>;
              const label = o.origin === "app" ? `App · ${o.provider_label ?? o.provider}` : o.origin === "bazarr" ? `Bazarr · ${o.provider_label ?? o.provider}`
                : o.origin === "upload" ? "Enviada por si" : `Traduzida · ${o.provider_label ?? o.provider}`;
              return <Badge tone={o.origin === "bazarr" ? "accent" : "info"} title={[o.detail, o.at ? new Date(o.at).toLocaleString("pt-PT") : null].filter(Boolean).join(" · ") || undefined}>{label}</Badge>;
            })()}
            {s.association !== "certain" && <Badge tone="warn" title="Associação baseada em heurística">Associação {s.association === "probable" ? "provável" : "incerta"}</Badge>}
            {s.status !== "ok" && <Badge tone="bad">{s.status === "empty" ? "Vazia" : "Inválida"}</Badge>}
          </div>
          <div className="text-xs text-dim break-all">{s.kind === "embedded" ? `Stream #${s.stream_index} · ${s.format}` : baseName(s.path)}</div>
          {s.kind === "external" && s.status === "ok" && (
            <div className="flex gap-3 text-xs text-mute flex-wrap">
              <span>{s.cue_count} eventos</span><span>{s.format?.toUpperCase()}</span><span>{fmtSize(s.size)}</span>
              {s.lang_source === "content" && <span>idioma estimado pelo conteúdo</span>}
              <span className={s.sync_status === "suspect" ? "text-bad" : ""}>
                {s.sync_status === "plausible" ? "Limites temporais plausíveis · sincronização não confirmada" : s.sync_status === "suspect" ? `Possível dessincronização: ${s.sync_detail}` : "Sincronização por verificar"}
              </span>
            </div>
          )}
          {s.issues.map((i: Issue, k: number) => (
            <div key={k} className="flex gap-2 text-xs"><AlertTriangle size={13} className={cx("shrink-0 mt-0.5", i.severity === "error" ? "text-bad" : i.severity === "warning" ? "text-warn" : "text-dim")} /><span className="text-mute">{i.message}</span></div>
          ))}
          {s.kind === "external" && s.status === "ok" && <div><button className="btn-ghost !h-8" onClick={() => setTools(s)}><Wrench size={13} />Ferramentas</button></div>}
          {s.kind === "embedded" && <EmbeddedTranslate d={d} sub={s} />}
          {s.issues.some((i) => ["duplicate", "language_mismatch", "name_mismatch", "beyond_video", "truncated", "unassociated"].includes(i.code)) && (
            <div className="text-xs text-info bg-info/10 rounded-md px-2.5 py-1.5">Recomendação: reveja manualmente este ficheiro. A aplicação nunca altera nem apaga legendas existentes.</div>
          )}
        </div>
      ))}
      <SubToolsModal sub={tools ? { id: tools.id, name: baseName(tools.path), lang: tools.lang, canEdit: d.library_writable, isSrt: tools.path.toLowerCase().endsWith(".srt"), isText: /\.(srt|ass|ssa|vtt)$/i.test(tools.path) } : null} onClose={() => setTools(null)} />
    </div>
  );
}

function SearchPanel({ d, meta }: { d: Detail; meta: any }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [res, setRes] = useState<SearchRes | null>(null);
  const [langs, setLangs] = useState<string[] | undefined>(undefined);
  const [detail, setDetail] = useState<Candidate | null>(null);
  const [prepared, setPrepared] = useState<Download | null>(null);
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api.get<Record<string, any>>("/settings") });
  const baseLangs: string[] = settings.data ? settings.data.languages.map((l: any) => l.code) : [];
  const trOn = !!settings.data?.translation?.enabled;
  const sources: string[] = trOn ? (settings.data?.translation?.source_languages ?? []) : [];
  const configured: string[] = [...baseLangs, ...sources.filter((x) => !baseLangs.includes(x))];
  const tr = useQuery({ queryKey: ["translation"], queryFn: () => api.get<any>("/translation"), enabled: !!settings.data?.translation?.enabled });
  const targets: string[] = baseLangs.filter((c) => ["pt-PT", "pt-BR", "es", "fr", "de", "it", "nl"].includes(c));
  const canTranslate = !!settings.data?.translation?.enabled && tr.data?.configured && targets.length > 0;
  const [target, setTarget] = useState<string>("");
  const [askTr, setAskTr] = useState<Candidate | null>(null);
  useEffect(() => { if (!target && targets.length) setTarget(targets[0]); }, [targets.join(",")]);
  const bz = useQuery({ queryKey: ["integrations"], queryFn: () => api.get<Record<string, any>>("/integrations") });
  const bazarrOn = !!bz.data?.bazarr?.enabled && !!bz.data?.bazarr?.secret_set && d.kind === "movie" && !!d.radarr_id && d.library_writable;
  const [bzBusy, setBzBusy] = useState<string | null>(null);
  const askBazarr = async (lang: string) => {
    setBzBusy(lang);
    try {
      const r = await api.post<{ found: boolean; message: string }>(`/media/${d.id}/bazarr`, { language: lang });
      toast(r.found ? "ok" : "warn", r.message);
      qc.invalidateQueries();
    } catch (e) { toast("bad", (e as Error).message); } finally { setBzBusy(null); }
  };
  // tradução em segundo plano: arranca uma tarefa e mostra o progresso (continua mesmo que o modal seja fechado)
  const [trJob, setTrJob] = useState<number | null>(null);
  const trans = useMutation({
    mutationFn: (c: Candidate) => api.post<{ job_id: number }>(`/media/${d.id}/translate/start`, { result: c.result, target }),
    onSuccess: (r) => setTrJob(r.job_id),
    onError: (e: Error) => { toast("bad", e.message); setAskTr(null); },
  });
  const trJobQ = useQuery({ queryKey: ["translate-job", trJob], enabled: trJob != null,
    queryFn: () => api.get<{ id: number; queue_position: number | null; status: string; progress: number; total: number; message: string | null; current: string | null; error: string | null; result: Download | null }>(`/jobs/${trJob}`),
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 1000 : false) });
  const trRunning = trJob != null && (!trJobQ.data || ["queued", "running"].includes(trJobQ.data.status));
  useEffect(() => {
    const j = trJobQ.data;
    if (!j || trJob == null) return;
    if (j.status === "done" && j.result) { setPrepared(j.result); setAskTr(null); setTrJob(null); qc.invalidateQueries({ queryKey: ["downloads"] }); qc.invalidateQueries({ queryKey: ["translation"] }); }
    else if (["failed", "cancelled", "interrupted"].includes(j.status)) { if (j.status === "failed") toast("bad", j.error || "A tradução falhou."); else toast("warn", "Tradução cancelada."); setAskTr(null); setTrJob(null); }
  }, [trJobQ.data?.status]);   // eslint-disable-line react-hooks/exhaustive-deps
  const cancelTr = useMutation({ mutationFn: () => api.post(`/jobs/${trJob}/cancel`), onError: (e: Error) => toast("bad", e.message) });
  const bazarrSearchOn = !!bz.data?.bazarr?.enabled && !!bz.data?.bazarr?.secret_set && d.kind === "movie" && !!d.radarr_id;
  const [useApp, setUseApp] = useState(true);
  const [useBazarr, setUseBazarr] = useState(true);
  const search = useMutation({
    mutationFn: () => api.post<SearchRes>(`/media/${d.id}/search`, { languages: langs, ...(bazarrSearchOn ? { sources: [...(useApp ? ["app"] : []), ...(useBazarr ? ["bazarr"] : [])] } : {}) }),
    onSuccess: setRes, onError: (e: Error) => toast("bad", e.message),
  });
  // candidata do Bazarr: o Bazarr descarrega e grava diretamente na pasta do filme; a app valida depois
  const bzDownload = useMutation({
    mutationFn: (c: Candidate) => api.post<{ found: boolean; message: string }>(`/media/${d.id}/bazarr/download`, { result: c.result }),
    onSuccess: (r) => { toast(r.found ? "ok" : "warn", r.message); setDetail(null); qc.invalidateQueries(); },
    onError: (e: Error) => toast("bad", e.message),
  });
  const pickCandidate = (c: Candidate) => {
    if (c.result.provider === "bazarr") {
      if (window.confirm(`O Bazarr vai descarregar esta legenda (${c.result.extra?.provider_label ?? c.result.via}) e gravá-la diretamente na pasta do filme, sem pré-visualização.\n\nSe substituir um ficheiro existente, a app guarda uma cópia restaurável no histórico de alterações. Continuar?`)) bzDownload.mutate(c);
    } else prep.mutate(c);
  };
  const prep = useMutation({
    mutationFn: (c: Candidate) => api.post<Download>(`/media/${d.id}/prepare`, { result: c.result, language: c.result.language }),
    onSuccess: (r) => { setPrepared(r); setDetail(null); qc.invalidateQueries({ queryKey: ["downloads"] }); },
    onError: (e: Error) => toast("bad", e.message),
  });
  return (
    <div className="space-y-4">
      <div className="card p-3.5 space-y-3">
        <div className="flex flex-wrap gap-1.5 items-center">
          <span className="label mr-1">Idiomas</span>
          {configured.map((c) => {
            const on = !langs || langs.includes(c);
            return <button key={c} onClick={() => setLangs((cur) => { const base = cur ?? configured; return base.includes(c) ? base.filter((x) => x !== c) : [...base, c]; })}
              className={cx("h-6 px-2 rounded-md text-xs border", on ? "bg-accent/15 border-accent/40 text-accent" : "border-line text-dim")}>{c}</button>;
          })}
        </div>
        {bazarrSearchOn && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs">
            <span className="label mr-1">Fontes</span>
            <label className="flex items-center gap-1.5 cursor-pointer"><input type="checkbox" className="accent-[rgb(229,160,13)]" checked={useApp} onChange={(e) => setUseApp(e.target.checked)} />App (OpenSubtitles, SubDL…)</label>
            <label className="flex items-center gap-1.5 cursor-pointer"><input type="checkbox" className="accent-[rgb(229,160,13)]" checked={useBazarr} onChange={(e) => setUseBazarr(e.target.checked)} />Bazarr (fornecedores dele; demora mais)</label>
          </div>
        )}
        <button className="btn-primary w-full" onClick={() => search.mutate()} disabled={search.isPending || (bazarrSearchOn && !useApp && !useBazarr)}>
          {search.isPending ? <Loader2 size={15} className="animate-spin" /> : <Search size={15} />}Procurar legendas
        </button>
        <div className="text-[11px] text-dim">A pesquisa usa o hash do ficheiro quando o fornecedor o suporta. Nada é descarregado nem escrito até confirmar.</div>
      </div>
      {res && !trOn && (
        <div className="card border-info/30 bg-info/5 p-3.5 text-sm flex gap-2.5"><Languages size={16} className="text-info shrink-0 mt-0.5" />
          <div>Sem legenda no seu idioma? Pode <b>traduzir automaticamente</b> uma legenda inglesa (DeepL ou Gemini). Ative-o em <Link to="/definicoes" className="text-accent underline">Definições → Idiomas</Link> e coloque a chave em <Link to="/fornecedores" className="text-accent underline">Fornecedores</Link>.</div></div>
      )}
      {res && trOn && !tr.data?.configured && !tr.isLoading && (
        <div className="card border-warn/30 bg-warn/5 p-3.5 text-sm flex gap-2.5"><Languages size={16} className="text-warn shrink-0 mt-0.5" />
          <div>A tradução está ativada mas falta a chave do {tr.data?.engine === "gemini" ? "Gemini" : "DeepL"}: <Link to="/fornecedores" className="text-accent underline">Fornecedores → Tradução</Link>.</div></div>
      )}
      {bazarrOn && (
        <div className="card p-3.5 space-y-2.5">
          <div className="text-sm font-medium">Pedir ao Bazarr <span className="text-xs text-mute font-normal">· fonte: Bazarr (fornecedores dele)</span></div>
          <div className="text-xs text-mute">O Bazarr procura nos fornecedores dele (ex.: LegendasDivx) e grava a legenda; a app valida-a depois. Pode demorar até um minuto.</div>
          <div className="flex gap-2 flex-wrap">{baseLangs.filter((l) => ["pt-PT", "pt-BR", "en", "es", "fr", "de", "it"].includes(l)).map((l) => (
            <button key={l} className="btn-ghost" disabled={bzBusy !== null} onClick={() => askBazarr(l)}>{bzBusy === l ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}{l}</button>
          ))}</div>
        </div>
      )}
      {res?.errors.map((e, i) => (
        <div key={i} className="card border-warn/30 p-3 text-sm flex gap-2.5"><AlertTriangle size={16} className="text-warn shrink-0 mt-0.5" /><div><b className="font-medium">{e.provider}</b>: {e.message}{e.auth && <span className="text-mute"> — configure em Fornecedores.</span>}{e.retry_after ? ` Tente novamente em ${e.retry_after}s.` : ""}</div></div>
      ))}
      {search.isPending && <div className="space-y-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-24" />)}</div>}
      {res && !search.isPending && res.results.length === 0 && !res.errors.length && (
        <>
          <Empty icon={<Search size={20} />} title="Sem resultados" hint="O fornecedor não devolveu legendas para os idiomas escolhidos, mesmo após as pesquisas alargadas." />
          {!!res.query.tried?.length && <div className="text-xs text-dim -mt-6 text-center">Tentado: {res.query.tried.join(" · ")}</div>}
        </>
      )}
      {res && canTranslate && res.results.length > 0 && !res.results.some((c) => c.result.language === target) && (
        <div className="card border-info/30 bg-info/5 p-3.5 text-sm flex gap-2.5"><Languages size={16} className="text-info shrink-0 mt-0.5" /><div>Sem resultados em <b>{target}</b>. Pode traduzir uma legenda de outro idioma (botão «Traduzir» no resultado).</div></div>
      )}
      {res && canTranslate && (
        <div className="flex items-center gap-2 text-xs text-mute"><Languages size={14} />Traduzir para
          <select className="input !h-7 !w-auto !text-xs" value={target} onChange={(e) => setTarget(e.target.value)}>{targets.map((t) => <option key={t}>{t}</option>)}</select></div>
      )}
      {res?.results.map((c, i) => <ResultCard key={i} c={c} meta={meta} onOpen={() => setDetail(c)} onPick={() => pickCandidate(c)} busy={prep.isPending || bzDownload.isPending}
        onTranslate={canTranslate && c.result.language !== target && c.result.provider !== "bazarr" ? () => setAskTr(c) : undefined} />)}
      <Modal open={!!askTr} onClose={() => setAskTr(null)} title={`Traduzir para ${target} (${tr.data?.engine === "gemini" ? "Gemini" : "DeepL"})`}
        footer={trRunning ? (<><button className="btn-ghost" onClick={() => setAskTr(null)}>Fechar (continua em segundo plano)</button>
          <button className="btn-danger" disabled={cancelTr.isPending} onClick={() => cancelTr.mutate()}>Cancelar tradução</button></>) : (<><button className="btn-ghost" onClick={() => setAskTr(null)}>Cancelar</button>
          <button className="btn-primary" disabled={trans.isPending} onClick={() => askTr && trans.mutate(askTr)}>{trans.isPending && <Loader2 size={14} className="animate-spin" />}Descarregar e traduzir</button></>)}>
        <div className="space-y-3 text-sm">
          {trRunning && (
            <div className="card bg-s2 p-3.5 space-y-2">
              <div className="flex items-center gap-2 font-medium"><Loader2 size={15} className="animate-spin text-accent" />{trJobQ.data?.status === "queued" ? `Em fila de espera${trJobQ.data.queue_position ? ` (${trJobQ.data.queue_position} à frente)` : " (a seguir)"}` : trJobQ.data?.current || "A iniciar…"}</div>
              <div className="h-2 rounded-full bg-s3 overflow-hidden"><div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${trJobQ.data?.total ? Math.min(100, (100 * trJobQ.data.progress) / trJobQ.data.total) : 4}%` }} /></div>
              <div className="flex justify-between text-xs text-mute"><span>{trJobQ.data?.status === "queued" ? "As traduções correm uma de cada vez, por causa dos limites do serviço" : trJobQ.data?.message || "A preparar…"}</span><span className="tabular-nums">{trJobQ.data?.total ? `${Math.round((100 * trJobQ.data.progress) / trJobQ.data.total)} %` : ""}</span></div>
              <div className="text-xs text-dim">Pode fechar esta janela: a tradução continua em segundo plano (Tarefas e atividade) e, quando acabar, a legenda fica em «Histórico de downloads» por confirmar.</div>
            </div>
          )}
          <div>Vai descarregar <b className="break-all">{askTr?.result.release || askTr?.result.file_name}</b> ({askTr?.result.language}) e traduzir o texto para <b>{target}</b>, mantendo os tempos.</div>
          <div className="card bg-s2 p-3 text-xs text-mute space-y-1">
            <div>• O texto da legenda é enviado ao {tr.data?.engine === "gemini" ? "Gemini (Google)" : "DeepL"}.</div>
            <div>• {tr.data?.engine === "gemini" ? "Consome pedidos/tokens da sua quota do Gemini (um filme costuma ser poucas dezenas de pedidos)." : <>Consome tipicamente 40 000–60 000 caracteres da sua quota</>}{tr.data?.usage ? ` (restam ${tr.data.usage.remaining.toLocaleString("pt-PT")})` : ""}.</div>
            <div>• Fica «por confirmar» e assinalada como <b>tradução automática</b>; reveja a pré-visualização antes de instalar.</div>
          </div>
        </div>
      </Modal>
      <DetailModal c={detail} meta={meta} onClose={() => setDetail(null)} onPick={(c) => pickCandidate(c)} busy={prep.isPending || bzDownload.isPending} />
      <PreviewModal dl={prepared} onClose={() => setPrepared(null)} writable={d.library_writable} onDone={() => { qc.invalidateQueries({ queryKey: ["media", d.id] }); qc.invalidateQueries(); }} />
    </div>
  );
}

function ResultCard({ c, meta, onOpen, onPick, busy, onTranslate }: { c: Candidate; meta: any; onOpen: () => void; onPick: () => void; busy: boolean; onTranslate?: () => void }) {
  const r = c.result, ev = c.evaluation;
  return (
    <div className={cx("card p-3.5 space-y-2.5 hover:border-mute/40 transition-colors", ev.compat === "incompatible" && "opacity-70")}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-medium break-all">{r.release || r.file_name || "(sem nome de release)"}</div>
          <div className="text-xs text-dim mt-0.5 flex gap-2 flex-wrap items-center">
            <Badge tone="accent">{langName(meta, r.language)}</Badge>{r.provider === "bazarr"
              ? <Badge tone="accent" title={`Candidata da pesquisa manual do Bazarr${r.extra?.bazarr_score != null ? ` · score do Bazarr ${r.extra.bazarr_score}` : ""}${r.extra?.matches?.length ? ` · coincide: ${r.extra.matches.join(", ")}` : ""}${r.extra?.dont_matches?.length ? ` · não coincide: ${r.extra.dont_matches.join(", ")}` : ""}`}>Fonte: Bazarr · {r.extra?.provider_label ?? providerLabel(r.via)}</Badge>
              : <Badge tone="info" title="Resultado da pesquisa da própria SubSync (API oficial do fornecedor)">Fonte: App · {providerLabel(r.provider)}</Badge>}
            {r.hearing_impaired && <Badge>SDH/HI</Badge>}{r.forced && <Badge tone="warn">Forçada</Badge>}
            {(r.ai_translated || r.machine_translated) && <Badge tone="warn">Tradução automática</Badge>}
            {r.from_trusted && <Badge tone="ok">Confiável</Badge>}
            {r.found_by && r.found_by !== "hash + identificação" && <Badge tone="warn" title="Pesquisa alargada: o primeiro pedido não devolveu nada">pesquisa: {r.found_by}</Badge>}
          </div>
        </div>
        <ScoreBar score={ev.score} />
      </div>
      <div className="flex flex-wrap gap-1.5 items-center"><CompatBadge ev={ev} /><SyncBadge ev={ev} />
        {r.hash_match && <Badge tone="ok">Hash coincide</Badge>}
        {r.download_count != null && <Badge title="Downloads no fornecedor"><DlIcon size={10} />{r.download_count}</Badge>}
        {r.rating ? <Badge><Star size={10} />{r.rating}</Badge> : null}
        {r.fps ? <Badge>{r.fps} fps</Badge> : null}
      </div>
      <div className="flex gap-2 justify-end">
        <button className="btn-ghost" onClick={onOpen}>Detalhes</button>
        {onTranslate && <button className="btn-ghost" disabled={busy || ev.compat === "incompatible"} onClick={onTranslate}><Languages size={14} />Traduzir</button>}
        <button className="btn-primary" disabled={busy || ev.compat === "incompatible"} onClick={onPick} title={ev.compat === "incompatible" ? "Provavelmente incompatível" : ""}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <ArrowDownToLine size={14} />}{r.provider === "bazarr" ? "Descarregar via Bazarr" : "Descarregar e pré-visualizar"}
        </button>
      </div>
    </div>
  );
}

function DetailModal({ c, meta, onClose, onPick, busy }: { c: Candidate | null; meta: any; onClose: () => void; onPick: (c: Candidate) => void; busy: boolean }) {
  if (!c) return null;
  const r = c.result;
  return (
    <Modal open onClose={onClose} title="Detalhes do resultado" wide
      footer={<><button className="btn-ghost" onClick={onClose}>Fechar</button><button className="btn-primary" disabled={busy || c.evaluation.compat === "incompatible"} onClick={() => onPick(c)}>{c.result.provider === "bazarr" ? "Descarregar via Bazarr" : "Descarregar e pré-visualizar"}</button></>}>
      <div className="space-y-4">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
          {[["Idioma", langName(meta, r.language)], ["Fornecedor", r.provider], ["Release", r.release || "—"], ["Ficheiro", r.file_name || "—"],
            ["Carregado por", r.uploader || "—"], ["Data", r.upload_date?.slice(0, 10) || "—"], ["Downloads", r.download_count ?? "—"], ["Frame rate", r.fps ?? "—"],
            ["Tipo", [r.forced && "Forçada", r.hearing_impaired && "SDH/HI", !r.forced && !r.hearing_impaired && "Completa"].filter(Boolean).join(", ")]].map(([k, v]) => (
            <div key={String(k)} className="min-w-0"><dt className="label">{k}</dt><dd className="break-words">{String(v)}</dd></div>
          ))}
        </dl>
        <div className="flex gap-1.5 flex-wrap"><CompatBadge ev={c.evaluation} /><SyncBadge ev={c.evaluation} /></div>
        <div className="card p-3.5 bg-s2"><div className="label mb-2">Porque esta pontuação ({c.evaluation.score}/100)</div><Factors ev={c.evaluation} /></div>
        {r.page_url && <a className="text-xs text-info hover:underline" href={r.page_url} target="_blank" rel="noreferrer noopener">Ver no fornecedor ↗</a>}
      </div>
    </Modal>
  );
}

export function PreviewModal({ dl, onClose, writable, onDone }: { dl: Download | null; onClose: () => void; writable: boolean; onDone: () => void }) {
  const toast = useToast();
  const [policy, setPolicy] = useState<"fail" | "replace" | "keep_both">("fail");
  useEffect(() => setPolicy("fail"), [dl?.id]);
  const install = useMutation({
    mutationFn: () => api.post<Download>(`/downloads/${dl!.id}/install`, { policy }),
    onSuccess: (r) => { toast("ok", `Legenda instalada: ${baseName(r.dest_path || "")}`); onDone(); onClose(); },
    onError: (e: Error) => toast("bad", e.message),
  });
  const discard = useMutation({ mutationFn: () => api.post(`/downloads/${dl!.id}/discard`), onSuccess: () => { onDone(); onClose(); } });
  if (!dl) return null;
  const ev = dl.evaluation as Evaluation;
  const blocked = !!ev?.writable_error;
  const conflict = dl.conflict;
  return (
    <Modal open onClose={onClose} title="Pré-visualização da instalação" wide
      footer={<>
        <button className="btn-quiet" onClick={() => discard.mutate()}>Descartar</button>
        <button className="btn-ghost" onClick={onClose}>Decidir depois</button>
        <button className="btn-primary" disabled={blocked || install.isPending} onClick={() => install.mutate()}>
          {install.isPending ? <Loader2 size={14} className="animate-spin" /> : <ArrowDownToLine size={14} />}Confirmar instalação
        </button>
      </>}>
      <div className="space-y-4">
        <div className="card bg-s2 p-3.5 space-y-1.5 text-sm">
          <div className="label">Destino</div>
          <div className="font-mono text-[12.5px] break-all">{dl.dest_path}</div>
          <div className="text-xs text-mute">Biblioteca: {ev?.library ?? "—"} · {ev?.cue_count} eventos · codificação original {ev?.encoding} → guardada em UTF-8</div>
        </div>
        {ev?.translation && (
          <div className="rounded-lg border border-warn/40 bg-warn/10 p-3.5 text-sm flex gap-2.5"><Languages size={16} className="text-warn shrink-0 mt-0.5" />
            <div><b>Tradução automática ({ev.translation.engine === "deepl" ? "DeepL" : ev.translation.engine === "gemini" ? "Gemini" : ev.translation.engine})</b> de «{ev.translation.source_lang ?? "?"}» para «{ev.translation.target}» · {ev.translation.characters.toLocaleString("pt-PT")} caracteres. Os tempos são os da legenda original; a qualidade é inferior à de uma legenda humana.</div></div>
        )}
        {blocked && <div className="flex gap-2 text-sm text-bad bg-bad/10 rounded-lg p-3"><ShieldAlert size={16} className="shrink-0 mt-0.5" />{ev.writable_error} Ative a escrita nesta biblioteca em Definições.</div>}
        {conflict && (
          <div className="rounded-lg border border-warn/40 bg-warn/10 p-3.5 space-y-2.5">
            <div className="flex gap-2 text-sm"><AlertTriangle size={16} className="text-warn shrink-0 mt-0.5" />
              <div>{conflict === "exists" ? "Já existe um ficheiro com este nome." : "Já existe uma legenda neste idioma para este vídeo."}
                <div className="text-xs text-mute break-all mt-1">{(ev?.existing ?? []).map(baseName).join(", ")}</div></div></div>
            <div className="grid gap-1.5">
              {([["fail", "Cancelar (não alterar nada)"], ["keep_both", "Manter ambas (guardar com sufixo .2)"], ["replace", "Substituir (cópia de segurança do original)"]] as const).map(([v, l]) => (
                <label key={v} className="flex items-center gap-2 text-sm cursor-pointer"><input type="radio" className="accent-[rgb(229,160,13)]" checked={policy === v} onChange={() => setPolicy(v)} />{l}</label>
              ))}
            </div>
          </div>
        )}
        <div className="flex gap-1.5 flex-wrap"><CompatBadge ev={ev} /><SyncBadge ev={ev} /></div>
        {ev.alignment && (
          <div className="text-sm card bg-s2 p-3.5 space-y-1">
            <div className="label">Alinhamento com «{ev.alignment.reference}»</div>
            <div className="text-mute">Offset estimado {(ev.alignment.offset_ms / 1000).toFixed(1)} s · deriva {(ev.alignment.drift_ms / 1000).toFixed(1)} s · {Math.round(ev.alignment.match_ratio * 100)} % dos {ev.alignment.events_compared} eventos coincidem</div>
          </div>
        )}
        {ev.duration_diff_s != null && <div className="text-xs text-mute">Fim da legenda vs. fim do vídeo: {ev.duration_diff_s > 0 ? "+" : ""}{ev.duration_diff_s}s</div>}
        <div className="card bg-s2 p-3.5"><div className="label mb-2">Pontuação {ev.score}/100 — fatores</div><Factors ev={ev} /></div>
      </div>
    </Modal>
  );
}
