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
| Tradução automática via **DeepL** (inglês → pt-PT/pt-BR e outros): só o texto, tempos intactos, sempre assinalada, «por confirmar», com verificação da quota antes de traduzir | ✅ (requer chave DeepL; só testado com respostas simuladas) |
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
docker compose pull && docker compose up -d      # imagem: ghcr.io/barroso88/subsync-pro:latest
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
| `GEMINI_API_KEY` | | Tradução por Gemini (Google AI Studio; escalão gratuito com limites que o Google pode alterar) |
| `DEEPL_API_KEY` | | Tradução automática (plano gratuito: 500 000 caracteres/mês; chaves gratuitas terminam em `:fx`) |
| `PLEX_TOKEN`, `RADARR_API_KEY`, `SONARR_API_KEY` | | Integrações opcionais |

Os segredos também podem ser introduzidos na UI (ficam em `/data/secrets.json`, modo `0600`, nunca devolvidos pela API). Variáveis de ambiente têm prioridade.

## Segurança

- Todos os caminhos passam por `realpath` + verificação de contenção; symlinks para fora são rejeitados; escrita só em bibliotecas com «escrita» ativa **e** permissão real no sistema de ficheiros.
- Downloads: limite de 10 MB, só HTTPS para anfitriões do fornecedor (`*.opensubtitles.com/.org`), redirecionamentos validados, ZIP lido em memória usando apenas o *basename*, rácio/tamanho limitados, conteúdo validado antes de instalar.
- FFprobe invocado sem shell, com lista de argumentos fixa e caminho absoluto.
- URLs de Plex/Radarr/Sonarr: só http(s), sem credenciais, sem link-local/metadata (SSRF). LAN/loopback são permitidos (é onde estes serviços vivem).
- Contentor: sem `privileged`, sem socket Docker, `cap_drop: ALL`, `no-new-privileges`, rootfs só de leitura, utilizador não-root.
- **Não exponha a porta à Internet.** Sem proxy reverso com TLS, a palavra-passe viaja em claro na LAN.

## Webhooks do Radarr/Sonarr (novo ficheiro → procurar legenda)

No Radarr/Sonarr: *Settings → Connect → Webhook* (eventos **On File Import** e **On File Upgrade** no Radarr — **On Import**/**On Upgrade** no Sonarr —, método POST) com o URL `http://IP:8484/api/webhooks/radarr` (ou `/sonarr`), utilizador `subsync` e palavra-passe = token mostrado em *Definições → Radarr/Sonarr → Webhook → Configurar*. A autenticação usa *basic auth* (o token não vai no URL, para não aparecer em logs); o endpoint não usa a sessão da interface. A app analisa apenas a pasta do ficheiro importado e, se *Procurar automaticamente após cada análise* estiver ativo, procura a legenda (respeitando «exigir confirmação»). O caminho recebido passa pelo **mapeamento de caminhos** e tem de ficar dentro de uma biblioteca; senão a tarefa falha com mensagem clara.

## Ferramentas de legendas existentes

Na ficha do filme, em cada legenda externa SRT: **Ferramentas** → *Atrasar/adiantar*, *Frame rate*, *Dois pontos*, *Sincronizar com referência* e *Apagar* (mover para backup).
- **Só os tempos mudam**: texto, itálicos e posições ficam byte a byte iguais (ida e volta exata).
- **Pré-visualização obrigatória** (antes → depois das primeiras e últimas falas, avisos de fim do vídeo) e **confirmação**. Se o ficheiro mudar entre a pré-visualização e a aplicação, recusa (hash).
- O **original vai sempre para `/data/backups`** e fica em *Alterações a legendas* com **Restaurar** (recusa se o ficheiro foi alterado entretanto, salvo confirmação). «Apagar» move para backup; nunca apaga de vez.
- Só em bibliotecas com **escrita ativa**, só ficheiros normais (não atalhos), escrita atómica.
- **Sincronizar com referência** compara os instantes de início das falas com outra legenda do mesmo filme (correlação de todos os pares; deteta offset e deriva de frame rate) e **assume a referência correta**; **não usa o áudio**. Exige ≥ 40 % de correspondência; legendas sem relação são recusadas.
- **Limpar texto** (combinável, com pré-visualização antes→depois): remover marcas HI (`[ruído]`, `(música)` no início/fim, `NOME:` em maiúsculas, linhas só com ♪), remover tags de estilo (`<i>`, `<b>`, `<font>`, `{\an8}`), correções comuns (espaços, pontuação, travessões, reticências, anúncios/marcas de água só nas primeiras/últimas falas) e maiúsculas → frase normal. Os **tempos e a ordem das falas são verificados** como iguais aos originais antes de gravar; falas que ficam vazias são removidas e a numeração refeita. Regras heurísticas e conservadoras (ex.: parêntesis a meio de uma frase ficam; «Mr. Smith:» não é apagado), mas **reveja sempre a pré-visualização**: o «maiúsculas → frase» perde nomes próprios.
- **Converter** (SRT em UTF-8 sem BOM, para Smart TV/Android TV/Plex): ASS/SSA/VTT → SRT (mantém itálico/negrito/sublinhado e os tempos; perde posições, cores e fontes; eventos só com desenhos ficam de fora), SRT em cp1252/UTF-16 ou com BOM → UTF-8 e reparação de acentos partidos («Ã£», «Ã©»…, só nas linhas em que o resultado é UTF-8 válido). O ASS/VTT passa a um **novo `.srt` que nunca substitui um existente** (409 se já houver) e o original vai para backup (ou fica, se marcar «manter»); o **restauro** remove o `.srt` criado e repõe o original. Tempos verificados contra o original antes de gravar. Caracteres já perdidos (�) não se recuperam: avisa e é preciso outra legenda.
- **Converter legendas (em lote)**: página própria. «Procurar legendas a converter» só **lê** os ficheiros e lista o que precisa (ASS/SSA/VTT, SRT fora de UTF-8 ou com BOM, acentos partidos), ignorando as que já têm `.srt` de destino. Escolha (ou «selecionar todas») e confirme: corre como tarefa em background, reavalia cada ficheiro no disco, guarda backup de cada original (restaurável no histórico do filme) e verifica os tempos antes de gravar. Só bibliotecas com escrita ativa.
- Limitações: só SRT (ASS/VTT não); backups não são apagados automaticamente; a pré-visualização é textual (sem leitor de vídeo), por isso confirme a sincronização a ver o filme.

## Bazarr (opcional)

Em *Definições → Bazarr* (URL + API key do Bazarr), a app pode **pedir ao Bazarr** que procure e descarregue a legenda de um filme (botão **Pedir ao Bazarr** na ficha do filme, ou «Pesquisar com: Bazarr» no lote). O Bazarr usa os fornecedores dele (ex.: LegendasDivx, com a conta do utilizador no Bazarr) e grava o ficheiro; a app **reanalisa e valida** o resultado (um ficheiro vazio ou inválido não conta). Usa a API publicada pelo próprio Bazarr (`PATCH /api/movies/subtitles`). Requisitos: filme com id do Radarr (sincronizar metadados do Radarr), biblioteca com escrita ativa. Não há passo «por confirmar», porque quem grava é o Bazarr; o lote corre um filme de cada vez.

## Legendas vazias

A página **Legendas vazias** lista ficheiros de legenda com 0 bytes (ex.: deixados por downloads falhados de outras ferramentas, que o Bazarr conta como «existentes»). Permite apagá-los em lote com confirmação. O servidor só apaga um ficheiro se, **no momento de apagar**, for uma legenda (`.srt/.ass/.ssa/.vtt/.sub/.idx`), tiver 0 bytes, não seja um atalho, e esteja numa biblioteca com **escrita ativa**. Nunca apaga vídeos nem legendas com conteúdo. É a única operação de apagar da aplicação; é sempre iniciada por si.

## Leitura dos vídeos

Por omissão a app só abre os vídeos com o FFprobe (metadados e lista de faixas, leitura mínima). **Nunca** lê o conteúdo completo dos vídeos, nem em «Reclassificar legendas». Só se ativar explicitamente *Definições → Scanner → Ler o texto das legendas embutidas «pt»* é que o ffmpeg extrai essas faixas (lê quase todo o ficheiro). Alternativa barata: *Definições → Idiomas → Legendas «pt» sem variante → Assumir pt-PT*.

## Tradução automática (DeepL)

Desligada por omissão (Definições → Idiomas). Quando não há legenda no idioma pedido, o botão **Traduzir** descarrega uma legenda noutro idioma e traduz o texto, **mantendo os tempos originais**. Notas:
- O texto das legendas é **enviado ao DeepL** (terceiro). Não use se isso não for aceitável.
- É sempre assinalada como tradução automática (fator de pontuação negativo, nota na pré-visualização, registo no histórico) e fica **por confirmar**; nunca é instalada sem confirmação, nem em lote.
- Antes de traduzir verifica-se a quota (`/v2/usage`); sem quota suficiente nada é enviado.
- Itálicos/formatação são removidos; diálogo com travessões é traduzido fala a fala; quebras de linha são refeitas (~42 caracteres).
- A tradução herda a sincronização da original (nunca é «validada» por si só).
- Opção «em lote, traduzir de inglês se não houver legenda»: desligada por omissão; gasta caracteres da quota.

## Limitações conhecidas

- Não foi possível testar contra a API real do OpenSubtitles neste ambiente (sem credenciais): o adapter foi escrito segundo a documentação v1 e testado com respostas simuladas. Valide com a sua API key em **Fornecedores → Testar ligação**.
- A imagem Docker não foi construída neste ambiente (sem Docker); o Dockerfile deve ser validado com `docker compose build`.
- Fornecedores: OpenSubtitles e SubDL. Legendas.tv/Addic7ed/Subf2m não têm API oficial (só scraping), pelo que não estão incluídas.
- O adapter DeepL foi testado só com respostas simuladas (sem chave real neste ambiente).
- O SubDL devolve «PT» sem distinguir Portugal/Brasil: a variante é estimada pelo conteúdo após o download (heurística).
- «Aceitar pt-BR quando faltar pt-PT» (desligado por omissão): conta como cobertura mas fica assinalado `≈pt-BR`.
- Legendas gráficas (PGS/VobSub) são detetadas mas não analisadas (nem extraídas como referência).
- A referência de sincronização tem de ser um ficheiro externo de texto; legendas embutidas não são usadas como referência.
- Os posters dependem de Plex ou Radarr/Sonarr; sem eles aparece um placeholder.
- Sem suporte a numeração absoluta de anime nem a vários episódios por ficheiro (é usado o primeiro).
- SQLite apenas (migração para PostgreSQL exigiria adaptar `db.py`).

- **Ordenar por data de adição** (Filmes e Legendas em falta): «Adicionados recentemente» / «há mais tempo». A data vem do Plex (quando entrou na biblioteca), senão do Radarr/Sonarr (data de importação do ficheiro), senão da data de modificação do ficheiro. Corra «Sincronizar metadados» para preencher as datas do Plex/Radarr/Sonarr.
- **Referência embutida**: se o filme não tem outra legenda externa para validar a sincronização, ao descarregar a app extrai **uma** faixa de texto embutida (ffmpeg, só leitura, com cache) e compara os tempos (funciona entre idiomas). Resultado «Alinhada com legenda de referência» (a nota indica «faixa embutida») ou «Possível dessincronização». Assume que a faixa embutida está sincronizada com o vídeo; **não analisa o áudio**. Desligável em Definições → Scanner.
- **Legendas duplicadas**: página que lista, por filme, ficheiros de legenda **byte a byte iguais** (ex.: `.pt.srt` e `.pt-PT.srt`). Por omissão fica o que tem a variante explícita no nome (pt-PT/pt-BR), mas pode escolher qual fica. Os repetidos vão para backup (restaurável); antes de mover, o servidor volta a comparar os dois ficheiros no disco. Há «resolver todos». Legendas parecidas mas não idênticas não aparecem aqui (são só avisadas na ficha do filme).
- **Tradução por Gemini** (alternativa ao DeepL, à escolha em Fornecedores): traduz blocos de 40 falas com o contexto das vizinhas, com instrução explícita de português de Portugal (pt-PT) ou do Brasil. Cada resposta é **validada** (mesmo número de falas, na mesma ordem): se o modelo perder ou juntar falas, o bloco é subdividido e repetido. Falas recusadas pelos filtros de segurança ficam no original e contam; **se forem mais de 3 % a tradução é rejeitada** (nunca se instala uma legenda incompleta). Tempos intactos; o resultado é sempre «tradução automática / por confirmar». **O texto da legenda é enviado ao Google**; no escalão gratuito o Google pode usar os pedidos para melhorar produtos (veja os termos atuais). A chave vai em cabeçalho, nunca no URL. O modelo é automático por omissão (o `gemini-X-flash-lite` estável mais recente que a chave pode usar (no escalão gratuito do AI Studio observámos 15 pedidos/min e 500/dia, contra 5/min e 20/dia dos Flash normais; os limites mudam, confirme no AI Studio) e, se não houver, o `flash` mais recente, via listagem de modelos; alias `gemini-flash-latest` como último recurso) e pode fixar-se em Fornecedores. Não foi testado contra a API real: use «Testar» em Fornecedores com a sua chave.
- **Tradução em segundo plano**: «Descarregar e traduzir» arranca uma tarefa com **barra de progresso** (blocos traduzidos) e botão **Cancelar**; pode fechar a janela e a tradução continua (Tarefas e atividade). No fim a legenda fica «por confirmar» em «Histórico de downloads» (ou abre a pré-visualização se a janela ainda estiver aberta). Evita cortes por timeout de browsers/proxies em traduções longas. No Gemini, o «raciocínio» do modelo é desligado (mais rápido) e a app recua sozinha se o modelo não o aceitar.
- **Limites do Gemini**: nos erros 429 por minuto a app espera o tempo indicado pelo Google e repete; se o limite **diário** estiver esgotado, falha logo com uma mensagem clara (não fica à espera em vão).
- **Gemma (via API Gemini)**: modelos «gemma-*» (e qualquer modelo que rejeite instruções de sistema/modo JSON) passam a um modo em que as instruções vão na própria mensagem e o JSON é lido do texto da resposta (tolera cercas markdown). Os pedidos são espaçados (~8 s) para respeitar o limite de tokens/minuto do escalão gratuito (16K no Gemma). **Não testado contra o Gemma real**: use «Testar» em Fornecedores (mostra 5 frases traduzidas para pt-PT) antes de traduzir um filme. As validações (nº de falas, tempos, recusas) são as mesmas.
- **Compatibilidade de modelos Gemini**: se um modelo responder «400 invalid argument», a app simplifica o pedido por degraus (sem `thinkingConfig` → sem `safetySettings` → instruções na mensagem com JSON lido do texto), lembra-se do degrau que funcionou e, se nada funcionar, diz o modelo e o que foi tentado.
