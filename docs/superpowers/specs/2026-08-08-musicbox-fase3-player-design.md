# MusicBox — Fase 3: Player rico (Design)

> Data: 2026-08-08 · Base: spec Fase 1 (b52d0d6) e Fase 2 (7f3570a) · App: PWA música pessoal (vanilla JS, FastAPI)

## Objetivo

Tornar a experiência de reprodução dinâmica e de nível "app nativo": vinil animado com agulha, player minimizável por gesto, notificação rica de mídia e transições entre faixas (crossfade/gapless).

## Requisitos (verbatim do usuário, m0190)

1. **Vinil girando**: "Fazer o vinil girar quando a música estiver tocando e pausar suavemente… (adicione uma agulha de toca-discos (tonearm) que se aproxima/afasta do disco)."
2. **Bottom sheet**: "Na tela de reprodução, permita arrastar para baixo para minimizar o player para um mini-player flutuante (Bottom Sheet)."
3. **Crossfade**: "fundir o final de uma música com o início da próxima, ajustável de 1 a 12 segundos."
4. **Gapless**: "para álbuns conceituais… sem pausas entre faixas contínuas."
5. **Media Session**: "Notificação rica de controle de mídia com suporte a seekbar na barra de notificações e tela de bloqueio."

## Decisões de arquitetura

- **Vinil**: rotação via `requestAnimationFrame` com easing de aceleração/desaceleração (pausa suave é impossível com `animation-play-state`). JS aplica `el.style.transform = rotate(Ndeg)`. A agulha (tonearm) é um elemento novo `.player-tonearm` posicionado sobre o disco, com transição CSS (levanta ao pausar, abaixa ao tocar).
- **Bottom sheet**: a tela de player existente (`#player-view` + `#player-bar`) ganha um handle de arraste `.player-drag-handle`; pointer events (pointerdown/move/up) fazem `translateY`; soltou com ≥30% da altura → minimiza (fecha a tela do player, volta ao mini-player); senão, snap de volta (transition 0.25s).
- **Media Session**: `navigator.mediaSession.metadata` (title/artist/album) + handlers `play/pause/previoustrack/nexttrack/seekto` + `setPositionState` (seekbar na notificação). Guard `if (!('mediaSession' in navigator))` (fallback inerte).
- **Crossfade (1–12s, default 0 = off)**: engine com **dois** elementos `<audio>`: ao trocar de faixa com crossfade > 0, o elemento auxiliar toca a próxima com fade-in enquanto o atual faz fade-out (volume ramp), depois o principal assume o src e o auxiliar é descartado. Com crossfade = 0 (default), troca imediata = **gapless**: a próxima faixa é pré-carregada (`preload="auto"` + `load()`) durante a atual e trocada no `ended` sem gap perceptível.
- **Config**: slider `#crossfade-range` (0–12, step 1) + label `#crossfade-label` na tela do player, persistido em `localStorage['musicbox.crossfade']` (default `"0"`). 0 = gapless, N = crossfade de N segundos.
- **Escopo**: apenas frontend (`app/static/app.js` + `app/static/styles.css`). Backend **não muda**. Nenhuma dependência nova.

## Contrato de classes/ids (acordado entre lanes)

| Alvo | Seletor | Comportamento |
|---|---|---|
| Disco | `.player-disc` (existe) | JS seta `style.transform` (rAF); classe `.is-spinning` opcional p/ debug |
| Agulha | `.player-tonearm` (novo, absoluto sobre o disco) | CSS transition ~0.6s; `.is-playing` = agulha abaixada, sem classe = levantada |
| Handle | `.player-drag-handle` (novo, topo da `#player-view`) | ~36px, cursor grab, aria-label "Minimizar player" |
| Drag state | `#player-view.is-dragging` | desativa transição durante o gesto |
| Crossfade UI | `.crossfade-control` > `#crossfade-range` + `#crossfade-label` | input range 0–12 step 1; label "Crossfade: Xs" / "Gapless" quando 0 |

## Erros/limites (honestidade técnica)

- Gapless "verdadeiro" depende do decode do navegador; pré-load + troca imediata no `ended` elimina o gap prático para arquivos locais/opus em LAN. Aceito como o padrão da indústria PWA.
- `setPositionState` pode lançar em navegadores sem suporte → try/catch.
- A notificação rica exige instalação (PWA) em alguns OS; no desktop Chrome funciona na media session global.

## Verificação

- `node --check app/static/app.js`.
- Smoke playwright (uvicorn 8099): tocar faixa → `.player-disc` rotaciona (ler transform 2× com intervalo), `.player-tonearm.is-playing` presente, drag handle arrasta e minimiza, `navigator.mediaSession.metadata` preenchido (via `page.evaluate`), slider crossfade persiste em localStorage; `CONSOLE_ERRORS=[]`.
- Vision nos screenshots (vinil girando/parado, tela do player com tonearm).
- Suíte pytest intacta (136+11=147, frontend não afeta).

## Fora de escopo (Fase 4)

Letras LRC, scan da biblioteca local, widget de tela inicial.
