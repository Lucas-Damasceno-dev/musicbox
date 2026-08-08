# Fase 4 — Scan da Biblioteca Local — Design

**Data:** 2026-08-08
**Status:** Aprovado (design aprovado pelo usuário em 2026-08-08)

## Objetivo

Tornar o MusicBox um tocador dos arquivos de áudio que o usuário já possui no dispositivo, sem upload para o servidor. Os arquivos ficam no dispositivo; o app lista, busca, filtra e toca via object URLs.

## Requisitos (verbatim do usuário, m0190)

> "Um escaneamento da biblioteca local para que o player não sirva apenas para baixar, mas também como o tocador principal de arquivos que o usuário já possui no dispositivo."

## Decisões acordadas

1. **Acesso**: ler a pasta do dispositivo, sem upload. `showDirectoryPicker()` no desktop Chromium (permissão persistente); `<input type="file" webkitdirectory multiple>` no Android/web (re-seleção a cada sessão); iOS Safari: fallback sessão (sem API de diretório persistente).
2. **Persistência parcial**: lista de arquivos (nome, caminho relativo, tamanho, tipo, pasta = "álbum") persistida em IndexedDB; object URLs regenerados a cada sessão; no mobile o usuário re-seleciona a pasta.
3. **UI**: segment **"Local"** na Biblioteca (Histórico | Artistas | Álbuns | **Local**), botão "Adicionar pasta"; lista com busca/filtro; tocar adiciona à fila do player (crossfade/gapless funcionam com object URLs).
4. **Metadados**: sem lib de tags — título = nome do arquivo (stem); agrupamento por pasta pai. `jsmediatags` pode ser adicionado depois se desejado.

## Arquitetura (100% frontend — backend intocado)

### `app/static/app.js`

- **Estado**: `state.localFiles = []` (itens `{id, name, title, album, size, type, file}`), `state.localIndexed = false`.
- **Segment "Local"** na Biblioteca: `bibliotecaViewHtml()` ganha 4º `.segment-btn[data-view="local"]`; `renderLibrary()` trata `view === 'local'` → `renderLibraryLocal()`.
- **Adicionar pasta** (`addLocalFolder()`):
  - Se `window.showDirectoryPicker` (desktop Chromium): `await showDirectoryPicker({mode:'read'})`, `dir.values()` recursivo, `file` + `webkitRelativePath`-like (caminho relativo montado manualmente).
  - Senão: `<input type="file" webkitdirectory multiple hidden>` (criado dinamicamente, `.click()`, evento change → `input.files` com `webkitRelativePath`).
  - Filtro de extensão: `.mp3 .opus .ogg .m4a .flac .wav .aac .webm` (lowercase). Título = stem; álbum = pasta pai imediata (ou "Músicas locais").
  - Persistência: IndexedDB (`musicbox-local-files`, store `files`, keyPath `id`; limpa e re-grava a cada seleção nova). Em sessões seguintes: carrega a lista de IndexedDB e mostra placeholder "Re-selecionar pasta para tocar" quando os arquivos não estão abertos (mobile); no desktop Chromium tenta reabrir os handles salvos (File System Access `queryPermission`/`requestPermission` + handles serializados via IndexedDB) — se a permissão foi concedida, a lista toca direto.
- **Tocar**: `playLocalTrack(item)` — `URL.createObjectURL(item.file)` → chama o fluxo central (`startPlayer`/fila) com `src` object URL; revoga a URL anterior da faixa em `closePlayer`/troca (`URL.revokeObjectURL`), guard para URLs locais.
- **Busca/filtro**: reuso do input de busca da Biblioteca filtrando `state.localFiles` por título/álbum.
- **Estado vazio**: ilustração da fita cassete SVG existente + texto "Adicione uma pasta com suas músicas" + botão "Adicionar pasta".
- **Badge**: "LOCAL" nas linhas (`.lib-row` reusado, classe `.is-local`).

### CSS (`app/static/styles.css`)

- Reuso de `.lib-row`/`.segment-btn` (já existem). Adições mínimas: `.btn-add-folder` (padrão dos botões outline), `.lib-row.is-local` (badge), `.local-hint` (texto de re-seleção). Tudo via variáveis de tema; zero hex novo.

### Testes

- Smoke DOM stub (mesmo padrão das fases anteriores): scan+filtro de extensões, montagem de itens (título/álbum), persistência IndexedDB mockada, `playLocalTrack` (object URL + startPlayer), render do segment Local/estado vazio.
- `node --check` app.js.
- Backend: nenhuma mudança → suíte 147 testes preservada (rodar completa uma vez para confirmar).

## Limitações honestas

- iOS Safari: sem diretório persistente — re-selecionar a pasta a cada sessão (lista do IndexedDB permanece visível com aviso).
- Sem tags ID3 no browser (título = nome do arquivo).
- Object URLs não sobrevivem a reload — reconstruídos ao re-selecionar/reabrir.

## Fora de escopo

- Upload/importação para o servidor; extração de tags; widget (sub-projeto 3); letras (sub-projeto 1 — spec próprio).
