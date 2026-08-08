# Fase 5 — Downloads Resilientes: retry automático e fila persistente

Data: 2026-08-08

## Objetivo

Fechar o item do escopo original do usuário: quando a conexão cai (ex.: Wi-Fi → 4G), os downloads devem ser **retomados automaticamente do ponto exato de interrupção**, sem ação manual. Requisitos do usuário (verbatim): "Se a conexão cair ou o usuário mudar de Wi-Fi para dados móveis, garanta suporte a HTTP Range Headers para retomar o download do exato ponto de interrupção" + decisões desta rodada: retry **backend + frontend**, **fila persistida no SQLite**, **3 tentativas com backoff 5s/30s/2min**.

## Decisões aprovadas

1. **Retry automático backend + frontend**:
   - Backend re-enfileira tasks que falharam por **erro de rede** (URLError/Timeout/5xx, mesmos tokens de `_is_network_error` do ytdlp_client) com backoff 5s → 30s → 2min (3 tentativas); após a 3ª, `failed` definitivo.
   - Frontend detecta `navigator.onLine`/eventos `online`/`offline`, mostra indicador de conexão, e ao voltar online dispara refresh + retry-failed automático (melhor esforço; o backend retenta sozinho de qualquer forma).
2. **Fila persistente**: estado da fila salvo em tabela nova `download_queue` no `history.db` (SQLite); no startup do servidor, a fila é restaurada — `pending`/`downloading` re-enfileiradas (o `.part` garante resume do ponto exato), `paused` restauradas pausadas.
3. **Erros não-rede (4xx, `SearchError`) continuam falhando na hora**, como hoje.

## Backend (app/downloader.py + app/history.py)

- **Downloader**:
  - Task ganha metadados de retry: `_retry_count: int` e `_retry_ts: float | None` (monotonic); task_id segue `{yt_id}` (padrão atual).
  - **Scheduler de retry**: nova daemon thread `_retry_scheduler` (iniciada no `start()`, encerrada no `stop()`; loop a cada 1s verificando tasks com `status == "failed"` + `_retry_ts is not None` + `time.monotonic() >= _retry_ts` → chama `resume([task_id])` — o `resume()` da Fase 2 já re-enfileira a MESMA task com `cancel_requested=False` e `progress=0`; o `.part` do yt-dlp continua o download do ponto exato).
  - **Classificação**: no `_fail`/except do `_run`, se a exceção for erro de rede (checagem equivalente a `ytdlp_client._is_network_error`, exposta como helper público `is_network_error(exc)` em ytdlp_client e importada pelo downloader) e `task._retry_count < 3` → em vez de `failed` terminal: `status="failed"`, `_retry_count += 1`, `_retry_ts = monotonic() + RETRY_DELAYS[_retry_count-1]` (5s/30s/120s), notifica WS (o card mostra "Reconectando — tentativa N/3"), persiste na fila. Senão `failed` definitivo.
  - **Persistência**: ao `enqueue`/`enqueue_album`: upsert na tabela `download_queue` (task_id, yt_id, title, artist, album, format, status, progress, retry_count, created_at); a cada mudança de status terminal/paused/pending: update; no `start()`: restaura do banco — `pending`/`downloading` → re-enfileirar (via mecanismo do `resume`), `paused` → marcar pausadas (sem enfileirar), `failed` com `retry_count < 3` → agenda retry com `_retry_ts = monotonic()` (imediato, 1ª nova tentativa). Tasks `done`/`cancelled`/`skipped` não são restauradas.
  - Contratos preservados: `start/stop/enqueue/enqueue_album/get/snapshot/add_listener/remove_listener/cancel/resume/pause/cleanup_partials`.
- **app/ytdlp_client.py**: expor `is_network_error(exc) -> bool` (renomear `_is_network_error` com alias ou wrapper; os usos internos continuam).
- **app/history.py**: helpers `queue_upsert(row: dict)`, `queue_update_status(task_id, status, progress)`, `queue_load() -> list[dict]` (tabela `download_queue`, criada no `init_db` com migração idempotente `CREATE TABLE IF NOT EXISTS`), `queue_clear_terminal()` (limpeza de terminais persistidos, chamada na poda).
- **API (app/main.py)**: `GET /api/downloads` já expõe `snapshot()` — tasks ganham `retry_count`/`next_retry_in` (derivado, para o frontend mostrar "Tentativa 2/3"). `POST /api/downloads/retry-failed` inalterado (funciona em cima de failed; o scheduler cobre os retries de rede). Sem rotas novas.

## Frontend (app/static/app.js)

- Constantes: `OFFLINE_BANNER_ID = 'conn-banner'`; estado `state.online = navigator.onLine`.
- **Indicador de conexão**: banner fixo no topo ("Você está offline — downloads pausados") mostrado quando `offline`, escondido quando `online`; eventos `window.addEventListener('online'|'offline')`; ao voltar: `refreshDownloads()` + se `state.tasks` tem algum `failed` com `retry` pendente → `retryFailedApi()` (existe) + toast "Conexão restaurada — retomando downloads".
- **Task card**: quando `task.retry_count > 0` e status `failed`, badge "Reconectando · tentativa {retry_count}/3" (estado transitório; o scheduler do backend vai mudar para pending/queued sozinho via WS).
- Snapshot/fila: sem mudanças estruturais (o WS já re-renderiza).

## CSS (app/static/styles.css)

- `.conn-banner` (fixo no topo, warning tint via color-mix, texto caps, z-index acima do header), `.badge-retry` (warning, padrão dos `.badge-*` existentes). Zero hex novo (só variáveis).

## Fora de escopo

- Widget/TWA (avaliado inviável em PWA puro — documentado).
- Backfill de letras para faixas antigas.
- HTTP Range no servidor de áudio (já suportado via FileResponse do Starlette — verificado na auditoria).
- Persistência de tasks `done` no banco de fila (histórico já guarda).

## Testes

- **test_downloader.py**: retry por rede agenda `_retry_ts` e re-enfileira (fake executor com erro de rede); erro não-rede NÃO retenta; 3 tentativas → failed definitivo; scheduler re-enfileira quando o tempo chega (relógio fake); restore no startup (pending/downloading → re-enfileiradas, paused → pausadas, failed retry_count<3 → agenda).
- **test_history.py**: queue_upsert/update/load/clear (criação idempotente da tabela).
- **test_main.py**: snapshot expõe retry_count/next_retry_in; retry-failed inalterado (regressão).
- **test_ytdlp_client.py**: `is_network_error` público (4xx False, rede True).
