import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, AudioLines, CheckCircle2, Clock, Film, FileCog, GitCompare, Languages, Loader2, Move, RotateCcw, Ruler, ScanSearch, Trash2, Wand2 } from "lucide-react";
import { api } from "../api";
import { cx, fmtTs } from "../lib";
import { Badge, Modal, Skeleton, Tabs, useToast } from "./ui";

type Tool = "shift" | "fps" | "twopoint" | "reference" | "audio" | "durations" | "quality" | "ptpt" | "text" | "convert" | "delete";
interface AudioWin { t: number; offset: number | null; z: number; used?: boolean; speech_pct?: number }
interface AudioRes { mode: "aligned" | "offset" | "linear" | "inconclusive"; offset_s: number; scale: number; drift_s_per_hour: number; agree: number; windows_total: number; windows_clear: number; windows_failed: number; confidence: number; z_total?: number; residual_s?: number; reason?: string; ratio?: number; windows: AudioWin[] }
interface Cue { n: number; start: number; end: number; text: string }
interface CuesRes { total: number; head: Cue[]; tail: Cue[]; references: { id: number; name: string; lang: string | null; cues: number }[] }
interface PRow { n: number; old_start: number; new_start: number; new_end: number; text: string }
interface QIssue { key: string; label: string; severity: "bad" | "warn" | "info"; fix: string | null; hint: string; count: number; examples: { n: number; time: string; text: string }[] }
interface QualityRes { cues: number; issues: QIssue[]; max_len: number; avg_chars: number; clean: boolean; name: string; encoding: string }
interface TextChange { n: number; before: string; after: string; tags: string[] }
interface Preview {
  kind?: "text" | "convert"; stats?: Record<string, number>; changes?: TextChange[];
  target?: string; format?: string; bom?: boolean; cues?: number; moji?: number; samples?: { n: number; start: number; end: number; text: string }[];
  summary: string; base_hash: string; total: number; head: PRow[]; tail: PRow[]; warnings: string[]; dropped: number; clamped: number;
  first_start_before: number; first_start_after: number; last_end_before: number; last_end_after: number; duration_check: string | null; encoding: string;
  estimate: { offset_ms?: number; match_ratio?: number; drift_ms?: number; compared?: number } | null;
}
const FPS = [23.976, 24, 25, 29.97, 30];

export function SubToolsModal({ sub, onClose }: { sub: { id: number; name: string; lang: string | null; canEdit: boolean; isSrt: boolean; isText: boolean } | null; onClose: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [tool, setTool] = useState<Tool>("shift");
  const [params, setParams] = useState<Record<string, any>>({ seconds: 0, from_fps: 23.976, to_fps: 25, mode: "offset", hi: true, style: false, common: true, uppercase: false, fix_mojibake: true, keep_original: false, cps: 17, min_seconds: 1, max_seconds: 7 });
  const [pv, setPv] = useState<Preview | null>(null);
  const [confirmDel, setConfirmDel] = useState(false);
  const [audioJob, setAudioJob] = useState<number | null>(null);
  const [audioRes, setAudioRes] = useState<AudioRes | null>(null);
  useEffect(() => { setAudioJob(null); setAudioRes(null); }, [sub?.id]);
  useEffect(() => { setPv(null); setConfirmDel(false); }, [tool, sub?.id]);
  const audioStart = useMutation({ mutationFn: () => api.post<{ job_id: number }>(`/subtitles/${sub!.id}/audio-sync/start`), onSuccess: (r) => { setAudioRes(null); setPv(null); setAudioJob(r.job_id); }, onError: (e: Error) => toast("bad", e.message) });
  const audioQ = useQuery({ queryKey: ["audio-job", audioJob], enabled: audioJob != null,
    queryFn: () => api.get<{ status: string; progress: number; total: number; message: string | null; current: string | null; error: string | null; result: AudioRes | null }>(`/jobs/${audioJob}`),
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 1000 : false) });
  const audioRunning = audioJob != null && (!audioQ.data || ["queued", "running"].includes(audioQ.data.status));
  useEffect(() => {
    const j = audioQ.data;
    if (!j || audioJob == null) return;
    if (j.status === "done" && j.result) { setAudioRes(j.result); setAudioJob(null); qc.invalidateQueries({ queryKey: ["media"] }); }
    else if (["failed", "cancelled", "interrupted"].includes(j.status)) { toast(j.status === "failed" ? "bad" : "warn", j.error || "Análise cancelada."); setAudioJob(null); }
  }, [audioQ.data?.status]);   // eslint-disable-line react-hooks/exhaustive-deps
  const audioCancel = useMutation({ mutationFn: () => api.post(`/jobs/${audioJob}/cancel`) });
  const audioUsable = !!audioRes && (audioRes.mode === "offset" || audioRes.mode === "linear");
  useEffect(() => { if (sub) { setTool(sub.isSrt ? "shift" : "convert"); setParams({ seconds: 0, from_fps: 23.976, to_fps: 25, mode: "offset", hi: true, style: false, common: true, uppercase: false, fix_mojibake: true, keep_original: false, cps: 17, min_seconds: 1, max_seconds: 7, max_len: 42, merge: true, vocab: true, spelling: true, gerund: true }); setPv(null); } }, [sub?.id]);

  const cues = useQuery({ queryKey: ["sub-cues", sub?.id], queryFn: () => api.get<CuesRes>(`/subtitles/${sub!.id}/cues`), enabled: !!sub && sub.isSrt });
  const quality = useQuery({ queryKey: ["sub-quality", sub?.id, params.max_len], queryFn: () => api.get<QualityRes>(`/subtitles/${sub!.id}/quality?max_len=${Number(params.max_len) || 42}`), enabled: !!sub && sub.isSrt && tool === "quality", retry: false });
  const body = () => ({ action: tool === "text" ? "textfix" : tool === "audio" ? "linear" : tool === "quality" ? "reflow" : tool, params: tool === "quality" ? { max_len: Number(params.max_len) || 42, merge: !!params.merge }
    : tool === "ptpt" ? { vocab: !!params.vocab, spelling: !!params.spelling, gerund: !!params.gerund } : tool === "text" ? { hi: !!params.hi, style: !!params.style, common: !!params.common, uppercase: !!params.uppercase }
    : tool === "convert" ? { fix_mojibake: !!params.fix_mojibake, keep_original: !!params.keep_original }
      : tool === "audio" ? { scale: audioRes?.scale ?? 1, offset_seconds: audioRes?.offset_s ?? 0 }
        : tool === "durations" ? { cps: Number(params.cps), min_seconds: Number(params.min_seconds), max_seconds: Number(params.max_seconds) } : params });
  const preview = useMutation({ mutationFn: () => api.post<Preview>(`/subtitles/${sub!.id}/preview`, body()), onSuccess: setPv, onError: (e: Error) => { setPv(null); toast("bad", e.message); } });
  const apply = useMutation({
    mutationFn: () => api.post<{ edit_id: number; summary: string }>(`/subtitles/${sub!.id}/apply`, { ...body(), base_hash: pv!.base_hash }),
    onSuccess: (r) => { toast("ok", `Aplicado. Cópia do original guardada (#${r.edit_id}); pode restaurá-la.`); qc.invalidateQueries(); onClose(); },
    onError: (e: Error) => toast("bad", e.message),
  });
  const del = useMutation({
    mutationFn: () => api.post<{ edit_id: number }>(`/subtitles/${sub!.id}/delete`),
    onSuccess: () => { toast("ok", "Legenda movida para backup. Pode restaurá-la no histórico de alterações."); qc.invalidateQueries(); onClose(); },
    onError: (e: Error) => toast("bad", e.message),
  });
  const set = (k: string, v: any) => { setParams((p) => ({ ...p, [k]: v })); setPv(null); };
  if (!sub) return null;
  const refs = cues.data?.references ?? [];
  const pickCue = (slot: 1 | 2, c: Cue) => { set(`cue${slot}`, c.n); if (!params[`time${slot}`]) set(`time${slot}`, fmtTs(c.start)); };
  const editable = sub.canEdit && (tool === "convert" ? sub.isText : tool === "audio" ? sub.isSrt && audioUsable : sub.isSrt);

  const row = (r: PRow) => (
    <tr key={r.n} className="border-b border-line/50 last:border-0 text-xs">
      <td className="px-2 py-1.5 text-dim tabular-nums">{r.n}</td>
      <td className="px-2 py-1.5 font-mono text-mute whitespace-nowrap">{fmtTs(r.old_start)} <ArrowRight size={10} className="inline mx-0.5" /> <span className="text-fg">{fmtTs(r.new_start)}</span></td>
      <td className="px-2 py-1.5 truncate max-w-[240px]">{r.text}</td>
    </tr>
  );
  const CueList = ({ list, slot }: { list: Cue[]; slot: 1 | 2 }) => (
    <div className="max-h-40 overflow-y-auto rounded-lg border border-line divide-y divide-line/50">
      {list.map((c) => (
        <button key={c.n} onClick={() => pickCue(slot, c)} className={cx("w-full text-left px-2.5 py-1.5 text-xs flex gap-2 hover:bg-s3", params[`cue${slot}`] === c.n && "bg-accent/10")}>
          <span className="text-dim w-8 tabular-nums">{c.n}</span><span className="font-mono text-mute">{fmtTs(c.start)}</span><span className="truncate">{c.text}</span>
        </button>
      ))}
    </div>
  );

  return (
    <Modal open onClose={onClose} wide title={<span className="flex items-center gap-2">Ferramentas · <span className="font-mono text-[13px] font-normal text-mute break-all">{sub.name}</span></span>}
      footer={tool === "delete" ? (
        <><button className="btn-ghost" onClick={onClose}>Fechar</button>
          {!confirmDel ? <button className="btn-danger" disabled={!sub.canEdit} onClick={() => setConfirmDel(true)}><Trash2 size={14} />Mover para backup…</button>
            : <button className="btn-danger" disabled={del.isPending} onClick={() => del.mutate()}>{del.isPending && <Loader2 size={14} className="animate-spin" />}Confirmar: mover para backup</button>}</>
      ) : (
        <><button className="btn-ghost" onClick={onClose}>Fechar</button>
          <button className="btn-ghost" disabled={!editable || preview.isPending} onClick={() => preview.mutate()}>{preview.isPending && <Loader2 size={14} className="animate-spin" />}Pré-visualizar</button>
          <button className="btn-primary" title={!pv ? "Carregue primeiro em «Pré-visualizar»: só depois se pode aplicar" : undefined} disabled={!pv || apply.isPending || (!pv.kind && pv.dropped > 0 && pv.dropped > pv.total * 0.05)} onClick={() => apply.mutate()}>{apply.isPending && <Loader2 size={14} className="animate-spin" />}Aplicar (guarda cópia do original)</button></>
      )}>
      <div className="space-y-4">
        {sub.canEdit && !pv && tool !== "delete" && <div className="text-xs text-dim">Para ativar «Aplicar», carregue primeiro em <b className="text-mute">Pré-visualizar</b> (se disser que não há nada a alterar, não há nada para aplicar).</div>}
        {!sub.canEdit && <div className="text-sm text-warn flex gap-2"><AlertTriangle size={16} className="shrink-0 mt-0.5" />Esta legenda está numa biblioteca só de leitura (ou sem permissão de escrita). Ative a escrita em Definições → Bibliotecas.</div>}
        {sub.canEdit && !sub.isSrt && tool !== "convert" && tool !== "delete" && <div className="text-sm text-warn flex gap-2"><AlertTriangle size={16} className="shrink-0 mt-0.5" />Estas ferramentas só editam legendas SRT. Converta-a primeiro no separador «Converter».</div>}
        <Tabs value={tool} onChange={setTool} items={[{ id: "shift", label: "Atrasar/adiantar" }, { id: "fps", label: "Frame rate" }, { id: "twopoint", label: "Dois pontos" }, { id: "reference", label: "Sincronizar com referência" }, { id: "audio", label: "Alinhar com o áudio" }, { id: "durations", label: "Durações" }, { id: "quality", label: "Qualidade" }, { id: "ptpt", label: "pt-BR → pt-PT" }, { id: "text", label: "Limpar texto" }, { id: "convert", label: "Converter" }, { id: "delete", label: "Apagar" }]} />

        {tool === "shift" && (
          <div className="space-y-2.5">
            <div className="text-sm text-mute flex gap-2"><Move size={16} className="shrink-0 mt-0.5" /><div>Desloca todas as falas. <b className="text-fg">Positivo atrasa</b> (a legenda aparece mais tarde), <b className="text-fg">negativo adianta</b>.</div></div>
            <div className="flex items-center gap-2 flex-wrap">
              <input className="input !w-32" type="number" step="0.1" value={params.seconds} onChange={(e) => set("seconds", Number(e.target.value))} aria-label="Segundos" /><span className="text-sm text-mute">segundos</span>
              {[-5, -1, -0.5, 0.5, 1, 5].map((v) => <button key={v} className="btn-ghost !h-8" onClick={() => set("seconds", Math.round(((Number(params.seconds) || 0) + v) * 100) / 100)}>{v > 0 ? "+" : ""}{v}</button>)}
            </div>
          </div>
        )}
        {tool === "fps" && (
          <div className="space-y-2.5">
            <div className="text-sm text-mute flex gap-2"><Film size={16} className="shrink-0 mt-0.5" /><div>Corrige uma legenda feita para outro frame rate (a fala vai ficando cada vez mais adiantada ou atrasada ao longo do filme). Multiplica os tempos por <i>origem ÷ destino</i>.</div></div>
            <div className="flex items-center gap-2 flex-wrap text-sm">
              <span>De</span><select className="input !w-28" value={params.from_fps} onChange={(e) => set("from_fps", Number(e.target.value))}>{FPS.map((f) => <option key={f} value={f}>{f}</option>)}</select>
              <span>para</span><select className="input !w-28" value={params.to_fps} onChange={(e) => set("to_fps", Number(e.target.value))}>{FPS.map((f) => <option key={f} value={f}>{f}</option>)}</select><span className="text-mute">fps</span>
            </div>
          </div>
        )}
        {tool === "twopoint" && (
          <div className="space-y-3">
            <div className="text-sm text-mute flex gap-2"><Ruler size={16} className="shrink-0 mt-0.5" /><div>Escolha <b className="text-fg">duas falas</b> (uma perto do início, outra perto do fim) e diga <b className="text-fg">quando devem aparecer no vídeo</b>. A app calcula o deslocamento e a escala.</div></div>
            {cues.isLoading && <Skeleton className="h-24" />}
            {cues.data && [1, 2].map((slot) => (
              <div key={slot} className="space-y-1.5">
                <div className="flex items-center gap-2 flex-wrap text-sm"><b>Ponto {slot}</b>
                  <span className="text-mute">fala nº</span><input className="input !w-20" type="number" min={1} max={cues.data.total} value={params[`cue${slot}`] ?? ""} onChange={(e) => set(`cue${slot}`, Number(e.target.value))} />
                  <span className="text-mute">deve aparecer às</span><input className="input !w-40 font-mono" placeholder="HH:MM:SS,mmm" value={params[`time${slot}`] ?? ""} onChange={(e) => set(`time${slot}`, e.target.value)} /></div>
                <CueList list={slot === 1 ? cues.data.head : (cues.data.tail.length ? cues.data.tail : cues.data.head)} slot={slot as 1 | 2} />
              </div>
            ))}
          </div>
        )}
        {tool === "reference" && (
          <div className="space-y-2.5">
            <div className="text-sm text-mute flex gap-2"><GitCompare size={16} className="shrink-0 mt-0.5" /><div>Alinha esta legenda com <b className="text-fg">outra legenda já existente</b> do mesmo filme (por exemplo, a que sabe estar certa). <b className="text-fg">Não usa o áudio</b>: assume que a referência está sincronizada.</div></div>
            {cues.isLoading && <Skeleton className="h-10" />}
            {cues.data && refs.length === 0 && <div className="text-sm text-warn">Este filme não tem outra legenda externa válida para usar como referência.</div>}
            {refs.length > 0 && (
              <div className="space-y-2">
                <select className="input" value={params.reference_id ?? ""} onChange={(e) => set("reference_id", Number(e.target.value))}>
                  <option value="">Escolha a referência…</option>{refs.map((r) => <option key={r.id} value={r.id}>{r.name} {r.lang ? `(${r.lang})` : ""} · {r.cues} falas</option>)}
                </select>
                <div className="flex gap-4 text-sm flex-wrap">
                  <label className="flex items-center gap-2 cursor-pointer"><input type="radio" className="accent-[rgb(229,160,13)]" checked={params.mode === "offset"} onChange={() => set("mode", "offset")} />Só deslocar (offset)</label>
                  <label className="flex items-center gap-2 cursor-pointer"><input type="radio" className="accent-[rgb(229,160,13)]" checked={params.mode === "linear"} onChange={() => set("mode", "linear")} />Corrigir também a deriva (frame rate)</label>
                </div>
              </div>
            )}
          </div>
        )}
        {tool === "durations" && (
          <div className="space-y-3">
            <div className="text-sm text-mute flex gap-2"><Clock size={16} className="shrink-0 mt-0.5" /><div>Para legendas em que as falas <b className="text-fg">desaparecem depressa demais</b> para se ler. Alonga o <b className="text-fg">fim</b> das falas curtas até ao tempo de leitura. <b className="text-fg">O início, o texto e a ordem não mudam</b>; nunca encurta uma fala nem a sobrepõe à seguinte.</div></div>
            <div className="grid sm:grid-cols-3 gap-3">
              <label className="space-y-1 text-xs text-mute">Velocidade de leitura (car/s)<input className="input" type="number" min={8} max={40} step={1} value={params.cps} onChange={(e) => set("cps", Number(e.target.value))} /><span className="text-dim">17 é o habitual; menos = fica mais tempo</span></label>
              <label className="space-y-1 text-xs text-mute">Mínimo por fala (s)<input className="input" type="number" min={0.3} max={3} step={0.1} value={params.min_seconds} onChange={(e) => set("min_seconds", Number(e.target.value))} /></label>
              <label className="space-y-1 text-xs text-mute">Máximo por fala (s)<input className="input" type="number" min={2} max={15} step={0.5} value={params.max_seconds} onChange={(e) => set("max_seconds", Number(e.target.value))} /></label>
            </div>
          </div>
        )}
        {tool === "quality" && (
          <div className="space-y-3">
            <div className="text-sm text-mute flex gap-2"><ScanSearch size={16} className="shrink-0 mt-0.5" /><div>Analisa a legenda e mostra <b className="text-fg">problemas reais</b> (falas sobrepostas, linhas compridas, leitura rápida demais, anúncios…). É só um relatório: nada é alterado. Para os problemas de formatação, a ferramenta abaixo reformata as linhas.</div></div>
            {quality.isLoading && <Skeleton className="h-24" />}
            {quality.error && <div className="text-sm text-warn flex gap-2"><AlertTriangle size={16} className="shrink-0 mt-0.5" />{(quality.error as Error).message}</div>}
            {quality.data && (
              <div className="space-y-2">
                {quality.data.clean
                  ? <div className="card border-ok/40 p-3.5 text-sm flex items-center gap-2 font-medium text-ok"><CheckCircle2 size={16} />Sem problemas encontrados em {quality.data.cues} falas.</div>
                  : <div className="text-xs text-dim">{quality.data.cues} falas analisadas · média de {quality.data.avg_chars} caracteres por fala · {quality.data.issues.length} tipo(s) de problema.</div>}
                {quality.data.issues.map((i) => (
                  <div key={i.key} className="card bg-s2 p-3 space-y-1.5">
                    <div className="flex items-center gap-2 flex-wrap text-sm font-medium">
                      <Badge tone={i.severity === "bad" ? "bad" : i.severity === "warn" ? "warn" : "neutral"}>{i.count}</Badge>{i.label}
                      {i.fix && i.fix !== "reflow" && <button className="btn-ghost !h-7 !px-2 text-xs ml-auto" onClick={() => setTool(i.fix === "durations" ? "durations" : "text")}>Abrir ferramenta</button>}
                    </div>
                    {i.hint && <div className="text-xs text-mute">{i.hint}</div>}
                    <div className="space-y-0.5">{i.examples.map((e) => <div key={e.n} className="text-xs flex gap-2"><span className="text-dim tabular-nums w-8 shrink-0">#{e.n}</span><span className="font-mono text-mute shrink-0">{e.time}</span><span className="truncate">{e.text}</span></div>)}</div>
                  </div>
                ))}
              </div>
            )}
            <div className="card bg-s2 p-3.5 space-y-2.5">
              <div className="text-sm font-medium">Reformatar linhas</div>
              <div className="text-xs text-mute">Reparte o texto por <b className="text-fg">no máximo 2 linhas equilibradas</b>, sem partir depois de «de», «o», «que»… e preferindo quebrar na pontuação. Diálogos («- Olá») e posições ({"{\an8}"}) não são tocados. <b className="text-fg">Só mudam as quebras de linha</b>: o texto e os tempos ficam iguais (verificado antes de gravar).</div>
              <div className="flex items-center gap-3 flex-wrap text-sm">
                <label className="flex items-center gap-2">Máximo por linha<input className="input !w-20" type="number" min={28} max={60} value={params.max_len} onChange={(e) => set("max_len", Number(e.target.value))} />caracteres</label>
                <label className="flex items-center gap-2 cursor-pointer"><input type="checkbox" className="accent-[rgb(229,160,13)]" checked={!!params.merge} onChange={(e) => set("merge", e.target.checked)} />Juntar em 1 linha o que cabe numa</label>
              </div>
            </div>
          </div>
        )}
        {tool === "ptpt" && (
          <div className="space-y-2.5">
            <div className="text-sm text-mute flex gap-2"><Languages size={16} className="shrink-0 mt-0.5" /><div>Adapta uma legenda em <b className="text-fg">português do Brasil</b> para <b className="text-fg">português de Portugal</b> com <b className="text-fg">regras fixas</b>, sem IA e sem gastar quota. <b className="text-fg">Os tempos não mudam.</b> Conservador: só mexe no que tem equivalente seguro; palavras ambíguas (por exemplo «fato», «legal») ficam como estão.</div></div>
            {([
              ["vocab", "Vocabulário", "ônibus→autocarro, trem→comboio, café da manhã→pequeno-almoço, celular→telemóvel, sorvete→gelado, suco→sumo, garoto/garota→rapaz/rapariga, banheiro→casa de banho, geladeira→frigorífico, equipe→equipa…"],
              ["spelling", "Grafia e acentos", "bebê→bebé, tênis→ténis, contato→contacto, recepção→receção, infecção→infeção, Antônio→António, vovó→avó, mamãe→mãe…"],
              ["gerund", "«Estar a» + infinitivo", "«estou fazendo»→«estou a fazer», «está me ouvindo»→«está a ouvir-me». Só depois de formas de «estar»."],
            ] as const).map(([k, t, d]) => (
              <label key={k} className="flex items-start gap-3 cursor-pointer card bg-s2 p-3"><input type="checkbox" className="mt-1 accent-[rgb(229,160,13)]" checked={!!params[k]} onChange={(e) => set(k, e.target.checked)} /><span className="text-sm">{t}<span className="block text-xs text-mute">{d}</span></span></label>
            ))}
            <div className="text-xs text-dim">Não altera o tratamento («você» → «tu»), a colocação dos pronomes nem a gíria: isso depende do contexto e pede revisão humana. Se quiser uma tradução completa para pt-PT, use o separador de tradução do filme.</div>
          </div>
        )}
        {tool === "audio" && (
          <div className="space-y-3">
            <div className="text-sm text-mute flex gap-2"><AudioLines size={16} className="shrink-0 mt-0.5" /><div>Compara quando a legenda mostra texto com quando há <b className="text-fg">fala no áudio do vídeo</b>, em 10 janelas de 4 min espalhadas pelo filme, e estima o desvio (ou a deriva de frame rate). <b className="text-fg">Só lê o áudio</b>; nada é gravado sem pré-visualização e confirmação.</div></div>
            <div className="card bg-s2 p-3 text-xs text-mute space-y-1">
              <div>• O ffmpeg lê cerca de 40 min de áudio do ficheiro, no canal central se for 5.1/7.1 (em ficheiros grandes na rede pode demorar 1–3 min). Corre em segundo plano; pode fechar a janela.</div>
              <div>• Só conclui se as janelas <b>concordarem</b>; senão diz «inconclusivo». Música, dobragens, legendas forçadas ou outro corte do filme podem enganar a deteção.</div>
              <div>• A «confiança» é a fração de janelas que concordam, não uma probabilidade.</div>
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              <button className="btn-primary" disabled={!sub.isSrt || audioRunning || audioStart.isPending} onClick={() => audioStart.mutate()}>{(audioRunning || audioStart.isPending) ? <Loader2 size={14} className="animate-spin" /> : <AudioLines size={14} />}Analisar o áudio do vídeo</button>
              {audioRunning && <button className="btn-ghost" onClick={() => audioCancel.mutate()}>Cancelar</button>}
            </div>
            {audioRunning && (
              <div className="card bg-s2 p-3.5 space-y-2">
                <div className="h-2 rounded-full bg-s3 overflow-hidden"><div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${audioQ.data?.total ? Math.min(100, (100 * audioQ.data.progress) / audioQ.data.total) : 4}%` }} /></div>
                <div className="text-xs text-mute">{audioQ.data?.status === "queued" ? "Em fila (uma análise de áudio de cada vez)…" : audioQ.data?.current || audioQ.data?.message || "A iniciar…"}</div>
              </div>
            )}
            {audioRes && (
              <div className="space-y-3">
                <div className={cx("card p-3.5 space-y-1.5 text-sm", audioRes.mode === "inconclusive" ? "border-warn/40" : "border-ok/40")}>
                  {audioRes.mode === "aligned" && <div className="flex items-center gap-2 font-medium text-ok"><CheckCircle2 size={16} />Já está alinhada com a fala do vídeo ({audioRes.agree} de {audioRes.windows_total} janelas concordam, desvio ≈ {audioRes.offset_s.toFixed(2)} s). Nada a ajustar.</div>}
                  {audioRes.mode === "offset" && <div className="font-medium">Desvio estimado: {audioRes.offset_s > 0 ? "atrasar" : "adiantar"} <b className="text-accent">{Math.abs(audioRes.offset_s).toFixed(2)} s</b> a toda a legenda ({audioRes.agree} de {audioRes.windows_total} janelas concordam).</div>}
                  {audioRes.mode === "linear" && <div className="font-medium">Deriva de frame rate: tempos × <b className="text-accent">{audioRes.scale.toFixed(6)}</b> {audioRes.offset_s >= 0 ? "+" : "−"} {Math.abs(audioRes.offset_s).toFixed(2)} s ({audioRes.agree} de {audioRes.windows_total} janelas concordam).</div>}
                  {audioRes.mode === "inconclusive" && <div className="space-y-1.5"><div className="flex gap-2 text-warn"><AlertTriangle size={16} className="shrink-0 mt-0.5" /><span>Inconclusivo: {audioRes.reason}</span></div>
                    <div className="text-xs text-mute">Alternativas: <b>Sincronizar com referência</b> (se tiver outra legenda do filme certa), <b>Dois pontos</b> ou o ajuste manual. «Fala detetada» muito baixa em todas as janelas indica que o áudio lido quase não tem diálogo (por exemplo, a primeira faixa de áudio não é a falada).</div></div>}
                  <div className="text-xs text-dim">Janelas: {audioRes.windows_total} · com fala clara {audioRes.windows_clear}{audioRes.windows_failed ? ` · sem áudio legível ${audioRes.windows_failed}` : ""}{audioRes.confidence ? ` · concordam ${Math.round(audioRes.confidence * 100)} %` : ""}{audioRes.z_total ? ` · pico somado ${audioRes.z_total.toFixed(1)}` : ""}{audioRes.residual_s != null ? ` · erro mediano ${audioRes.residual_s.toFixed(2)} s` : ""}</div>
                  {audioUsable && <div className="text-xs text-mute">Carregue em <b>Pré-visualizar</b> para ver o antes/depois e só depois em Aplicar (fica cópia do original).</div>}
                </div>
                <div className="card overflow-hidden"><table className="w-full"><thead className="border-b border-line"><tr><th className="th">Minuto</th><th className="th">Fala detetada</th><th className="th">Desvio medido</th><th className="th">Sinal</th><th className="th">Conta</th></tr></thead>
                  <tbody>{audioRes.windows.map((w, i) => (
                    <tr key={i} className="border-b border-line/50 last:border-0 text-xs"><td className="px-2 py-1.5 tabular-nums text-mute">{Math.round(w.t / 60)}</td>
                      <td className="px-2 py-1.5 tabular-nums text-dim">{w.speech_pct != null ? `${w.speech_pct} %` : "—"}</td>
                      <td className="px-2 py-1.5 tabular-nums">{w.offset == null ? "—" : `${w.offset > 0 ? "+" : ""}${w.offset.toFixed(2)} s`}</td>
                      <td className="px-2 py-1.5 tabular-nums text-dim">{w.z ? w.z.toFixed(1) : "—"}</td><td className="px-2 py-1.5">{w.used ? <CheckCircle2 size={13} className="text-ok" /> : <span className="text-dim">não</span>}</td></tr>))}</tbody></table></div>
              </div>
            )}
          </div>
        )}
        {tool === "text" && (
          <div className="space-y-2.5">
            <div className="text-sm text-mute flex gap-2"><Wand2 size={16} className="shrink-0 mt-0.5" /><div>Limpa o <b className="text-fg">texto</b> das falas. <b className="text-fg">Os tempos não mudam.</b> Escolha o que aplicar e confira na pré-visualização.</div></div>
            {([
              ["hi", "Remover marcas para surdos (HI)", "[ruído], (música) no início/fim, «NOME:» em maiúsculas e linhas só com ♪. Falas que ficam vazias são removidas."],
              ["style", "Remover tags de estilo", "<i>, <b>, <u>, <font> e códigos {\\an8}. Deixa de haver itálicos."],
              ["common", "Correções comuns", "Espaços duplos, espaço antes de pontuação, falta de espaço depois dela, travessões, reticências, e remove anúncios/marcas de água («Legendas por…», sites) nas primeiras e últimas falas."],
              ["uppercase", "Maiúsculas → frase normal", "Só falas inteiramente em MAIÚSCULAS. Perde a capitalização de nomes próprios (heurístico)."],
            ] as const).map(([k, t, d]) => (
              <label key={k} className="flex items-start gap-3 cursor-pointer card bg-s2 p-3"><input type="checkbox" className="mt-1 accent-[rgb(229,160,13)]" checked={!!params[k]} onChange={(e) => set(k, e.target.checked)} /><span className="text-sm">{t}<span className="block text-xs text-mute">{d}</span></span></label>
            ))}
          </div>
        )}
        {tool === "convert" && (
          <div className="space-y-2.5">
            <div className="text-sm text-mute flex gap-2"><FileCog size={16} className="shrink-0 mt-0.5" /><div>Converte para <b className="text-fg">SRT em UTF-8 sem BOM</b>, o formato que a Smart TV, o Android TV e o Plex leem melhor. <b className="text-fg">Os tempos não mudam.</b> ASS/SSA/VTT passam a um novo ficheiro .srt (nunca substitui um existente); um SRT em cp1252/UTF-16 ou com BOM é reescrito no mesmo ficheiro.</div></div>
            {([
              ["fix_mojibake", "Reparar acentos partidos", "«Ã£», «Ã©», «Ã§»… (UTF-8 gravado como cp1252). Só altera as linhas em que o resultado é UTF-8 válido."],
              ["keep_original", "Manter também o original", "Por omissão o ASS/VTT vai para backup (restaurável) para o Plex não mostrar duas legendas iguais. Só se aplica a ASS/SSA/VTT."],
            ] as const).map(([k, t, d]) => (
              <label key={k} className="flex items-start gap-3 cursor-pointer card bg-s2 p-3"><input type="checkbox" className="mt-1 accent-[rgb(229,160,13)]" checked={!!params[k]} onChange={(e) => set(k, e.target.checked)} /><span className="text-sm">{t}<span className="block text-xs text-mute">{d}</span></span></label>
            ))}
          </div>
        )}
        {tool === "delete" && (
          <div className="space-y-2.5 text-sm">
            <div className="text-mute">A legenda é <b className="text-fg">retirada da pasta</b> do filme e guardada em <code className="text-fg">/data/backups</code>. Pode <b className="text-fg">restaurá-la</b> a qualquer momento no histórico de alterações do filme. Vídeos nunca são tocados.</div>
            {confirmDel && <div className="text-warn flex gap-2"><AlertTriangle size={16} className="shrink-0 mt-0.5" />Vai mover <b>{sub.name}</b> para backup. Confirme no botão abaixo.</div>}
          </div>
        )}

        {(tool === "text" || tool === "durations" || tool === "quality" || tool === "ptpt") && pv?.kind === "text" && (
          <div className="space-y-3">
            <div className="card bg-s2 p-3.5 space-y-2">
              <div className="flex items-center gap-2 text-sm font-medium"><Wand2 size={15} className="text-accent" />{pv.summary}</div>
              {pv.warnings.map((w, i) => <div key={i} className="text-xs text-warn flex gap-1.5"><AlertTriangle size={13} className="shrink-0 mt-0.5" />{w}</div>)}
              <div className="text-xs text-dim">Mostra as primeiras {pv.changes?.length} alterações. {tool === "durations" ? "Só o fim das falas curtas é alongado: início, texto e ordem ficam iguais (verificado antes de gravar)." : tool === "quality" ? "Só mudam as quebras de linha: texto e tempos ficam iguais (verificado antes de gravar)." : "Os tempos e a ordem das falas ficam iguais (verificado antes de gravar)."}</div>
            </div>
            <div className="card overflow-hidden"><table className="w-full"><thead className="border-b border-line"><tr><th className="th">#</th><th className="th">Antes</th><th className="th">Depois</th></tr></thead>
              <tbody>{pv.changes?.map((c) => (
                <tr key={c.n} className="border-b border-line/50 last:border-0 text-xs align-top">
                  <td className="px-2 py-1.5 text-dim tabular-nums">{c.n}</td><td className="px-2 py-1.5 text-mute break-words max-w-[240px]">{c.before}</td>
                  <td className="px-2 py-1.5 break-words max-w-[240px]">{c.after}<div className="flex gap-1 mt-1 flex-wrap">{c.tags.map((t) => <Badge key={t} tone="neutral">{t}</Badge>)}</div></td></tr>))}</tbody></table></div>
            <div className="text-xs text-dim flex gap-1.5"><CheckCircle2 size={13} className="shrink-0 mt-0.5 text-ok" />O original fica em backup e pode ser restaurado.</div>
          </div>
        )}
        {tool === "convert" && pv?.kind === "convert" && (
          <div className="space-y-3">
            <div className="card bg-s2 p-3.5 space-y-2">
              <div className="flex items-center gap-2 text-sm font-medium break-all"><FileCog size={15} className="text-accent shrink-0" />{pv.summary}</div>
              <div className="text-xs text-mute">{pv.cues} falas · codificação original {pv.encoding}{pv.bom ? " com BOM" : ""}. Tempos e ordem verificados contra o original antes de gravar.</div>
              {pv.warnings.map((w, i) => <div key={i} className="text-xs text-warn flex gap-1.5"><AlertTriangle size={13} className="shrink-0 mt-0.5" />{w}</div>)}
            </div>
            {pv.samples && pv.samples.length > 0 && (
              <div className="card overflow-hidden"><table className="w-full"><thead className="border-b border-line"><tr><th className="th">#</th><th className="th">Tempo</th><th className="th">Texto no SRT</th></tr></thead>
                <tbody>{pv.samples.map((c) => (
                  <tr key={c.n} className="border-b border-line/50 last:border-0 text-xs align-top"><td className="px-2 py-1.5 text-dim tabular-nums">{c.n}</td>
                    <td className="px-2 py-1.5 font-mono text-mute whitespace-nowrap">{fmtTs(c.start)}</td><td className="px-2 py-1.5 break-words whitespace-pre-wrap">{c.text}</td></tr>))}</tbody></table></div>
            )}
            <div className="text-xs text-dim flex gap-1.5"><CheckCircle2 size={13} className="shrink-0 mt-0.5 text-ok" />O original fica em backup e pode ser restaurado (remove o .srt criado e repõe o ficheiro).</div>
          </div>
        )}
        {tool !== "delete" && tool !== "text" && tool !== "durations" && tool !== "quality" && tool !== "ptpt" && tool !== "convert" && pv && (
          <div className="space-y-3">
            <div className="card bg-s2 p-3.5 space-y-2">
              <div className="flex items-center gap-2 text-sm font-medium"><Clock size={15} className="text-accent" />{pv.summary}</div>
              {pv.estimate && <div className="text-xs text-mute">Offset estimado {((pv.estimate.offset_ms ?? 0) / 1000).toFixed(2)} s · {Math.round((pv.estimate.match_ratio ?? 0) * 100)} % dos {pv.estimate.compared} eventos coincidem{pv.estimate.drift_ms ? ` · deriva ${(pv.estimate.drift_ms / 1000).toFixed(1)} s` : ""}</div>}
              <div className="text-xs text-mute">Primeira fala {fmtTs(pv.first_start_before)} → <b className="text-fg">{fmtTs(pv.first_start_after)}</b> · última termina {fmtTs(pv.last_end_before)} → <b className="text-fg">{fmtTs(pv.last_end_after)}</b> · {pv.total} falas · codificação {pv.encoding}</div>
              {pv.duration_check && <div className="text-xs text-dim">{pv.duration_check}. Isto não prova sincronização: confirme a ver o filme.</div>}
              {pv.warnings.map((w, i) => <div key={i} className="text-xs text-warn flex gap-1.5"><AlertTriangle size={13} className="shrink-0 mt-0.5" />{w}</div>)}
            </div>
            <div className="card overflow-hidden"><table className="w-full"><thead className="border-b border-line"><tr><th className="th">#</th><th className="th">Tempo de início: antes → depois</th><th className="th">Texto</th></tr></thead>
              <tbody>{pv.head.map(row)}{pv.tail.length > 0 && <tr><td colSpan={3} className="text-center text-dim text-xs py-1">⋯</td></tr>}{pv.tail.map(row)}</tbody></table></div>
            <div className="text-xs text-dim flex gap-1.5"><CheckCircle2 size={13} className="shrink-0 mt-0.5 text-ok" />Só os tempos mudam: o texto, os itálicos e as posições ficam iguais. O original fica em backup e pode ser restaurado.</div>
          </div>
        )}
      </div>
    </Modal>
  );
}

interface Edit { id: number; name: string; action: string; created_at: string; restored_at: string | null; backup_exists: boolean; params: Record<string, any> }
const ACTION: Record<string, string> = { textfix: "Limpeza de texto", durations: "Durações das falas", reflow: "Linhas reformatadas", ptpt: "Adaptação pt-BR → pt-PT", convert: "Conversão para SRT/UTF-8", shift: "Atrasar/adiantar", fps: "Frame rate", twopoint: "Dois pontos", reference: "Sincronizar com referência", delete: "Movida para backup", linear: "Alinhada com o áudio", bazarr_overwrite: "Substituída pelo Bazarr" };

export function EditHistory({ mediaId }: { mediaId: number }) {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ["sub-edits", mediaId], queryFn: () => api.get<Edit[]>(`/subtitle-edits?media_id=${mediaId}`) });
  const restore = useMutation({
    mutationFn: ({ id, force }: { id: number; force?: boolean }) => api.post(`/subtitle-edits/${id}/restore`, { force: !!force }),
    onSuccess: () => { toast("ok", "Restaurado."); qc.invalidateQueries(); },
    onError: (e: Error, v) => {
      if (/foi alterado/.test(e.message) && !v.force && window.confirm(e.message + "\n\nRestaurar mesmo assim (sobrepõe as alterações)?")) restore.mutate({ id: v.id, force: true });
      else toast("bad", e.message);
    },
  });
  if (!q.data?.length) return null;
  return (
    <div className="space-y-2">
      <div className="label">Alterações a legendas (com cópia de segurança)</div>
      {q.data.map((e) => (
        <div key={e.id} className="card p-3 flex items-center gap-3 text-sm">
          <div className="min-w-0 flex-1"><div className="truncate font-mono text-xs">{e.name}</div>
            <div className="text-xs text-mute">{ACTION[e.action] ?? e.action} · {new Date(e.created_at).toLocaleString("pt-PT", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</div></div>
          {e.restored_at ? <Badge tone="neutral">Restaurada</Badge> : !e.backup_exists ? <Badge tone="bad">Backup em falta</Badge>
            : <button className="btn-ghost !h-8" disabled={restore.isPending} onClick={() => restore.mutate({ id: e.id })}><RotateCcw size={13} />Restaurar</button>}
        </div>
      ))}
    </div>
  );
}
