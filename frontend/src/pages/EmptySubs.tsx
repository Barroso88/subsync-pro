import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, FileX, Loader2, Lock, Trash2 } from "lucide-react";
import { api, qs } from "../api";
import { Badge, Checkbox, Empty, ErrorBox, Modal, PageHeader, Skeleton, Tabs, useToast } from "../components/ui";
import { SearchBox, useDebounced } from "../components/MediaBrowser";

interface Row { id: number; path: string; name: string; folder: string; lang: string | null; title: string | null; year: number | null; deletable: boolean }
interface Res { total: number; deletable: number; items: Row[] }
interface Del { deleted: number; requested: number; skipped: { id: number; path: string | null; reason: string }[] }

export default function EmptySubs() {
  const qc = useQueryClient();
  const toast = useToast();
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [lang, setLang] = useState("");
  const [page, setPage] = useState(0);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [confirm, setConfirm] = useState(false);
  const [result, setResult] = useState<Del | null>(null);
  const limit = 100;
  useEffect(() => setPage(0), [dq, lang]);

  const list = useQuery({ queryKey: ["empty-subs", dq, lang, page], queryFn: () => api.get<Res>("/subtitles/empty" + qs({ q: dq, lang, limit, offset: page * limit })) });
  const del = useMutation({
    mutationFn: () => api.post<Del>("/subtitles/empty/delete", { ids: [...sel] }),
    onSuccess: (r) => { setResult(r); setConfirm(false); setSel(new Set()); qc.invalidateQueries(); toast(r.deleted ? "ok" : "warn", `${r.deleted} legendas apagadas`); },
    onError: (e: Error) => { setConfirm(false); toast("bad", e.message); },
  });
  const selectAll = useMutation({
    mutationFn: () => api.get<{ ids: number[] }>("/subtitles/empty/ids" + qs({ q: dq, lang })),
    onSuccess: (r) => setSel(new Set(r.ids)),
  });
  const items = list.data?.items ?? [];
  const deletableOnPage = items.filter((i) => i.deletable);
  const allPage = deletableOnPage.length > 0 && deletableOnPage.every((i) => sel.has(i.id));
  const toggle = (id: number) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const pages = Math.max(1, Math.ceil((list.data?.total ?? 0) / limit));

  return (
    <>
      <PageHeader title="Legendas vazias" subtitle="Ficheiros de legenda com 0 bytes. Não servem para nada e impedem o filme de aparecer como «em falta» noutras ferramentas (ex.: Bazarr)."
        actions={<SearchBox value={q} onChange={setQ} placeholder="Filme ou ficheiro…" />} />
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Tabs value={lang} onChange={setLang} items={[{ id: "", label: "Todas" }, { id: "pt-BR", label: "pt-BR" }, { id: "pt-PT", label: "pt-PT" }, { id: "pt", label: "pt" }, { id: "none", label: "Sem idioma" }]} />
        <span className="text-xs text-dim ml-auto tabular-nums">{list.data ? `${list.data.total} ficheiros · ${list.data.deletable} apagáveis` : ""}</span>
      </div>
      <div className="card p-3.5 mb-4 text-xs text-mute flex gap-2.5"><AlertTriangle size={15} className="text-warn shrink-0 mt-0.5" />
        <div>Só são apagados ficheiros de <b className="text-fg">legenda</b> que estejam <b className="text-fg">a 0 bytes no momento de apagar</b> (volta a verificar no disco), dentro de bibliotecas com <b className="text-fg">escrita ativa</b>. Vídeos e legendas com conteúdo nunca são tocados. A apagação é definitiva.</div></div>

      {list.isError && <ErrorBox error={list.error} retry={() => list.refetch()} />}
      {list.isLoading && <div className="space-y-2">{[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-11" />)}</div>}
      {!list.isLoading && !list.isError && items.length === 0 && (
        <Empty icon={<CheckCircle2 size={22} />} title="Sem legendas vazias" hint={dq || lang ? "Nenhuma com estes filtros." : "Todas as legendas externas têm conteúdo."} />
      )}
      {result && (
        <div className="card p-4 mb-4 text-sm space-y-1.5 border-ok/30">
          <div className="font-medium flex items-center gap-2"><CheckCircle2 size={16} className="text-ok" />{result.deleted} de {result.requested} legendas apagadas.</div>
          {result.skipped.length > 0 && <div className="text-mute text-xs">Ignoradas ({result.skipped.length}): {result.skipped.slice(0, 5).map((s) => `${s.path?.split("/").pop() ?? s.id} — ${s.reason}`).join("; ")}{result.skipped.length > 5 ? "…" : ""}</div>}
          <div className="text-xs text-dim">Corra <b>Analisar bibliotecas</b> para atualizar o inventário; os filmes sem legenda válida voltam a aparecer em «Legendas em falta».</div>
        </div>
      )}

      {items.length > 0 && (
        <div className="card overflow-x-auto">
          <table className="w-full min-w-[640px]">
            <thead className="border-b border-line"><tr>
              <th className="th w-9"><Checkbox checked={allPage} label="Selecionar página" onChange={(v) => setSel((s) => { const n = new Set(s); deletableOnPage.forEach((i) => v ? n.add(i.id) : n.delete(i.id)); return n; })} /></th>
              <th className="th">Filme</th><th className="th">Ficheiro</th><th className="th">Idioma</th>
            </tr></thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.id} className={"border-b border-line/60 last:border-0 " + (sel.has(i.id) ? "bg-accent/5" : "hover:bg-s2/50")}>
                  <td className="td">{i.deletable ? <Checkbox checked={sel.has(i.id)} onChange={() => toggle(i.id)} label="Selecionar" /> : <Lock size={14} className="text-dim" aria-label="Biblioteca só de leitura" />}</td>
                  <td className="td font-medium max-w-[260px] truncate">{i.title ?? "—"}{i.year && <span className="text-dim font-normal"> ({i.year})</span>}</td>
                  <td className="td font-mono text-xs text-mute max-w-[420px] truncate" title={i.path}>{i.name}</td>
                  <td className="td">{i.lang ? <Badge>{i.lang}</Badge> : <Badge tone="warn">sem idioma</Badge>}{!i.deletable && <Badge tone="neutral" className="ml-1.5">só leitura</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {pages > 1 && (
        <div className="flex items-center justify-center gap-3 mt-4 text-sm text-mute">
          <button className="btn-ghost !h-8" disabled={page === 0} onClick={() => setPage(page - 1)}>Anterior</button><span className="tabular-nums">{page + 1} / {pages}</span>
          <button className="btn-ghost !h-8" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Seguinte</button>
        </div>
      )}

      <div className="fixed bottom-5 left-1/2 -translate-x-1/2 z-30 card shadow-pop px-4 py-2.5 flex items-center gap-3 animate-rise max-w-[calc(100vw-2rem)] flex-wrap justify-center" hidden={!list.data || list.data.deletable === 0}>
        <span className="text-sm font-medium tabular-nums">{sel.size} selecionada{sel.size === 1 ? "" : "s"}</span>
        <button className="btn-ghost" onClick={() => selectAll.mutate()} disabled={selectAll.isPending}>{selectAll.isPending && <Loader2 size={14} className="animate-spin" />}Selecionar todas ({list.data?.deletable ?? 0}{dq || lang ? ", com este filtro" : ""})</button>
        {sel.size > 0 && <button className="btn-quiet" onClick={() => setSel(new Set())}>Limpar</button>}
        <button className="btn-danger" disabled={sel.size === 0} onClick={() => setConfirm(true)}><Trash2 size={15} />Apagar selecionadas</button>
      </div>

      <Modal open={confirm} onClose={() => setConfirm(false)} title="Apagar legendas vazias?"
        footer={<><button className="btn-ghost" onClick={() => setConfirm(false)}>Cancelar</button>
          <button className="btn-danger" disabled={del.isPending} onClick={() => del.mutate()}>{del.isPending && <Loader2 size={14} className="animate-spin" />}Apagar {sel.size} ficheiros</button></>}>
        <div className="space-y-3 text-sm">
          <div>Vai apagar <b>{sel.size}</b> ficheiros de legenda com <b>0 bytes</b>. <b className="text-bad">Esta ação não se pode desfazer.</b></div>
          <div className="card bg-s2 p-3 text-xs text-mute space-y-1">
            <div>• Antes de apagar cada um, o servidor confirma no disco que continua com 0 bytes e que está numa biblioteca com escrita ativa.</div>
            <div>• Vídeos e legendas com conteúdo não são tocados.</div>
            <div>• Fica registado em «Tarefas e atividade».</div>
          </div>
        </div>
      </Modal>
    </>
  );
}
