import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, AudioLines, CheckCircle2, Loader2 } from "lucide-react";
import { api, Job } from "../api";
import { cx } from "../lib";
import { Badge, Checkbox, Empty, ErrorBox, PageHeader, Skeleton, Tabs, useToast } from "../components/ui";

interface Item {
  subtitle_id: number; media_id: number; title: string | null; year: number | null; series_title: string | null; season: number | null; episode: number | null;
  name: string; lang: string | null; mode: string; status: string; stale: boolean; created_at: string; decided_at: string | null;
  offset_s: number | null; scale: number | null; agree: number | null; windows_total: number | null; windows_clear: number | null; reason: string | null; z_total: number | null;
}
interface Res { items: Item[]; counts: Record<string, number>; unchecked: number }
type Tab = "pending" | "aligned" | "inconclusive" | "done";

const TAB_STATUS: Record<Tab, string[]> = { pending: ["pending"], aligned: ["aligned"], inconclusive: ["inconclusive", "error"], done: ["applied", "dismissed"] };

function describe(i: Item): string {
  if (i.mode === "aligned") return `Alinhada (${i.agree}/${i.windows_total} janelas concordam, desvio ≈ ${(i.offset_s ?? 0).toFixed(2)} s)`;
  if (i.mode === "offset") return `${(i.offset_s ?? 0) > 0 ? "Atrasar" : "Adiantar"} ${Math.abs(i.offset_s ?? 0).toFixed(2)} s (${i.agree}/${i.windows_total} janelas concordam)`;
  if (i.mode === "linear") return `Deriva de frame rate: × ${(i.scale ?? 1).toFixed(6)} ${(i.offset_s ?? 0) >= 0 ? "+" : "−"} ${Math.abs(i.offset_s ?? 0).toFixed(2)} s (${i.agree}/${i.windows_total} janelas)`;
  if (i.mode === "error") return `Não foi possível analisar: ${i.reason ?? "erro"}`;
  return `Inconclusivo: ${i.reason ?? "as janelas não concordam"}`;
}

export default function SyncCheck() {
  const qc = useQueryClient();
  const toast = useToast();
  const [tab, setTab] = useState<Tab>("pending");
  const [job, setJob] = useState<number | null>(null);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const q = useQuery({ queryKey: ["audio-checks"], queryFn: () => api.get<Res>("/audio-checks"), refetchInterval: job != null ? 4000 : false });
  const jq = useQuery({ queryKey: ["audio-batch-job", job], enabled: job != null, queryFn: () => api.get<Job & { result: Record<string, number> | null }>(`/jobs/${job}`),
    refetchInterval: (x) => (x.state.data && ["queued", "running"].includes(x.state.data.status) ? 1500 : false) });
  const running = job != null && (!jq.data || ["queued", "running"].includes(jq.data.status));
  useEffect(() => {
    const j = jq.data;
    if (!j || job == null || ["queued", "running"].includes(j.status)) return;
    qc.invalidateQueries({ queryKey: ["audio-checks"] });
    if (j.status === "done" && j.result) toast("ok", `Verificação concluída: ${j.result.aligned} alinhadas, ${j.result.pending} à espera de aprovação, ${j.result.inconclusive} inconclusivas.`);
    else toast("warn", j.error || `Verificação ${j.status}.`);
    setJob(null);
  }, [jq.data?.status]);   // eslint-disable-line react-hooks/exhaustive-deps
  const start = useMutation({ mutationFn: (redo: boolean) => api.post<{ job_id: number; count: number }>("/audio-checks/start", { redo }),
    onSuccess: (r) => { setJob(r.job_id); toast("ok", `${r.count} legendas na fila (corre em segundo plano; uma de cada vez).`); }, onError: (e: Error) => toast("bad", e.message) });
  const cancel = useMutation({ mutationFn: () => api.post(`/jobs/${job}/cancel`) });

  const data = q.data;
  const items = (data?.items ?? []).filter((i) => TAB_STATUS[tab].includes(i.status));
  const cnt = (...s: string[]) => s.reduce((a, k) => a + (data?.counts[k] ?? 0), 0);
  const title = (i: Item) => i.series_title ? `${i.series_title} S${String(i.season ?? 0).padStart(2, "0")}E${String(i.episode ?? 0).padStart(2, "0")}` : `${i.title ?? "—"}${i.year ? ` (${i.year})` : ""}`;
  const apply = async (ids: number[]) => {
    setBusy(true);
    let ok = 0, fail = 0;
    for (const id of ids) {
      try { await api.post(`/audio-checks/${id}/apply`); ok++; } catch (e) { fail++; toast("bad", (e as Error).message); }
    }
    setBusy(false); setSel(new Set());
    qc.invalidateQueries();
    if (ok) toast("ok", `${ok} corrigida${ok === 1 ? "" : "s"}. Cada original ficou em backup (Alterações a legendas do filme).`);
  };
  const dismiss = useMutation({ mutationFn: (id: number) => api.post(`/audio-checks/${id}/dismiss`), onSuccess: () => qc.invalidateQueries({ queryKey: ["audio-checks"] }), onError: (e: Error) => toast("bad", e.message) });
  const pendingIds = items.filter((i) => i.status === "pending" && !i.stale).map((i) => i.subtitle_id);
  const allSel = pendingIds.length > 0 && pendingIds.every((id) => sel.has(id));

  return (
    <>
      <PageHeader title="Verificação de sincronização" subtitle="Compara cada legenda com a fala do áudio do vídeo, em segundo plano. Só lê o áudio: as correções ficam à espera da sua aprovação, e o que não for claro fica «inconclusivo»."
        actions={<button className="btn-primary" disabled={start.isPending || running || (data?.unchecked ?? 0) === 0} onClick={() => start.mutate(false)}>{(start.isPending || running) ? <Loader2 size={15} className="animate-spin" /> : <AudioLines size={15} />}Verificar as por confirmar{data ? ` (${data.unchecked})` : ""}</button>} />
      <div className="card p-3.5 mb-4 text-xs text-mute flex gap-2.5"><AlertTriangle size={15} className="text-warn shrink-0 mt-0.5" />
        <div>Cada legenda gasta cerca de 1–3 minutos de leitura de áudio (uma de cada vez, para não sobrecarregar o disco ou a rede). Só entram as legendas nos seus idiomas, associadas com certeza a um vídeo e ainda sem prova de sincronização. <b className="text-fg">Nada é alterado sem o seu «Aplicar»</b>; cada correção faz backup e pode ser restaurada.</div></div>

      {running && (
        <div className="card p-4 mb-4 space-y-2">
          <div className="flex items-center gap-2 text-sm"><Loader2 size={15} className="animate-spin text-accent" />{jq.data?.status === "queued" ? "Em fila…" : jq.data?.message || "A iniciar…"} {jq.data?.total ? `· ${jq.data.progress} / ${jq.data.total}` : ""}</div>
          <div className="h-1.5 bg-s3 rounded"><div className="h-1.5 bg-accent rounded transition-all" style={{ width: `${jq.data?.total ? (100 * jq.data.progress) / jq.data.total : 3}%` }} /></div>
          <div className="text-xs text-dim truncate">{jq.data?.current}</div>
          <button className="btn-ghost" onClick={() => cancel.mutate()}>Cancelar</button>
        </div>
      )}

      <Tabs value={tab} onChange={(t) => { setTab(t as Tab); setSel(new Set()); }} items={[
        { id: "pending", label: `À espera de aprovação (${cnt("pending")})` }, { id: "aligned", label: `Alinhadas (${cnt("aligned")})` },
        { id: "inconclusive", label: `Inconclusivas (${cnt("inconclusive", "error")})` }, { id: "done", label: `Decididas (${cnt("applied", "dismissed")})` }]} />
      <div className="mt-4 space-y-3">
        {q.isLoading && <Skeleton className="h-32" />}
        {q.isError && <ErrorBox error={q.error} retry={() => q.refetch()} />}
        {data && items.length === 0 && (
          <Empty icon={<CheckCircle2 size={22} />} title={tab === "pending" ? "Nada à espera de aprovação" : "Nada aqui"} hint={tab === "pending" ? ((data.unchecked ?? 0) > 0 ? "Carregue em «Verificar as por confirmar» para analisar as legendas ainda sem prova de sincronização." : "Todas as legendas por confirmar já foram verificadas.") : undefined} />
        )}
        {tab === "pending" && pendingIds.length > 0 && (
          <div className="flex items-center gap-3 flex-wrap text-sm">
            <span className="tabular-nums">{sel.size} selecionada{sel.size === 1 ? "" : "s"}</span>
            <button className="btn-primary" disabled={sel.size === 0 || busy} onClick={() => window.confirm(`Aplicar a correção a ${sel.size} legenda(s)? Cada original fica em backup e pode ser restaurado.`) && apply([...sel])}>{busy ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}Aplicar selecionadas</button>
          </div>
        )}
        {items.length > 0 && (
          <div className="card overflow-x-auto"><table className="w-full min-w-[760px]">
            <thead className="border-b border-line"><tr>{tab === "pending" && <th className="th w-9"><Checkbox checked={allSel} label="Selecionar todas" onChange={(v) => setSel(v ? new Set(pendingIds) : new Set())} /></th>}
              <th className="th">Título</th><th className="th">Legenda</th><th className="th">Resultado do áudio</th><th className="th" /></tr></thead>
            <tbody>{items.map((i) => (
              <tr key={i.subtitle_id} className="border-b border-line/60 last:border-0 align-top">
                {tab === "pending" && <td className="td"><Checkbox checked={sel.has(i.subtitle_id)} label="Selecionar" onChange={(v) => i.stale ? undefined : setSel((s) => { const n = new Set(s); v ? n.add(i.subtitle_id) : n.delete(i.subtitle_id); return n; })} /></td>}
                <td className="td font-medium max-w-[220px] truncate" title={title(i)}>{title(i)}</td>
                <td className="td font-mono text-xs text-mute max-w-[280px] truncate" title={i.name}>{i.name}</td>
                <td className="td text-xs">
                  <div className={cx(i.mode === "inconclusive" || i.mode === "error" ? "text-mute" : "text-fg")}>{describe(i)}</div>
                  {i.status === "applied" && <Badge tone="ok">Aplicada</Badge>}{i.status === "dismissed" && <Badge>Ignorada</Badge>}
                  {i.stale && i.status === "pending" && <div className="text-warn mt-0.5">A legenda mudou desde a análise: volte a verificar.</div>}
                </td>
                <td className="td text-right whitespace-nowrap">
                  {i.status === "pending" && <>
                    <button className="btn-primary !h-8" disabled={i.stale || busy} onClick={() => window.confirm(`Aplicar: ${describe(i)}?\nO original fica em backup.`) && apply([i.subtitle_id])}>Aplicar</button>{" "}
                    <button className="btn-ghost !h-8" onClick={() => dismiss.mutate(i.subtitle_id)}>Ignorar</button></>}
                </td>
              </tr>))}</tbody></table></div>
        )}
      </div>
    </>
  );
}
