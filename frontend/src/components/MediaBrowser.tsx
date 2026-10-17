import { useEffect, useMemo, useState } from "react";
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Film, LayoutGrid, List, Loader2, Search, SearchCheck, Tv, X, AlertTriangle, Clapperboard } from "lucide-react";
import { api, Item, qs } from "../api";
import { cx, epCode, fmtDur } from "../lib";
import { useMeta, useSettings, useStartJob } from "../hooks";
import { Badge, Checkbox, Empty, ErrorBox, Modal, Skeleton, Tabs, Toggle } from "./ui";
import { LangChips } from "./subs";
import { ItemDrawer } from "./ItemDrawer";

export function useDebounced<T>(v: T, ms = 220) {
  const [x, setX] = useState(v);
  useEffect(() => { const t = setTimeout(() => setX(v), ms); return () => clearTimeout(t); }, [v, ms]);
  return x;
}

export function SearchBox({ value, onChange, placeholder = "Pesquisar título, ano, ficheiro, idioma…" }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <div className="relative w-full sm:w-72">
      <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-dim" />
      <input className="input pl-9 pr-8" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} aria-label="Pesquisar" />
      {value && <button className="absolute right-2 top-1/2 -translate-y-1/2 text-dim hover:text-fg" onClick={() => onChange("")} aria-label="Limpar"><X size={14} /></button>}
    </div>
  );
}

const STATUS = [
  { id: "", label: "Todos" }, { id: "missing", label: "Em falta" }, { id: "has", label: "Com legenda" },
  { id: "unverified", label: "Por verificar" }, { id: "suspect", label: "Possível dessincronização" },
] as const;

export function Poster({ item, className }: { item: Item; className?: string }) {
  const [err, setErr] = useState(false);
  return (
    <div className={cx("bg-s2 overflow-hidden grid place-items-center text-dim", className)}>
      {item.has_poster && !err ? <img src={`/api/media/${item.id}/poster`} alt="" loading="lazy" onError={() => setErr(true)} className="w-full h-full object-cover" />
        : item.kind === "movie" ? <Film size={22} /> : <Tv size={22} />}
    </div>
  );
}

export function MediaBrowser({ kind, defaultStatus = "", allowGrid = false, missingMode = false, initialLang = "" }: {
  kind: "movie" | "episode"; defaultStatus?: string; allowGrid?: boolean; missingMode?: boolean; initialLang?: string;
}) {
  const meta = useMeta();
  const settings = useSettings();
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [lang, setLang] = useState(initialLang);
  const [status, setStatus] = useState(defaultStatus);
  const [view, setView] = useState<"grid" | "table">(allowGrid ? ((localStorage.getItem("view") as any) || "grid") : "table");
  const [page, setPage] = useState(0);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [open, setOpen] = useState<number | null>(null);
  const [batch, setBatch] = useState(false);
  const limit = view === "grid" ? 48 : 50;
  useEffect(() => setPage(0), [dq, lang, status, view, kind]);
  useEffect(() => { if (allowGrid) localStorage.setItem("view", view); }, [view, allowGrid]);

  const params = { kind, q: dq, lang, status, limit, offset: page * limit };
  const list = useQuery({ queryKey: ["media", params], queryFn: () => api.get<{ total: number; items: Item[] }>("/media" + qs(params)), placeholderData: keepPreviousData });
  const langs: string[] = settings.data?.languages.map((l: any) => l.code) ?? [];
  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / limit));
  const toggle = (id: number) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const allOnPage = items.length > 0 && items.every((i) => sel.has(i.id));
  const selectAll = useMutation({
    mutationFn: () => api.get<{ ids: number[] }>("/media/ids" + qs({ kind, q: dq, lang, status })),
    onSuccess: (r) => setSel(new Set(r.ids)),
  });
  const batchLangs = missingMode && lang ? [lang] : undefined;

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2.5 mb-4">
        <SearchBox value={q} onChange={setQ} />
        {!missingMode && <select className="input !w-auto" value={lang} onChange={(e) => setLang(e.target.value)} aria-label="Idioma">
          <option value="">Todos os idiomas</option>
          {langs.map((l) => <option key={l} value={l}>{meta.data?.languages.find((x) => x.code === l)?.name ?? l}</option>)}
        </select>}
        {!missingMode && <Tabs value={status} onChange={setStatus} items={STATUS.map((s) => ({ id: s.id, label: s.label }))} />}
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-dim tabular-nums">{total} {total === 1 ? "item" : "itens"}</span>
          {allowGrid && (
            <div className="inline-flex p-0.5 rounded-lg bg-s2 border border-line">
              {([["grid", LayoutGrid], ["table", List]] as const).map(([v, I]) => (
                <button key={v} aria-label={v === "grid" ? "Grelha" : "Tabela"} onClick={() => setView(v)} className={cx("h-8 w-8 grid place-items-center rounded-md", view === v ? "bg-s3 text-fg" : "text-mute")}><I size={15} /></button>
              ))}
            </div>
          )}
        </div>
      </div>

      {missingMode && (
        <div className="card p-3.5 mb-4 space-y-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2"><span className="label w-28">Situação</span>
            <Tabs value={status || "missing"} onChange={setStatus} items={[
              { id: "missing", label: "Todas as em falta" }, { id: "none", label: "Sem nenhuma legenda" }, { id: "other", label: "Só noutros idiomas" }, { id: "genericpt", label: "Tem «pt» sem variante" }]} /></div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2"><span className="label w-28">Idioma em falta</span>
            <Tabs value={lang} onChange={setLang} items={[{ id: "", label: "Qualquer prioritário" }, ...langs.map((l) => ({ id: l, label: l }))]} /></div>
          <div className="text-xs text-dim">«Só noutros idiomas»: já têm legenda (externa ou embutida) mas não no idioma escolhido — são as melhores candidatas a tradução automática. «Tem pt sem variante»: legenda portuguesa cujo nome não indica pt-PT ou pt-BR e cujo conteúdo não permitiu estimar a variante.</div>
        </div>
      )}
      {list.isError && <ErrorBox error={list.error} retry={() => list.refetch()} />}
      {list.isLoading && (view === "grid" ? <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6 gap-4">{Array.from({ length: 12 }).map((_, i) => <Skeleton key={i} className="aspect-[2/3]" />)}</div>
        : <div className="space-y-2">{Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-11" />)}</div>)}
      {!list.isLoading && !list.isError && items.length === 0 && (
        <Empty icon={kind === "movie" ? <Clapperboard size={22} /> : <Tv size={22} />}
          title={q || lang || status ? "Nenhum resultado com estes filtros" : "Ainda não há itens"}
          hint={q || lang || status ? "Experimente remover filtros ou alterar a pesquisa." : "Adicione uma biblioteca em Definições e execute a primeira análise."} />
      )}

      {items.length > 0 && total > items.length && (allOnPage || sel.size > 0) && sel.size < total && (
        <div className="card px-4 py-2.5 mb-3 text-sm flex items-center gap-3 flex-wrap">
          <span>{sel.size} selecionados.</span>
          <button className="text-accent hover:underline" onClick={() => selectAll.mutate()} disabled={selectAll.isPending}>
            {selectAll.isPending ? "A selecionar…" : `Selecionar todos os ${total}${batchLangs ? ` em falta em ${lang}` : ""} (não só esta página)`}</button>
        </div>
      )}
      {sel.size >= total && total > items.length && <div className="card px-4 py-2.5 mb-3 text-sm flex items-center gap-3">Todos os {total} itens do filtro estão selecionados. <button className="text-accent hover:underline" onClick={() => setSel(new Set())}>Limpar</button></div>}
      {items.length > 0 && view === "grid" && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6 gap-4">
          {items.map((i) => (
            <div key={i.id} className={cx("group relative text-left animate-rise", sel.has(i.id) && "ring-2 ring-accent rounded-xl")}>
              <button className="block w-full text-left" onClick={() => setOpen(i.id)}>
                <Poster item={i} className="aspect-[2/3] rounded-xl border border-line group-hover:border-mute/50 transition-colors" />
                <div className="mt-2 text-[13px] font-medium truncate">{i.title}</div>
                <div className="text-xs text-dim mb-1.5">{i.year ?? "—"}</div>
                <LangChips item={i} />
              </button>
              <div className={cx("absolute top-2 left-2 transition-opacity", sel.size || sel.has(i.id) ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus-within:opacity-100")}>
                <Checkbox checked={sel.has(i.id)} onChange={() => toggle(i.id)} label="Selecionar" />
              </div>
              {i.suspect && <div className="absolute top-2 right-2"><Badge tone="bad" title="Possível dessincronização / legenda inválida"><AlertTriangle size={10} /></Badge></div>}
            </div>
          ))}
        </div>
      )}

      {items.length > 0 && view === "table" && (
        <div className="card overflow-x-auto">
          <table className="w-full min-w-[720px]">
            <thead className="border-b border-line"><tr>
              <th className="th w-9"><Checkbox checked={allOnPage} onChange={(v) => setSel((s) => { const n = new Set(s); items.forEach((i) => v ? n.add(i.id) : n.delete(i.id)); return n; })} label="Selecionar página" /></th>
              <th className="th">{kind === "movie" ? "Filme" : "Episódio"}</th><th className="th hidden md:table-cell">Duração</th><th className="th">Legendas</th><th className="th hidden lg:table-cell">Estado</th>
            </tr></thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.id} onClick={() => setOpen(i.id)} className={cx("border-b border-line/60 last:border-0 cursor-pointer hover:bg-s2/60", sel.has(i.id) && "bg-accent/5")}>
                  <td className="td"><Checkbox checked={sel.has(i.id)} onChange={() => toggle(i.id)} label="Selecionar" /></td>
                  <td className="td min-w-0">
                    <div className="font-medium truncate max-w-[360px]">{kind === "episode" ? <><span className="text-dim font-normal">{i.series_title} · </span>{epCode(i)}{i.episode_title ? ` · ${i.episode_title}` : ""}</> : <>{i.title}{i.year && <span className="text-dim font-normal"> ({i.year})</span>}</>}</div>
                    <div className="text-xs text-dim truncate max-w-[360px]">{i.file_name}</div>
                  </td>
                  <td className="td hidden md:table-cell text-mute tabular-nums">{fmtDur(i.duration)}</td>
                  <td className="td"><LangChips item={i} /></td>
                  <td className="td hidden lg:table-cell">
                    <div className="flex gap-1 flex-wrap">
                      {i.suspect && <Badge tone="bad">Possível dessincronização</Badge>}
                      {i.unverified && !i.suspect && <Badge title="Sincronização por verificar">Por verificar</Badge>}
                      {i.unknown_lang > 0 && <Badge tone="warn">{i.unknown_lang} sem idioma</Badge>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {pages > 1 && (
        <div className="flex items-center justify-center gap-3 mt-5 text-sm text-mute">
          <button className="btn-ghost !h-8" disabled={page === 0} onClick={() => setPage(page - 1)} aria-label="Anterior"><ChevronLeft size={15} /></button>
          <span className="tabular-nums">{page + 1} / {pages}</span>
          <button className="btn-ghost !h-8" disabled={page >= pages - 1} onClick={() => setPage(page + 1)} aria-label="Seguinte"><ChevronRight size={15} /></button>
        </div>
      )}

      {sel.size > 0 && (
        <div className="fixed bottom-5 left-1/2 -translate-x-1/2 z-30 card shadow-pop px-4 py-2.5 flex items-center gap-3 animate-rise max-w-[calc(100vw-2rem)]">
          <span className="text-sm font-medium">{sel.size} selecionado{sel.size > 1 ? "s" : ""}</span>
          <button className="btn-primary" onClick={() => setBatch(true)}><SearchCheck size={15} />Procurar legendas</button>
          <button className="btn-quiet" onClick={() => setSel(new Set())}>Limpar</button>
        </div>
      )}
      <BatchModal open={batch} ids={[...sel]} languages={batchLangs} onClose={() => setBatch(false)} onStarted={() => { setBatch(false); setSel(new Set()); }} />
      <ItemDrawer id={open} onClose={() => setOpen(null)} />
    </div>
  );
}

export function BatchModal({ open, ids, languages, onClose, onStarted }: { open: boolean; ids: number[]; languages?: string[]; onClose: () => void; onStarted: () => void }) {
  const [install, setInstall] = useState(false);
  const meta = useMeta();
  const start = useStartJob();
  const pv = useQuery({ queryKey: ["batch-preview", ids, languages], queryFn: () => api.post<any>("/batch/preview", { media_ids: ids, languages }), enabled: open && ids.length > 0 });
  const canWrite = (meta.data?.writable_roots.length ?? 0) > 0;
  useEffect(() => { if (open) setInstall(false); }, [open]);
  const p = pv.data;
  return (
    <Modal open={open} onClose={onClose} title="Procurar legendas em lote"
      footer={<>
        <button className="btn-ghost" onClick={onClose}>Cancelar</button>
        <button className="btn-primary" disabled={!p || !p.needing_subtitles || start.isPending}
          onClick={() => start.mutate({ url: "/batch", body: { media_ids: ids, languages, install, min_tier: "probable" } }, { onSuccess: onStarted })}>
          {start.isPending && <Loader2 size={14} className="animate-spin" />}Confirmar e iniciar
        </button>
      </>}>
      {pv.isLoading && <Skeleton className="h-32" />}
      {p && (
        <div className="space-y-4 text-sm">
          <div className="grid grid-cols-3 gap-3">
            {[["Selecionados", p.selected], ["Precisam de legenda", p.needing_subtitles], ["Pedidos estimados", `~${p.estimated_requests}`]].map(([k, v]) => (
              <div key={String(k)} className="card bg-s2 p-3"><div className="label">{k}</div><div className="text-xl font-semibold tabular-nums mt-1">{v}</div></div>
            ))}
          </div>
          {p.days_estimate && p.days_estimate > 1 && <div className="rounded-lg border border-warn/40 bg-warn/10 p-3 text-xs">Com o limite diário do fornecedor, esta lista demora cerca de <b>{p.days_estimate} dias</b> a ficar completa. A tarefa pára quando o limite diário é atingido; volte a lançá-la no dia seguinte (só procura o que ainda falta).</div>}
          <div className="text-mute">Idiomas em falta a procurar: <b className="text-fg font-medium">{p.languages.join(", ") || "—"}</b>. Itens já cobertos são ignorados.</div>
          {p.providers.map((f: any) => (
            <div key={f.name} className="flex items-center gap-2 text-xs text-mute">
              <Badge tone={f.configured ? "ok" : "bad"}>{f.name}</Badge>
              {f.configured ? `${f.downloads_today}${f.daily_limit ? ` / ${f.daily_limit}` : ""} downloads hoje` : "sem credenciais"}
            </div>
          ))}
          <div className="rounded-lg border border-line p-3.5 space-y-2.5 bg-s2/50">
            <Toggle checked={install} onChange={setInstall} disabled={!canWrite}
              label="Instalar automaticamente as melhores legendas"
              hint={canWrite ? "Só instala resultados com compatibilidade provável ou confirmada por hash, sem conflitos. Nunca substitui ficheiros." : "Nenhuma biblioteca tem escrita autorizada."} />
            {!install && <div className="text-xs text-mute">Modo seguro: as melhores candidatas são descarregadas e ficam <b className="text-fg">por confirmar</b> em Histórico de downloads.</div>}
          </div>
          <div className="text-xs text-dim">A tarefa corre em segundo plano, respeita os limites do fornecedor e pode ser cancelada em Tarefas.</div>
        </div>
      )}
    </Modal>
  );
}
