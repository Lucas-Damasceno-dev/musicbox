# MusicBox — Fase 1 UX/UI (tema claro "Polido", navegação e cards)

Data: 2026-08-07
Status: Aprovado pelo usuário (design em mockups validados)

## Contexto

O MusicBox é um PWA pessoal de música (FastAPI + yt-dlp no backend; frontend vanilla JS em `app/static/app.js` ~2622 linhas, `styles.css`, `index.html`, `manifest.json`). O app é hoje 100% dark (roxo/rosa). O usuário pediu um foco em UX/UI e, após rodada de brainstorming com mockups visuais no companion, aprovou a direção **Analog** (clima fita cassete/vinil, serifa Georgia, monograma MB) com refinamentos, e depois aprovou esta Fase 1 específica (única fase desta rodada — Fases 2-4 ficam para rodadas futuras).

Decisões prévias preservadas: direção Analog; toggle claro/escuro no header; claro por padrão na 1ª visita; preferência em localStorage.

## Escopo da Fase 1 (aprovado)

### 1. Tema claro "Polido" + toggle claro/escuro

- **Claro (novo, padrão na 1ª visita)** — paleta "Polido": terracota rosé `#d97a63` (ações), âmbar pálido `#e6c87d`, fundo quase branco `#fbf7ee` (degradê sutil `#fdfaf3 → #f6f0e2`), superfícies `#ffffff`, texto `#2b2118`, metadados `#5f4e3c`, bordas `#e6d9c0`, download concluído = oliva `#7f9a5a`. Tipografia: Georgia serif. Monograma circular MB terracota.
- **Escuro (mantido como está)** — tema atual do app, NÃO será redesenhado: `--bg #0b0910`, `--surface #171122`, `--surface-2 #201831`, `--accent #c084fc`, `--accent-2 #f472b6`, `--text #f4f1fa`, `--text-dim #a99fc4`, `--text-faint #8b81a8`, gradiente `#a78bfa → #f472b6`, sucesso `#34d399`.
- Toggle sol/lua em pílula no header; estado persistido em `localStorage` (chave nova, ex.: `musicbox.theme`); claro é o default quando nada salvo. Aplicar via atributo `data-theme` no `<html>` (ou classe no body) com CSS variables: paletas definidas em ambos os temas; o dark atual vira o bloco `[data-theme="dark"]` sem mudança de valores.
- Atenção: `styles.css` hoje define o dark como padrão (valores nas variáveis raiz). Estratégia: manter as variáveis raiz = **claro Polido** (novo padrão) e `[data-theme="dark"]` = valores atuais. Verificar cada uso de variável para garantir contraste AA no claro (texto `#2b2118` sobre `#fbf7ee` é alto; validar `--text-dim`/`--text-faint` no claro — no mockup os metadados claros usam `#5f4e3c`).
- No dark, alguns componentes podem precisar de ajuste mínimo de variáveis derivadas (bordas, sombras) — manter fidelidade ao atual.

### 2. Navegação inferior — nova ordem

- Ordem atual em `app/static/index.html:133-150`: Buscar | Biblioteca | Player | Downloads (`.nav-btn[data-tab]`, comentário na linha 133 desatualizado).
- **Nova ordem: Player | Biblioteca | Busca | Downloads** (esquerda → direita), mesma lista de abas e mesma mecânica de `data-tab`/badge de downloads.
- O player (mini-player / tela de player) continua existindo como aba; reordenar o HTML e atualizar o comentário.

### 3. Chips de formato — Opus 160 padrão

- Hoje: `app.js:2499-2512` (`loadFormat()` → `DEFAULT_FORMAT` do `/api/config` com fallback em `localStorage['musicbox.format']`; chips em `app.js:2614` etc.; `FORMAT_LABEL = { mp3: 'MP3 320', opus: 'Opus 160' }`).
- **Opus 160 passa a ser o padrão selecionado** na 1ª visita. Implementar trocando o default do frontend para `opus` quando não houver nada salvo (e/ou default do backend `DEFAULT_FORMAT=opus` em `.env.example`/README — decidir na implementação; o chip ativo é o que está destacado, mantendo os dois formatos).

### 4. Cards de busca — hierarquia: tocar é primário, download secundário

- **Clicar no card toca a faixa** (ação primária): indicar estado "tocando" com overlay de play no artwork + anel/realce e equalizer (3 barras) junto ao título no card ativo.
- **Botão de download vira secundário**: ícone de seta para baixo em estilo ghost/outline, sem glow, sem destaque primário.
- **Álbum tem CTA diferenciado**: "Baixar Álbum · N faixas" (com contagem de faixas do álbum), visualmente distinto do download de música avulsa.

### 5. Feedback de download no botão (3 estados)

No card de música (e demais lugares onde há botão de download de item individual), o botão reflete o estado da task (dados já chegam via WS/`state.tasks`):
- **Idle**: ícone de seta para baixo (ghost).
- **Em progresso**: spinner (arco SVG girando) + percentual (ex.: "62%") — mesma fonte do progresso da task.
- **Baixado**: check + "Baixado" (preenchido oliva no claro / `--success` no dark).
- Objetivo: impedir cliques repetidos e dar feedback imediato. Estados mapeados a partir dos status/progresso já existentes das tasks (ver `updateTaskCard`/patch incremental já implementado).

### 6. Biblioteca — agrupamento e filtros

- Hoje a aba Biblioteca mostra principalmente o histórico. Adicionar:
  - **Segment control: Histórico | Artistas | Álbuns** (persistir seleção em localStorage, ex.: `musicbox.libraryView`).
  - **Chips de filtro de formato: Todos | MP3 320 | Opus 160**.
  - **Artistas**: agrupado por artista (header de grupo com nome + contagem; linhas com capa ~42px, título, subtítulo "Artista · TIPO", play circular à direita).
  - **Álbuns**: agrupado por álbum (capa ~48px, nome do álbum, artista · ano, contagem de faixas).
- Fonte de dados: registros do histórico (`history` tem `artist`/`album`/`format` após metadata). Agrupamento feito no frontend a partir da lista da biblioteca existente; manter os dados atuais do histórico como base (sem novas rotas a princípio — se faltar campo, avaliar na implementação).

## Fora de escopo (fases futuras, mencionadas mas não aprovadas)

Fase 2 (storage manager, pausa/retomada em lote, resume Range, swipe), Fase 3 (vinil animado + tonearm, bottom-sheet, Media Session, crossfade, gapless), Fase 4 (letras LRC, scan de arquivos locais, widget). Nada disso entra nesta rodada.

## Validação de design

- Mockups: `.superpowers/brainstorm/98318-1786125289/content/fase1.html` (6 telas: busca-claro, busca-dark, dl-feedback, biblio-artista, biblio-album, biblio-dark), phones 300px.
- Validação geométrica via playwright: zero overflow horizontal em 1280/900/640; 6 phones 300px uniformes.
- Validação visual via subagente vision: **aprovado** — sem clipping, sem sobreposição, contraste adequado em claro e dark, identidade coerente, todas as decisões representadas fielmente (nav nova ordem, Opus 160 ativo, card=tocar com overlay/equalizer, download ghost, CTA "Baixar Álbum · 16 faixas", 3 estados do botão, biblioteca com segment + chips).

## Critérios de aceitação (implementação)

1. Primeira visita sem nada salvo → tema claro Polido; toggle alterna para o dark atual roxo/rosa; escolha persiste após reload.
2. Nav inferior na ordem Player | Biblioteca | Busca | Downloads, com abas funcionando como hoje.
3. Opus 160 aparece selecionado por padrão nos chips (1ª visita); troca manual persiste.
4. Clicar num card de música toca a faixa; card ativo mostra indicador de play; download por ícone seta ghost; álbum mostra "Baixar Álbum · N faixas".
5. Botão de download de item reflete os 3 estados (idle → spinner+% → baixado com check) conforme o progresso real da task.
6. Biblioteca com segment Histórico/Artistas/Álbuns + filtro de formato, agrupando corretamente.
7. Suíte pytest existente continua passando; `node --check` limpo em app.js/sw.js.
