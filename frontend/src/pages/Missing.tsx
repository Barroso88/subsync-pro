import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { MediaBrowser } from "../components/MediaBrowser";
import { PageHeader, Tabs } from "../components/ui";
export default function Missing() {
  const [params] = useSearchParams();
  const lang = params.get("lang") ?? "";
  const [kind, setKind] = useState<"movie" | "episode">("movie");
  return (
    <>
      <PageHeader title="Legendas em falta" subtitle={lang ? `Itens sem legenda em ${lang}` : "Itens sem legenda num dos idiomas prioritários (as embutidas contam, se ativado nas Definições)."}
        actions={<Tabs value={kind} onChange={setKind} items={[{ id: "movie", label: "Filmes" }, { id: "episode", label: "Episódios" }]} />} />
      <MediaBrowser key={`${kind}-${lang}`} kind={kind} defaultStatus="missing" missingMode initialLang={lang} />
    </>
  );
}
