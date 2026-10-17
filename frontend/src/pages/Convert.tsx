import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, FileCog, Loader2 } from "lucide-react";
import { api, Job } from "../api";
import { Badge, Checkbox, Empty, ErrorBox, PageHeader, Skeleton, useToast } from "../components/ui";

interface Item { id: number; name: string; title: string | null; year: number | null; lang: string | null; format: string; encoding: string; bom: boolean; moji: number; target: string; reason: string; warnings: string[] }
interface ScanRes { checked: number; items: Item[]; conflicts: number; errors: number; truncated: boolean; by_reason: Record<string, number> }
interface ApplyRes { converted: number; skipped: number; errors: number; details: { id: number; name: string | null; error: string }[] }

const REASON: Record<string, string> = { formato: "ASS/SSA/VTT → SRT", codificação: "codificação → UTF-8", acentos: "acentos partidos" };

function useJob<T>(id: number | null) {
  return useQuery({ queryKey: ["convert-job", id], enabled: id != null, queryFn: () => api.get<Job & { result: T | null }>(`/jobs/${id}`),
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 1000 : false) });
}

export default function Convert() {
  const qc = useQueryClient();
  const toast = useToast();
  const [scanId, setScanId] = useState<number | null>(null);
  const [applyId, setApplyId] = useState<number | null>(null);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [fix, setFix] = useState(true);
  const [keep, setKeep] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const scan = useJob<ScanRes>(scanId);
  const apply = useJob<ApplyRes>(applyId);
  const res = scan.data?.status === "done" ? scan.data.result : null;
  const running = (j?: { status: string }) => !!j && ["queued", "running"].includes(j.status);

  const startScan = useMutation({ mutationFn: () => api.post<{ job_id: number }>("/conversions/scan"), onSuccess: (r) => { setScanId(r.job_id); setApplyId(null); setSel(new Set()); }, onError: (e: Error) => toast("bad", e.message) });
  const startApply = useMutation({
    mutationFn: () => api.post<{ job_id: number }>("/conversions/apply", { ids: [...sel], fix_mojibake: fix, keep_original: keep }),
    onSuccess: (r) => { setApplyId(r.job_id); setConfirm(false); }, onError: (e: Error) => { setConfirm(false); toast("bad", e.message); },
  });
  useEffect(() => { if (apply.data?.status === "done") { qc.invalidateQueries(); } }, [apply.data?.status]);   // eslint-disable-line react-hooks/exhaustive-deps

  const items = res?.items ?? [];
  const toggle = (id: number) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const allSel = items.length > 0 && items.every((i) => sel.has(i.id));

  return (
    <>
      <PageHeader title="Converter legendas" subtitle="Converte ASS/SSA/VTT para SRT e passa SRT para UTF-8 sem BOM (o formato que a Smart TV, o Android TV e o Plex leem melhor). Só em bibliotecas com escrita ativa."
        actions={<button className="btn-primary" disabled={startScan.isPending || running(scan.data)} onClick={() => startScan.mutate()}>{(startScan.isPending || running(scan.data)) ? <Loader2 size={15} className="animate-spin" /> : <FileCog size={15} />}Procurar legendas a converter</button>} />
      <div className="card p-3.5 mb-4 text-xs text-mute flex gap-2.5"><AlertTriangle size={15} className="text-warn shrink-0 mt-0.5" />
        <div>A procura <b className="text-fg">só lê</b> os ficheiros. Ao converter, o <b className="text-fg">original vai para backup</b> (restaurável no histórico de alterações do filme), os <b className="text-fg">tempos são verificados</b> e <b className="text-fg">nunca se substitui um .srt existente</b>. ASS/VTT perdem posições, cores e fontes.</div></div>

      {scanId == null && <Empty icon={<FileCog size={22} />} title="Ainda não procurou" hint="Carregue em «Procurar legendas a converter» para ver o que precisa de conversão." />}
      {scan.isError && <ErrorBox error={scan.error} retry={() => scan.refetch()} />}
      {running(scan.data) && (
        <div className="card p-4 space-y-2"><div className="flex items-center gap-2 text-sm"><Loader2 size={15} className="animate-spin text-accent" />A verificar… {scan.data!.progress} / {scan.data!.total}</div>
          <div className="h-1.5 bg-s3 rounded"><div className="h-1.5 bg-accent rounded" style={{ width: `${scan.data!.total ? (100 * scan.data!.progress) / scan.data!.total : 0}%` }} /></div>
          <div className="text-xs text-dim truncate">{scan.data!.current}</div></div>
      )}
      {scanId != null && scan.isLoading && <Skeleton className="h-24" />}
      {scan.data && ["failed", "cancelled", "interrupted"].includes(scan.data.status) && <div className="text-sm text-bad">A procura terminou com estado «{scan.data.status}»{scan.data.error ? `: ${scan.data.error}` : ""}.</div>}

      {res && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-mute tabular-nums">{res.checked} legendas verificadas · <b className="text-fg">{items.length}</b> a converter{res.truncated ? " (lista truncada; repita depois de converter)" : ""}</span>
            {Object.entries(res.by_reason).map(([k, v]) => <Badge key={k}>{REASON[k] ?? k}: {v}</Badge>)}
            {res.conflicts > 0 && <Badge tone="warn">{res.conflicts} com .srt já existente (ignoradas)</Badge>}
            {res.errors > 0 && <Badge tone="bad">{res.errors} com erro</Badge>}
          </div>
          {items.length === 0 && <Empty icon={<CheckCircle2 size={22} />} title="Nada a converter" hint="Todas as legendas já são SRT em UTF-8." />}
          {items.length > 0 && (
            <>
              <div className="flex flex-wrap items-center gap-4 text-sm">
                <label className="flex items-center gap-2 cursor-pointer"><input type="checkbox" className="accent-[rgb(229,160,13)]" checked={fix} onChange={(e) => setFix(e.target.checked)} />Reparar acentos partidos</label>
                <label className="flex items-center gap-2 cursor-pointer"><input type="checkbox" className="accent-[rgb(229,160,13)]" checked={keep} onChange={(e) => setKeep(e.target.checked)} />Manter também o ASS/VTT original</label>
              </div>
              <div className="card overflow-x-auto"><table className="w-full min-w-[720px]">
                <thead className="border-b border-line"><tr><th className="th w-9"><Checkbox checked={allSel} label="Selecionar todas" onChange={(v) => setSel(v ? new Set(items.map((i) => i.id)) : new Set())} /></th>
                  <th className="th">Filme</th><th className="th">Ficheiro</th><th className="th">Conversão</th></tr></thead>
                <tbody>{items.map((i) => (
                  <tr key={i.id} className={"border-b border-line/60 last:border-0 " + (sel.has(i.id) ? "bg-accent/5" : "hover:bg-s2/50")}>
                    <td className="td"><Checkbox checked={sel.has(i.id)} onChange={() => toggle(i.id)} label="Selecionar" /></td>
                    <td className="td font-medium max-w-[240px] truncate">{i.title ?? "—"}{i.year && <span className="text-dim font-normal"> ({i.year})</span>}</td>
                    <td className="td font-mono text-xs text-mute max-w-[360px] truncate" title={i.name}>{i.name}</td>
                    <td className="td text-xs"><Badge>{REASON[i.reason] ?? i.reason}</Badge> <span className="text-dim">{i.format !== "srt" ? `→ ${i.target}` : `${i.encoding}${i.bom ? " + BOM" : ""}`}</span>
                      {i.warnings.map((w, k) => <div key={k} className="text-warn mt-0.5">{w}</div>)}</td>
                  </tr>))}</tbody></table></div>
              <div className="flex items-center gap-3 flex-wrap">
                <span className="text-sm tabular-nums">{sel.size} selecionada{sel.size === 1 ? "" : "s"}</span>
                {!confirm ? <button className="btn-primary" disabled={sel.size === 0 || running(apply.data)} onClick={() => setConfirm(true)}><FileCog size={15} />Converter selecionadas…</button>
                  : <><button className="btn-primary" disabled={startApply.isPending} onClick={() => startApply.mutate()}>{startApply.isPending && <Loader2 size={14} className="animate-spin" />}Confirmar: converter {sel.size} (com backup)</button>
                    <button className="btn-ghost" onClick={() => setConfirm(false)}>Cancelar</button></>}
              </div>
            </>
          )}
        </div>
      )}

      {apply.data && (
        <div className="card p-4 mt-4 text-sm space-y-1.5">
          {running(apply.data) ? <div className="flex items-center gap-2"><Loader2 size={15} className="animate-spin text-accent" />A converter… {apply.data.progress} / {apply.data.total}</div>
            : apply.data.result && (<>
              <div className="font-medium flex items-center gap-2"><CheckCircle2 size={16} className="text-ok" />{apply.data.result.converted} convertidas · {apply.data.result.skipped} ignoradas · {apply.data.result.errors} erros</div>
              {apply.data.result.details.slice(0, 8).map((d) => <div key={d.id} className="text-xs text-mute">{d.name ?? d.id} — {d.error}</div>)}
              <div className="text-xs text-dim">Cada original está em backup; restaure no histórico de alterações do filme. Pode voltar a procurar para confirmar.</div></>)}
        </div>
      )}
    </>
  );
}
