import { Badge } from "./ui";
import { cx } from "../lib";
import { useLangFilter } from "../hooks";
import { Evaluation, Item, Meta } from "../api";
import { Star, Check, FileText, Layers, ShieldCheck, TriangleAlert, HelpCircle, Ban, Hash } from "lucide-react";

export function LangChips({ item, wanted }: { item: Pick<Item, "covered" | "missing"> & { fallback?: Record<string, string> }; wanted?: string[] }) {
  const lf = useLangFilter();
  const all = Object.entries(item.covered);
  const have = all.filter(([l]) => lf.visible(l) || !!item.fallback?.[l]);
  const hidden = all.filter(([l]) => !have.some(([h]) => h === l)).map(([l]) => l);
  return (
    <div className="flex flex-wrap gap-1">
      {have.map(([l, src]) => (
        <Badge key={l} tone={item.fallback?.[l] ? "warn" : "ok"} title={item.fallback?.[l] ? `Coberto por ${item.fallback[l]} (fallback ativo)` : src === "embedded" ? "Legenda embutida no vídeo" : "Ficheiro externo"}>
          {src === "embedded" ? <Layers size={10} /> : <FileText size={10} />}{l}{item.fallback?.[l] && <span className="opacity-70">≈{item.fallback[l]}</span>}
        </Badge>
      ))}
      {item.missing.map((l) => <Badge key={l} tone="bad" title="Em falta"><span className="opacity-70">−</span>{l}</Badge>)}
      {hidden.length > 0 && <Badge tone="neutral" title={`Escondidas (outros idiomas): ${hidden.join(", ")}`}>+{hidden.length}</Badge>}
      {!all.length && !item.missing.length && <span className="text-dim text-xs">—</span>}
    </div>
  );
}

export function CompatBadge({ ev }: { ev: Evaluation }) {
  const m = {
    user: { tone: "accent" as const, icon: <Check size={11} /> },
    hash: { tone: "ok" as const, icon: <Hash size={11} /> },
    probable: { tone: "info" as const, icon: <Check size={11} /> },
    weak: { tone: "warn" as const, icon: <HelpCircle size={11} /> },
    incompatible: { tone: "bad" as const, icon: <Ban size={11} /> },
  }[ev.compat];
  return <Badge tone={m.tone}>{m.icon}{ev.compat_label}</Badge>;
}

export function SyncBadge({ ev }: { ev: Evaluation }) {
  const m = {
    validated_reference: { tone: "ok" as const, icon: <ShieldCheck size={11} /> },
    not_confirmed: { tone: "neutral" as const, icon: <HelpCircle size={11} /> },
    suspect: { tone: "bad" as const, icon: <TriangleAlert size={11} /> },
    inconclusive: { tone: "warn" as const, icon: <HelpCircle size={11} /> },
  }[ev.sync];
  return <Badge tone={m.tone}>{m.icon}{ev.sync_label}</Badge>;
}

export function ScoreBar({ score }: { score: number }) {
  return (
    <div className="flex items-center gap-2" title="Pontuação heurística de ordenação (não é uma probabilidade)">
      <div className="w-14 h-1.5 rounded-full bg-s3 overflow-hidden"><div className="h-full bg-accent/80" style={{ width: `${score}%` }} /></div>
      <span className="text-xs tabular-nums text-mute w-6">{score}</span>
    </div>
  );
}

export function Factors({ ev }: { ev: Evaluation }) {
  return (
    <div className="space-y-1.5">
      {ev.factors.map((f, i) => (
        <div key={i} className="flex items-start gap-2 text-[13px]">
          <span className={"tabular-nums w-9 text-right shrink-0 " + (f.points > 0 ? "text-ok" : f.points < 0 ? "text-bad" : "text-dim")}>{f.points > 0 ? "+" : ""}{f.points}</span>
          <span className="text-mute">{f.label}</span>
        </div>
      ))}
      {ev.notes.map((n, i) => <div key={"n" + i} className="text-xs text-mute pl-11">ℹ {n}</div>)}
      <div className="text-[11px] text-dim pl-11 pt-1">{ev.disclaimer}</div>
    </div>
  );
}

export const langName = (meta: Meta | undefined, c: string | null) => (!c ? "Desconhecido" : meta?.languages.find((l) => l.code === c)?.name ?? c);

/** Nota IMDb (vinda do Radarr) em etiqueta; não mostra nada se o filme não tiver nota. */
export function RatingBadge({ rating, votes, className }: { rating?: number | null; votes?: number | null; className?: string }) {
  if (!rating) return null;
  return (
    <span className={cx("inline-flex items-center gap-1 rounded-md bg-black/75 backdrop-blur px-1.5 h-5 text-[11px] font-semibold text-white tabular-nums", className)}
      title={`Nota IMDb ${rating.toFixed(1)}/10${votes ? ` · ${votes.toLocaleString("pt-PT")} votos` : ""}`}>
      <Star size={10} className="text-[#f5c518] fill-[#f5c518]" />{rating.toFixed(1)}
    </span>
  );
}
