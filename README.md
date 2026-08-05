# MusicBox — Player de Música & Downloader Pessoal

MusicBox é um **Player de Música e Downloader Pessoal (Self-Hosted)** que busca, reproduz e baixa músicas e álbuns do YouTube Music para ouvir offline no celular e no computador.

## Recurso e Funcionalidades

- **Full Music Player**: Mini-Player de áudio flutuante com reprodução direta no navegador (online e offline).
- **PWA (Progressive Web App)**: Pode ser instalado na tela inicial do celular (Android/iOS) rodando como app nativo em tela cheia.
- **Cache de Busca Instantâneo (LRU)**: Buscas repetidas respondem em ~0.01s.
- **Capas em Alta Definição (HD Cover Art)**: Up-scaling automático de thumbnails para alta resolução (600x600 px).
- **Busca por URL Direta**: Cole qualquer link do YouTube / YouTube Music diretamente na busca.
- **Edição de Metadados / Tags ID3**: Edite títulos, artistas e álbuns diretamente na interface e nas tags dos arquivos de mídia.
- **Exportação de Playlists `.M3U`**: Baixe um arquivo `.m3u` para carregar suas músicas em players externos.
- **Recuperação de Falhas**: Botão para re-enfileirar todos os downloads que falharam em 1 clique.
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

Para desenvolvimento (adiciona `pytest` e `httpx` aos deps de produção):

```bash
.venv/bin/pip install -r requirements-dev.txt
```

## Uso

```bash
make dev
```

O `make dev` cria o venv se ausente, instala as dependências e avisa se o `ffmpeg` faltar (continua mesmo assim). O servidor sobe em `http://0.0.0.0:8080` e imprime o IP local no startup — acesse pelo celular na mesma rede para baixar músicas.

> **Acesso pelo Celular / Firewall:** Se o celular não conseguir conectar, libere a porta 8080 no firewall do Linux executando: `sudo ufw allow 8080/tcp`.

```bash
make test
```

Roda a suíte pytest (61 testes, com yt-dlp mockado — sem rede).

## Configuração

Opcional: copie `.env.example` para `.env` e ajuste conforme necessário.

```bash
cp .env.example .env
```

| Chave | Padrão | Descrição |
|---|---|---|
| `PORT` | `8080` | Porta do servidor HTTP (FastAPI/Uvicorn) |
| `MUSICBOX_DIR` | `~/Music/musicbox/` | Diretório onde as músicas baixadas são salvas |
| `DEFAULT_FORMAT` | `mp3` | Formato padrão de download: `mp3` ou `opus` |
| `WORKERS` | `2` | Número de downloads simultâneos |
| `SOCKET_TIMEOUT` | `30` | Timeout de socket (segundos) nas requisições de rede |
| `RETRIES` | `2` | Número de tentativas de download antes de considerar falha |
| `COOKIES_FILE` | (não definido) | Caminho para um `cookies.txt` (formato Netscape) exportado de um navegador logado no YouTube (ex.: `~/cookies/youtube.txt`). Opção recomendada quando `COOKIES_FROM_BROWSER` não funciona |
| `COOKIES_FROM_BROWSER` | (não definido) | Nome do navegador para ler cookies direto: `chrome`, `chromium`, `brave`, `edge`, `firefox`, `safari`. Exige o navegador logado no YouTube no mesmo usuário do servidor |

Precedência: **variável de ambiente > `.env` > padrão**. Em `MUSICBOX_DIR`, `~` e `$VAR` são expandidos. Se `COOKIES_FILE` e `COOKIES_FROM_BROWSER` forem definidos, **`COOKIES_FILE` tem precedência**.

> **Escopo dos cookies:** `COOKIES_FILE`/`COOKIES_FROM_BROWSER` são usados **apenas na busca e nos metadados** (`track_metadata`). O **download roda anônimo de propósito** (sessão logada é flagada pelo YouTube — ver seção abaixo). Cookies são **opcionais** e podem ficar vazios: sem eles a busca e os metadados também funcionam.

## Bloqueio do YouTube (Sign in to confirm you're not a bot)

A sessão logada nos cookies do YouTube está **flagada**: o player response vem sem `streamingData`, então o yt-dlp falha com `Requested format is not available` (e variações do `Sign in to confirm you're not a bot`). A **busca e o `track_metadata` continuam funcionando com ou sem cookies** — só o download era afetado.

A solução aplicada foi o **download anônimo com `player_client=android`** (`app/downloader.py::_default_executor`, `extractor_args={"youtube": {"player_client": ["android"]}}`): o YouTube devolve o formato **18 (mp4, ~44k de áudio mp4a.40.2)**, convertido para mp3/opus via FFmpeg. O app **não usa cookies no download de propósito** — a sessão logada está flagada, e tentar usá-la faz o download falhar.

Os cookies continuam disponíveis para **busca/metadados** (opcionais):

1. **Arquivo de cookies**: exporte um `cookies.txt` (formato Netscape) de um navegador logado no YouTube e aponte `COOKIES_FILE` para ele:

   ```bash
   COOKIES_FILE=~/cookies/youtube.txt
   ```

2. **Ler cookies do navegador**: defina `COOKIES_FROM_BROWSER` com o nome do navegador (`chrome`, `chromium`, `brave`, `edge`, `firefox`, `safari`) — o yt-dlp lê os cookies do próprio navegador:

   ```bash
   COOKIES_FROM_BROWSER=firefox
   ```

   Exige o navegador logado no YouTube na mesma máquina do servidor; o `firefox` costuma ser o mais simples, mas pode ser necessário fechar o navegador para liberar o acesso aos cookies.

### Qualidade do áudio

Com a abordagem anônima + client `android`, o áudio vem do formato **18 (mp4, ~44k)** e é convertido para mp3/opus via FFmpeg — qualidade **funcional, porém inferior** à de uma sessão logada saudável. Se no futuro o YouTube voltar a liberar formatos melhores para sessão logada, o uso de cookies no download pode ser reativado.

Referência: [FAQ do yt-dlp — How do I pass cookies to yt-dlp?](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)

## Como funciona

Adaptado ao yt-dlp **2026.07.04**: o extrator atual não tem mais `ytmsearch:` nem fornece `track_number`/ano para álbuns. O cliente (`app/ytdlp_client.py`) contorna isso:

- **Busca** via URL `https://music.youtube.com/search?q=...` com `extract_flat=True`, separando as seções de álbuns e artistas (parâmetro `sp`).
- **Álbum**: o id `MPRE...` (browse) resolve por redirect para a playlist `OLAK...`; as faixas são numeradas por **posição** (1..N) e não há ano na UI (`year=None`).
- **Artista**: não há página de álbuns de artista no yt-dlp — a tela de artista usa a **busca filtrada a álbuns**.
- **Latência de busca** de ~11–20s: a resolução de títulos é **sequencial** por causa do rate-limit do YouTube (paralelismo se mostrou mais lento) — comportamento deliberado.
- **Download**: yt-dlp `-x` com `FFmpegExtractAudio` (mp3/opus) + `EmbedThumbnail`; o arquivo final fica em `MUSICBOX_DIR/<artista>/<álbum>/<NN> - <título>.<ext>` (NN = posição da faixa, zero-padded).

## API

| Método | Caminho | Descrição | Erros |
|---|---|---|---|
| `GET` | `/` | Serve o `index.html` do frontend (200) | 503 (fallback quando `index.html` está ausente) |
| `GET` | `/api/search?q=` | Busca artistas e álbuns no YouTube Music | 404, 422, 502, 503 |
| `GET` | `/api/artists/{artist_name}/albums` | Álbuns de um artista (pelo nome) | 404, 502, 503 |
| `GET` | `/api/albums/{browse_id}/tracks` | Faixas de um álbum pelo browse_id | 404, 502, 503 |
| `POST` | `/api/downloads` | Enfileira um download (`yt_id` ou `album_id`, `formato: mp3\|opus`) → 202 | 404, 422, 502, 503 |
| `GET` | `/api/downloads` | Snapshot das tasks em memória (status/progresso/stage) | — |
| `GET` | `/api/history` | Histórico persistido de downloads | — |
| `GET` | `/api/library/{rel_path:path}` | Serve um arquivo baixado (com proteção contra path traversal) | 404 |

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

67 testes, distribuídos por módulo: `config` 8 · `downloader` 14 · `history` 10 · `main` 25 · `ytdlp_client` 10.

## Limitações e convenções

- A busca pode demorar (~11–20s) — é o comportamento esperado, dado o rate-limit do YouTube.
- **ffmpeg é obrigatório para TODOS os downloads — mp3 E opus**: a conversão passa por
  `FFmpegExtractAudio` nos dois formatos (opus não é "nativo"). Sem ffmpeg no servidor os
  downloads falham; a UI exibe um banner persistente e desabilita os botões de download.
- Não há CORS habilitado (o frontend é servido pelo próprio FastAPI, mesmo domínio).
- Comentários, docstrings e este README estão em **português**; identificadores de código em **inglês**.
- Docker é usado para **infraestrutura apenas** neste portfolio — o MusicBox roda 100% nativo.
