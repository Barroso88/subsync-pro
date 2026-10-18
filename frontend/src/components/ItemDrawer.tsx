import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowDownToLine, FileText, Layers, Loader2, RefreshCw, Search, ShieldAlert, Star, Download as DlIcon, Info, FolderOpen, Languages, Wrench } from "lucide-react";
import { api, Candidate, Download, Evaluation, Item, Issue, Sub } from "../api";
import { baseName, epCode, fmtDur, fmtSize, resolution, cx } from "../lib";
import { useMeta } from "../hooks";
import { Badge, Drawer, Empty, Modal, Skeleton, Tabs, useToast } from "./ui";
import { CompatBadge, Factors, LangChips, ScoreBar, SyncBadge, langName } from "./subs";
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
                {d.height && <Badge>{resolution(d.height)}</Badge>}
                {d.fps && <Badge>{d.fps} fps</Badge>}
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

const ISSUE_TONE: Record<string, "bad" | "warn" | "neutral"> = { error: "bad", warning: "warn", info: "neutral" };

function SubsList({ d, onSearch }: { d: Detail; onSearch: () => void }) {
  const meta = useMeta();
  const [tools, setTools] = useState<Sub | null>(null);
  if (!d.subtitles.length) return <Empty icon={<FileText size={20} />} title="Sem legendas" hint="Não foram encontradas legendas externas nem embutidas para este vídeo." action={<button className="btn-primary" onClick={onSearch}><Search size={14} />Procurar online</button>} />;
  return (
    <div className="space-y-2.5">
      {d.subtitles.map((s) => (
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
  const trans = useMutation({
    mutationFn: (c: Candidate) => api.post<Download>(`/media/${d.id}/translate`, { result: c.result, target }),
    onSuccess: (r) => { setPrepared(r); setAskTr(null); qc.invalidateQueries({ queryKey: ["downloads"] }); qc.invalidateQueries({ queryKey: ["translation"] }); },
    onError: (e: Error) => { toast("bad", e.message); setAskTr(null); },
  });
  const search = useMutation({
    mutationFn: () => api.post<SearchRes>(`/media/${d.id}/search`, { languages: langs }),
    onSuccess: setRes, onError: (e: Error) => toast("bad", e.message),
  });
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
        <button className="btn-primary w-full" onClick={() => search.mutate()} disabled={search.isPending}>
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
          <div className="text-sm font-medium">Pedir ao Bazarr</div>
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
      {res?.results.map((c, i) => <ResultCard key={i} c={c} meta={meta} onOpen={() => setDetail(c)} onPick={() => prep.mutate(c)} busy={prep.isPending}
        onTranslate={canTranslate && c.result.language !== target ? () => setAskTr(c) : undefined} />)}
      <Modal open={!!askTr} onClose={() => setAskTr(null)} title={`Traduzir para ${target} (${tr.data?.engine === "gemini" ? "Gemini" : "DeepL"})`}
        footer={<><button className="btn-ghost" onClick={() => setAskTr(null)}>Cancelar</button>
          <button className="btn-primary" disabled={trans.isPending} onClick={() => askTr && trans.mutate(askTr)}>{trans.isPending && <Loader2 size={14} className="animate-spin" />}Descarregar e traduzir</button></>}>
        <div className="space-y-3 text-sm">
          <div>Vai descarregar <b className="break-all">{askTr?.result.release || askTr?.result.file_name}</b> ({askTr?.result.language}) e traduzir o texto para <b>{target}</b>, mantendo os tempos.</div>
          <div className="card bg-s2 p-3 text-xs text-mute space-y-1">
            <div>• O texto da legenda é enviado ao {tr.data?.engine === "gemini" ? "Gemini (Google)" : "DeepL"}.</div>
            <div>• {tr.data?.engine === "gemini" ? "Consome pedidos/tokens da sua quota do Gemini (um filme costuma ser poucas dezenas de pedidos)." : <>Consome tipicamente 40 000–60 000 caracteres da sua quota</>}{tr.data?.usage ? ` (restam ${tr.data.usage.remaining.toLocaleString("pt-PT")})` : ""}.</div>
            <div>• Fica «por confirmar» e assinalada como <b>tradução automática</b>; reveja a pré-visualização antes de instalar.</div>
          </div>
        </div>
      </Modal>
      <DetailModal c={detail} meta={meta} onClose={() => setDetail(null)} onPick={(c) => prep.mutate(c)} busy={prep.isPending} />
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
            <Badge tone="accent">{langName(meta, r.language)}</Badge><span>{r.provider}</span>
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
          {busy ? <Loader2 size={14} className="animate-spin" /> : <ArrowDownToLine size={14} />}Descarregar e pré-visualizar
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
      footer={<><button className="btn-ghost" onClick={onClose}>Fechar</button><button className="btn-primary" disabled={busy || c.evaluation.compat === "incompatible"} onClick={() => onPick(c)}>Descarregar e pré-visualizar</button></>}>
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
