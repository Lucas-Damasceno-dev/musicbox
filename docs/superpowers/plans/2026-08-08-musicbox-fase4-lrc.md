# Fase 4a — Letras Sincronizadas (LRC) — Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Letras sincronizadas estilo karaokê no player, buscadas da LRCLIB no download e salvas como `.lrc` ao lado do áudio (offline total depois).

**Architecture:** Novo módulo `app/lyrics.py` (fetch LRCLIB via urllib da stdlib, nunca levanta); `Downloader._run` grava o `.lrc` após o move final; rota `GET /api/library/{yt_id}/lyrics` serve o arquivo; frontend ganha panes Fila|Letras no player com parse LRC + linha ativa + auto-scroll. Execução em 3 lanes paralelas (backend 1+2, frontend app.js, CSS) com contratos fixados abaixo.

**Tech Stack:** Python 3.12 + FastAPI + pytest (mock urllib); vanilla JS (app.js ~3995 linhas); CSS com variáveis de tema.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-08-musicbox-fase4-lrc-design.md` (aprovada pelo usuário; seguir exatamente arquitetura/rotas/contratos).
- NENHUMA dependência nova em requirements.txt (usar `urllib` da stdlib).
- Falha/ausência de letra NUNCA bloqueia download (try/except + log debug).
- CSS: zero hex novo (só variáveis `--text`, `--text-dim`, `--accent`, `--accent-soft`, `--surface-2`, `--border`, `--hover`, `--on-accent`, `--text-faint`); funciona nos 2 temas (claro Polido + dark Analog).
- Suíte pytest baseline: **147 passed** — rodar completa ao final.
- `node --check app/static/app.js` obrigatório ao final do frontend.
- NÃO commitar código (padrão das fases anteriores; só specs são commitados).

---

### Task 1: Módulo `app/lyrics.py` + testes (backend — lane 1)

**Files:**
- Create: `app/lyrics.py`
- Test: `tests/test_lyrics.py`

**Interfaces:**
- Produces: `fetch_lrc(artist: str, title: str, album: str | None = None) -> str | None` — retorna LRC (com timestamps) quando existe `syncedLyrics`; LRC estático (texto) quando só `plainLyrics`; `None` quando sem match ou qualquer erro (rede/timeout/HTTP != 200/JSON inválido). Nunca levanta exceção.

**Steps:**
- [ ] **1. Escrever testes que falham** em `tests/test_lyrics.py` (mock `urllib.request.urlopen` via monkeypatch + `unittest.mock`). Casos: (a) hit com `syncedLyrics` → retorna o LRC com timestamps; (b) só `plainLyrics` → retorna o texto; (c) lista vazia → `None`; (d) HTTP 500 → `None`; (e) `urllib.error.URLError` → `None`; (f) URL montada com query codificada (`artist_name=Daft+Punk`, `track_name` com `%28feat.+X%29`, `album_name` presente quando passado); (g) sem `album` → `album_name` ausente na URL.
- [ ] **2. Rodar e ver falhar**: `pytest tests/test_lyrics.py -q` → FAIL (módulo não existe).
- [ ] **3. Implementar** `app/lyrics.py`: constante `LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"`, `_TIMEOUT = 5`, logger `musicbox.lyrics`; monta `urllib.parse.urlencode` com `artist_name`/`track_name`/`album_name` (só se album); `urllib.request.urlopen(url, timeout=_TIMEOUT)`; resp.status != 200 → None; `json.loads(resp.read().decode("utf-8", "replace"))`; lista → 1º hit dict com `syncedLyrics` truthy, senão 1º com `plainLyrics` truthy; qualquer exceção → log debug + None. (Implementação completa no spec, seção "Backend / Novo módulo".)
- [ ] **4. Rodar e ver passar**: `pytest tests/test_lyrics.py -q` → 7 passed.
- [ ] **5. py_compile + smoke** (opcional, rede real): `.venv/bin/python -c "from app.lyrics import fetch_lrc; print(fetch_lrc('Daft Punk', 'One More Time'))"` — pode demorar ou retornar None, sem erro. NÃO commitar.

---

### Task 2: Downloader grava `.lrc` + rota + delete irmão (backend — lane 1, continuação)

**Files:**
- Modify: `app/downloader.py` (`_run`, ~L544-562 — após `shutil.move(temp_file, dest_final)` bem-sucedido, antes do notify done)
- Modify: `app/main.py` (rota nova; `delete_history` ~L666)
- Test: `tests/test_downloader.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: `fetch_lrc(artist, title, album) -> str | None` (Task 1); `history.get(yt_id)` (existe); `DownloadTask` com `.artist/.title/.album` (existe).
- Produces: rota `GET /api/library/{yt_id}/lyrics` (com `Depends(require_auth)`) → `200` PlainTextResponse (conteúdo do `.lrc`) | `404 {"detail": "Sem letra"}` (sem registro, sem path ou `.lrc` inexistente). `delete_history` também remove `Path(file_path).with_suffix(".lrc")` (`unlink(missing_ok=True)` no mesmo try do áudio).

**Steps:**
- [ ] **1. Testes que falham** em `tests/test_downloader.py` (padrão do executor fake existente, ex.: `test_download_ok`): (a) `monkeypatch.setattr(dl_mod, "fetch_lrc", fake)` retornando `"[00:01.00]oi"` → após o download done, `dest_final.with_suffix(".lrc")` existe com esse conteúdo e `fetch_lrc` foi chamado com `(task.artist, task.title, task.album)`; (b) `fetch_lrc` retornando `None` → download done e NENHUM `.lrc` criado.
- [ ] **2. Rodar e ver falhar**: `pytest tests/test_downloader.py -q` → 2 novos FAIL.
- [ ] **3. Implementar** em `downloader.py`: import `from app.lyrics import fetch_lrc`; no `_run`, após o `shutil.move` e antes do notify done: `try: lrc = fetch_lrc(task.artist or "", task.title or "", task.album); if lrc: dest_final.with_suffix(".lrc").write_text(lrc, encoding="utf-8") except Exception as exc: logger.debug(...)`.
- [ ] **4. Testes da rota** em `tests/test_main.py` (client de teste existente): (a) 200 com texto e content-type text/plain (registro com path, `.lrc` criado); (b) 404 sem arquivo `.lrc`; (c) 404 sem registro; (d) `DELETE /api/history/{yt_id}` remove o `.lrc` irmão.
- [ ] **5. Implementar rotas** em `main.py` (conforme spec): rota `get_lyrics` (history.get → 404 sem registro/path → 404 sem `.lrc` → PlainTextResponse) e o `unlink(missing_ok=True)` do irmão em `delete_history`. Confirmar imports de `PlainTextResponse`/`JSONResponse` (existem).
- [ ] **6. Rodar**: `pytest tests/test_main.py tests/test_downloader.py tests/test_lyrics.py -q` → tudo verde.
- [ ] **7. Suíte completa**: `.venv/bin/python -m pytest -q` → **151 passed** (147 + 4 novos). NÃO commitar.

---

### Task 3: Frontend — panes Fila|Letras + karaokê (lane 2, app.js)

**Files:**
- Modify: `app/static/app.js` (API ~L31-35; estado ~L100-115; `playerViewHtml` ~L2753-2797; `bindPlayerViewEvents`; `updateSeekUI`/timeupdate ~L2621; `startPlayer`)

**Interfaces:**
- Consumes: rota `GET /api/library/{yt_id}/lyrics` (200 text/plain | 404); CSS `.player-pane-switch/.pane-btn/.lyric-line/.lyrics-empty` (Task 4 — o JS emite os elementos, o CSS os estiliza; classes acordadas: `.pane-btn[data-pane]` + `.is-active`, `#player-lyrics`, `.lyric-line` + `.is-active`, `.lyrics-empty`).
- Produces: estado `state.playerPane = 'queue' | 'lyrics'` (default `'queue'`); markup do pane switch; `#player-lyrics` com linhas; entrada `API.lyrics(yt_id)`.

**Steps:**
- [ ] **1. Markup**: em `playerViewHtml()`, no cabeçalho do player (junto do "AGORA TOCANDO"/acesso à fila): `.player-pane-switch` (role=tablist, aria-label "Visão do player") com 2 `.pane-btn` role=tab — `Fila` (data-pane="queue", is-active, aria-selected=true) e `Letras` (data-pane="lyrics", aria-selected=false). Área da fila existente fica em `#player-queue`; nova `#player-lyrics` (hidden quando pane != lyrics).
- [ ] **2. API + estado**: `API.lyrics: (yt_id) => \`/api/library/${encodeURIComponent(yt_id)}/lyrics\``; `state.playerPane = 'queue'`; `state._lyricsCache = null`; `state._lyricsFor = null` (yt_id do cache).
- [ ] **3. Parse LRC**: `parseLrc(text)` → `{timed: [{time, text}], plain: [string]}` — regex por linha `^\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\](.*)$`; `time = mm*60 + ss + frac` (frac: 1 dígito → /10, 2 → /100, 3 → ms); linhas sem timestamp → `plain`; nenhuma linha com timestamp → modo estático.
- [ ] **4. Render**: `renderLyricsPane(yt_id)` — fetch `API.lyrics(yt_id)`; 200 → `parseLrc` → `.lyric-line` por entrada timed (ou parágrafos estáticos); 404 → `.lyrics-empty` "Sem letras para esta faixa"; erro → `.lyrics-empty` + botão "Tentar novamente" (re-chama). Guard: só renderiza se a faixa atual ainda é `yt_id` e `state.playerPane === 'lyrics'`.
- [ ] **5. Karaokê**: no handler de `timeupdate` (onde chama `updateSeekUI`): se pane lyrics e cache timed → `syncLyricLine(currentTime)` — remove `.is-active` de todas, marca a última com `time <= currentTime`; auto-scroll `scrollIntoView({block:'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth'})` SÓ quando a linha ativa mudou (guard de mudança para não scrollar a cada tick). Chamar `renderLyricsPane(yt_id atual)` no `startPlayer` (troca de faixa) e no clique do pane Letras quando cache desatualizado.
- [ ] **6. Bind**: em `bindPlayerViewEvents`, clique nos `.pane-btn` → alterna `state.playerPane`, classes `.is-active`/`aria-selected`, visibilidade de `#player-queue`/`#player-lyrics`.
- [ ] **7. Verificação**: `node --check app/static/app.js` → SYNTAX OK; smoke DOM stub (padrão das fases anteriores): parseLrc (timed/estático/linha sem timestamp), renderLyricsPane (200/404/erro+retry, guard de faixa/pane), syncLyricLine (linha ativa correta, scroll só na mudança), panes (troca, aria-selected, cache por yt_id). NÃO commitar.

---

### Task 4: CSS — pane switch + letras (lane 3, styles.css)

**Files:**
- Modify: `app/static/styles.css` (bloco "Fase 4 — letras", antes dos media queries finais ~L2313)

**Interfaces:**
- Consumes: markup do Task 3 (`.player-pane-switch`, `.pane-btn[data-pane]`, `#player-lyrics`, `.lyric-line`, `.lyrics-empty`).
- Produces: estilos visíveis nos 2 temas (variáveis apenas).

**Steps:**
- [ ] **1. Regras** (padrão dos `.segment-btn` da biblioteca; variáveis; zero hex):
  - `.player-pane-switch { display:flex; gap:8px; margin:0 0 12px; }`
  - `.pane-btn` — pílula: padding 6px 14px, radius 999px, borda `--border`, transparente, `--text-dim`, font `--font`, 0.8rem, cursor pointer; hover `--hover`; `.is-active` → bg `--accent`, borda `--accent`, cor `--on-accent`, weight 700.
  - `#player-lyrics { max-height: 42vh; overflow-y: auto; padding: 4px 2px; }`
  - `.lyric-line` — `--text-dim`, padding 5px 10px, radius 8px, transition color/background .2s, line-height 1.5; `.is-active` → `--text`, bg `--accent-soft`, weight 700, border-left 3px `--accent`.
  - `.lyrics-empty` — `--text-faint`, italic, padding 16px 8px, center, 0.9rem.
- [ ] **2. Verificação**: chaves CSS pareadas (`{` == `}`); grep das classes novas (5+ ocorrências); zero hex novo no bloco; variáveis usadas existem nos 2 blocos de tema (`:root` e `html[data-theme="dark"]`). NÃO commitar.

---

## Self-Review (do orquestrador)

**Cobertura do spec:** módulo lyrics (T1) ✅ · downloader grava .lrc (T2) ✅ · rota lyrics (T2) ✅ · delete irmão (T2) ✅ · panes Fila|Letras (T3) ✅ · parse LRC + karaokê + auto-scroll (T3) ✅ · estados vazio/erro/retry (T3) ✅ · CSS (T4) ✅ · testes backend (T1/T2) ✅ · faixas antigas sem letra (T3: 404 → estado vazio) ✅.
**Contratos consistentes:** `fetch_lrc` assinatura idêntica em T1/T2; rota e códigos 200/404 idênticos em T2/T3; classes CSS idênticas em T3/T4. Sem placeholders.
