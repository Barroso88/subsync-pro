import { ReactNode, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Activity, AlertCircle, Clapperboard, Download, Gauge, Menu, PlugZap, Search, Settings, Tv, ListVideo, X, Subtitles } from "lucide-react";
import { api } from "../api";
import { cx } from "../lib";
import { useJobWatcher, useMeta, useSidebar } from "../hooks";

const NAV: { to: string; label: string; icon: any; badge?: "missing" | "active_jobs" | "pending_downloads" }[] = [
  { to: "/", label: "Dashboard", icon: Gauge },
  { to: "/filmes", label: "Filmes", icon: Clapperboard },
  { to: "/series", label: "Séries", icon: Tv },
  { to: "/episodios", label: "Episódios", icon: ListVideo },
  { to: "/em-falta", label: "Legendas em falta", icon: AlertCircle, badge: "missing" },
  { to: "/procurar", label: "Procurar legendas", icon: Search },
  { to: "/downloads", label: "Histórico de downloads", icon: Download, badge: "pending_downloads" },
  { to: "/tarefas", label: "Tarefas e atividade", icon: Activity, badge: "active_jobs" },
  { to: "/fornecedores", label: "Fornecedores", icon: PlugZap },
  { to: "/definicoes", label: "Definições", icon: Settings },
];

export function Layout() {
  const [open, setOpen] = useState(false);
  const sb = useSidebar();
  const meta = useMeta();
  useJobWatcher();
  const loc = useLocation();
  const missingLangs = Object.entries(sb.data?.missing_by_lang ?? {});
  const nav = (
    <nav className="flex flex-col gap-0.5 px-3 py-2" aria-label="Principal">
      {NAV.map((n) => {
        const count = n.badge ? sb.data?.[n.badge] : 0;
        return (
          <div key={n.to}>
          <NavLink to={n.to} end={n.to === "/" || n.to === "/em-falta"} onClick={() => setOpen(false)}
            className={({ isActive }) => cx("flex items-center gap-3 h-9 px-3 rounded-lg text-[13.5px] transition-colors", isActive ? "bg-s3 text-fg" : "text-mute hover:text-fg hover:bg-s2")}>
            {({ isActive }) => (<>
              <n.icon size={17} className={isActive ? "text-accent" : ""} />
              <span className="flex-1 truncate">{n.label}</span>
              {!!count && <span className={cx("min-w-5 h-5 px-1.5 rounded-full text-[11px] font-semibold grid place-items-center tabular-nums", n.badge === "missing" ? "bg-bad/20 text-bad" : "bg-accent/20 text-accent")}>{count > 999 ? "999+" : count}</span>}
            </>)}
          </NavLink>
          {n.to === "/em-falta" && missingLangs.map(([code, c]) => {
            const active = loc.pathname === "/em-falta" && new URLSearchParams(loc.search).get("lang") === code;
            return (
              <NavLink key={code} to={`/em-falta?lang=${code}`} onClick={() => setOpen(false)}
                className={cx("flex items-center gap-2.5 h-8 pl-11 pr-3 rounded-lg text-[13px] transition-colors", active ? "bg-s3 text-fg" : "text-mute hover:text-fg hover:bg-s2")}>
                <span className="flex-1 truncate">Em falta · {code}</span>
                <span className={cx("min-w-5 h-5 px-1.5 rounded-full text-[11px] font-semibold grid place-items-center tabular-nums", c ? "bg-bad/15 text-bad" : "bg-ok/15 text-ok")}>{c > 999 ? "999+" : c}</span>
              </NavLink>
            );
          })}
          </div>
        );
      })}
    </nav>
  );
  const brand = (
    <div className="flex items-center gap-2.5 px-5 h-14 shrink-0">
      <div className="w-7 h-7 rounded-lg bg-accent grid place-items-center text-black"><Subtitles size={16} strokeWidth={2.4} /></div>
      <div className="font-semibold tracking-tight">SubSync <span className="text-accent">Pro</span></div>
    </div>
  );
  return (
    <div className="h-full flex">
      <aside className="hidden lg:flex w-[248px] shrink-0 flex-col border-r border-line bg-s1/60">
        {brand}{nav}
        <div className="mt-auto p-4 text-[11px] text-dim">v{meta.data?.version}{meta.data && !meta.data.ffprobe && <div className="text-warn mt-1">FFprobe indisponível</div>}</div>
      </aside>
      {open && (
        <div className="lg:hidden fixed inset-0 z-40 animate-fade">
          <div className="absolute inset-0 bg-black/60" onClick={() => setOpen(false)} />
          <aside className="absolute left-0 top-0 h-full w-[272px] bg-bg border-r border-line flex flex-col animate-slide">
            <div className="flex items-center justify-between pr-3">{brand}<button className="btn-quiet !w-8 !p-0" onClick={() => setOpen(false)} aria-label="Fechar menu"><X size={18} /></button></div>
            {nav}
          </aside>
        </div>
      )}
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="lg:hidden h-14 shrink-0 border-b border-line flex items-center gap-3 px-4 bg-s1/60">
          <button className="btn-quiet !w-9 !p-0" onClick={() => setOpen(true)} aria-label="Abrir menu"><Menu size={19} /></button>
          <div className="font-semibold">SubSync <span className="text-accent">Pro</span></div>
          {!!sb.data?.active_jobs && <span className="ml-auto text-xs text-accent flex items-center gap-1.5"><Activity size={14} className="animate-pulse" />{sb.data.active_jobs}</span>}
        </header>
        <main className="flex-1 overflow-y-auto"><div className="max-w-[1400px] mx-auto px-4 sm:px-6 lg:px-8 py-6 pb-24"><Outlet /></div></main>
      </div>
    </div>
  );
}
