# Fase 4 — Scan da Biblioteca Local — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permitir que o MusicBox toque os arquivos de áudio que o usuário já possui no dispositivo, sem upload — segmento "Local" na Biblioteca com seletor de pasta, persistência parcial e playback via object URLs.

**Architecture:** 100% frontend (backend intocado). `app.js` ganha estado `state.localFiles`, segmento "Local" no `bibliotecaViewHtml`/`renderLibrary`, `addLocalFolder()` (showDirectoryPicker no desktop Chromium, `<input webkitdirectory>` no Android/web), persistência em IndexedDB, `playLocalTrack()` com object URLs; `styles.css` ganha estilos mínimos (`.btn-add-folder`, `.lib-row.is-local`, `.local-hint`).

**Tech Stack:** Vanilla JS (app/static/app.js, ~4250 linhas), CSS (app/static/styles.css, ~2380 linhas), IndexedDB (nativo), File System Access API / webkitdirectory.

## Global Constraints

- Só `app/static/app.js` e `app/static/styles.css` são tocados — **1 lane por arquivo** (sem lanes paralelas no mesmo arquivo).
- NENHUM arquivo do backend muda; nenhuma dependência nova.
- Título = stem do nome do arquivo (sem lib de tags). Álbum = pasta pai imediata, fallback "Músicas locais".
- Extensões aceitas: `.mp3 .opus .ogg .m4a .flac .wav .aac .webm` (lowercase, exatas).
- CSS: zero hex novo, só variáveis de tema existentes (`--text-faint`, `--border`, `--accent`, `--hover` etc.); contratos de classe do Task 1 são a fonte da verdade.
- Não commitar código (usuário não pede commits de código; só specs são commitados).

---

### Task 1: Segmento "Local" + seletor de pasta + playback (app.js)

**Files:**
- Modify: `app/static/app.js`
- Test: `node --check app/static/app.js` + smoke DOM stub (padrão das fases anteriores)

**Interfaces:**
- Consumes: `bibliotecaViewHtml()` (segments atuais: historico/artistas/albuns), `renderLibrary()`, `renderHistory(records)`, `.lib-row` HTML existente, `state.playerQueue`, `startPlayer`, `closePlayer`, fluxo de busca da Biblioteca.
- Produces (contrato com Task 2): classes `.lib-row.is-local`, `.btn-add-folder`, `.local-hint`, badge "LOCAL"; funções `addLocalFolder()`, `renderLibraryLocal()`, `playLocalTrack(item)`, `state.localFiles`.

- [ ] **Step 1: Estado e constantes**

Adicionar no bloco de estado (perto de `state`):
```js
localFiles: [],      // itens {id, name, title, album, size, type, file, handle?}
localIndexed: false, // IndexedDB carregado?
```
E perto das outras consts de storage:
```js
const LOCAL_DB_NAME = 'musicbox-local-files';
const LOCAL_DB_STORE = 'files';
const LOCAL_EXTENSIONS = new Set(['.mp3', '.opus', '.ogg', '.m4a', '.flac', '.wav', '.aac', '.webm']);
```

- [ ] **Step 2: Segmento "Local" no template da Biblioteca**

Em `bibliotecaViewHtml()`: adicionar 4º `.segment-btn[data-view="local"]` com label "Local", `aria-pressed` e `is-active` quando `state.libraryView === 'local'` (padrão idêntico aos 3 existentes).

Em `renderLibrary()`: tratar `view === 'local'` → `renderLibraryLocal()` (antes do fallback genérico).

- [ ] **Step 3: `renderLibraryLocal()` + estado vazio**

- Vazio (`state.localFiles.length === 0`): ilustração da fita cassete SVG existente (reusar o SVG inline usado no estado vazio da busca) + título "Adicione uma pasta com suas músicas" + subtítulo "Selecione uma pasta do dispositivo — os arquivos não saem dele." + botão `<button class="btn-add-folder">Adicionar pasta</button>`.
- Com arquivos: manter o input de busca da Biblioteca filtrando `state.localFiles` por `title`/`album` (lowercase includes); lista de `.lib-row` reusando o padrão `libRowHtml` com badge "LOCAL" (classe `is-local` no `.lib-row`) e botão play circular `.lib-row-play` com aria-label `Tocar {title}`.
- Se `state.localIndexed` e os `file` não estão disponíveis (sessão nova, sem reabertura): mostrar `.local-hint` ("Re-selecione a pasta para tocar as músicas") + botão "Adicionar pasta" (`btn-add-folder`).

- [ ] **Step 4: `addLocalFolder()` — seletor com fallback**

```js
async function addLocalFolder() { ... }
```
- Se `window.showDirectoryPicker`: `const dir = await showDirectoryPicker({ mode: 'read' })`; percorrer recursivamente `dir.values()` (diretórios entram, arquivos são coletados); montar `item.file = fileHandle`, `item.handle = fileHandle`, caminho relativo montado manualmente (nome do dir raiz + subpastas).
- Senão: criar dinamicamente `<input type="file" webkitdirectory multiple hidden>` (append no body, `.click()`, listener `change` uma vez) e usar `e.target.files` com `webkitRelativePath`.
- Para cada arquivo: `const ext = '.' + (name.split('.').pop() || '').toLowerCase(); if (!LOCAL_EXTENSIONS.has(ext)) continue;`
- Item: `{ id: crypto.randomUUID ? crypto.randomUUID() : name + size, name, title: name.replace(/\.[^.]+$/, ''), album: pasta pai imediata (do path relativo, ou 'Músicas locais'), size, type: file.type, file, handle }` — ordenar por album+title (`localeCompare('pt-BR')`).
- `state.localFiles = items; saveLocalFiles(items); renderLibrary();`
- try/catch: usuário cancelou o picker (AbortError) → silencioso; erro real → toast "Não foi possível ler a pasta".

- [ ] **Step 5: IndexedDB — persistência parcial**

Helpers: `openLocalDb()` (open `musicbox-local-files` v1, onupgradeneeded cria store `files` keyPath `id`), `saveLocalFiles(items)` (transaction readwrite: limpa store, `put` de cada item **sem** `file`/`handle` — persistir só metadados `{id, name, title, album, size, type}`), `loadLocalFiles()` (getAll → metadados; seta `state.localFiles` (sem `file`) e `state.localIndexed = true`).

No init (ou no `openBibliotecaTab`): chamar `loadLocalFiles().then(...)` — se houver metadados, `renderLibrary()` mostra a lista com `.local-hint` de re-seleção (a menos que handles reabram — ver Step 6).

- [ ] **Step 6: Reabertura no desktop (best-effort)**

Se `window.showDirectoryPicker` existe e os metadados não têm `file`: tentar reabrir via handles — **simplificação aceitável**: na prática, a re-seleção pelo usuário (botão "Adicionar pasta") refaz a lista; documentar com comentário. (YAGNI: serialização de handles via IndexedDB fica fora; o `.local-hint` cobre a experiência.)

- [ ] **Step 7: `playLocalTrack(item)` + revogação de object URLs**

```js
function playLocalTrack(item) {
  if (!item || !item.file) { showToast('Re-selecione a pasta para tocar esta música.', 'error'); return; }
  const url = URL.createObjectURL(item.file);
  // adicionar à fila do player (mesmo fluxo dos tracks do histórico) e tocar via startPlayer com src=url
}
```
- Antes de criar nova URL, revogar a URL local anterior: guard no `closePlayer`/troca de faixa — manter `state._localObjectUrl` e `URL.revokeObjectURL(state._localObjectUrl)` quando substituída (só para `blob:`).
- Integrar com `bindLibraryRowEvents` (ou bind específico do segment Local): clique na linha/play → `playLocalTrack`; swipe esquerda (adicionar à fila) pode reusar o fluxo; swipe direita (download) **não se aplica** a arquivos locais (sem yt_id) — deixar só o play.

- [ ] **Step 8: Verificação**

- `node --check app/static/app.js` → SYNTAX OK.
- Smoke DOM stub (node/vm, padrão das fases anteriores): segmento Local renderizado e ativo; `addLocalFolder` filtro de extensões (inclui .mp3/.opus, exclui .txt/.jpg, caminho relativo → álbum); `renderLibraryLocal` vazio (fita cassete + btn-add-folder) e com itens (badge LOCAL, .local-hint quando sem `file`); IndexedDB mock (save/load metadados sem file); `playLocalTrack` (object URL criado, startPlayer chamado, revogação na troca); busca filtra por título/álbum. Reportar contagem de checks.

### Task 2: CSS do segmento Local (styles.css)

**Files:**
- Modify: `app/static/styles.css`

**Interfaces:**
- Consumes: contrato de classes do Task 1 (`.btn-add-folder`, `.lib-row.is-local`, `.local-hint`); padrões existentes `.lib-row`/`.segment-btn`/badges.
- Produces: estilos dessas classes.

- [ ] **Step 1: Estilos**

Adicionar (junto da seção da biblioteca, antes dos media queries finais):
- `.btn-add-folder`: padrão dos botões outline existentes (borda `--border-strong`, `--text-dim`, hover `--hover`, radius, padding; foco `--focus-ring`).
- `.lib-row.is-local .badge` (ou variante do badge existente): "LOCAL" em caps pequeno, tint `--accent-soft` com `color-mix` (padrão dos `.badge-*` existentes), sem hex novo.
- `.local-hint`: `--text-faint`, itálico, 0.85rem, margin.
- Verificação: chaves CSS pareadas (contagem `{` = `}`); grep das classes novas; zero hex novo no bloco adicionado; releitura. Não commitar.

### Task 3: Verificação integrada (orquestrador)

- [ ] Rodar suíte pytest completa (`.venv/bin/python -m pytest -q`) → **160 passed, 1 warning** (backend intocado — baseline).
- [ ] `node --check app/static/app.js` + `sw.js` → OK.
- [ ] Smoke real: subir uvicorn 8099, validar segmento "Local" na UI (sem erros de console), screenshot para vision.
- [ ] Relatório final ao usuário em pt-BR; NÃO commitar código.
