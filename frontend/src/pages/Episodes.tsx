import { MediaBrowser } from "../components/MediaBrowser";
import { PageHeader } from "../components/ui";
export default function Episodes() {
  return (<><PageHeader title="Episódios" subtitle="Todos os episódios analisados, com filtros por idioma e estado." /><MediaBrowser kind="episode" /></>);
}
