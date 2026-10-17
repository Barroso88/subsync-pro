import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Ban, CheckCircle2, Clock, Loader2, RotateCcw, XCircle } from "lucide-react";
import { api, Job, qs } from "../api";
import { fmtDate } from "../lib";
import { Badge, Empty, ErrorBox, Modal, PageHeader, Progress, Skeleton, Tabs, useToast } from "../components/ui";

const TONE: Record<string, any> = { done: "ok", failed: "bad", running: "accent", queued: "info", cancelled: "neutral", interrupted: "warn" };
const LABEL: Record<string, string> = { done: "Concluída", failed: "Falhou", running: "Em curso", queued: "Em fila", cancelled: "Cancelada", interrupted: "Interrompida" };
const TYPE: Record<string, string> = { scan: "Análise", batch: "Lote de legendas", sync_integrations: "Metadados", revalidate: "Revalidação", refresh_plex: "Plex" };
const ICON: Record<string, any> = { done: CheckCircle2, failed: XCircle, running: Loader2, queued: Clock, cancelled: Ban, interrupted: Ban };

export default function Jobs() {
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<Job | null>(null);
  const qc = useQueryClient();
  const toast = useToast();
  const list = useQuery({ queryKey: ["jobs", status], queryFn: () => api.get<{ total: number; items: Job[] }>("/jobs" + qs({ status, limit: 100 })), refetchInterval: 1500 });
  const events = useQuery({ queryKey: ["events"], queryFn: () => api.get<any[]>("/events?limit=30"), refetchInterval: 5000 });
  const cancel = useMutation({ mutationFn: (id: number) => api.post(`/jobs/${id}/cancel`), onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }) });
  const retry = useMutation({ mutationFn: (id: number) => api.post(`/jobs/${id}/retry`), onSuccess: () => { toast("info", "Tarefa reenviada"); qc.invalidateQueries({ queryKey: ["jobs"] }); }, onError: (e: Error) => toast("bad", e.message) });
  return (
    <>
      <PageHeader title="Tarefas e atividade" subtitle="Análises, pesquisas, validações e instalações em segundo plano." />
      <div className="mb-4"><Tabs value={status} onChange={setStatus} items={[{ id: "", label: "Todas" }, { id: "running", label: "Em curso" }, { id: "queued", label: "Em fila" }, { id: "done", label: "Concluídas" }, { id: "failed", label: "Falhadas" }]} /></div>
      {list.isError && <ErrorBox error={list.error} retry={() => list.refetch()} />}
      {list.isLoading && <div className="space-y-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-20" />)}</div>}
      {list.data?.items.length === 0 && <Empty icon={<Activity size={22} />} title="Sem tarefas" hint="As tarefas aparecem aqui quando iniciar uma análise ou pesquisa." />}
      <div className="space-y-2.5">
        {list.data?.items.map((j) => { const I = ICON[j.status] ?? Clock; const active = j.status === "running" || j.status === "queued"; return (
          <div key={j.id} className="card p-4 animate-rise">
            <div className="flex items-start gap-3">
              <I size={18} className={`shrink-0 mt-0.5 ${j.status === "running" ? "animate-spin text-accent" : j.status === "done" ? "text-ok" : j.status === "failed" ? "text-bad" : "text-mute"}`} />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap"><button className="font-medium text-sm hover:underline" onClick={() => setOpen(j)}>#{j.id} · {j.title}</button><Badge tone={TONE[j.status]}>{LABEL[j.status]}</Badge><Badge>{TYPE[j.type] ?? j.type}</Badge></div>
                {active && j.total > 0 && <div className="mt-2.5"><Progress value={j.progress} total={j.total} /><div className="flex justify-between text-xs text-dim mt-1.5"><span className="truncate pr-3">{j.current ?? "…"}</span><span className="tabular-nums shrink-0">{j.progress}/{j.total} · {j.message}</span></div></div>}
                {j.error && <div className="text-xs text-bad mt-1.5">{j.error}</div>}
                <div className="text-xs text-dim mt-1.5">Iniciada {fmtDate(j.started_at || j.created_at)}{j.finished_at && ` · terminou ${fmtDate(j.finished_at)}`}</div>
              </div>
              <div className="flex gap-1.5 shrink-0">
                {active && <button className="btn-danger !h-8" onClick={() => cancel.mutate(j.id)}>Cancelar</button>}
                {!active && j.status !== "done" && <button className="btn-ghost !h-8" onClick={() => retry.mutate(j.id)}><RotateCcw size={13} />Repetir</button>}
              </div>
            </div>
          </div>
        ); })}
      </div>
      <div className="mt-8"><div className="font-medium text-sm mb-3">Registo de atividade</div>
        <div className="card divide-y divide-line/60">{events.data?.length ? events.data.map((e) => (
          <div key={e.id} className="px-4 py-2.5 flex gap-3 text-[13px]"><span className={"w-1.5 h-1.5 rounded-full mt-1.5 shrink-0 " + (e.level === "error" ? "bg-bad" : e.level === "warning" ? "bg-warn" : "bg-ok")} /><span className="flex-1 break-words">{e.message}</span><span className="text-xs text-dim shrink-0">{fmtDate(e.ts)}</span></div>
        )) : <div className="p-6 text-sm text-dim text-center">Sem eventos</div>}</div></div>
      <Modal open={!!open} onClose={() => setOpen(null)} title={open ? `Tarefa #${open.id}` : ""} footer={<button className="btn-ghost" onClick={() => setOpen(null)}>Fechar</button>}>
        {open && <div className="space-y-3 text-sm">
          <div className="grid grid-cols-2 gap-3">{[["Tipo", TYPE[open.type] ?? open.type], ["Estado", LABEL[open.status]], ["Início", fmtDate(open.started_at)], ["Fim", fmtDate(open.finished_at)], ["Progresso", `${open.progress}/${open.total}`]].map(([k, v]) => <div key={String(k)}><div className="label">{k}</div><div>{v}</div></div>)}</div>
          {open.error && <div className="text-bad bg-bad/10 rounded-lg p-3">{open.error}</div>}
          {open.result && <pre className="text-xs bg-s2 rounded-lg p-3 overflow-auto max-h-64">{JSON.stringify(open.result, null, 2)}</pre>}
        </div>}
      </Modal>
    </>
  );
}
