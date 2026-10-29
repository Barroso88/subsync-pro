import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Copy, Loader2, Lock } from "lucide-react";
import { api, qs } from "../api";
import { Badge, Checkbox, Empty, ErrorBox, PageHeader, Skeleton, useToast } from "../components/ui";
import { SearchBox, useDebounced } from "../components/MediaBrowser";
import { cx } from "../lib";

interface File { reappeared: number; id: number; name: string; lang: string | null; lang_source: string; size: number; mtime: number | null; keep: boolean }
interface Group { reappeared: number; lang_conflict: boolean; content_lang: string | null; media_id: number; hash: string; title: string; year: number | null; deletable: boolean; files: File[] }
interface Res { total: number; removable: number; conflicts: number; groups: Group[] }
interface Out { moved: number; requested: number; skipped: { id: number; reason: string }[] }
const key = (g: Group) => `${g.media_id}:${g.hash}`;

export default function Duplicates() {
  const qc = useQueryClient();
  const toast = useToast();
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [page, setPage] = useState(0);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [keepOverride, setKeepOverride] = useState<Record<string, number>>({});
  const [confirm, setConfirm] = useState(false);
  const [result, setResult] = useState<Out | null>(null);
  const limit = 50;
  useEffect(() => setPage(0), [dq]);

  const list = useQuery({ queryKey: ["dups", dq, page], queryFn: () => api.get<Res>("/subtitles/duplicates" + qs({ q: dq, limit, offset: page * limit })) });
  const groups = list.data?.groups ?? [];
  const keepOf = (g: Group) => keepOverride[key(g)] ?? g.files.find((f) => f.keep)!.id;
  const toggle = (k: string) => setSel((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n; });
  const selectedGroups = groups.filter((g) => sel.has(key(g)) && g.deletable);

  const resolve = useMutation({
    mutationFn: (items: { keep: number; remove: number[] }[]) => api.post<Out>("/subtitles/duplicates/resolve", { items }),
    onSuccess: (r) => { setResult(r); setConfirm(false); setSel(new Set()); setKeepOverride({}); qc.invalidateQueries(); toast(r.moved ? "ok" : "warn", `${r.moved} legendas movidas para backup`); },
    onError: (e: Error) => { setConfirm(false); toast("bad", e.message); },
  });
  const selectAll = useMutation({
    mutationFn: () => api.get<{ items: { keep: number; remove: number[] }[] }>("/subtitles/duplicates/all" + qs({ q: dq })),
    onSuccess: (r) => resolve.mutate(r.items),
  });
  const items = selectedGroups.map((g) => ({ keep: keepOf(g), remove: g.files.filter((f) => f.id !== keepOf(g)).map((f) => f.id) }));
  const pages = Math.max(1, Math.ceil((list.data?.total ?? 0) / limit));

  return (
    <>
      <PageHeader title="Legendas duplicadas" subtitle="Ficheiros de legenda com exatamente o mesmo conteúdo no mesmo filme (por exemplo «.pt.srt» e «.pt-PT.srt»). O Plex mostra-os como duas legendas iguais."
        actions={<SearchBox value={q} onChange={setQ} placeholder="Filme ou ficheiro…" />} />
      <div className="card p-3.5 mb-4 text-xs text-mute flex gap-2.5"><AlertTriangle size={15} className="text-warn shrink-0 mt-0.5" />
        <div>Só aparecem ficheiros <b className="text-fg">byte a byte iguais</b>. Os repetidos <b className="text-fg">vão para backup</b> (restaurável no histórico de alterações do filme); nunca se apaga definitivamente. Antes de mover, o servidor volta a comparar os dois ficheiros no disco. Por omissão fica a legenda com a variante explícita no nome (pt-PT/pt-BR) e, se os nomes divergirem (ex.: «.pt-PT» e «.pt-BR» iguais), a que corresponde ao idioma real do conteúdo; pode escolher outra.</div></div>

      {list.isError && <ErrorBox error={list.error} retry={() => list.refetch()} />}
      {list.isLoading && <div className="space-y-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-24" />)}</div>}
      {!list.isLoading && !list.isError && groups.length === 0 && <Empty icon={<CheckCircle2 size={22} />} title="Sem legendas duplicadas" hint={dq ? "Nenhuma com este filtro." : "Nenhum filme tem duas legendas com o mesmo conteúdo."} />}
      {result && (
        <div className="card p-4 mb-4 text-sm space-y-1.5">
          <div className="font-medium flex items-center gap-2"><CheckCircle2 size={16} className="text-ok" />{result.moved} de {result.requested} legendas movidas para backup.</div>
          {result.skipped.length > 0 && <div className="text-xs text-mute">Ignoradas ({result.skipped.length}): {result.skipped.slice(0, 4).map((s) => s.reason).join("; ")}{result.skipped.length > 4 ? "…" : ""}</div>}
        </div>
      )}

      <div className="space-y-3">
        {groups.map((g) => {
          const k = key(g);
          return (
            <div key={k} className={cx("card p-3.5", sel.has(k) && "ring-1 ring-accent/50")}>
              <div className="flex items-center gap-3 mb-2">
                {g.deletable ? <Checkbox checked={sel.has(k)} onChange={() => toggle(k)} label="Selecionar grupo" /> : <Lock size={14} className="text-dim" aria-label="Biblioteca só de leitura" />}
                <div className="font-medium truncate">{g.title}{g.year && <span className="text-dim font-normal"> ({g.year})</span>}</div>
                {!g.deletable && <Badge tone="neutral">só leitura</Badge>}
              </div>
              {g.reappeared > 0 && (
                <div className="text-xs text-bad flex gap-1.5 mb-2"><AlertTriangle size={13} className="shrink-0 mt-0.5" />
                  <div>Este conteúdo já foi movido para backup {g.reappeared}× e <b>voltou a aparecer</b>: outra ferramenta (o Bazarr, por exemplo) está a recriá-lo. Por isso a app propõe <b>ficar com o ficheiro que volta</b> e mover o outro; se mover o que ele recria, volta sempre.
                    {g.lang_conflict && <> Os nomes indicam idiomas diferentes ({g.files.map((f) => f.lang).filter(Boolean).join(" / ")}) e o conteúdo parece {g.content_lang ?? "outro idioma"}: ao ficar com o que o Bazarr recria, a legenda continua a contar pelo nome dele.</>}</div></div>
              )}
              {g.lang_conflict && g.reappeared === 0 && (
                <div className="text-xs text-warn flex gap-1.5 mb-2"><AlertTriangle size={13} className="shrink-0 mt-0.5" /><div>Os nomes indicam idiomas diferentes mas o conteúdo é igual{g.content_lang ? ` (parece ${g.content_lang})` : ""}: uma das duas está mal etiquetada e conta como legenda que não é. Fica a que corresponde ao conteúdo; confirme antes de mover. Não entra em «Resolver todos».</div></div>
              )}
              <div className="space-y-1">
                {g.files.map((f) => (
                  <label key={f.id} className={cx("flex items-center gap-2.5 text-xs rounded-md px-2 py-1.5", keepOf(g) === f.id ? "bg-ok/10" : "bg-s2")}>
                    <input type="radio" className="accent-[rgb(229,160,13)]" name={k} disabled={!g.deletable} checked={keepOf(g) === f.id} onChange={() => setKeepOverride((o) => ({ ...o, [k]: f.id }))} />
                    <span className="font-mono truncate flex-1 min-w-0" title={f.name}>{f.name}</span>
                    {f.lang && <Badge>{f.lang}</Badge>}
                    <span className="text-dim tabular-nums">{Math.round(f.size / 1024)} KB</span>
                    {f.mtime && <span className="text-dim tabular-nums hidden md:inline" title="Data de modificação do ficheiro">{new Date(f.mtime * 1000).toLocaleString("pt-PT", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</span>}
                    {f.reappeared > 0 && <Badge tone="bad" title="Já movido para backup e recriado">reapareceu {f.reappeared}×</Badge>}
                    {keepOf(g) === f.id ? <Badge tone="ok">fica</Badge> : <Badge tone="warn">vai para backup</Badge>}
                  </label>
                ))}
              </div>
            </div>
          );
        })}
      </div>
      {pages > 1 && (
        <div className="flex items-center justify-center gap-3 mt-4 text-sm text-mute">
          <button className="btn-ghost !h-8" disabled={page === 0} onClick={() => setPage(page - 1)}>Anterior</button><span className="tabular-nums">{page + 1} / {pages}</span>
          <button className="btn-ghost !h-8" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Seguinte</button>
        </div>
      )}

      <div className="fixed bottom-5 left-1/2 -translate-x-1/2 z-30 card shadow-pop px-4 py-2.5 flex items-center gap-3 max-w-[calc(100vw-2rem)] flex-wrap justify-center" hidden={!list.data || list.data.removable === 0}>
        <span className="text-sm font-medium tabular-nums">{selectedGroups.length} grupo{selectedGroups.length === 1 ? "" : "s"} selecionado{selectedGroups.length === 1 ? "" : "s"}</span>
        <button className="btn-ghost" onClick={() => setSel(new Set(groups.filter((g) => g.deletable).map(key)))}>Selecionar página</button>
        {!confirm ? <>
          <button className="btn-primary" disabled={selectedGroups.length === 0 || resolve.isPending} onClick={() => setConfirm(true)}><Copy size={15} />Mover repetidos para backup…</button>
          <button className="btn-quiet" disabled={selectAll.isPending || resolve.isPending} onClick={() => { if (window.confirm(`Mover os repetidos de TODOS os grupos (${list.data?.removable ?? 0} ficheiros) para backup, mantendo em cada grupo a legenda proposta?`)) selectAll.mutate(); }}>
            {selectAll.isPending && <Loader2 size={14} className="animate-spin" />}Resolver todos ({list.data?.removable ?? 0})</button></>
          : <><button className="btn-primary" disabled={resolve.isPending} onClick={() => resolve.mutate(items)}>{resolve.isPending && <Loader2 size={14} className="animate-spin" />}Confirmar: mover {items.reduce((n, i) => n + i.remove.length, 0)}</button>
            <button className="btn-ghost" onClick={() => setConfirm(false)}>Cancelar</button></>}
      </div>
    </>
  );
}
