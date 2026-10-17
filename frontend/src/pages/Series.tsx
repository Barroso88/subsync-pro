import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Tv } from "lucide-react";
import { api, Item } from "../api";
import { cx, epCode } from "../lib";
import { Badge, Empty, ErrorBox, PageHeader, Skeleton } from "../components/ui";
import { LangChips } from "../components/subs";
import { SearchBox, useDebounced } from "../components/MediaBrowser";
import { ItemDrawer } from "../components/ItemDrawer";

interface S { title: string; year: number | null; episodes: number; missing: number; seasons: number; poster_id: number | null }

export default function Series() {
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [open, setOpen] = useState<string | null>(null);
  const [item, setItem] = useState<number | null>(null);
  const list = useQuery({ queryKey: ["series", dq], queryFn: () => api.get<S[]>("/series" + (dq ? `?q=${encodeURIComponent(dq)}` : "")) });
  return (
    <>
      <PageHeader title="Séries" subtitle="Expanda uma série para ver temporadas e episódios." actions={<SearchBox value={q} onChange={setQ} placeholder="Pesquisar série…" />} />
      {list.isError && <ErrorBox error={list.error} retry={() => list.refetch()} />}
      {list.isLoading && <div className="space-y-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-16" />)}</div>}
      {list.data?.length === 0 && <Empty icon={<Tv size={22} />} title="Nenhuma série encontrada" hint="Adicione uma biblioteca do tipo «séries» em Definições e execute uma análise." />}
      <div className="space-y-2">
        {list.data?.map((s) => (
          <div key={s.title} className="card overflow-hidden animate-rise">
            <button className="w-full flex items-center gap-3.5 p-3 text-left hover:bg-s2/50" onClick={() => setOpen(open === s.title ? null : s.title)} aria-expanded={open === s.title}>
              <div className="w-10 h-14 rounded-md bg-s2 border border-line overflow-hidden grid place-items-center shrink-0">
                {s.poster_id ? <img src={`/api/media/${s.poster_id}/poster`} className="w-full h-full object-cover" loading="lazy" alt="" /> : <Tv size={16} className="text-dim" />}
              </div>
              <div className="flex-1 min-w-0"><div className="font-medium truncate">{s.title}</div><div className="text-xs text-dim">{s.seasons} temporada{s.seasons !== 1 ? "s" : ""} · {s.episodes} episódios</div></div>
              {s.missing > 0 ? <Badge tone="bad">{s.missing} sem legenda</Badge> : <Badge tone="ok">Completa</Badge>}
              {open === s.title ? <ChevronDown size={16} className="text-mute" /> : <ChevronRight size={16} className="text-mute" />}
            </button>
            {open === s.title && <Episodes title={s.title} onOpen={setItem} />}
          </div>
        ))}
      </div>
      <ItemDrawer id={item} onClose={() => setItem(null)} />
    </>
  );
}

function Episodes({ title, onOpen }: { title: string; onOpen: (id: number) => void }) {
  const q = useQuery({ queryKey: ["series-eps", title], queryFn: () => api.get<Item[]>(`/series/episodes?title=${encodeURIComponent(title)}`) });
  const [seasonOpen, setSeasonOpen] = useState<Record<number, boolean>>({});
  if (q.isLoading) return <div className="p-3"><Skeleton className="h-24" /></div>;
  const by = new Map<number, Item[]>();
  (q.data ?? []).forEach((e) => { const k = e.season ?? 0; by.set(k, [...(by.get(k) ?? []), e]); });
  return (
    <div className="border-t border-line">
      {[...by.entries()].map(([season, eps]) => {
        const miss = eps.filter((e) => e.missing.length).length;
        const isOpen = seasonOpen[season] ?? by.size === 1;
        return (
          <div key={season}>
            <button className="w-full flex items-center gap-2 px-4 py-2.5 text-sm hover:bg-s2/40 border-b border-line/50" onClick={() => setSeasonOpen({ ...seasonOpen, [season]: !isOpen })}>
              {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}<span className="font-medium">{season ? `Temporada ${season}` : "Sem temporada"}</span>
              <span className="text-dim text-xs">{eps.length} ep.</span>{miss > 0 && <Badge tone="bad" className="ml-auto">{miss} em falta</Badge>}
            </button>
            {isOpen && eps.map((e) => (
              <button key={e.id} onClick={() => onOpen(e.id)} className={cx("w-full flex items-center gap-3 pl-10 pr-4 py-2 text-left hover:bg-s2/50 border-b border-line/40 last:border-0")}>
                <span className="text-xs text-dim tabular-nums w-14 shrink-0">{epCode(e)}</span>
                <span className="flex-1 min-w-0 truncate text-[13px]">{e.episode_title || e.file_name}</span>
                <LangChips item={e} />
              </button>
            ))}
          </div>
        );
      })}
    </div>
  );
}
