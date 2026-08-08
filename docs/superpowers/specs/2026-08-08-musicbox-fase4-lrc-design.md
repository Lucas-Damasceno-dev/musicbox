# Fase 4 — Letras Sincronizadas (LRC) — Design

**Data:** 2026-08-08
**Status:** Aprovado (design aprovado pelo usuário em 2026-08-08)

## Objetivo

Exibir letras sincronizadas (estilo karaokê) no player do MusicBox. Letras buscadas da LRCLIB no momento do download e salvas ao lado do arquivo de áudio, permitindo visualização 100% offline depois.

## Requisitos (verbatim do usuário, m0190)

> "Aba dedicada para letras sincronizadas (estilo Karaokê / LRC format), com suporte para visualizar a letra mesmo offline se ela tiver sido baixada junto com a música."

## Decisões acordadas

1. **Fonte**: LRCLIB (`https://lrclib.net/api/search`) via backend, no momento do download. API pública, gratuita, sem chave. `syncedLyrics` (LRC com timestamps) preferido; fallback `plainLyrics` (LRC estático).
2. **Salvamento**: junto com o áudio, `{mesmo_diretorio}/{stem}.lrc`. Falha ou ausência NUNCA bloqueia o download (try/except + log debug).
3. **Faixas antigas**: sem letra (mostram estado vazio). Sem backfill, sem busca sob demanda.
4. **UX**: seletor **Fila | Letras** no player de tela cheia. Karaokê: linha ativa destacada sincronizada com `currentTime`, auto-scroll suave. Fallback: letra estática. Estados vazio/erro com mensagem amigável.

## Arquitetura

### Backend

**Novo módulo `app/lyrics.py`** (sem dependências novas — `urllib` da stdlib):
- `fetch_lrc(artist: str, title: str, album: str | None = None) -> str | None`
  - GET `https://lrclib.net/api/search?artist_name=..&track_name=..&album_name=..` (URL-encoded), timeout 5s.
  - Resposta: lista de hits com `{trackName, artistName, albumName, duration, plainLyrics, syncedLyrics}`.
  - Escolha: 1º hit com `syncedLyrics` → retorna LRC com timestamps; senão 1º com `plainLyrics` → retorna como LRC estático (texto puro); nenhum → `None`.
  - Erro de rede/timeout/HTTP != 200 → `None` (nunca levanta).

**`app/downloader.py` (`_run`, após `shutil.move(temp_file → dest_final)` ~L549)**:
- Se `dest_final` terminou com sucesso: `fetch_lrc(task.artist, task.title, task.album)`; se não-None, escreve `dest_final.with_suffix(".lrc")` (UTF-8). Embrulhado em try/except (log debug) — nunca falha o download.
- Álbum (`enqueue_album`): a letra é buscada por faixa (cada `_run` de faixa cuida da própria).

**`app/main.py`**:
- Rota nova: `GET /api/library/{yt_id}/lyrics` (com `Depends(require_auth)`):
  - Busca `record = history.get(yt_id)`; se não existe ou sem `path` → 404.
  - `lrc = Path(record["path"]).with_suffix(".lrc")`; se `is_file()` → `PlainTextResponse(lrc.read_text(encoding="utf-8"))`; senão → 404 `{"detail": "Sem letra"}`.
- `delete_history` (~L666): além do áudio, remove o `.lrc` irmão (`Path(file_path).with_suffix(".lrc")`, `unlink(missing_ok=True)`).

### Frontend (`app/static/app.js`)

- **Player de tela cheia**: seletor de visão **Fila | Letras** acima da área de conteúdo do player (próximo ao "AGORA TOCANDO"/acesso à fila). Estado: `state.playerPane = 'queue' | 'lyrics'`.
- **Aba Letras**:
  - Ao abrir a aba e a cada troca de faixa (startPlayer): `fetch(API.lyrics(yt_id))` — nova entrada no objeto `API`: `lyrics: (yt_id) => \`/api/library/${encodeURIComponent(yt_id)}/lyrics\``.
  - 200 → parse LRC: linhas `[mm:ss.xx]texto` (regex `^\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\](.*)$`); sem timestamps → letra estática (parágrafos).
  - Sincronização: no `timeupdate` (handler existente), destaca a última linha com `time <= currentTime` (classe `.lyric-line.is-active`), auto-scroll suave (`scrollIntoView({block:'center', behavior:'smooth'})` no elemento ativo — guard `prefers-reduced-motion`).
  - 404 → estado vazio ("Sem letras para esta faixa"); erro de rede → estado de erro com botão "Tentar novamente".
- Fila atual permanece como está (pane default).

### CSS (`app/static/styles.css`)

- `.player-pane-switch` (seletor Fila|Letras, padrão dos segment-btns da biblioteca), `.lyrics-view` (altura própria, overflow-y auto), `.lyric-line` (padding, line-height confortável, `--text-dim`), `.lyric-line.is-active` (destaque `--text` + peso + barra/marcação `--accent`), `.lyrics-empty` (estado vazio). Tudo via variáveis de tema; zero hex novo; funciona nos 2 temas.

### Testes

- `tests/test_lyrics.py` (novo): `fetch_lrc` com `urllib.request.urlopen` mockado:
  - hit com `syncedLyrics` → LRC com timestamps retornado;
  - só `plainLyrics` → texto retornado;
  - lista vazia → `None`; HTTP 500 → `None`; timeout/URLError → `None`; URL montada corretamente (query codificada).
- `tests/test_main.py` (+3): rota lyrics 200 (arquivo .lrc real em tmp), 404 sem .lrc, 404 sem registro; `delete_history` remove o `.lrc` irmão.
- `tests/test_downloader.py` (+2): `_run` com executor fake grava `.lrc` ao lado quando `fetch_lrc` retorna texto (mock em `app.lyrics.fetch_lrc`); `_run` com `fetch_lrc → None` termina OK sem arquivo.
- Frontend: smoke DOM stub (parse LRC, linha ativa por tempo, panes) + `node --check`.

## Fora de escopo

- Backfill de faixas existentes; busca sob demanda; letras em outras fontes; widget (sub-projeto 3).
