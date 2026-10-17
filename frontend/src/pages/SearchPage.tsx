import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { api, Item, qs } from "../api";
import { epCode } from "../lib";
import { Empty, PageHeader, Skeleton } from "../components/ui";
import { LangChips } from "../components/subs";
import { Poster, SearchBox, useDebounced } from "../components/MediaBrowser";
import { ItemDrawer } from "../components/ItemDrawer";

export default function SearchPage() {
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [open, setOpen] = useState<number | null>(null);
  const list = useQuery({ queryKey: ["media", "search", dq], queryFn: () => api.get<{ items: Item[]; total: number }>("/media" + qs({ q: dq, limit: 30, status: dq ? "" : "missing" })) });
  return (
    <>
      <PageHeader title="Procurar legendas" subtitle="Escolha um filme ou episódio para pesquisar nos fornecedores. Sem pesquisa, mostra os itens com legendas em falta." />
      <div className="mb-5"><SearchBox value={q} onChange={setQ} placeholder="Título, série, ano, ficheiro…" /></div>
      {list.isLoading && <div className="space-y-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-16" />)}</div>}
      {list.data?.items.length === 0 && <Empty icon={<Search size={22} />} title={dq ? "Sem resultados" : "Nada em falta 🎉"} hint={dq ? "Tente outro termo." : "Todos os itens têm legendas nos idiomas prioritários."} />}
      <div className="grid gap-2 md:grid-cols-2">
        {list.data?.items.map((i) => (
          <button key={i.id} onClick={() => setOpen(i.id)} className="card p-3 flex items-center gap-3 text-left hover:border-mute/40 animate-rise">
            <Poster item={i} className="w-10 h-14 rounded-md border border-line shrink-0" />
            <div className="min-w-0 flex-1"><div className="text-sm font-medium truncate">{i.kind === "episode" ? `${i.series_title} · ${epCode(i)}` : `${i.title}${i.year ? ` (${i.year})` : ""}`}</div>
              <div className="text-xs text-dim truncate mb-1">{i.file_name}</div><LangChips item={i} /></div>
          </button>
        ))}
      </div>
      <ItemDrawer id={open} onClose={() => setOpen(null)} initialTab="search" />
    </>
  );
}
