# Design — Fase 2: Downloads & Armazenamento

Data: 2026-08-08
Status: aprovado pelo usuário

## Objetivo

Evoluir a aba Downloads do MusicBox com gerenciamento de armazenamento (servidor + dispositivo), pausa/retomada de downloads (individual e em lote) com resume real via `.part`, alerta de disco cheio e gestos de swipe nas listas de músicas. A reprodução via HTTP Range já funciona (FileResponse do Starlette serve 206) — sem mudança.

## Decisões de produto (do usuário)

- Storage manager mostra **3 visões**: disco do servidor (total/livre/ocupado + tamanho da biblioteca + `.part` órfãos), tamanho da biblioteca e quota do dispositivo/PWA (cache offline).
- Ações incluídas: **limpar `.part` órfãos**, **limpar cache do dispositivo**, **alerta de disco cheio**.
- Pausa/retomada modelada com **novo estado `paused`** no backend (distinto de `cancelled`).

## 1. API (backend)

### GET /api/storage
Resposta:
```json
{
  "disk": {"total": 0, "used": 0, "free": 0},   // shutil.disk_usage(musicbox_dir)
  "library_size": 0,                            // soma de tamanhos sob musicbox_dir (arquivos completos)
  "partials_size": 0,                           // soma de *.part/*.ytdl sob musicbox_dir
  "partials_count": 0
}
```
- `library_size`/`partials_size`: um walk único sobre `musicbox_dir`, separando por sufixo (`_PARTIAL_SUFFIXES` = `.part`, `.ytdl`, já definido no downloader). Barato o suficiente para chamada sob demanda (refresh manual + ao abrir a aba).

### POST /api/storage/cleanup
- Chama `Downloader.cleanup_partials()` (já existe, usado no startup).
- Resposta: `{"removed": N, "freed_bytes": X}` — medir tamanho antes/depois no walk.
- Requer auth (mesma regra das demais rotas de escrita).

### POST /api/downloads/pause e POST /api/downloads/resume
- Body opcional: `{"task_ids": ["..."]}`. Se ausente/vazio: pause → todas as tasks `pending`/`running`; resume → todas as `paused`.
- **pause**: `Downloader.pause(task_ids) -> {"paused": [task_id...]}` — marca `status = "paused"`, `stage = "paused"`, `cancel_requested = True` (aborta o download em andamento no progress hook), **preserva o `.part`** (o cancel já preserva; `cleanup_partials` só roda no startup — confirmado). Notifica via WS (`type: update`).
- **resume**: `Downloader.resume(task_ids) -> {"resumed": [task_id...]}` — re-enfileira via lógica do `enqueue` (dedupe com `_find_active`): a task volta `pending` e o yt-dlp **retoma do ponto exato** (resume nativo com `.part` existente + mesma outtmpl/formato). Notifica via WS.
- Rotas single: `POST /api/downloads/{task_id}/pause` e `POST /api/downloads/{task_id}/resume` (mesmos handlers, `task_ids=[task_id]`).
- Erros: task inexistente → 404; task em estado incompatível (pause numa `done`, resume numa não-pausada) → 409 com `{"detail": "..."}`.
- **Histórico**: `History.mark(yt_id, "paused")` na pausa (novo status no banco — o campo é string livre). `resume` **não** toca o histórico (o worker marca `running`/`done` no fluxo normal; o `mark("pending")` do enqueue existente segue). Nota: após restart do processo, tasks `paused` viram `pending` no histórico e `.part` é limpo pelo startup — comportamento aceito (documentar no spec; sem persistência de fila entre restarts, consistente com o T5 existente).

### Impacto no Downloader (app/downloader.py)
- `_TERMINAL_STATUSES` (L40) passa a incluir `"paused"` **apenas** para a poda de `_tasks` (tasks pausadas podem ser podadas após o TTL como as terminais — a retomada re-enfileira do histórico via `enqueue`).
- `_worker_loop`/`_run`: um worker que pega uma task já marcada `paused` descarta (mesmo padrão do `cancelled`).
- `retry_failed_downloads` (main.py): filtra `paused` (não são falhas).
- `cancel()` não muda (semântica separada: cancelar = desistir, mantém `.part` até cleanup).
- `snapshot()`/WS: `paused` aparece como status normal (frontend já renderiza status dinâmicos).

## 2. Frontend (app/static/app.js + styles.css + index.html)

### Aba Downloads — seção "Armazenamento"
- Novo bloco no topo da aba (abaixo do header da aba, antes da lista de tasks):
  - **Servidor**: barra de uso do disco (usado/livre + %), linha com "Biblioteca: X · Órfãos (.part): Y" e botão **"Limpar órfãos"** (chama `POST /api/storage/cleanup`, atualiza números, toast de confirmação).
  - **Dispositivo**: quota do PWA (`navigator.storage.estimate()`) + tamanho do `AUDIO_CACHE` (métricas já usadas em `evictAudioCache` — extrair helper reutilizável `measureAudioCache()`) e botão **"Limpar cache"** (`caches.delete(AUDIO_CACHE)`, atualiza).
  - Dados de servidor buscados via `GET /api/storage` ao abrir a aba e num botão de refresh; dados de dispositivo calculados no client.
- **Alerta de disco cheio**: se `disk.free < max(0.10 * disk.total, 2GB)` → banner de aviso no topo da aba ("Pouco espaço no servidor — X livres") e toast de aviso ao iniciar download. Constante `DISK_LOW_RATIO = 0.10`, `DISK_LOW_MIN_BYTES = 2GB`.
- Classes CSS seguindo a linguagem Analog existente (`.storage-card`, `.storage-bar`, `.storage-fill`, `.storage-row`, `.storage-actions`, `.storage-warn`), variáveis de tema já existentes; dark e light.

### Pausa/retomada na UI
- Task com `status === "paused"`: badge "Pausado" (reusa estilo de badge com cor `--warning`), ações **Retomar** e **Cancelar** (linha); estado com `.is-paused` no card da task.
- Botões em lote no header da seção de tasks (visíveis quando há tarefas relevantes): **"Pausar tudo"** (ativas) e **"Retomar todos"** (pausadas) — confirmar antes de pausar tudo (pausa é destrutiva para o progresso em memória do worker? não: `.part` preserva; sem confirm).
- Toasts: "Download pausado", "Download retomado (continua de X%)", contagens em lote.
- WS `update` já repassa status novo (sem mudança de protocolo).

### Swipe nas listas (histórico + biblioteca)
- Alvo: linhas do histórico (`renderHistory`) e `.lib-row` (biblioteca). Não nas filas do player nem nos cards de busca (já têm ações dedicadas).
- Gesto: `pointerdown`/`pointermove`/`pointerup` com captura; limiar horizontal (≥ 60px) e dominância horizontal (|dx| > |dy|); deslocamento visual da linha (transform translateX) com retorno suave; sem conflito com clique (clique só se o movimento não ultrapassar o limiar).
- **Esquerda** → adicionar à fila do player (mesmo handler do botão de fila existente, `addToQueue`/equivalente) + toast + badge de contagem.
- **Direita** → download rápido (mesmo fluxo do `.dl-btn`/`downloadSingleTrack`, com feedback 3 estados) + toast.
- Classes: `.row-swipe`/`.swipe-actions-l`/`.swipe-actions-r` (reveal sob a linha durante o gesto, ícones seta-lista/baixar), `prefers-reduced-motion` respeitado.
- Teclado/AC: a ação continua acessível por botões/botão de fila já existentes (swipe é aceleração, não substituição).

## 3. HTTP Range / resume de conexão
- Reprodução: já suportado (FileResponse 206) — nenhuma mudança.
- Download interrompido por queda de rede: coberto pelo pause/resume via `.part` (o yt-dlp retoma do ponto exato; o `retry_delay`/backoff existentes cobrem falhas 5xx).

## Fora de escopo (fases futuras)
- Fase 3: player rico (vinil girando/tonearm, bottom-sheet, Media Session, crossfade, gapless).
- Fase 4: letras LRC, scan da biblioteca local, widget de tela inicial.

## Verificação
- Backend: pytest com casos novos — `GET /api/storage` (shape + walk), `POST /api/storage/cleanup` (remove `.part` fake, `freed_bytes`), pause/resume (single + lote + 404/409 + status no snapshot + `.part` preservado + resume re-enfileira), `retry_failed` ignora paused, poda inclui paused. Baseline: 136 passed.
- Frontend: `node --check` + smoke DOM stub; smoke playwright real (uvicorn 8099): abrir Downloads → storage renderiza, pausar/retomar uma task fake, alerta de disco aparece com mock.
- Visual: screenshot + vision (abas e estados no tema claro e dark).
- NÃO commitar ao final (usuário não pediu); spec commitado separadamente.
