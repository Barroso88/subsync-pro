import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, CheckCircle2, Clock, Film, FileCog, GitCompare, Loader2, Move, RotateCcw, Ruler, Trash2, Wand2 } from "lucide-react";
import { api } from "../api";
import { cx, fmtTs } from "../lib";
import { Badge, Modal, Skeleton, Tabs, useToast } from "./ui";

type Tool = "shift" | "fps" | "twopoint" | "reference" | "text" | "convert" | "delete";
interface Cue { n: number; start: number; end: number; text: string }
interface CuesRes { total: number; head: Cue[]; tail: Cue[]; references: { id: number; name: string; lang: string | null; cues: number }[] }
interface PRow { n: number; old_start: number; new_start: number; new_end: number; text: string }
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
  const [params, setParams] = useState<Record<string, any>>({ seconds: 0, from_fps: 23.976, to_fps: 25, mode: "offset", hi: true, style: false, common: true, uppercase: false, fix_mojibake: true, keep_original: false });
  const [pv, setPv] = useState<Preview | null>(null);
  const [confirmDel, setConfirmDel] = useState(false);
  useEffect(() => { setPv(null); setConfirmDel(false); }, [tool, sub?.id]);
  useEffect(() => { if (sub) { setTool(sub.isSrt ? "shift" : "convert"); setParams({ seconds: 0, from_fps: 23.976, to_fps: 25, mode: "offset", hi: true, style: false, common: true, uppercase: false, fix_mojibake: true, keep_original: false }); setPv(null); } }, [sub?.id]);

  const cues = useQuery({ queryKey: ["sub-cues", sub?.id], queryFn: () => api.get<CuesRes>(`/subtitles/${sub!.id}/cues`), enabled: !!sub && sub.isSrt });
  const body = () => ({ action: tool === "text" ? "textfix" : tool, params: tool === "text" ? { hi: !!params.hi, style: !!params.style, common: !!params.common, uppercase: !!params.uppercase }
    : tool === "convert" ? { fix_mojibake: !!params.fix_mojibake, keep_original: !!params.keep_original } : params });
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
  const editable = sub.canEdit && (tool === "convert" ? sub.isText : sub.isSrt);

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
        <Tabs value={tool} onChange={setTool} items={[{ id: "shift", label: "Atrasar/adiantar" }, { id: "fps", label: "Frame rate" }, { id: "twopoint", label: "Dois pontos" }, { id: "reference", label: "Sincronizar com referência" }, { id: "text", label: "Limpar texto" }, { id: "convert", label: "Converter" }, { id: "delete", label: "Apagar" }]} />

        {tool === "shift" && (
          <div className="space-y-2.5">
            <div className="text-sm text-mute flex gap-2"><Move size={16} className="shrink-0 mt-0.5" />Desloca todas as falas. <b className="text-fg">Positivo atrasa</b> (a legenda aparece mais tarde), <b className="text-fg">negativo adianta</b>.</div>
            <div className="flex items-center gap-2 flex-wrap">
              <input className="input !w-32" type="number" step="0.1" value={params.seconds} onChange={(e) => set("seconds", Number(e.target.value))} aria-label="Segundos" /><span className="text-sm text-mute">segundos</span>
              {[-5, -1, -0.5, 0.5, 1, 5].map((v) => <button key={v} className="btn-ghost !h-8" onClick={() => set("seconds", Math.round(((Number(params.seconds) || 0) + v) * 100) / 100)}>{v > 0 ? "+" : ""}{v}</button>)}
            </div>
          </div>
        )}
        {tool === "fps" && (
          <div className="space-y-2.5">
            <div className="text-sm text-mute flex gap-2"><Film size={16} className="shrink-0 mt-0.5" />Corrige uma legenda feita para outro frame rate (a fala vai ficando cada vez mais adiantada ou atrasada ao longo do filme). Multiplica os tempos por <i>origem ÷ destino</i>.</div>
            <div className="flex items-center gap-2 flex-wrap text-sm">
              <span>De</span><select className="input !w-28" value={params.from_fps} onChange={(e) => set("from_fps", Number(e.target.value))}>{FPS.map((f) => <option key={f} value={f}>{f}</option>)}</select>
              <span>para</span><select className="input !w-28" value={params.to_fps} onChange={(e) => set("to_fps", Number(e.target.value))}>{FPS.map((f) => <option key={f} value={f}>{f}</option>)}</select><span className="text-mute">fps</span>
            </div>
          </div>
        )}
        {tool === "twopoint" && (
          <div className="space-y-3">
            <div className="text-sm text-mute flex gap-2"><Ruler size={16} className="shrink-0 mt-0.5" />Escolha <b className="text-fg">duas falas</b> (uma perto do início, outra perto do fim) e diga <b className="text-fg">quando devem aparecer no vídeo</b>. A app calcula o deslocamento e a escala.</div>
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
            <div className="text-sm text-mute flex gap-2"><GitCompare size={16} className="shrink-0 mt-0.5" />Alinha esta legenda com <b className="text-fg">outra legenda já existente</b> do mesmo filme (por exemplo, a que sabe estar certa). <b className="text-fg">Não usa o áudio</b>: assume que a referência está sincronizada.</div>
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
        {tool === "text" && (
          <div className="space-y-2.5">
            <div className="text-sm text-mute flex gap-2"><Wand2 size={16} className="shrink-0 mt-0.5" />Limpa o <b className="text-fg">texto</b> das falas. <b className="text-fg">Os tempos não mudam.</b> Escolha o que aplicar e confira na pré-visualização.</div>
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
            <div className="text-sm text-mute flex gap-2"><FileCog size={16} className="shrink-0 mt-0.5" />Converte para <b className="text-fg">SRT em UTF-8 sem BOM</b>, o formato que a Smart TV, o Android TV e o Plex leem melhor. <b className="text-fg">Os tempos não mudam.</b> ASS/SSA/VTT passam a um novo ficheiro .srt (nunca substitui um existente); um SRT em cp1252/UTF-16 ou com BOM é reescrito no mesmo ficheiro.</div>
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

        {tool === "text" && pv?.kind === "text" && (
          <div className="space-y-3">
            <div className="card bg-s2 p-3.5 space-y-2">
              <div className="flex items-center gap-2 text-sm font-medium"><Wand2 size={15} className="text-accent" />{pv.summary}</div>
              {pv.warnings.map((w, i) => <div key={i} className="text-xs text-warn flex gap-1.5"><AlertTriangle size={13} className="shrink-0 mt-0.5" />{w}</div>)}
              <div className="text-xs text-dim">Mostra as primeiras {pv.changes?.length} alterações. Os tempos e a ordem das falas ficam iguais (verificado antes de gravar).</div>
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
        {tool !== "delete" && tool !== "text" && tool !== "convert" && pv && (
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
const ACTION: Record<string, string> = { textfix: "Limpeza de texto", convert: "Conversão para SRT/UTF-8", shift: "Atrasar/adiantar", fps: "Frame rate", twopoint: "Dois pontos", reference: "Sincronizar com referência", delete: "Movida para backup", bazarr_overwrite: "Substituída pelo Bazarr" };

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
