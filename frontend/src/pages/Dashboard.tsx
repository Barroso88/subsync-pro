import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AlertTriangle, Clapperboard, Download, FileVideo, FolderPlus, Languages, ListVideo, ScanSearch, Subtitles, Tv, Loader2 } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { fmtDate } from "../lib";
import { Badge, Empty, ErrorBox, PageHeader, Skeleton } from "../components/ui";
import { useStartJob } from "../hooks";

interface D {
  by_variant: Record<string, { assumed: number; movies: number; episodes: number; total: number; pct: number }>;
  totals: Record<string, number>; coverage_by_language: { code: string; name: string; priority: string; have: number; total: number; pct: number }[];
  downloads_per_day: { day: string; n: number }[]; recent: { id: number; ts: string; level: string; kind: string; message: string }[]; last_scan_at: string | null;
}

function Stat({ icon: I, label, value, tone, to, hint }: { icon: any; label: string; value: number | string; tone?: string; to?: string; hint?: string }) {
  const body = (
    <div className="card p-4 h-full hover:border-mute/40 transition-colors animate-rise">
      <div className="flex items-center gap-2 text-mute text-xs"><I size={15} className={tone} />{label}</div>
      <div className="text-2xl font-semibold tabular-nums mt-2 tracking-tight">{typeof value === "number" ? value.toLocaleString("pt-PT") : value}</div>
      {hint && <div className="text-[11px] text-dim mt-1">{hint}</div>}
    </div>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

export default function Dashboard() {
  const q = useQuery({ queryKey: ["dashboard"], queryFn: () => api.get<D>("/dashboard"), refetchInterval: 15000 });
  const libs = useQuery({ queryKey: ["libraries"], queryFn: () => api.get<any[]>("/libraries") });
  const scan = useStartJob();
  if (q.isLoading) return <div className="space-y-4"><Skeleton className="h-8 w-48" /><div className="grid grid-cols-2 lg:grid-cols-4 gap-3">{Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-24" />)}</div><Skeleton className="h-64" /></div>;
  if (q.isError) return <ErrorBox error={q.error} retry={() => q.refetch()} />;
  const d = q.data!, t = d.totals;
  if (libs.data && libs.data.length === 0)
    return (<><PageHeader title="Dashboard" /><Empty icon={<FolderPlus size={22} />} title="Bem-vindo ao SubSync Pro" hint="Comece por registar as pastas de filmes e séries. A aplicação só lê os vídeos; escreve apenas legendas e apenas nas pastas que autorizar."
      action={<Link to="/definicoes" className="btn-primary">Adicionar biblioteca</Link>} /></>);
  const days = Array.from({ length: 30 }, (_, i) => { const x = new Date(); x.setDate(x.getDate() - (29 - i)); const k = x.toISOString().slice(0, 10); return { day: k.slice(5), n: d.downloads_per_day.find((p) => p.day === k)?.n ?? 0 }; });
  return (
    <>
      <PageHeader title="Dashboard" subtitle={`Última análise: ${fmtDate(d.last_scan_at)}`}
        actions={<button className="btn-ghost" disabled={scan.isPending} onClick={() => scan.mutate({ url: "/scan", body: {} })}>{scan.isPending ? <Loader2 size={15} className="animate-spin" /> : <ScanSearch size={15} />}Analisar bibliotecas</button>} />
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <Stat icon={Clapperboard} label="Filmes" value={t.movies} to="/filmes" />
        <Stat icon={Tv} label="Séries" value={t.series} to="/series" />
        <Stat icon={ListVideo} label="Episódios" value={t.episodes} to="/episodios" />
        <Stat icon={FileVideo} label="Ficheiros analisados" value={t.files} />
        <Stat icon={Subtitles} label="Com legendas" value={t.with_subtitles} tone="text-ok" hint="Qualquer idioma, externa ou embutida" />
        <Stat icon={Languages} label="Com legenda pt-PT" value={d.by_variant["pt-PT"].total} tone="text-ok" hint={`${d.by_variant["pt-PT"].pct}% · ${d.by_variant["pt-PT"].movies.toLocaleString("pt-PT")} filmes · ${d.by_variant["pt-PT"].episodes.toLocaleString("pt-PT")} episódios${d.by_variant["pt-PT"].assumed ? ` · ${d.by_variant["pt-PT"].assumed.toLocaleString("pt-PT")} por assunção (≈), ${(d.by_variant["pt-PT"].total - d.by_variant["pt-PT"].assumed).toLocaleString("pt-PT")} identificadas` : ""}`} />
        <Stat icon={Languages} label="Com legenda pt-BR" value={d.by_variant["pt-BR"].total} tone="text-info" hint={`${d.by_variant["pt-BR"].pct}% · ${d.by_variant["pt-BR"].movies.toLocaleString("pt-PT")} filmes · ${d.by_variant["pt-BR"].episodes.toLocaleString("pt-PT")} episódios${d.by_variant["pt-BR"].assumed ? ` · ${d.by_variant["pt-BR"].assumed.toLocaleString("pt-PT")} por assunção (≈), ${(d.by_variant["pt-BR"].total - d.by_variant["pt-BR"].assumed).toLocaleString("pt-PT")} identificadas` : ""}`} />
        <Stat icon={AlertTriangle} label="Sem legenda no idioma preferido" value={t.missing_preferred} tone="text-bad" to="/em-falta" />
        <Stat icon={AlertTriangle} label="Possível dessincronização" value={t.suspect} tone="text-warn" hint={`${t.unverified} por verificar`} to="/episodios" />
        <Stat icon={Download} label="Downloads efetuados" value={t.downloads} tone="text-accent" to="/downloads" />
      </div>
      <div className="grid lg:grid-cols-5 gap-4 mt-4">
        <div className="card p-5 lg:col-span-3">
          <div className="font-medium text-sm mb-1">Cobertura por idioma</div><div className="text-xs text-dim mb-4">% de ficheiros com legenda (externa ou, se ativado, embutida) em cada idioma, incluindo os cobertos por assunção (≈pt, ≈pt-BR) definida nas Definições</div>
          {d.coverage_by_language.length === 0 ? <Empty title="Sem idiomas configurados" /> : (
            <div className="space-y-3">
              {d.coverage_by_language.map((l) => (
                <div key={l.code}>
                  <div className="flex items-center justify-between text-[13px] mb-1.5"><span className="flex items-center gap-2">{l.name}<Badge tone={l.priority === "primary" ? "accent" : "neutral"}>{l.code}</Badge></span><span className="tabular-nums text-mute">{l.have}/{l.total} · {l.pct}%</span></div>
                  <div className="h-2 rounded-full bg-s3 overflow-hidden"><div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${l.pct}%` }} /></div>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="card p-5 lg:col-span-2">
          <div className="font-medium text-sm mb-1">Downloads · últimos 30 dias</div><div className="text-xs text-dim mb-3">Legendas instaladas por dia</div>
          {t.downloads === 0 ? <div className="h-40 grid place-items-center text-sm text-dim">Ainda sem downloads</div> : (
            <ResponsiveContainer width="100%" height={170}>
              <AreaChart data={days} margin={{ left: -24, right: 4, top: 4 }}>
                <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="rgb(229,160,13)" stopOpacity={0.35} /><stop offset="100%" stopColor="rgb(229,160,13)" stopOpacity={0} /></linearGradient></defs>
                <CartesianGrid stroke="rgb(42,46,55)" vertical={false} />
                <XAxis dataKey="day" tick={{ fill: "#626976", fontSize: 10 }} tickLine={false} axisLine={false} interval={4} />
                <YAxis allowDecimals={false} tick={{ fill: "#626976", fontSize: 10 }} tickLine={false} axisLine={false} />
                <Tooltip contentStyle={{ background: "#1b1e24", border: "1px solid #2a2e37", borderRadius: 8, fontSize: 12 }} labelStyle={{ color: "#8b93a1" }} />
                <Area type="monotone" dataKey="n" name="Instaladas" stroke="rgb(229,160,13)" strokeWidth={2} fill="url(#g)" />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
      <div className="grid lg:grid-cols-5 gap-4 mt-4">
        <div className="card p-5 lg:col-span-5">
          <div className="font-medium text-sm mb-3">Atividade recente</div>
          {d.recent.length === 0 ? <div className="text-sm text-dim py-6 text-center">Sem atividade registada</div> : (
            <ul className="divide-y divide-line/60">
              {d.recent.map((e) => (
                <li key={e.id} className="py-2 flex items-start gap-3 text-[13px]">
                  <span className={"w-1.5 h-1.5 rounded-full mt-1.5 shrink-0 " + (e.level === "error" ? "bg-bad" : e.level === "warning" ? "bg-warn" : "bg-ok")} />
                  <span className="flex-1 min-w-0 break-words">{e.message}</span><span className="text-xs text-dim shrink-0">{fmtDate(e.ts)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </>
  );
}
