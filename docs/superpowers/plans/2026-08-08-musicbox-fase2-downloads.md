# Fase 2: Downloads & Armazenamento — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar gerenciamento de armazenamento (servidor + dispositivo), pausa/retomada de downloads com resume via `.part`, alerta de disco cheio e gestos de swipe nas listas do MusicBox.

**Architecture:** Backend FastAPI ganha 4 rotas novas (`GET /api/storage`, `POST /api/storage/cleanup`, pause/resume por task ou lote) com novo status `paused` no `Downloader`; o frontend vanilla JS ganha seção de armazenamento na aba Downloads, ações de pausa/retomada e swipe com pointer events. Resume real: pause preserva o `.part` e o re-enqueue faz o yt-dlp continuar do ponto exato.

**Tech Stack:** Python 3.12 + FastAPI, SQLite (history), vanilla JS + CSS (tema Analog claro/dark), pytest, playwright (smoke).

## Global Constraints

- Contrato de classes do tema (spec Fase 1): `html[data-theme="dark"]`, variáveis `--bg/--surface/--accent/--success/--warning/--error/--info` já definidas — NOVAS classes CSS usam apenas essas variáveis (nada de hex hardcoded).
- `_PARTIAL_SUFFIXES = {".part", ".ytdl"}` (app/downloader.py:33) — reusar, não redefinir.
- Status da task: `pending | running | done | failed | skipped | cancelled | paused`. `paused` ENTRA em `_TERMINAL_STATUSES` (necessário para o dedupe `_find_active` não devolver task pausada como ativa no enqueue; e para a poda `_register`).
- `queue.Queue` Python 3.12: `join()` NÃO aceita timeout.
- yt-dlp 2026.07.04: `extractor_retries: 1` já setado; resume nativo via `.part` (mesma URL + formato + outtmpl).
- Auth: rotas novas de escrita exigem `Depends(require_auth)` (mesmo padrão das demais).
- Testes: rodar com `.venv/bin/python -m pytest`. Baseline: 136 passed.
- pt-BR: todos os textos de UI em português.
- NÃO commitar mudanças de código ao final (usuário não pediu); apenas o spec já foi commitado (`7f3570a`).

---

### Task 1: Downloader — status `paused` + pause()/resume()

**Files:**
- Modify: `app/downloader.py` (L40 `_TERMINAL_STATUSES`; próximo de `cancel()` L251; `_worker_loop` ~L324; `_run` ~L434)
- Test: `tests/test_downloader.py`

**Interfaces:**
- Consumes: `DownloadTask` (campos `task_id`, `yt_id`, `title`, `format`, `status`, `stage`, `progress`, `cancel_requested`), `self._lock`, `self._enqueue_lock`, `self._queue`, `self._history`, `self._notify(task_id, status, progress, stage)`.
- Produces:
  - `pause(self, task_ids: list[str] | None = None) -> list[str]` — retorna lista de task_ids pausados.
  - `resume(self, task_ids: list[str] | None = None) -> list[str]` — retorna lista de task_ids retomados (re-enfileirados, MESMA task — sem duplicata na snapshot).
  - `_TERMINAL_STATUSES = ("done", "failed", "skipped", "cancelled", "paused")`.

- [ ] **Step 1: Escrever os testes que falham**

Adicionar a `tests/test_downloader.py` (usar o padrão fake existente: `FakeExecutor`, `history` stub — seguir o estilo dos testes de cancel já presentes no arquivo):

```python
def test_pause_marca_paused_e_preserva_part(downloader, executor, history):
    # enqueue → start → task em running (executor em execução) → pause
    task = downloader.enqueue("yt1", "opus", "Titulo", "Artista")
    downloader.start()
    # espera task virar running (usar o padrão wait_for/Event do arquivo)
    paused = downloader.pause([task.task_id])
    assert paused == [task.task_id]
    t = downloader.get(task.task_id)
    assert t.status == "paused"
    assert t.cancel_requested is True
    # arquivo .part do executor fake NÃO foi removido (sem chamada a cleanup)
    downloader.stop()

def test_resume_reusa_mesma_task_e_reenfileira(downloader, executor, history):
    task = downloader.enqueue("yt1", "opus", "Titulo", "Artista")
    downloader.start()
    downloader.pause([task.task_id])
    resumed = downloader.resume([task.task_id])
    assert resumed == [task.task_id]
    t = downloader.get(task.task_id)
    assert t.status == "pending"
    assert t.cancel_requested is False
    # snapshot tem UMA task só (mesmo id, sem duplicata)
    assert [s["task_id"] for s in downloader.snapshot()].count(task.task_id) == 1

def test_pause_lote_none_pausa_todas_ativas(downloader, executor, history):
    t1 = downloader.enqueue("yt1", "opus", "A", "Art")
    t2 = downloader.enqueue("yt2", "opus", "B", "Art")
    downloader.start()
    paused = downloader.pause()  # todas ativas
    assert sorted(paused) == sorted([t1.task_id, t2.task_id])

def test_pause_task_terminal_retorna_false(downloader, executor, history):
    task = downloader.enqueue("yt1", "opus", "A", "Art")
    assert downloader.pause([task.task_id]) == []  # ainda pending, não iniciada -> não pausa? ver nota
```

Nota de comportamento (decidir no teste de acordo com o fake): pause de task `pending` (ainda na fila) DEVE pausar (marca paused; o worker descarta ao pegar). Pause de task `done`/`failed`/`cancelled`/`skipped` → ignorada (não entra na lista). Se o executor fake não dá pra deixar a task em `running`, usar enqueue + start + `wait_for` no status running (padrão já usado no arquivo para o teste de cancel).

- [ ] **Step 2: Rodar e confirmar que falham**

Run: `.venv/bin/python -m pytest tests/test_downloader.py -q`
Expected: novos testes FAIL (AttributeError: Downloader sem `pause`/`resume`).

- [ ] **Step 3: Implementar no Downloader**

```python
# L40: adicionar "paused"
_TERMINAL_STATUSES = ("done", "failed", "skipped", "cancelled", "paused")
```

Novos métodos após `cancel()` (~L265):

```python
def pause(self, task_ids: list[str] | None = None) -> list[str]:
    """Pausa tasks pending/running (todas ativas se task_ids None). Preserva o
    .part (resume nativo do yt-dlp retoma do ponto). """
    paused: list[str] = []
    with self._lock:
        targets = [
            t for t in self._tasks.values()
            if t.status in ("pending", "running")
            and (task_ids is None or t.task_id in task_ids)
        ]
        for task in targets:
            task.status = "paused"
            task.stage = "paused"
            task.cancel_requested = True  # aborta o download em andamento (progress hook)
            paused.append(task.task_id)
            self._history.mark(task.yt_id, "paused")
    for task_id in paused:
        self._notify(task_id, "paused", 0.0, "paused")
    return paused

def resume(self, task_ids: list[str] | None = None) -> list[str]:
    """Retoma tasks paused re-enfileirando a MESMA task (o yt-dlp continua do .part)."""
    resumed: list[str] = []
    with self._enqueue_lock, self._lock:
        targets = [
            t for t in self._tasks.values()
            if t.status == "paused"
            and (task_ids is None or t.task_id in task_ids)
        ]
        for task in targets:
            task.status = "pending"
            task.stage = "queued"
            task.cancel_requested = False
            task.progress = 0.0
            resumed.append(task.task_id)
            self._queue.put(task)  # MESMA task: sem duplicata na snapshot
    for task_id in resumed:
        self._notify(task_id, "pending", 0.0, "queued")
    return resumed
```

Ajustes obrigatórios nos loops existentes (o worker e o run NÃO podem sobrescrever `paused`):

- [ ] `_worker_loop` (~L324): ao pegar da fila, descartar task com `status in ("cancelled", "paused")` (mesmo guard do cancelled atual, incluir "paused").
- [ ] `_run` (~L434, tratamento de exceção com `cancel_requested`): `if task.status == "cancelled": self._cancel(task)` ; `elif task.status == "paused": pass  # mantém paused (não _fail, não _cancel)` ; senão `self._fail(...)`.

- [ ] **Step 4: Rodar os testes do arquivo**

Run: `.venv/bin/python -m pytest tests/test_downloader.py -q`
Expected: ALL PASS (incl. os testes de cancel existentes — cancelamento continua `cancelled`).

- [ ] **Step 5: Commit**

```bash
git add app/downloader.py tests/test_downloader.py
git commit -m "feat: status paused + pause/resume no Downloader (resume via .part)"
```

---

### Task 2: API — rotas de storage e pause/resume (main.py)

**Files:**
- Modify: `app/main.py` (junto de `list_downloads` L542 e `retry_failed_downloads` L685; import `shutil`)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `downloader` (da closure da factory), `settings.musicbox_dir`, `require_auth`, `Downloader.cleanup_partials()` (já existe), `pause()`/`resume()` da Task 1.
- Produces:
  - `GET /api/storage` → `{"disk": {"total","used","free"}, "library_size", "partials_size", "partials_count"}`
  - `POST /api/storage/cleanup` → `{"removed": int, "freed_bytes": int}`
  - `POST /api/downloads/pause` e `POST /api/downloads/resume` (body opcional `{"task_ids": [...]}`) → `{"paused": [...]}` / `{"resumed": [...]}`
  - `POST /api/downloads/{task_id}/pause` e `POST /api/downloads/{task_id}/resume` → `{"task_id": ..., "status": "paused"|"pending"}` (404 se task não existe; 409 se estado incompatível)

- [ ] **Step 1: Testes que falham** (adicionar a `tests/test_main.py`, padrão de client/TestClient com downloader fake já usado no arquivo)

```python
def test_storage_retorna_shape(tmp_path, client, monkeypatch):
    # preparar musicbox_dir com 1 mp3 fake e 1 .part fake
    (tmp_path / "a.mp3").write_bytes(b"x" * 100)
    (tmp_path / "b.mp3.part").write_bytes(b"x" * 50)
    r = client.get("/api/storage")
    assert r.status_code == 200
    body = r.json()
    assert set(body["disk"]) == {"total", "used", "free"}
    assert body["library_size"] == 100
    assert body["partials_size"] == 50
    assert body["partials_count"] == 1

def test_storage_cleanup_remove_part(tmp_path, client):
    (tmp_path / "b.mp3.part").write_bytes(b"x" * 50)
    (tmp_path / "a.mp3").write_bytes(b"x" * 100)
    r = client.post("/api/storage/cleanup")
    assert r.status_code == 200
    body = r.json()
    assert body["removed"] == 1 and body["freed_bytes"] == 50
    assert not (tmp_path / "b.mp3.part").exists()

def test_pause_resume_rotas(client, downloader_fake):
    r = client.post("/api/downloads/pause", json={})
    assert r.status_code == 200 and "paused" in r.json()
    r = client.post("/api/downloads/resume", json={})
    assert r.status_code == 200 and "resumed" in r.json()

def test_pause_task_inexistente_404(client):
    r = client.post("/api/downloads/nao-existe/pause")
    assert r.status_code == 404

def test_resume_task_nao_pausada_409(client):
    # task em status done (fake) -> 409
    r = client.post("/api/downloads/{id_done}/resume")
    assert r.status_code == 409

def test_retry_failed_ignora_paused(client, downloader_fake):
    # histórico com 1 failed e 1 paused -> retried_count só conta o failed
    r = client.post("/api/downloads/retry-failed")
    assert r.json()["retried_count"] == 1
```

- [ ] **Step 2: Rodar e confirmar falhas**

Run: `.venv/bin/python -m pytest tests/test_main.py -q`
Expected: FAIL (404 na rota /api/storage).

- [ ] **Step 3: Implementar**

```python
import shutil  # topo do main.py

# helper interno (perto de _m3u_lines):
def _storage_stats(musicbox_dir: Path) -> dict:
    usage = shutil.disk_usage(musicbox_dir)
    library_size = 0
    partials_size = 0
    partials_count = 0
    for p in musicbox_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix in _PARTIAL_SUFFIXES or p.name.endswith(".part"):
            partials_size += p.stat().st_size
            partials_count += 1
        else:
            library_size += p.stat().st_size
    return {
        "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
        "library_size": library_size,
        "partials_size": partials_size,
        "partials_count": partials_count,
    }
```

(Importar `_PARTIAL_SUFFIXES` do downloader — já exportado? Se não, definir localmente `(".part", ".ytdl")` — verificar no módulo.)

Rotas (junto das de downloads, todas com `Depends(require_auth)`):

```python
@fastapi_app.get("/api/storage", dependencies=[Depends(require_auth)])
def get_storage() -> dict:
    return _storage_stats(settings.musicbox_dir)

@fastapi_app.post("/api/storage/cleanup", dependencies=[Depends(require_auth)])
def cleanup_storage() -> dict:
    before = _storage_stats(settings.musicbox_dir)["partials_size"]
    downloader.cleanup_partials()
    after = _storage_stats(settings.musicbox_dir)["partials_size"]
    removed = before - after
    return {"removed": removed, "freed_bytes": removed}

class _TaskIdsIn(BaseModel):
    task_ids: list[str] | None = None

@fastapi_app.post("/api/downloads/pause", dependencies=[Depends(require_auth)])
def pause_downloads(body: _TaskIdsIn | None = None) -> dict:
    return {"paused": downloader.pause(body.task_ids if body else None)}

@fastapi_app.post("/api/downloads/resume", dependencies=[Depends(require_auth)])
def resume_downloads(body: _TaskIdsIn | None = None) -> dict:
    return {"resumed": downloader.resume(body.task_ids if body else None)}

@fastapi_app.post("/api/downloads/{task_id}/pause", dependencies=[Depends(require_auth)])
def pause_download(task_id: str) -> dict:
    task = downloader.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task não encontrada")
    if task.status in ("done", "failed", "skipped", "cancelled", "paused"):
        raise HTTPException(status_code=409, detail=f"estado incompatível: {task.status}")
    downloader.pause([task_id])
    return {"task_id": task_id, "status": "paused"}

@fastapi_app.post("/api/downloads/{task_id}/resume", dependencies=[Depends(require_auth)])
def resume_download(task_id: str) -> dict:
    task = downloader.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task não encontrada")
    if task.status != "paused":
        raise HTTPException(status_code=409, detail=f"estado incompatível: {task.status}")
    downloader.resume([task_id])
    return {"task_id": task_id, "status": "pending"}
```

Ajustar `retry_failed_downloads` (L685): garantir que `paused` não é tratado como failed (se o filtro for por `status == "failed"` no histórico, nada a fazer; se for por exclusão de terminais, adicionar guard `!= "paused"`).

- [ ] **Step 4: Rodar testes**

Run: `.venv/bin/python -m pytest tests/test_main.py -q`
Expected: ALL PASS. Depois suíte completa: `.venv/bin/python -m pytest -q` → 136 + novos, ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_main.py
git commit -m "feat: rotas /api/storage + pause/resume de downloads"
```

---

### Task 3: Frontend — storage, pausa/retomada e swipe (app.js)

**Files:**
- Modify: `app/static/app.js` (perto de `downloadsViewHtml` L1206, `taskCardHtml` L1535, `taskActionKey` L1597, `bindTaskCardEvents` L1606, `renderHistory` L1709, `libRowHtml` L2425, `evictAudioCache` ~L2586)

**Interfaces:**
- Consumes: API `GET /api/storage`, `POST /api/storage/cleanup`, `POST /api/downloads/pause|resume` (+ single), `navigator.storage.estimate()`, `caches.delete(AUDIO_CACHE)`, helpers existentes `postDownloadApi`, `api` object, `showToast`, `addToQueue` (fila do player), `downloadSingleTrack` (fluxo de download).
- Produces:
  - `loadStorageData()` → popula `state.storage = {disk, library_size, partials_size, partials_count, deviceQuota, deviceUsage}` (servidor via API + dispositivo via estimate/measureAudioCache).
  - `measureAudioCache()` → extraído do `evictAudioCache` existente; retorna bytes usados do AUDIO_CACHE.
  - `renderStorageSection()` → HTML da seção (server + device + alerta).
  - `pauseTasks(taskIds?)` / `resumeTasks(taskIds?)` / `cleanupPartials()` / `clearDeviceCache()`.
  - `bindSwipe(el, handlers)` → pointer events; `swipeLeft`/`swipeRight` handlers para linhas de histórico e biblioteca.
  - `taskActionKey()` inclui `"paused"` (chave própria) e `taskCardHtml()` branch `paused` (badge "Pausado" + ações Retomar/Cancelar).

- [ ] **Step 1: Implementar (app.js) — este é um frontend vanilla com smoke stub, não TDD estrito; validar com node --check + stub após cada bloco**

1. **Estado**: `state.storage = null` no objeto `state` (perto de L112).
2. **Helper de medida** — extrair de `evictAudioCache` (L2586-2620):
```js
async function measureAudioCache() {
  try {
    const cache = await caches.open(AUDIO_CACHE);
    const keys = await cache.keys();
    let bytes = 0;
    for (const req of keys) {
      const res = await cache.match(req);
      if (!res) continue;
      bytes += (await res.clone().blob()).size;
    }
    return { count: keys.length, bytes };
  } catch { return { count: 0, bytes: 0 }; }
}
```
   (`evictAudioCache` passa a chamar `measureAudioCache()` e reusar o resultado.)
3. **loadStorageData + renderStorageSection** (chamado em `refreshDownloads` e ao abrir a aba):
```js
const DISK_LOW_RATIO = 0.10;
const DISK_LOW_MIN_BYTES = 2 * 1024 * 1024 * 1024;

async function loadStorageData() {
  try {
    const [srv, dev] = await Promise.all([
      api.storage(),                     // GET /api/storage (adicionar no objeto api)
      navigator.storage && navigator.storage.estimate
        ? navigator.storage.estimate().catch(() => null)
        : null,
    ]);
    const cache = await measureAudioCache();
    state.storage = {
      disk: srv?.disk ?? null,
      librarySize: srv?.library_size ?? 0,
      partialsSize: srv?.partials_size ?? 0,
      partialsCount: srv?.partials_count ?? 0,
      deviceQuota: dev?.quota ?? null,
      deviceUsage: dev?.usage ?? null,
      cacheBytes: cache.bytes,
    };
  } catch { state.storage = null; }
  renderStorageSection();
}

function storageSectionHtml() {
  const s = state.storage;
  if (!s) return '<div class="storage-card"><p>Armazenamento indisponível.</p></div>';
  const free = s.disk ? s.disk.free : 0;
  const total = s.disk ? s.disk.total : 0;
  const low = s.disk && free < Math.max(DISK_LOW_RATIO * total, DISK_LOW_MIN_BYTES);
  const fmt = (n) => formatBytes(n); // helper existente do app
  return `
    <div class="storage-card${low ? ' storage-warn' : ''}" id="storage-section">
      ${low ? '<p class="storage-alert">⚠ Pouco espaço no servidor — ' + fmt(free) + ' livres.</p>' : ''}
      <div class="storage-block">
        <div class="storage-head"><strong>Servidor</strong>
          <button class="storage-refresh" data-action="refresh-storage" aria-label="Atualizar">↻</button></div>
        ${s.disk ? `<div class="storage-bar"><div class="storage-fill" style="width:${Math.min(100, (s.disk.used / Math.max(1, s.disk.total)) * 100)}%"></div></div>
        <div class="storage-row"><span>${fmt(s.disk.used)} de ${fmt(s.disk.total)}</span><span>${fmt(free)} livres</span></div>` : ''}
        <div class="storage-row"><span>Biblioteca: ${fmt(s.librarySize)}</span>
          <span>Órfãos (.part): ${fmt(s.partialsSize)} (${s.partialsCount})</span></div>
        <div class="storage-actions">
          <button class="dl-btn ghost" data-action="cleanup-partials" ${s.partialsCount ? '' : 'disabled'}>Limpar órfãos</button>
        </div>
      </div>
      <div class="storage-block">
        <div class="storage-head"><strong>Dispositivo</strong></div>
        ${s.deviceQuota ? `<div class="storage-bar"><div class="storage-fill" style="width:${Math.min(100, (s.deviceUsage / Math.max(1, s.deviceQuota)) * 100)}%"></div></div>
        <div class="storage-row"><span>${fmt(s.deviceUsage)} de ${fmt(s.deviceQuota)}</span></div>` : ''}
        <div class="storage-row"><span>Cache offline: ${fmt(s.cacheBytes)}</span></div>
        <div class="storage-actions">
          <button class="dl-btn ghost" data-action="clear-device-cache" ${s.cacheBytes ? '' : 'disabled'}>Limpar cache</button>
        </div>
      </div>
    </div>`;
}
```
4. **Ações** (delegate no container da aba, mesmo padrão dos binds existentes):
```js
async function pauseTasks(ids) { const r = await postDownloadApi({ endpoint: 'pause', task_ids: ids }); showToast((r.paused?.length ?? 0) + ' download(s) pausado(s)'); refreshDownloads(); }
async function resumeTasks(ids) { const r = await postDownloadApi({ endpoint: 'resume', task_ids: ids }); showToast((r.resumed?.length ?? 0) + ' download(s) retomado(s)'); refreshDownloads(); }
async function cleanupPartials() { const r = await postDownloadApi({ endpoint: 'storage/cleanup' }); showToast((r.freed_bytes ?? 0) > 0 ? formatBytes(r.freed_bytes) + ' liberados' : 'Nada a limpar'); loadStorageData(); }
async function clearDeviceCache() { await caches.delete(AUDIO_CACHE); showToast('Cache offline limpo'); loadStorageData(); }
```
   (Adaptar `postDownloadApi` ou criar helper `api.pause/resume/cleanup` no objeto api — usar o padrão existente.)
5. **Task card**: `taskCardHtml` — branch `status === 'paused'` → badge `badge-paused` "Pausado" + actions `[retomar] [cancelar]` (aria-labels pt); `taskActionKey` — `case 'paused': return 'paused'` (chave distinta p/ o patch incremental). `bindTaskCardEvents` — bind dos novos botões `[data-action="resume-task"]`/`[data-action="cancel-task"]` chamando `resumeTasks([id])`/`cancelTask(id)` (função existente de cancelar).
6. **Botões em lote** (no header da lista de tasks, dentro de `downloadsViewHtml`): `Pausar tudo` (visível se há pending/running) e `Retomar todos` (se há paused) → `pauseTasks()`/`resumeTasks()` sem ids. Atualizar visibilidade em `refreshDownloads`.
7. **Alerta de disco cheio no download**: em `downloadSingleTrack`/`downloadSearchAlbum`/`downloadAlbum`, antes de postar: `if (state.storage?.disk && state.storage.disk.free < Math.max(DISK_LOW_RATIO * state.storage.disk.total, DISK_LOW_MIN_BYTES)) showToast('Atenção: pouco espaço no servidor');` (toast warning; não bloqueia).
8. **Swipe** — helper + binds:
```js
function bindSwipe(el, onLeft, onRight) {
  let startX = 0, startY = 0, dragging = false, dx = 0;
  const THRESHOLD = 60;
  el.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'touch') return;
    startX = e.clientX; startY = e.clientY; dragging = true; dx = 0;
    el.setPointerCapture(e.pointerId);
  });
  el.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    dx = e.clientX - startX;
    if (Math.abs(e.clientY - startY) > Math.abs(dx)) { dragging = false; el.style.transform = ''; return; }
    if (Math.abs(dx) > THRESHOLD) el.classList.add('row-swipe');
    el.style.transform = `translateX(${Math.max(-120, Math.min(120, dx))}px)`;
  });
  el.addEventListener('pointerup', () => {
    if (!dragging) return;
    dragging = false;
    el.style.transform = '';
    if (dx <= -THRESHOLD) onLeft(el);
    else if (dx >= THRESHOLD) onRight(el);
    el.classList.remove('row-swipe');
  });
}
```
   - `renderHistory` e `libRowHtml`: após criar a linha, `bindSwipe(row, () => addToQueue(record), () => downloadSingleTrack(record))` — usar os handlers/funções de fila e download EXISTENTES do app (verificar nomes reais no código: `addToQueue`/equivalente e `downloadSingleTrack`). Swipe não substitui os botões (AC por teclado permanece).
   - `prefers-reduced-motion`: se ativo, ignorar o transform (guard `matchMedia('(prefers-reduced-motion: reduce)').matches`).

- [ ] **Step 2: Validar sintaxe + smoke stub**

Run: `node --check app/static/app.js` → SYNTAX OK.
Run: smoke DOM stub do projeto (mesmo padrão dos 19/19 da Fase 1 — reexecutar e garantir que continua passando; se o stub não cobrir storage, adicionar checks: `loadStorageData` com api mockada preenche state.storage; `storageSectionHtml` com disk baixo → classe storage-warn presente; `taskCardHtml` paused → badge-paused; `measureAudioCache` retorna {count, bytes}).

- [ ] **Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "feat: storage manager, pause/resume e swipe na UI"
```

---

### Task 4: Design — CSS das novas classes (styles.css)

**Files:**
- Modify: `app/static/styles.css` (após a seção de downloads ~L1885-1958)

**Interfaces:**
- Consumes: variáveis de tema existentes (`--surface`, `--border`, `--text-dim`, `--accent`, `--warning`, `--success`, `--error`, `--shadow-md`, `--hover`, `--accent-soft`), classes `.dl-btn`, `.badge` (L974), `.badge-*` (L996-1001, L1554).
- Produces: `.storage-card`, `.storage-warn`, `.storage-alert`, `.storage-block`, `.storage-head`, `.storage-refresh`, `.storage-bar`, `.storage-fill`, `.storage-row`, `.storage-actions`, `.badge-paused`, `.row-swipe` (linhas de histórico/biblioteca).

- [ ] **Step 1: Implementar (seguindo a linguagem Analog dos componentes existentes — mesmo estilo visual da Fase 1, validado por vision)**

```css
/* ==== Armazenamento (aba Downloads) ==== */
.storage-card { background: var(--surface); border: 1px solid var(--border);
  border-radius: 14px; padding: 14px; margin-bottom: 14px; box-shadow: var(--shadow-md); }
.storage-warn { border-color: var(--warning); }
.storage-alert { color: var(--warning); font-size: 0.85rem; margin-bottom: 10px; }
.storage-block + .storage-block { margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--border); }
.storage-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.storage-head strong { font-size: 0.85rem; letter-spacing: 0.04em; text-transform: uppercase; color: var(--text-dim); }
.storage-refresh { background: none; border: none; color: var(--text-dim); cursor: pointer; padding: 4px; border-radius: 6px; }
.storage-refresh:hover { color: var(--accent); background: var(--hover); }
.storage-bar { height: 6px; border-radius: 3px; background: var(--hover); overflow: hidden; margin-bottom: 6px; }
.storage-fill { height: 100%; border-radius: 3px; background: var(--grad); transition: width .3s ease; }
.storage-row { display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-dim); margin-bottom: 4px; }
.storage-actions { display: flex; gap: 8px; margin-top: 8px; }
.badge-paused { color: var(--warning); background: color-mix(in srgb, var(--warning) 16%, transparent); }
.row-swipe { position: relative; transition: transform .2s ease; touch-action: pan-y; }
```

- [ ] **Step 2: Validar**

Run: verificação de chaves CSS pareadas (script/padrão das fases anteriores) + `grep` confirmando que só usa variáveis de tema existentes (sem hex hardcoded novo).
Visual: screenshot + vision (opcional nesta task; feito na verificação final do orquestrador).

- [ ] **Step 3: Commit**

```bash
git add app/static/styles.css
git commit -m "feat: estilos do storage manager e badge paused"
```

---

## Self-Review

**Spec coverage:**
- GET /api/storage → Task 2 ✅
- POST /api/storage/cleanup → Task 2 ✅
- pause/resume single + lote → Tasks 1+2 ✅
- estado `paused` + `.part` preservado + resume nativo → Task 1 ✅
- `_TERMINAL_STATUSES` com paused (poda/dedupe/worker) → Task 1 ✅
- retry-failed ignora paused → Task 2 ✅
- UI: 3 visões (servidor/biblioteca/dispositivo) → Task 3 ✅
- Alerta disco cheio (aba + toast de download) → Task 3 ✅
- Limpar órfãos / limpar cache dispositivo → Task 3 ✅
- Badge/estado paused na UI + ações + lote → Task 3 ✅
- Swipe esquerda=fila / direita=baixar (histórico + biblioteca) → Task 3 ✅
- Range/resume de conexão: coberto (FileResponse já 206; resume via .part) — sem task, documentado no spec ✅

**Placeholder scan:** nenhum TBD/TODO; steps com código concreto. Nomes de funções verificados contra o código real (grep): `downloadsViewHtml` L1206, `taskCardHtml` L1535, `taskActionKey` L1597, `renderHistory` L1709, `libRowHtml` L2425, `_TERMINAL_STATUSES` L40, `cancel` L251, `cleanup_partials` L268, rotas de downloads L507/542/685.

**Type consistency:** `pause()/resume() -> list[str]` consistentes entre Task 1 e 2; `measureAudioCache() -> {count, bytes}` usado em Task 3 (loadStorageData + evictAudioCache); `state.storage` shape único definido na Task 3. ✅

## Execution Handoff

- **Orquestrador**: execução via lanes paralelas (backend fix-2, frontend fix-1, design des-1) conforme contrato acima; verificação final: pytest completo (baseline 136 + novos), node --check, smoke playwright (uvicorn 8099), vision. NÃO commitar código no final (só o spec foi commitado).
