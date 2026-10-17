import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download as DlIcon } from "lucide-react";
import { api, Download, qs } from "../api";
import { baseName, fmtDate } from "../lib";
import { Badge, Empty, ErrorBox, Modal, PageHeader, Skeleton, Tabs } from "../components/ui";
import { SearchBox, useDebounced } from "../components/MediaBrowser";
import { CompatBadge, Factors, SyncBadge } from "../components/subs";
import { PreviewModal } from "../components/ItemDrawer";

const TONE: Record<string, "ok" | "bad" | "warn" | "neutral" | "info"> = { installed: "ok", failed: "bad", prepared: "warn", discarded: "neutral", preparing: "info", cancelled: "neutral" };
const LABEL: Record<string, string> = { installed: "Instalada", failed: "Falhou", prepared: "Por confirmar", discarded: "Descartada", preparing: "A preparar", cancelled: "Cancelada" };

export default function Downloads() {
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [detail, setDetail] = useState<Download | null>(null);
  const [confirm, setConfirm] = useState<Download | null>(null);
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["downloads", status, dq], queryFn: () => api.get<{ total: number; items: Download[] }>("/downloads" + qs({ status, q: dq, limit: 200 })), refetchInterval: 8000 });
  const del = useMutation({ mutationFn: (id: number) => api.post(`/downloads/${id}/discard`), onSuccess: () => qc.invalidateQueries({ queryKey: ["downloads"] }) });
  return (
    <>
      <PageHeader title="Histórico de downloads" subtitle="Registo de cada legenda descarregada: fornecedor, resultado, idioma, data e estado."
        actions={<SearchBox value={q} onChange={setQ} placeholder="Título, release, ficheiro…" />} />
      <div className="mb-4"><Tabs value={status} onChange={setStatus} items={[{ id: "", label: "Todos" }, { id: "prepared", label: "Por confirmar" }, { id: "installed", label: "Instaladas" }, { id: "failed", label: "Falhadas" }, { id: "discarded", label: "Descartadas" }]} /></div>
      {list.isError && <ErrorBox error={list.error} retry={() => list.refetch()} />}
      {list.isLoading && <div className="space-y-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-12" />)}</div>}
      {list.data?.items.length === 0 && <Empty icon={<DlIcon size={22} />} title="Sem downloads" hint="Quando instalar legendas, ficam registadas aqui." />}
      {!!list.data?.items.length && (
        <div className="card overflow-x-auto">
          <table className="w-full min-w-[760px]">
            <thead className="border-b border-line"><tr><th className="th">Quando</th><th className="th">Item</th><th className="th">Idioma</th><th className="th">Fornecedor</th><th className="th">Estado</th><th className="th" /></tr></thead>
            <tbody>
              {list.data.items.map((d) => (
                <tr key={d.id} className="border-b border-line/60 last:border-0 hover:bg-s2/50 cursor-pointer" onClick={() => setDetail(d)}>
                  <td className="td text-mute whitespace-nowrap">{fmtDate(d.installed_at || d.created_at)}</td>
                  <td className="td min-w-0"><div className="truncate max-w-[320px] font-medium">{d.media_title ?? "—"}</div><div className="truncate max-w-[320px] text-xs text-dim">{d.dest_path ? baseName(d.dest_path) : d.release}</div></td>
                  <td className="td">{d.lang && <Badge tone="accent">{d.lang}</Badge>}</td>
                  <td className="td text-mute">{d.provider}</td>
                  <td className="td"><div className="flex gap-1.5 items-center"><Badge tone={TONE[d.status]}>{LABEL[d.status] ?? d.status}</Badge>{d.evaluation && <SyncBadge ev={d.evaluation} />}</div>{d.error && <div className="text-xs text-bad mt-1 max-w-[260px] truncate" title={d.error}>{d.error}</div>}</td>
                  <td className="td text-right" onClick={(e) => e.stopPropagation()}>
                    {d.status === "prepared" && <div className="flex gap-1.5 justify-end"><button className="btn-primary !h-8" onClick={() => setConfirm(d)}>Rever e instalar</button><button className="btn-quiet !h-8" onClick={() => del.mutate(d.id)}>Descartar</button></div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Modal open={!!detail} onClose={() => setDetail(null)} title="Detalhe do download" wide footer={<button className="btn-ghost" onClick={() => setDetail(null)}>Fechar</button>}>
        {detail && (
          <div className="space-y-3 text-sm">
            <div className="grid grid-cols-2 gap-3">{[["Item", detail.media_title], ["Fornecedor", detail.provider], ["Release", detail.release], ["Idioma", detail.lang], ["Destino", detail.dest_path], ["Estado", LABEL[detail.status]]].map(([k, v]) => <div key={String(k)} className="min-w-0"><div className="label">{k}</div><div className="break-all">{v || "—"}</div></div>)}</div>
            {detail.error && <div className="text-bad bg-bad/10 rounded-lg p-3">{detail.error}</div>}
            {detail.evaluation && <><div className="flex gap-1.5"><CompatBadge ev={detail.evaluation} /><SyncBadge ev={detail.evaluation} /></div><div className="card bg-s2 p-3.5"><Factors ev={detail.evaluation} /></div></>}
          </div>
        )}
      </Modal>
      <PreviewModal dl={confirm} writable onClose={() => setConfirm(null)} onDone={() => qc.invalidateQueries()} />
    </>
  );
}
