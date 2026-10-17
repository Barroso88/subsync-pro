import { useState } from "react";
import { MediaBrowser } from "../components/MediaBrowser";
import { PageHeader, Tabs } from "../components/ui";
export default function Missing() {
  const [kind, setKind] = useState<"movie" | "episode">("episode");
  return (
    <>
      <PageHeader title="Legendas em falta" subtitle="Itens sem legenda num dos idiomas prioritários (as embutidas contam, se ativado nas Definições)."
        actions={<Tabs value={kind} onChange={setKind} items={[{ id: "episode", label: "Episódios" }, { id: "movie", label: "Filmes" }]} />} />
      <MediaBrowser key={kind} kind={kind} defaultStatus="missing" lockStatus />
    </>
  );
}
