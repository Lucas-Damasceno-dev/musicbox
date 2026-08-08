# MusicBox — Player de Música & Downloader Pessoal

MusicBox é um **Player de Música e Downloader Pessoal (Self-Hosted)** que busca, reproduz e baixa músicas e álbuns do YouTube Music para ouvir offline no celular e no computador.

## Recurso e Funcionalidades

- **Full Music Player**: Tela dedicada de "now playing" (capa em disco de vinil girando, seek, volume, fila, salvar offline e salvar em playlist) + mini-player flutuante com capa — clicar no mini-player abre a tela cheia. Toca direto da biblioteca/histórico; suporta Media Session na tela de bloqueio.
- **Biblioteca Navegável**: aba própria com artistas → álbuns → faixas (só o que está baixado), com reprodução por álbum.
- **Playlists do Usuário**: crie playlists, adicione a faixa tocando no player, toque e exporte `.m3u` por playlist (persistência SQLite).
- **Busca Incremental (SSE)**: a busca (~11–20s) agora entrega as seções **conforme resolvem** — a UI mostra músicas/álbuns progressivamente com skeleton, sem esperar tudo.
- **Badge de Downloads Ativos**: contador ao vivo no botão Downloads da navegação (WebSocket sempre conectado).
- **Notificações de Download**: notificação nativa quando um download termina ou falha (Notification API, permissão pedida no primeiro download).
- **Offline no Navegador**: Service Worker cacheia os áudios tocados/baixados — reproduza sem rede (botão "Salvar offline" na tela do player).
- **PWA (Progressive Web App)**: Pode ser instalado na tela inicial do celular (Android/iOS) rodando como app nativo em tela cheia (ícones PNG reais 192/512).
- **Cache de Busca Instantâneo (LRU + disco)**: Buscas repetidas respondem em ~0.01s e o cache sobrevive a restarts (SQLite).
- **Capas em Alta Definição (HD Cover Art)**: Up-scaling automático de thumbnails para alta resolução (600x600 px).
- **Busca por URL Direta**: Cole qualquer link do YouTube / YouTube Music — inclusive **URLs de playlist** (`list=PL...`) — diretamente na busca.
- **Playlists do YouTube Music**: aba própria na busca + download de playlists inteiras (faixas numeradas por posição).
- **Edição de Metadados / Tags ID3**: Edite títulos, artistas e álbuns diretamente na interface e nas tags dos arquivos de mídia.
- **Exportação de Playlists `.M3U`**: Baixe um arquivo `.m3u` para carregar suas músicas em players externos.
- **Recuperação de Falhas**: Botão para re-enfileirar todos os downloads que falharam em 1 clique.
- **Cancelamento de Downloads**: Cancele tarefas na fila ou em execução (o yt-dlp é abortado na hora).
- **Remoção da Biblioteca**: Remova faixas do histórico apagando também o arquivo no servidor.
- **Token de Acesso Opcional** (`MUSICBOX_TOKEN`): proteja a API na rede local — sem token, qualquer dispositivo da Wi-Fi acessa a biblioteca.
- **Transferência Automática para o Celular**: O download é disparado automaticamente para o dispositivo assim que a conversão conclui.

## Stack

- **Python 3.11+** (testado com 3.12)
- **FastAPI** — servidor HTTP, rotas REST e WebSocket
- **yt-dlp** — extração de metadados e download
- **mutagen** — incorporação e edição de tags de áudio (ID3/Ogg)
- **SQLite** — histórico de downloads e biblioteca (via stdlib `sqlite3`, zero-config)
- **Frontend Vanilla & PWA** (HTML5, ServiceWorker, Google Fonts, Audio Engine) em `app/static/`
- **Sem Docker** — o backend roda nativo

## Requisitos

- Python 3.11+
- `ffmpeg` — necessário para a conversão para mp3/opus e o embed da capa (cheque com `ffmpeg -version`)
- `mutagen` — dependência Python do `yt-dlp` para pós-processamento de capas de áudio
- `pip`

## Instalação

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Para desenvolvimento (adiciona `pytest`, `httpx`, `pytest-cov` e `ruff` aos deps de produção):

```bash
.venv/bin/pip install -r requirements-dev.txt
```

Ou use o Makefile: `make install` (produção) e `make install-dev` (desenvolvimento). O `install-dev` só reinstala quando `requirements-dev.txt` mudar.

## Uso

```bash
make dev
```

O `make dev` cria o venv se ausente, instala as dependências e avisa se o `ffmpeg` faltar (continua mesmo assim). O servidor sobe em `http://0.0.0.0:8080` e imprime o IP local no startup — acesse pelo celular na mesma rede para baixar músicas.

> **Acesso pelo Celular / Firewall:** Se o celular não conseguir conectar, libere a porta 8080 no firewall do Linux executando: `sudo ufw allow 8080/tcp`.

```bash
make test
```

Roda a suíte pytest (115 testes, com yt-dlp mockado — sem rede).

## Configuração

Opcional: copie `.env.example` para `.env` e ajuste conforme necessário.

```bash
cp .env.example .env
```

| Chave | Padrão | Descrição |
|---|---|---|
| `PORT` | `8080` | Porta do servidor HTTP (FastAPI/Uvicorn) |
| `MUSICBOX_DIR` | `~/Music/musicbox/` | Diretório onde as músicas baixadas são salvas |
| `DEFAULT_FORMAT` | `opus` | Formato padrão de download: `mp3` ou `opus` |
| `WORKERS` | `2` | Número de downloads simultâneos |
| `SOCKET_TIMEOUT` | `30` | Timeout de socket (segundos) nas requisições de rede |
| `RETRIES` | `2` | Número de tentativas de download antes de considerar falha |
| `MUSICBOX_TOKEN` | (não definido) | Token de acesso compartilhado exigido nas rotas `/api/*` (header `X-MusicBox-Token` ou query `?token=`). Sem ele, a API fica aberta na rede local |

Precedência: **variável de ambiente > `.env` > padrão**. Em `MUSICBOX_DIR`, `~` e `$VAR` são expandidos.

## Download anônimo (Sign in to confirm you're not a bot)

A sessão logada do YouTube está **flagada**: o player response vem sem `streamingData`, então o yt-dlp falha com `Requested format is not available` (e variações do `Sign in to confirm you're not a bot`).

A solução aplicada foi o **download anônimo com `player_client=android`** (`app/downloader.py::_default_executor`, `extractor_args={"youtube": {"player_client": ["android"]}}`): o YouTube devolve o formato **18 (mp4, ~44k de áudio mp4a.40.2)**, convertido para mp3/opus via FFmpeg. O app **não usa cookies no download de propósito** — a sessão logada está flagada, e tentar usá-la faz o download falhar. Por isso não há configuração de cookies (`COOKIES_FILE`/`COOKIES_FROM_BROWSER` foram removidas).

### Qualidade do áudio

Com a abordagem anônima + client `android`, o áudio vem do formato **18 (mp4, ~44k)** e é convertido para mp3/opus via FFmpeg — qualidade **funcional, porém inferior** à de uma sessão logada saudável. Se no futuro o YouTube voltar a liberar formatos melhores para sessão logada, a estratégia de download pode ser revisada.

## Como funciona

Adaptado ao yt-dlp **2026.07.04**: o extrator atual não tem mais `ytmsearch:` nem fornece `track_number`/ano para álbuns. O cliente (`app/ytdlp_client.py`) contorna isso:

- **Busca** via URL `https://music.youtube.com/search?q=...` com `extract_flat=True`, separando as seções de músicas, álbuns, artistas e playlists (parâmetro `sp`). Resultados ficam em **cache LRU em memória + SQLite em disco** (`search_cache.db` em `MUSICBOX_DIR`), TTL 600s.
- **URLs diretas**: `watch?v=`/`youtu.be` viram música avulsa; `list=PL...`/`VL...`/`OLAK...` viram um item de playlist que abre a lista de faixas.
- **Álbum/Playlist**: o id `MPRE...` (browse) resolve por redirect para a playlist `OLAK...`; playlists `PL`/`VL`/mixes resolvem direto pela URL de playlist; as faixas são numeradas por **posição** (1..N) e não há ano na UI (`year=None`).
- **Artista**: não há página de álbuns de artista no yt-dlp — a tela de artista usa a **busca filtrada a álbuns**.
- **Latência de busca** de ~11–20s: a resolução de títulos é **sequencial** por causa do rate-limit do YouTube (paralelismo se mostrou mais lento) — comportamento deliberado.
- **Download**: yt-dlp `-x` com `FFmpegExtractAudio` (mp3/opus) + `EmbedThumbnail`; o arquivo final fica em `MUSICBOX_DIR/<artista>/<álbum>/<NN> - <título>.<ext>` (NN = posição da faixa, zero-padded).

## API

| Método | Caminho | Descrição | Erros |
|---|---|---|---|
| `GET` | `/` | Serve o `index.html` do frontend (200) | 503 (fallback quando `index.html` está ausente) |
| `GET` | `/api/search?q=&limit=` | Busca músicas, artistas, álbuns e playlists no YouTube Music (`limit`: 1–40 itens por seção, padrão 10) | 404, 422, 502, 503 |
| `GET` | `/api/search/stream?q=` | SSE da busca: eventos `section`/`done`/`error` conforme cada seção resolve | 422 |
| `GET` | `/api/browse` | Biblioteca navegável: artistas → álbuns → faixas (baixadas) | — |
| `GET/POST` | `/api/playlists` | Lista / cria playlists do usuário (`POST` com `{name}` → 201) | 422 |
| `DELETE` | `/api/playlists/{id}` | Apaga playlist (faixas em cascata) | 404 |
| `GET` | `/api/playlists/{id}` | Playlist com faixas (metadados do histórico) | 404 |
| `POST` | `/api/playlists/{id}/tracks` | Adiciona faixa (`{yt_id}`), dedupe por yt_id → 201 | 404, 422 |
| `DELETE` | `/api/playlists/{id}/tracks/{yt_id}` | Remove faixa da playlist | 404 |
| `GET` | `/api/playlists/{id}/export.m3u` | Exporta a playlist como `.m3u` (só faixas baixadas) | 404 |
| `GET` | `/api/artists/{artist_name}/albums` | Álbuns de um artista (pelo nome) | 404, 502, 503 |
| `GET` | `/api/albums/{browse_id}/tracks` | Faixas de um álbum pelo browse_id | 404, 502, 503 |
| `POST` | `/api/downloads` | Enfileira um download (`yt_id`, `album_id` ou `playlist_id`, `formato: mp3\|opus`) → 202 | 404, 422, 502, 503 |
| `GET` | `/api/downloads` | Snapshot das tasks em memória (status/progresso/stage) | — |
| `DELETE` | `/api/downloads/{task_id}` | Cancela uma tarefa (pendente ou em execução) | 404 |
| `GET` | `/api/history` | Histórico persistido de downloads | — |
| `DELETE` | `/api/history/{yt_id}` | Remove o registro E o arquivo de mídia do servidor | 404 |
| `GET` | `/api/library/{rel_path:path}` | Serve um arquivo baixado (com proteção contra path traversal) | 404 |

> **Autenticação:** com `MUSICBOX_TOKEN` definido, TODAS as rotas `/api/*` (exceto `/api/config`) exigem o token via header `X-MusicBox-Token` ou query `?token=` (necessário para `<audio>`/downloads). O `/ws` exige o token na query.

## WebSocket `/ws`

Ao conectar, recebe um snapshot do estado atual e depois updates de progresso:

```json
{"type": "snapshot", "tasks": [...]}
{"type": "update", "task_id": "...", "status": "running", "progress": 42.5, "stage": "extracting"}
```

## Estrutura

```
app/
  main.py           # rotas REST, WebSocket /ws, static e startup
  config.py         # Settings + parser de .env (env > .env > padrão)
  models.py         # Track, Album, SearchItem, SearchResults, DownloadTask
  ytdlp_client.py   # cliente do YouTube Music via yt-dlp (busca/álbum/metadados)
  downloader.py     # fila FIFO + thread pool, progresso, sanitização
  history.py        # histórico em SQLite (dedupe por yt_id)
  static/           # frontend vanilla (servido pelo FastAPI)
tests/              # suíte pytest (yt-dlp mockado, sem rede)
```

## Testes

```bash
make test
```

115 testes, distribuídos por módulo: `config` 12 · `downloader` 23 · `history` 13 · `main` 36 · `playlists` 9 · `ytdlp_client` 22.

## Limitações e convenções

- A busca pode demorar (~11–20s) — é o comportamento esperado, dado o rate-limit do YouTube.
- **ffmpeg é obrigatório para TODOS os downloads — mp3 E opus**: a conversão passa por
  `FFmpegExtractAudio` nos dois formatos (opus não é "nativo"). Sem ffmpeg no servidor os
  downloads falham; a UI exibe um banner persistente e desabilita os botões de download.
- Não há CORS habilitado (o frontend é servido pelo próprio FastAPI, mesmo domínio).
- Comentários, docstrings e este README estão em **português**; identificadores de código em **inglês**.
- Docker é usado para **infraestrutura apenas** neste portfolio — o MusicBox roda 100% nativo.
