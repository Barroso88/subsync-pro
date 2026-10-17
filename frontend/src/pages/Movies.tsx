import { MediaBrowser } from "../components/MediaBrowser";
import { PageHeader } from "../components/ui";
export default function Movies() {
  return (<><PageHeader title="Filmes" subtitle="Grelha ou tabela, com o estado das legendas por idioma." /><MediaBrowser kind="movie" allowGrid /></>);
}
