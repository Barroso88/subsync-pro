# SubSync Pro

Gestor self-hosted de legendas para bibliotecas Plex/Radarr/Sonarr: inventaria filmes e séries, deteta idiomas em falta (externas **e** embutidas), pesquisa legendas em fornecedores oficiais, avalia a compatibilidade **de forma honesta** e instala-as com segurança na pasta certa.

- **Backend** FastAPI + SQLite (WAL, migrações versionadas) · **Frontend** React + TypeScript + Tailwind + TanStack Query · **1 só container**.
- Nunca altera nem apaga vídeos. Nunca substitui legendas sem confirmação explícita.
- Funciona sem Plex/Radarr/Sonarr (opcionais).

## Funcionalidades (todas ligadas a dados reais)

| Área | Estado |
|---|---|
| Scanner incremental (MKV, MP4, AVI, MOV, M4V, TS, WEBM; SRT/ASS/SSA/VTT/SUB/IDX; pastas `Subs/`), FFprobe para duração/codec/streams embutidas, deteção de novos/alterados/removidos, salvaguarda contra share desmontada | ✅ |
| Identificação de filmes (título/ano) e episódios (`SxxExx`, `1x02`, `Season NN`), associação legenda↔vídeo com grau de certeza (certa/provável/incerta) | ✅ |
| Idiomas: pt-PT, pt-BR, pt (distintos), en, es, fr, de, it e mais; prioritários vs secundários; embutidas contam (configurável); forçadas nunca contam como cobertura; SDH/HI | ✅ |
| Análise de legendas existentes: vazia, inválida, truncada, para lá do fim do vídeo, idioma incoerente (nome vs conteúdo), nome não corresponde ao vídeo, duplicadas, associação fraca. **Só recomenda; nunca altera.** | ✅ |
| Fornecedores **OpenSubtitles.com** (API REST oficial v1) e **SubDL** (2.º adapter, ⚠️ só testado com respostas simuladas) com pesquisa por hash (só OpenSubtitles) (moviehash), IDs, temporada/episódio; limites de pedidos/min, downloads/dia e quota do fornecedor | ✅ (requer as suas credenciais) |
| Avaliação: compatibilidade (hash / provável / fraca / incompatível) **separada** de sincronização (validada vs referência / não confirmada / suspeita / inconclusiva), pontuação explicável por fatores | ✅ |
| Download seguro: staging, deteção de HTML/vazio/binário/corrompido, ZIP seguro (sem path traversal, anti-bomba), conversão para UTF-8, pré-visualização do destino, conflitos (cancelar / manter ambas / substituir com cópia de segurança), escrita atómica, nomes Plex-friendly (`Nome.pt-PT.srt`, `.forced`, `.sdh`) | ✅ |
| Lote com resumo + confirmação, em background, com concorrência limitada; modo «preparar para revisão» ou «instalar melhores» | ✅ |
| Tarefas: progresso, item atual, cancelar, repetir, histórico; reinício marca tarefas a meio como «interrompidas» | ✅ |
| Plex (refresh do item após instalar, IDs, posters), Radarr, Sonarr, mapeamento de caminhos, proteção SSRF | ✅ (requer os seus serviços) |
| Autenticação opcional por palavra-passe, segredos fora do código e fora da API | ✅ |

## Como a «sincronização» é avaliada (e o que **não** é garantido)

Uma legenda descarregada **não pode ser validada temporalmente com confiança total só com timestamps**. Por isso:

1. **Compatibilidade** (a legenda foi feita para este vídeo?): *Confirmada por hash* só quando o fornecedor devolve `moviehash_match`; *Provável* por episódio/título/release/fps; *Fraca*; *Provavelmente incompatível* (episódio errado, termina depois do vídeo…).
2. **Sincronização** é um eixo à parte:
   - **Alinhada com legenda de referência** — só quando existe, na mesma pasta, outra legenda (outro idioma) e os instantes das falas coincidem (offset ≤ 1,5 s, deriva ≤ 2 s, ≥ 60 % dos eventos). Assume que a referência está certa; **não analisa o áudio**.
   - **Possível dessincronização** — offset/deriva grandes face à referência, ou limites incompatíveis com a duração do vídeo.
   - **Inconclusivo** — poucos eventos / pouca correspondência.
   - **Sincronização não confirmada** — o caso por omissão. Duração compatível sozinha **nunca** valida.
3. A pontuação (0–100) é uma **heurística de ordenação**, não uma probabilidade calibrada; cada fator e o respetivo peso são mostrados.

Limitação conhecida: o alinhamento áudio↔legenda (ex.: ffsubsync/aligners com VAD) **não está implementado**; seria opcional e pesado em CPU. A definição `audio_sync` existe só como reserva.

## Executar (desenvolvimento)

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
cd frontend && npm install && npm run build && cd ..
SUBSYNC_DATA_DIR=./data SUBSYNC_ALLOWED_ROOTS=/caminho/para/media SUBSYNC_STATIC_DIR=./frontend/dist \
  .venv/bin/python -m uvicorn app.main:app_factory --factory --app-dir backend --port 8484
# UI com hot reload: (cd frontend && npm run dev)  →  http://localhost:5173 (proxy para :8484)
# API: http://localhost:8484/api/docs
```

Testes: `cd backend && ../.venv/bin/python -m pytest -q`

## Executar com Docker

```bash
cp .env.example .env     # ajuste caminhos, PUID/PGID e (recomendado) SUBSYNC_AUTH_PASSWORD
docker compose up -d --build
```

Abra `http://<servidor>:8484` → **Definições → Bibliotecas → Adicionar**. Guia completo para Unraid: [docs/UNRAID.md](docs/UNRAID.md).

## Variáveis de ambiente

| Variável | Predefinição | Descrição |
|---|---|---|
| `SUBSYNC_DATA_DIR` | `/data` | Config, BD, logs, staging, backups, posters |
| `SUBSYNC_ALLOWED_ROOTS` | `/media` | Raízes (separadas por `:`) dentro das quais se podem registar bibliotecas |
| `SUBSYNC_AUTH_PASSWORD` | _(vazio)_ | Ativa login por palavra-passe (**recomendado**) |
| `SUBSYNC_MAX_JOBS` | `2` | Tarefas em background em simultâneo |
| `SUBSYNC_FFPROBE_WORKERS` | `4` | Sondas FFprobe em paralelo por pasta |
| `SUBSYNC_LOG_LEVEL` | `INFO` | Logs JSON estruturados (segredos são mascarados) |
| `OPENSUBTITLES_API_KEY` / `_USERNAME` / `_PASSWORD` | | API key (pesquisa) + conta (descarregar) |
| `SUBDL_API_KEY` | | Chave gratuita do SubDL |
| `PLEX_TOKEN`, `RADARR_API_KEY`, `SONARR_API_KEY` | | Integrações opcionais |

Os segredos também podem ser introduzidos na UI (ficam em `/data/secrets.json`, modo `0600`, nunca devolvidos pela API). Variáveis de ambiente têm prioridade.

## Segurança

- Todos os caminhos passam por `realpath` + verificação de contenção; symlinks para fora são rejeitados; escrita só em bibliotecas com «escrita» ativa **e** permissão real no sistema de ficheiros.
- Downloads: limite de 10 MB, só HTTPS para anfitriões do fornecedor (`*.opensubtitles.com/.org`), redirecionamentos validados, ZIP lido em memória usando apenas o *basename*, rácio/tamanho limitados, conteúdo validado antes de instalar.
- FFprobe invocado sem shell, com lista de argumentos fixa e caminho absoluto.
- URLs de Plex/Radarr/Sonarr: só http(s), sem credenciais, sem link-local/metadata (SSRF). LAN/loopback são permitidos (é onde estes serviços vivem).
- Contentor: sem `privileged`, sem socket Docker, `cap_drop: ALL`, `no-new-privileges`, rootfs só de leitura, utilizador não-root.
- **Não exponha a porta à Internet.** Sem proxy reverso com TLS, a palavra-passe viaja em claro na LAN.

## Limitações conhecidas

- Não foi possível testar contra a API real do OpenSubtitles neste ambiente (sem credenciais): o adapter foi escrito segundo a documentação v1 e testado com respostas simuladas. Valide com a sua API key em **Fornecedores → Testar ligação**.
- A imagem Docker não foi construída neste ambiente (sem Docker); o Dockerfile deve ser validado com `docker compose build`.
- Fornecedores: OpenSubtitles e SubDL. Legendas.tv/Addic7ed/Subf2m não têm API oficial (só scraping), pelo que não estão incluídas.
- O SubDL devolve «PT» sem distinguir Portugal/Brasil: a variante é estimada pelo conteúdo após o download (heurística).
- «Aceitar pt-BR quando faltar pt-PT» (desligado por omissão): conta como cobertura mas fica assinalado `≈pt-BR`.
- Legendas gráficas (PGS/VobSub) são detetadas mas não analisadas (nem extraídas como referência).
- A referência de sincronização tem de ser um ficheiro externo de texto; legendas embutidas não são usadas como referência.
- Os posters dependem de Plex ou Radarr/Sonarr; sem eles aparece um placeholder.
- Sem suporte a numeração absoluta de anime nem a vários episódios por ficheiro (é usado o primeiro).
- SQLite apenas (migração para PostgreSQL exigiria adaptar `db.py`).
