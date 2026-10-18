# Instalação no Unraid

## Requisitos
- Unraid 6.12+ com Docker ativo. Plugin **Docker Compose Manager** (recomendado) *ou* adicionar o contentor manualmente.
- Os caminhos abaixo são exemplos; use os das suas shares (`/mnt/user/Filmes`, `/mnt/user/Séries`, …).

## Opção 0 — Template Unraid (mais simples)

A imagem é publicada automaticamente em `ghcr.io/barroso88/subsync-pro:latest` (amd64) a cada push para `main`; a imagem arm64 só é construída em versões (tags `v*`) ou manualmente (Actions → Docker image → Run workflow → arm64).

1. No Unraid: **Docker → Add Container → Template** e, em *Template repositories*, ou simplesmente cole o conteúdo de [`unraid/subsync-pro.xml`](../unraid/subsync-pro.xml) em `/boot/config/plugins/dockerMan/templates-user/subsync-pro.xml` e escolha **subsync-pro** na lista de templates.
2. Ajuste os caminhos das shares e defina a palavra-passe. **Apply**.
3. **Pacote privado:** por omissão o pacote GHCR de um repositório privado é privado. Ou o torna público (GitHub → Packages → subsync-pro → *Package settings* → *Change visibility*), ou no Unraid faz `docker login ghcr.io -u Barroso88` com um *Personal Access Token* com permissão `read:packages` antes de instalar.

## Opção A — Docker Compose

1. Copie o projeto para `/mnt/user/appdata/subsync-pro-src` (ou clone o repositório).
2. `cp .env.example .env` e edite:
   ```ini
   CONFIG_DIR=/mnt/user/appdata/subsync-pro
   MOVIES_DIR=/mnt/user/Filmes
   SERIES_DIR=/mnt/user/Séries
   PUID=99
   PGID=100
   SUBSYNC_AUTH_PASSWORD=uma-palavra-passe-forte
   ```
3. `docker compose pull && docker compose up -d` (ou `--build` com a linha `build: .` ativada, para construir localmente)
4. Abra `http://IP-DO-UNRAID:8484`.

Se tiver mais shares (ex.: `/mnt/user/Docs`), acrescente linhas ao `volumes:` do `docker-compose.yml`, por exemplo `- /mnt/user/Anime:/media/Anime`.

## Opção B — Template manual (Add Container)

| Campo | Valor |
|---|---|
| Repository | `ghcr.io/barroso88/subsync-pro:latest` |
| Network | bridge |
| Port | Host `8484` → Container `8484` |
| Path | `/mnt/user/appdata/subsync-pro` → `/data` (Read/Write) |
| Path | `/mnt/user/Filmes` → `/media/Filmes` (**Read/Write** se quiser guardar legendas; **Read Only** caso contrário) |
| Path | `/mnt/user/Séries` → `/media/Séries` (idem) |
| Variable | `SUBSYNC_AUTH_PASSWORD` = … |
| Variable | `OPENSUBTITLES_API_KEY` (e `_USERNAME`, `_PASSWORD` para descarregar) |
| Extra Parameters | `--user 99:100 --security-opt no-new-privileges --cap-drop ALL --memory 1g --cpus 2` |

Não ative *Privileged* nem monte `/var/run/docker.sock`: não é necessário.

## Primeira configuração

1. **Definições → Bibliotecas → Adicionar**: navegue até `/media/Filmes` (tipo *Filmes*) e `/media/Séries` (tipo *Séries*).
   Ative **Permitir escrita de legendas** só nas que quiser. A UI mostra «Só leitura» ou «Leitura + escrita».
2. **Definições → Idiomas**: escolha p.ex. `pt-PT` prioritário e `en` secundário.
3. **Analisar bibliotecas** (Dashboard). A primeira análise percorre tudo; as seguintes só tocam no que mudou.
4. **Fornecedores**: introduza a API key do OpenSubtitles (https://www.opensubtitles.com → *API consumers*) e, para descarregar, utilizador e palavra-passe. *Testar ligação*.
5. (Opcional) **Plex/Radarr/Sonarr**: URL + token/API key. Se o Plex vê `/data/movies` e esta app `/media/Filmes`, adicione o mapeamento de caminhos.

## Permissões (UID/GID) e Plex

- As legendas são criadas com o UID/GID do contentor (`user: 99:100` = `nobody:users`, o padrão das shares Unraid) e modo `664`, pelo que o Plex (que normalmente corre como `nobody`/`users` ou `abc` 99:100) consegue lê-las.
- Se o seu Plex usa outro UID/GID, defina `PUID`/`PGID` iguais aos dele (ou assegure que o grupo tem leitura).
- Erro «Sem permissão de escrita»: a pasta está montada `:ro` ou o UID não tem escrita. Verifique com `ls -ln /mnt/user/Filmes` e o terminal do contentor: `docker exec subsync-pro sh -c 'id; touch /media/Filmes/.t && rm /media/Filmes/.t'`.
- Pastas Unraid com cache/shfs: alguns sistemas de ficheiros FUSE não suportam *hard links*; a instalação faz fallback seguro (renomeação atómica verificando existência).

## Atualização

```bash
cd /mnt/user/appdata/subsync-pro-src
docker compose pull && docker compose up -d   # nova imagem do GHCR
# no template Unraid: Docker → subsync-pro → Force update
```
Os dados em `/data` (BD, definições, segredos) persistem; as migrações da BD aplicam-se no arranque.

## Backup e restauro

- **Backup**: pare o contentor (`docker compose stop`) e copie `CONFIG_DIR` (`subsync.db`, `subsync.db-wal`, `subsync.db-shm`, `secrets.json`). Com o contentor em execução, use `sqlite3 subsync.db ".backup out.db"`.
- **Restauro**: pare, substitua o conteúdo de `CONFIG_DIR`, arranque. O inventário também pode ser reconstruído com **Analisar bibliotecas** (as definições e o histórico é que só existem na BD).
- `/data/backups` contém cópias das legendas que substituiu manualmente.

## Diagnóstico

| Sintoma | O que verificar |
|---|---|
| UI não abre | `docker logs subsync-pro`; `docker inspect --format '{{.State.Health.Status}}' subsync-pro` |
| «Pasta inacessível» / análise aborta | Share desmontada ou caminho errado; a app recusa apagar o inventário quando uma pasta aparece vazia |
| Sem duração / embutidas | Banner «FFprobe indisponível»; `docker exec subsync-pro ffprobe -version` |
| Fornecedor «erro de autenticação» | API key/credenciais; *Fornecedores → Testar ligação* |
| 429 / limite diário | O OpenSubtitles limita descarregamentos por conta; veja a quota restante no cartão do fornecedor |
| Legenda instalada mas o Plex não a mostra | Nome (`Vídeo.pt-PT.srt`), permissões, e *Atualizar metadados* no Plex (a app tenta fazê-lo se configurado) |
| Logs | JSON em stdout; segredos são mascarados. API: `/api/docs` |
