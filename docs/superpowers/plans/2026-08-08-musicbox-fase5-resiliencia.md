# Fase 5 — Downloads Resilientes: retry automático e fila persistente

> **For agentic workers:** implemente task por task. Especificação completa em `docs/superpowers/specs/2026-08-08-musicbox-fase5-resiliencia-design.md` (leia antes).

**Goal:** Retry automático com backoff (5s/30s/2min, 3 tentativas) para downloads que falham por erro de rede + fila de downloads persistida no SQLite que sobrevive a restart do servidor.

**Architecture:** Downloader ganha um scheduler de retry (daemon thread) que re-enfileira tasks `failed` por rede via o `resume()` existente (que já re-enfileira a MESMA task; o `.part` do yt-dlp garante o resume do ponto exato). Fila persistida em tabela nova `download_queue` no history.db; startup restaura pending/downloading/paused/failed-retry. Frontend mostra banner offline/online e dispara retry-failed ao reconectar.

**Tech Stack:** Python 3.12 + FastAPI + SQLite (stdlib sqlite3), vanilla JS, pytest (160 baseline).

## Global Constraints

- NÃO commitar (usuário só pediu commits no início; esta fase segue sem commit até pedido).
- Nomes de funções/classes preservados: `Downloader.start/stop/enqueue/enqueue_album/get/snapshot/add_listener/remove_listener/cancel/resume/pause/cleanup_partials`.
- Erros não-rede (4xx/SearchError) continuam falhando na hora (nunca retry).
- `is_network_error` (novo, público em ytdlp_client) deve classificar: 4xx → False; URLError/Timeout/5xx → True (mesma lógica do `_is_network_error` atual).
- Testes: baseline 160; o fake executor existente nos testes deve suportar simular erro de rede.
- CSS: zero hex novo (só variáveis do tema); JS: node --check obrigatório; Python: py_compile + pytest.
- 1 lane por arquivo (backend toca app/*.py + testes correspondentes; frontend só app/static/app.js; CSS só app/static/styles.css).

---

### Task 1: Backend — retry com backoff + fila persistida

**Files:**
- Modify: `app/ytdlp_client.py` (expor `is_network_error`)
- Modify: `app/history.py` (tabela + helpers da fila)
- Modify: `app/downloader.py` (scheduler, classificação, persistência, restore)
- Test: `tests/test_ytdlp_client.py`, `tests/test_history.py`, `tests/test_downloader.py`, `tests/test_main.py`

**Interfaces (contrato entre lanes):**
- `app/ytdlp_client.py`: `is_network_error(exc) -> bool` — alias público da função interna existente `_is_network_error` (ex.: `is_network_error = _is_network_error` logo após a definição; usos internos inalterados).
- `app/history.py`:
  - `init_db()` passa a criar `CREATE TABLE IF NOT EXISTS download_queue (task_id TEXT PRIMARY KEY, yt_id TEXT NOT NULL, title TEXT DEFAULT '', artist TEXT DEFAULT '', album TEXT DEFAULT '', format TEXT DEFAULT '', status TEXT NOT NULL, progress REAL DEFAULT 0, retry_count INTEGER DEFAULT 0, created_at TEXT NOT NULL)` (idempotente, junto das migrações existentes).
  - `queue_upsert(row: dict) -> None` — INSERT OR REPLACE com as chaves acima; try/except log warning (nunca quebra download).
  - `queue_update_status(task_id: str, status: str, progress: float | None = None) -> None` — UPDATE status/progress; try/except log.
  - `queue_load() -> list[dict]` — SELECT * ORDER BY created_at; try/except → [].
  - `queue_delete(task_id: str) -> None` — DELETE; try/except log.
- `app/downloader.py`:
  - Constantes: `_RETRY_DELAYS = (5.0, 30.0, 120.0)` e `_RETRY_SCHEDULER_INTERVAL = 1.0`.
  - `__init__`: `self._retry_scheduler_stop = threading.Event()`, `self._retry_scheduler_thread: threading.Thread | None = None`.
  - Task interna (dict criado em `_register`): campos novos `_retry_count: int = 0` e `_retry_ts: float | None = None` (time.monotonic).
  - `start()`: (1) antes de iniciar workers, chamar `self._restore_queue()` (ver abaixo); (2) iniciar thread `self._retry_scheduler_loop` (daemon) com o Event de stop.
  - `_restore_queue()`: para cada row de `history.queue_load()`: `status == "paused"` → recriar task (via `_register` com os campos persistidos + `_retry_count` da row) marcada `paused`/`cancel_requested=True`, SEM enfileirar; `status in ("pending", "downloading")` → recriar task e enfileirar (mesmo caminho do `resume`: `_queued` check + `_queue.put`), `_retry_count` preservado; `status == "failed"` e `retry_count < 3` → recriar task failed com `_retry_ts = time.monotonic()` (retry imediato na 1ª varredura); demais status → `queue_delete`.
  - `enqueue()`/`enqueue_album()`: após criar a task, `history.queue_upsert({task_id, yt_id, title, artist, album, format, status, progress: 0, retry_count: 0, created_at: agora-utc})`.
  - Mudanças de estado que persistem: transições para `done/skipped` → `queue_delete`; `cancelled` → `queue_delete`; `paused` → `queue_update_status(id, "paused")`; `failed` → `queue_update_status(id, "failed", progress)` + quando com retry pendente persistir também `retry_count` (via `queue_upsert` com os campos da task). Onde: nos pontos onde o status da task muda (`_run`/`_fail`/`cancel`/`pause`).
  - **Classificação no `_run`** (except genérico, onde hoje decide `_fail`): se `is_network_error(exc)` (import de app.ytdlp_client) e `task._retry_count < 3`: `task.status = "failed"`, `task.stage = "failed"`, `task._retry_count += 1`, `task._retry_ts = time.monotonic() + _RETRY_DELAYS[task._retry_count - 1]`, `task.error = "network"`, persistir, notify WS (payload normal de failed + campos retry) e NÃO chamar `_fail` (a task fica "failed com retry pendente"; o scheduler a re-enfileira). Se `_retry_count >= 3` → `_fail` normal (failed definitivo + `queue_update_status`/delete). O comportamento de `cancel_requested`/`paused` existente NÃO muda (checado antes).
  - `_retry_scheduler_loop()`: while not stop_event.is_set(): varrer `self._tasks` (sob `self._lock`): tasks com `status == "failed"` e `_retry_ts is not None` e `time.monotonic() >= _retry_ts` → coletar ids; fora do lock: `self.resume([task_id])` para cada (o resume já trata `_queued`/lock/persistência). `stop_event.wait(_RETRY_SCHEDULER_INTERVAL)` no fim do loop.
  - `stop()`: `self._retry_scheduler_stop.set()` + `join(timeout=2)` no thread do scheduler (junto do desligamento existente).
  - `snapshot()`: cada item ganha `retry_count: task._retry_count` e `next_retry_in: round(max(0, task._retry_ts - time.monotonic())) if task._retry_ts else None` (float | None, segundos).
- `app/main.py`: NENHUMA mudança (snapshot já propaga; `POST /api/downloads/retry-failed` inalterado).

**Testes novos (rodar com `.venv/bin/python -m pytest tests/test_ytdlp_client.py tests/test_history.py tests/test_downloader.py tests/test_main.py -q`):**
- test_ytdlp_client: `is_network_error` público — 404 → False, 503 → True, URLError → True (reusar padrão do fake existente).
- test_history: queue_upsert/load/update/delete round-trip; init_db cria tabela idempotente (2× init não falha).
- test_downloader (fake executor que lança URLError):
  1. `test_falha_rede_agenda_retry_nao_failed_definitivo` — task vira failed com `_retry_count == 1`, `_retry_ts` futuro, `next_retry_in > 0` no snapshot, history persistido.
  2. `test_retry_scheduler_reenfileira_no_tempo` — relógio fake (monkeypatch `time.monotonic` e `downloader.time.monotonic`) avança além do delay → `resume` chamado (task volta pending e vai para a fila).
  3. `test_erro_nao_rede_nao_retenta` — exceção 4xx/DownloadError não-rede → failed definitivo, `_retry_count == 0`.
  4. `test_3_tentativas_falham_failed_definitivo` — 3 ciclos de rede → failed definitivo (resume não é mais chamado).
  5. `test_restore_queue_no_startup` — tabela com pending/paused/failed<3 → start() restaura (pending enfileirada e tocável; paused pausada; failed com _retry_ts imediato).
- test_main: snapshot do `/api/downloads` inclui chaves `retry_count`/`next_retry_in` (regressão).

**Verificação:** py_compile nos 4 arquivos; pytest dos 4 arquivos; suíte completa 160 + novos.

---

### Task 2: Frontend — indicador de conexão + auto-retry

**Files:** Modify: `app/static/app.js` · Test: node --check + smoke DOM stub

**Interfaces (consuma do backend):** task do snapshot/WS pode ter `retry_count: int` e `next_retry_in: float | None`; `POST /api/downloads/retry-failed` já existe (helper do app: a função usada pelo botão "Retentar Falhas").

**Implementar:**
- Constante `OFFLINE_BANNER_ID = 'conn-banner'`; estado `state.online = navigator.onLine`.
- `updateConnBanner()`: se `!state.online` → garante `<div id="conn-banner" role="status">Você está offline — os downloads serão retomados automaticamente.</div>` como primeiro filho do body com classe `is-visible`; se online → remove/hide (remove o elemento ou remove `is-visible`).
- No `init`: `updateConnBanner()`; `window.addEventListener('online', onNetworkOnline)` e `('offline', onNetworkOffline)`; `onNetworkOnline`: `state.online = true`, esconder banner, toast "Conexão restaurada — retomando downloads", `refreshDownloads()`, e se a lista de tasks (state.tasks) tiver alguma com `status === 'failed'` → chamar o helper de retry-failed existente (o do botão "Retentar Falhas"); `onNetworkOffline`: `state.online = false`, mostrar banner.
- `taskCardHtml()` (branch `failed`): se `task.retry_count > 0` → no lugar do badge "ERRO" mostrar `<span class="badge badge-retry">Reconectando · tentativa {task.retry_count}/3</span>` (o backend muda para pending sozinho; o WS re-renderiza).
- Guards: `navigator.onLine` pode ser undefined em alguns ambientes → `state.online = typeof navigator !== 'undefined' ? navigator.onLine : true`.

**Verificação:** `node --check`; smoke DOM stub: banner criado quando offline (evento `offline` disparado), removido no `online`, toast, auto-retry-failed chamado quando há failed, taskCardHtml com retry_count>0 → badge-retry, sem regressão nos 55+ checks existentes (rodar o smoke da Fase 2/3 se disponível em /tmp/opencode — senão recriar o essencial).

---

### Task 3: CSS — banner de conexão + badge de retry

**Files:** Modify: `app/static/styles.css`

**Implementar (na seção de badges/estados, junto dos `.badge-*` existentes):**
- `#conn-banner`: `position: fixed; top: 0; left: 0; right: 0; z-index: 999; display: none;` + `#conn-banner.is-visible { display: block; }`; fundo warning tint (`color-mix(in srgb, var(--warning) 18%, var(--surface))`), texto `var(--text)` caps 0.75rem, padding 8px 16px, text-align center, border-bottom `1px solid var(--border)`.
- `.badge-retry`: mesmo padrão dos `.badge-*` existentes (uppercase 11px 800, pill) com `color: var(--warning)` + `background: color-mix(in srgb, var(--warning) 14%, transparent)`.
- Verificações obrigatórias: chaves CSS pareadas; zero hex novo; grep das classes novas; não commitar.
