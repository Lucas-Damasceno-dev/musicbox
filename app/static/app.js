/* MusicBox — frontend vanilla (sem dependências, offline).
 * Textos de UI/aria-labels/comentários em português; identificadores em inglês.
 * Contrato de API conforme .sdd/briefs/task-7-brief.md (valores exatos).
 */
'use strict';

// ------------------------------------------------------------- constantes

const STORAGE_FORMAT_KEY = 'musicbox.format';
const STORAGE_TOKEN_KEY = 'musicbox.token';
const STORAGE_THEME_KEY = 'musicbox.theme';
const STORAGE_LIB_VIEW_KEY = 'musicbox.libraryView';
const STORAGE_LIB_FMT_KEY = 'musicbox.libraryFmt';
const STORAGE_CROSSFADE_KEY = 'musicbox.crossfade';
const DEFAULT_FORMAT = 'opus';

// Indicador de conexão (banner fixo no topo quando offline).
const OFFLINE_BANNER_ID = 'conn-banner';

// Biblioteca local (arquivos do dispositivo — nunca saem dele).
const LOCAL_DB_NAME = 'musicbox-local-files';
const LOCAL_DB_STORE = 'files';
const LOCAL_EXTENSIONS = new Set(['.mp3', '.opus', '.ogg', '.m4a', '.flac', '.wav', '.aac', '.webm']);

// Cores do <meta name="theme-color"> por tema (barra do navegador).
const THEME_COLORS = { light: '#fbf7ee', dark: '#1c1512' };

// Rótulos exibidos para cada formato (toggle do header).
const FORMAT_LABEL = { mp3: 'MP3 320', opus: 'Opus 160' };

// Rotas do contrato. `library` codifica cada segmento do path (tem barras) e
// anexa o token na query quando a autenticação está ativa (o <audio> e os
// links de download não enviam header customizado).
const API = {
  config: () => '/api/config',
  search: (q) => `/api/search?q=${encodeURIComponent(q)}`,
  artistAlbums: (name) => `/api/artists/${encodeURIComponent(name)}/albums`,
  albumTracks: (browseId) => `/api/albums/${encodeURIComponent(browseId)}/tracks`,
  downloads: () => '/api/downloads',
  downloadsPause: () => '/api/downloads/pause',
  downloadsResume: () => '/api/downloads/resume',
  history: () => '/api/history',
  storage: () => '/api/storage',
  storageCleanup: () => '/api/storage/cleanup',
  lyrics: (ytId) => `/api/library/${encodeURIComponent(ytId)}/lyrics`,
  library: (relPath) => {
    const base = `/api/library/${relPath.split('/').map(encodeURIComponent).join('/')}`;
    return state.token ? `${base}?token=${encodeURIComponent(state.token)}` : base;
  },
};

// Rótulos PT dos status e etapas de DownloadTask (contrato).
const STATUS_LABEL = {
  pending: 'na fila',
  running: 'baixando',
  done: 'concluído',
  failed: 'erro',
  skipped: 'pulado',
  cancelled: 'cancelado',
  paused: 'pausado',
};

const STAGE_LABEL = {
  queued: 'na fila',
  extracting: 'baixando',
  converting: 'convertendo',
  moving: 'movendo',
  done: 'concluído',
  cancelled: 'cancelado',
  paused: 'pausado',
};

const STATUS_BADGE = {
  pending: 'badge-pending',
  running: 'badge-running',
  done: 'badge-done',
  failed: 'badge-failed',
  skipped: 'badge-skipped',
  cancelled: 'badge-cancelled',
  paused: 'badge-paused',
};

// Ícones inline (SVG — sem dependências externas).
const ICONS = {
  download:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v10m0 0 4-4m-4 4-4-4"/><path d="M5 20h14"/></svg>',
  check:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 13l4 4L19 7"/></svg>',
  play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>',
  back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>',
  chevron:
    '<svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>',
  volume:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16.5 8.5a4.5 4.5 0 0 1 0 7"/></svg>',
};

// ---------------------------------------------------------------- estado

const state = {
  format: DEFAULT_FORMAT, // formato ativo (mp3 | opus), persistido
  theme: 'light', // tema ativo (light | dark), persistido
  libraryView: 'historico', // visão da aba Biblioteca (historico|artistas|albuns), persistida
  libraryFmt: 'all', // filtro de formato da Biblioteca (all|mp3|opus), persistido
  hasFfmpeg: true, // servidor com ffmpeg (config carregada no init)
  token: '', // token de acesso (auth opcional do servidor)
  authRequired: false, // servidor exige token (config auth_required)
  activeTab: 'songs', // aba da busca: 'songs' | 'albums' | 'artists' | 'playlists'
  results: { songs: [], albums: [], artists: [], playlists: [] },
  lastQuery: '', // último termo buscado (restaurado ao voltar para a busca)
  currentView: 'search', // view renderizada (search|artist|album|downloads)
  currentData: {},
  backStack: [], // pilha de navegação interna (search → artist → album)
  tasks: new Map(), // task_id -> task (fila ao vivo, ordem de inserção)
  history: [],
  storage: null, // {disk, librarySize, partialsSize, partialsCount, deviceQuota, deviceUsage, cacheBytes}
  taskEls: new Map(), // task_id -> elemento do card (update in place)
  ws: null, // conexão WebSocket ativa (reconexão automática com backoff)
  wsReconnectTimer: null,
  wsReconnectDelay: null, // backoff atual da reconexão (3s → 6s → … → 30s)
  mainEl: null,
  toastRegion: null,
  autoDownloaded: new Map(), // task_id -> timestamp (evita re-download; podado por TTL)
  notifiedTasks: new Map(), // task_id -> timestamp de notificação (evita spam; podado por TTL)
  autoDownloadPending: false, // auto-download no dispositivo: só para downloads avulsos
  queueSessionIds: new Set(), // task_ids vistos NESTA sessão de página (a Fila não acumula lixo velho)
  searchSeq: 0, // contador anti-race: invalida handlers de buscas anteriores
  searchStream: null, // {es, timer, resolve} da busca streaming atual (para matar a anterior)
  searchLimit: 10, // limite de resultados por seção; "Carregar mais" aumenta
  searchLoading: null, // {songs: bool, albums: bool, artists: bool, playlists: bool} durante a busca por streaming
  searchSections: [], // seções (songs/albums/artists/playlists) já recebidas na busca atual — status dinâmico
  seekDragging: false, // true enquanto o usuário arrasta o seek (timeupdate não sobrescreve)
  biblioteca: [], // árvore artista→álbum→faixas da tela Biblioteca
  playlists: [], // playlists do usuário (painel em Downloads + seleção no player)
  playerQueue: [], // fila do mini-player (faixas tocáveis)
  playerIndex: 0, // índice atual na fila
  crossfadeSeconds: 0, // crossfade entre faixas em segundos (0 = gapless), persistido
  // Vinil (rotação via rAF com easing)
  discAngle: 0, // ângulo acumulado do disco (graus)
  discVelocity: 0, // velocidade angular atual (deg/s)
  discTarget: 0, // velocidade alvo (0 pausado / DISC_DEG_PER_S tocando)
  discDecelerating: false, // true durante a pausa suave (deceleração)
  discRaf: null, // id do requestAnimationFrame do vinil (null = parado)
  _discLastTs: null, // timestamp do último frame (para o dt)
  _discSuppressPause: false, // true durante troca de faixa (disco não decelera)
  // Crossfade/gapless
  _auxAudio: null, // elemento <audio> auxiliar (fade-in da próxima / pré-decode)
  _auxUrl: null, // url pré-carregada no auxiliar (evita reload duplicado)
  _crossfading: false, // true durante um crossfade em andamento
  _crossfadeBaseVolume: undefined, // volume do usuário preservado durante o fade
  _crossfadeSeq: 0, // token anti-race: invalida fades substituídos/abortados
  // Letras (pane Fila | Letras + karaokê)
  playerPane: 'queue', // visão do player: 'queue' | 'lyrics'
  _lyricsCache: null, // {timed: [{time, text}], plain: [string]} resolvido (ou null)
  _lyricsFor: null, // yt_id a que o cache/erro se refere
  _lyricsError: null, // status HTTP do último fetch (null = ok) — usado no estado de erro
  _lyricsFetching: null, // yt_id com fetch em andamento (dedupe)
  _lyricsActiveIdx: -1, // índice da linha ativa (guarda o auto-scroll)
  // Biblioteca local (arquivos do dispositivo)
  localFiles: [], // itens {id, name, title, album, size, type, file, handle?}
  localIndexed: false, // IndexedDB carregado (metadados sem file)?
  localQuery: '', // termo de busca das músicas locais
  _localObjectUrl: null, // object URL da faixa local ATUAL (para revogar)
  _localObjectUrls: new Set(), // URLs blob: da fila local vigente (limpeza)
  online: true, // estado da conexão (navigator.onLine; ajustado no init)
};

// ---------------------------------------------------------------- helpers

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function clampNumber(value, min, max) {
  const n = Number(value);
  if (Number.isNaN(n)) return min;
  return Math.min(max, Math.max(min, n));
}

// Elementos clicáveis (cards/linhas com role="button") também respondem a
// Enter/Espaço. `e.target !== el` evita disparar quando um botão interno
// (ex.: "Baixar") tem o foco.
function bindCardKeyboard(el, action) {
  el.addEventListener('keydown', (e) => {
    if (e.target !== el) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      action();
    }
  });
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) {
    return '—';
  }
  const total = Math.max(0, Math.round(Number(seconds)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function formatLabel(fmt) {
  return FORMAT_LABEL[fmt] || String(fmt || '').toUpperCase();
}

// Formata bytes de forma legível (ex.: "512 B", "1.5 KB", "12 GB").
function formatBytes(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '0 B';
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = n;
  let unit = 'B';
  for (const u of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = u;
  }
  const digits = value >= 10 ? 0 : 1;
  return `${value.toFixed(digits)} ${unit}`;
}

function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast toast--${type}`;
  toast.setAttribute('role', 'status');
  toast.textContent = message;
  state.toastRegion.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('is-visible'));
  setTimeout(() => {
    toast.classList.remove('is-visible');
    setTimeout(() => toast.remove(), 300);
  }, 3800);
}

// ----------------------------------------------------------- API (fetch)

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `Erro HTTP ${status}`);
    this.status = status;
    this.detail = detail || null;
    this.isNetwork = false;
  }
}

async function apiFetch(url, options = {}) {
  const opts = { ...options };
  const headers = new Headers(opts.headers || {});
  if (state.token) headers.set('X-MusicBox-Token', state.token);
  opts.headers = headers;

  let res;
  try {
    res = await fetch(url, opts);
  } catch {
    // TypeError: servidor inacessível (brief: toast "Sem conexão com o servidor")
    showToast('Sem conexão com o servidor', 'error');
    const err = new ApiError(0, 'Sem conexão com o servidor');
    err.isNetwork = true;
    throw err;
  }
  if (res.status === 401) {
    // Token ausente/errado: abre o modal de token (auth opcional do servidor).
    showToast('Token de acesso necessário', 'error');
    openTokenModal();
    throw new ApiError(401, 'Token de acesso necessário');
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null; // corpo vazio ou não-JSON
  }
  if (!res.ok) {
    const detail =
      body && body.detail
        ? typeof body.detail === 'string'
          ? body.detail
          : JSON.stringify(body.detail)
        : null;
    throw new ApiError(res.status, detail);
  }
  return body;
}

async function searchApi(q, limit) {
  const url = `${API.search(q)}${limit ? `&limit=${limit}` : ''}`;
  return apiFetch(url);
}

async function artistAlbumsApi(name) {
  return apiFetch(API.artistAlbums(name));
}

async function albumTracksApi(browseId) {
  return apiFetch(API.albumTracks(browseId));
}

async function postDownloadApi(payload) {
  return apiFetch(API.downloads(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

async function storageApi() {
  return apiFetch(API.storage());
}

async function storageCleanupApi() {
  return apiFetch(API.storageCleanup(), { method: 'POST' });
}

// Pausa/retoma tasks (lote). Sem ids → todas ativas/pausadas (contrato do backend).
async function pauseTasksApi(ids) {
  const body = ids && ids.length ? { task_ids: ids } : {};
  return apiFetch(API.downloadsPause(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function resumeTasksApi(ids) {
  const body = ids && ids.length ? { task_ids: ids } : {};
  return apiFetch(API.downloadsResume(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function listDownloadsApi() {
  return apiFetch(API.downloads());
}

async function historyApi() {
  return apiFetch(API.history());
}

// Tratamento de erro HTTP genérico (toast já exibido para erro de rede).
function handleApiError(err, fallback) {
  if (err.isNetwork) return;
  if (err.status === 401) {
    showToast('Token de acesso necessário', 'error');
    openTokenModal();
  } else if (err.status === 422) showToast(err.detail || 'Dados inválidos.', 'error');
  else if (err.status === 404) showToast('Não encontrado.', 'error');
  else if (err.status === 503) showToast('Sem conexão com o servidor.', 'error');
  else if (err.status === 502) showToast('Erro no servidor.', 'error');
  else showToast(fallback || err.message || 'Erro inesperado.', 'error');
}

// ------------------------------------------------------- navegação/views

function showView(name, data = {}) {
  state.currentView = name;
  state.currentData = data;
  state.mainEl.innerHTML = viewHtml(name, data);
  bindViewEvents(name, data);
  applyFfmpegState(); // desabilita botões de download se o servidor não tem ffmpeg
  if (name === 'player') syncDiscState(); // tonearm/vinil refletem o estado real do áudio (play pode ter ocorrido antes da view existir)
}

function viewHtml(name, data) {
  if (name === 'artist') return artistViewHtml(data);
  if (name === 'album') return albumViewHtml(data.album);
  if (name === 'downloads') return downloadsViewHtml();
  if (name === 'player') return playerViewHtml();
  if (name === 'biblioteca') return bibliotecaViewHtml();
  if (name === 'lib-artist') return libArtistViewHtml(data);
  if (name === 'lib-album') return libAlbumViewHtml(data);
  return searchViewHtml();
}

function goBack() {
  const prev = state.backStack.pop();
  const name = prev ? prev.name : 'search';
  showView(name, prev ? prev.data : {});
  if (name === 'search') restoreSearchResults();
}

// Ao voltar de álbum/artista, restaura o termo, a aba ativa e a lista de
// resultados (que continuam em state.results/lastQuery).
function restoreSearchResults() {
  const input = document.getElementById('search-input');
  if (input && state.lastQuery) input.value = state.lastQuery;

  // Reaplica a aba ativa (a view de busca nasce com Músicas selecionado)
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    const active = btn.dataset.tab === state.activeTab;
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-selected', String(active));
  });

  if (state.results.songs.length || state.results.albums.length || state.results.artists.length || state.results.playlists.length) {
    renderResults();
  }
}

function openArtist(name, items) {
  state.backStack.push({ name: 'search', data: {} });
  showView('artist', { name, items });
}

function openAlbum(album) {
  state.backStack.push({ name: state.currentView, data: state.currentData });
  showView('album', { album });
}

function setActiveTab(tab) {
  document.querySelectorAll('.nav-btn').forEach((btn) => {
    const active = btn.dataset.tab === tab;
    btn.classList.toggle('is-active', active);
    if (active) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  });
}

function openSearchTab() {
  setActiveTab('buscar');
  state.backStack = [];
  showView('search', {});
}

function openDownloadsTab() {
  setActiveTab('downloads');
  state.backStack = [];
  showView('downloads', {});
  refreshDownloads();
  refreshPlaylists();
}

function openPlayerTab() {
  setActiveTab('player');
  state.backStack = [];
  showView('player', {});
  refreshPlaylists(); // alimenta o select "Salvar na playlist…"
}

function openBibliotecaTab() {
  setActiveTab('biblioteca');
  state.backStack = [];
  showView('biblioteca', {});
  refreshLibraryData();
}

// Carrega o histórico (fonte da Biblioteca) e renderiza a visão ativa. A guarda
// de currentView evita renderizar na lista errada se o usuário trocar de aba
// enquanto o fetch está em andamento.
async function refreshLibraryData() {
  try {
    state.history = await historyApi();
  } catch (err) {
    handleApiError(err, 'Não foi possível carregar a biblioteca.');
  }
  if (state.currentView === 'biblioteca') renderLibrary();
}

function bindViewEvents(name, data) {
  if (name === 'search') bindSearchEvents();
  else if (name === 'artist') bindArtistEvents();
  else if (name === 'album') bindAlbumEvents(data.album);
  else if (name === 'downloads') bindDownloadsEvents();
  else if (name === 'player') bindPlayerViewEvents();
  else if (name === 'biblioteca') bindBibliotecaEvents();
  else if (name === 'lib-artist') bindLibArtistEvents(data);
  else if (name === 'lib-album') bindLibAlbumEvents(data);
}

// --------------------------------------------------------------- busca

function searchViewHtml() {
  return `
    <section class="view search-view" aria-label="Buscar">
      <form class="search-bar" id="search-form" role="search">
        <input
          id="search-input"
          type="search"
          placeholder="Música, artista, álbum ou link..."
          autocomplete="off"
          aria-label="Buscar música, artista, álbum ou cole um link"
        />
        <button type="submit" class="btn btn-primary btn-search">Buscar</button>
      </form>
      <div class="tabbar" role="tablist" aria-label="Tipo de resultado">
        <button type="button" role="tab" data-tab="songs" class="tab-btn is-active" aria-selected="true">Músicas</button>
        <button type="button" role="tab" data-tab="albums" class="tab-btn" aria-selected="false">Álbuns</button>
        <button type="button" role="tab" data-tab="artists" class="tab-btn" aria-selected="false">Artistas</button>
        <button type="button" role="tab" data-tab="playlists" class="tab-btn" aria-selected="false">Playlists</button>
      </div>
      <div id="results" class="results" aria-live="polite">
        <p class="empty-state">Digite algo para buscar.</p>
      </div>
    </section>`;
}

function bindSearchEvents() {
  const form = document.getElementById('search-form');
  const input = document.getElementById('search-input');

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) {
      showSearchMessage('Digite algo para buscar.');
      input.focus();
      return;
    }
    runSearch(q);
  });

  document.querySelectorAll('.tab-btn').forEach((tab) => {
    tab.addEventListener('click', () => {
      state.activeTab = tab.dataset.tab;
      document.querySelectorAll('.tab-btn').forEach((btn) => {
        const active = btn.dataset.tab === state.activeTab;
        btn.classList.toggle('is-active', active);
        btn.setAttribute('aria-selected', String(active));
      });
      renderResults();
    });
  });
}

function showSearchMessage(message) {
  const results = document.getElementById('results');
  if (results) results.innerHTML = `<p class="empty-state">${escapeHtml(message)}</p>`;
}

// Rótulos e ordem das seções da busca (ordem fixa do stream SSE:
// songs → albums → artists → playlists). Gênero gramatical para a
// concordância do texto de progresso (PT-BR).
const SEARCH_KIND_LABEL = {
  songs: 'músicas',
  albums: 'álbuns',
  artists: 'artistas',
  playlists: 'playlists',
};
const SEARCH_KIND_ORDER = ['songs', 'albums', 'artists', 'playlists'];
const SEARCH_KIND_FEMININE = { songs: true, albums: false, artists: false, playlists: true };

// Une uma lista com vírgulas e "e" final (ex.: "músicas, álbuns e artistas").
function formatSearchList(items) {
  if (items.length <= 1) return items[0] || '';
  return `${items.slice(0, -1).join(', ')} e ${items[items.length - 1]}`;
}

// Texto dinâmico de progresso da busca: seções já recebidas vs. restantes.
// Ex.: "Músicas e álbuns encontrados — buscando artistas e playlists…"
function searchStatusLabel(received) {
  const sections = Array.isArray(received) ? received : [];
  const found = SEARCH_KIND_ORDER.filter((k) => sections.includes(k));
  const pending = SEARCH_KIND_ORDER.filter((k) => !sections.includes(k));
  if (found.length === 0) return 'Buscando músicas…'; // nada chegou ainda (songs é a 1ª)
  if (pending.length === 0) return 'Buscando…'; // todas recebidas — não deve aparecer no skeleton
  const foundText = formatSearchList(found.map((k) => SEARCH_KIND_LABEL[k]));
  const pendingText = formatSearchList(pending.map((k) => SEARCH_KIND_LABEL[k]));
  const feminine = found.every((k) => SEARCH_KIND_FEMININE[k]);
  const foundLabel = foundText.charAt(0).toUpperCase() + foundText.slice(1);
  return `${foundLabel} encontrad${feminine ? 'as' : 'os'} — buscando ${pendingText}…`;
}

// Skeleton de carregamento da busca (shimmer) enquanto o stream não entrega a
// seção ativa. O texto do status é dinâmico: reflete o progresso das seções já
// recebidas (state.searchSections) e as que ainda faltam.
function searchLoadingHtml() {
  const rows = Array.from(
    { length: 4 },
    () => `<div class="skeleton-row"><span class="skeleton-cover"></span><span class="skeleton-lines"><i></i><i></i></span></div>`
  ).join('');
  return `
    <div class="search-loading" role="status" aria-live="polite">
      <div class="search-loading-head">
        <span class="spinner" aria-hidden="true"></span>
        <p>${searchStatusLabel(state.searchSections)}</p>
      </div>
      <div class="skeleton-list">${rows}</div>
    </div>`;
}

async function runSearch(q, limit) {
  state.lastQuery = q;
  if (limit) state.searchLimit = limit;

  // Anti-race: invalida qualquer busca anterior (stream, timer e handlers) —
  // eventos da busca A não podem sobrescrever os resultados da busca B.
  const prev = state.searchStream;
  if (prev) {
    try {
      prev.es.close();
    } catch {
      /* noop */
    }
    clearTimeout(prev.timer);
    prev.resolve(false); // libera o await da busca antiga
    state.searchStream = null;
  }
  const seq = ++state.searchSeq;

  // Busca por streaming: cada seção aparece assim que o servidor resolve (a
  // busca leva ~11–20s; a UI mostra músicas/álbuns conforme chegam em vez de
  // ficar parada em "Buscando…"). Se o stream falhar, cai para o REST.
  state.searchLoading = { songs: true, albums: true, artists: true, playlists: true };
  state.searchSections = []; // reseta o registro de seções recebidas (status dinâmico)
  state.results = { songs: [], albums: [], artists: [], playlists: [] };
  if (/[?&]list=[A-Za-z0-9_-]{13,}/.test(q) && /youtube\.com|youtu\.be/.test(q)) {
    state.activeTab = 'playlists'; // URL de playlist colada → abre na aba certa
  }
  if (!state.activeTab) state.activeTab = 'songs';
  renderResults();
  const ok = await searchStream(q);
  // Só faz o fallback REST se esta ainda for a busca atual (não a que foi
  // substituída por "Carregar mais" ou por um novo termo).
  if (!ok && seq === state.searchSeq) await searchApiFallback(q);
}

// Busca via EventSource (/api/search/stream). EventSource não envia header
// customizado → o token vai na query (mesmo padrão do /ws e do <audio>).
function searchStream(q) {
  return new Promise((resolve) => {
    if (typeof EventSource === 'undefined') return resolve(false);
    const seq = state.searchSeq; // seq desta busca: handlers obsoletos são ignorados
    const params = new URLSearchParams({ q, limit: String(state.searchLimit) });
    if (state.token) params.set('token', state.token);
    const es = new EventSource(`/api/search/stream?${params.toString()}`);
    let settled = false;
    let timer = null;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer); // fix: o timeout de 35s é sempre limpo no fim
      state.searchSections = []; // stream encerrou (done/erro/timeout) → status volta ao padrão
      es.close();
      resolve(ok);
    };
    // Trava de segurança: nada chegou em 35s → cai para o REST.
    timer = setTimeout(() => {
      if (seq !== state.searchSeq) return; // busca antiga — já substituída
      finish(false);
    }, 35000);
    // Referência para runSearch matar esta busca na próxima (race do
    // "Carregar mais"/novo termo durante o stream).
    state.searchStream = { es, timer, resolve };

    es.addEventListener('section', (e) => {
      if (seq !== state.searchSeq) return; // resposta de uma busca antiga — ignora
      try {
        const data = JSON.parse(e.data);
        let any = false;
        for (const kind of ['songs', 'albums', 'artists', 'playlists']) {
          if (Array.isArray(data[kind])) {
            state.results[kind] = data[kind];
            state.searchLoading[kind] = false;
            if (!state.searchSections.includes(kind)) state.searchSections.push(kind); // status dinâmico
            any = true;
          }
        }
        if (any) renderResults();
      } catch {
        // evento malformado — ignora
      }
    });
    es.addEventListener('done', () => {
      if (seq !== state.searchSeq) return;
      finish(true);
    });
    es.addEventListener('error', (e) => {
      if (seq !== state.searchSeq) return;
      // O EventSource dispara 'error' para falha de CONEXÃO E para o evento
      // nomeado `event: error` do servidor (MessageEvent com .data).
      if (e && typeof e === 'object' && 'data' in e) {
        let detail = 'Erro ao buscar.';
        try {
          detail = JSON.parse(e.data).detail || detail;
        } catch {
          /* fallback */
        }
        // Falha no meio do stream: zera os loading (sem skeleton eterno) e avisa.
        state.searchLoading = { songs: false, albums: false, artists: false, playlists: false };
        showToast(detail, 'error');
        finish(true);
        return;
      }
      const started = Object.values(state.searchLoading || {}).some((v) => !v);
      es.close();
      finish(!started ? false : true); // falhou antes de qualquer seção → fallback REST
    });
  });
}

// Fallback REST da busca (stream indisponível/erro precoce).
async function searchApiFallback(q) {
  state.searchSections = []; // sem SSE não há seções incrementais — status volta ao padrão
  try {
    const data = await searchApi(q, state.searchLimit);
    state.results = {
      songs: data.songs || [],
      albums: data.albums || [],
      artists: data.artists || [],
      playlists: data.playlists || [],
    };
    state.searchLoading = { songs: false, albums: false, artists: false, playlists: false };
    renderResults();
  } catch (err) {
    state.results = { songs: [], albums: [], artists: [], playlists: [] };
    state.searchLoading = { songs: false, albums: false, artists: false, playlists: false };
    if (err.isNetwork) {
      showSearchMessage('Sem conexão com o servidor.');
    } else if (err.status === 404) {
      showToast('Nada encontrado', 'error');
      showSearchMessage(`Nada encontrado para “${q}”.`);
    } else if (err.status === 503) {
      showSearchMessage('Sem conexão com o servidor.');
    } else if (err.status === 422) {
      showSearchMessage(err.detail || 'Digite algo para buscar.');
    } else {
      showSearchMessage(err.detail || 'Erro ao buscar.');
    }
  }
}

function renderResults() {
  const results = document.getElementById('results');
  if (!results) return;
  const tab = state.activeTab || 'songs';
  const items = state.results ? (state.results[tab] || []) : [];

  // Streaming: a seção ativa ainda está carregando → skeleton em vez de vazio.
  if (state.searchLoading && state.searchLoading[tab] !== false) {
    results.innerHTML = searchLoadingHtml();
    return;
  }

  if (!items || items.length === 0) {
    const labels = {
      songs: 'Nenhuma música encontrada.',
      albums: 'Nenhum álbum encontrado.',
      artists: 'Nenhum artista encontrado.',
      playlists: 'Nenhuma playlist encontrada.',
    };
    showSearchMessage(labels[tab] || 'Nenhum resultado.');
    return;
  }

  // "Carregar mais" aparece quando a seção veio cheia (provavelmente há mais) e
  // ainda não chegou no teto de 40 itens por seção.
  const showMore = items.length > 0 && items.length >= state.searchLimit && state.searchLimit < 40;
  results.innerHTML = `<ul class="card-list">${items
    .map((item, i) => cardHtml(item, tab.slice(0, -1), i))
    .join('')}</ul>${
      showMore
        ? '<button type="button" id="load-more-btn" class="btn btn-ghost load-more">Carregar mais resultados</button>'
        : ''
    }`;

  const loadMore = results.querySelector('#load-more-btn');
  if (loadMore) {
    loadMore.addEventListener('click', () => {
      runSearch(state.lastQuery, Math.min(state.searchLimit + 10, 40));
    });
  }

  results.querySelectorAll('.card').forEach((card, i) => {
    card.addEventListener('click', (e) => {
      if (e.target.closest('.dl-btn, .dl-album-btn')) return; // botões de download têm clique próprio
      onCardClick(card.dataset, items[i]);
    });
    // Acessibilidade: todos os cards respondem a Enter/Espaço (música toca,
    // artista/álbum/playlist navegam). Botões internos (.dl-btn) têm foco
    // próprio — bindCardKeyboard ignora quando o alvo é o botão.
    bindCardKeyboard(card, () => onCardClick(card.dataset, items[i]));
  });

  bindDlButtons(results);
  syncDlButtons();
  syncPlayingCards();
}

// Liga os cliques dos botões de download (música .dl-btn e álbum .dl-album-btn)
// dentro de um escopo. Reutilizado por renderResults e pela view de artista.
function bindDlButtons(scope) {
  const root = scope || document;
  root.querySelectorAll('.dl-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      downloadSingleTrack({
        yt_id: btn.dataset.ytId,
        title: btn.dataset.title,
        artist: btn.dataset.artist,
      });
    });
  });
  root.querySelectorAll('.dl-album-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      downloadSearchAlbum(
        { id: btn.dataset.albumId, title: btn.dataset.title, artist: btn.dataset.artist },
        btn
      );
    });
  });
}

function cardHtml(item, kind, index) {
  const isArtist = kind === 'artist';
  const isSong = kind === 'song';
  const isPlaylist = kind === 'playlist';
  const emoji = isArtist ? '🎤' : isSong ? '🎵' : isPlaylist ? '🎧' : '💿';

  const coverImg = item.thumbnail
    ? `<img src="${escapeHtml(item.thumbnail)}" alt="" class="cover" loading="lazy" />`
    : `<span class="cover cover--${kind}" aria-hidden="true">${emoji}</span>`;

  const metaText = isArtist
    ? 'Artista'
    : isSong
    ? (item.artist ? escapeHtml(item.artist) : 'Música')
    : isPlaylist
    ? 'Playlist'
    : 'Álbum';

  // Contagem de faixas do álbum (quando conhecida — cards de busca não trazem).
  const trackCount =
    item.tracks && Array.isArray(item.tracks) ? item.tracks.length : 0;

  let action = ICONS.chevron;
  let cardLabel = '';
  if (isSong) {
    // Ação primária do card de música é TOCAR; o download fica no .dl-btn.
    action = `<button
      type="button"
      class="dl-btn"
      data-yt-id="${escapeHtml(item.id)}"
      data-title="${escapeHtml(item.title)}"
      data-artist="${escapeHtml(item.artist || '')}"
      aria-label="Baixar ${escapeHtml(item.title)}"
    >${ICONS.download}</button>`;
    cardLabel = `Tocar ${item.title}`;
  } else if (kind === 'album') {
    // CTA de download do álbum + chevron de navegação (mantido).
    action = `
      <button
        type="button"
        class="dl-album-btn"
        data-album-id="${escapeHtml(item.id)}"
        data-title="${escapeHtml(item.title)}"
        data-artist="${escapeHtml(item.artist || '')}"
        ${trackCount ? `data-count="${trackCount}"` : ''}
        aria-label="Baixar álbum ${escapeHtml(item.title)}"
      >${ICONS.download}<span class="dl-label">Baixar Álbum${trackCount ? ` · ${trackCount} ${trackCount === 1 ? 'faixa' : 'faixas'}` : ''}</span></button>
      ${ICONS.chevron}`;
    cardLabel = `Abrir álbum ${item.title}`;
  } else {
    cardLabel = isArtist
      ? `Ver álbuns de ${item.title}`
      : `Abrir playlist ${item.title}`;
  }

  return `
    <li>
      <div
        class="card"
        role="button"
        tabindex="0"
        aria-label="${escapeHtml(cardLabel)}"
        data-kind="${escapeHtml(kind)}"
        data-id="${escapeHtml(item.id)}"
        data-title="${escapeHtml(item.title)}"
        style="animation-delay:${Math.min(index * 40, 400)}ms"
      >
        ${coverImg}
        <span class="card-body">
          <span class="card-title">${escapeHtml(item.title)}</span>
          <span class="card-kind">${metaText}</span>
        </span>
        ${action}
      </div>
    </li>`;
}

function onCardClick({ kind, id, title }, item) {
  if (kind === 'artist') {
    artistAlbumsApi(title)
      .then((items) => openArtist(title, items))
      .catch((err) => handleApiError(err, 'Não foi possível carregar os álbuns.'));
  } else if (kind === 'album' || kind === 'playlist') {
    // Playlists resolvem pelo mesmo fluxo de álbum (lista de faixas + download).
    albumTracksApi(id)
      .then((album) => openAlbum(album))
      .catch((err) => handleApiError(err, 'Não foi possível carregar o álbum.'));
  } else if (kind === 'song') {
    playSearchTrack(item || { id, title });
  }
}

// Toca uma música vinda da busca. Se já estiver baixada (histórico com path),
// usa o arquivo real; senão tenta o stub /api/library/{yt_id} — o <audio> falha
// com 404 e o handler de erro do player mostra "Baixe a faixa para ouvir".
function playSearchTrack(item) {
  const rec = (state.history || []).find(
    (r) => r.yt_id === item.id && r.status === 'done' && r.path
  );
  if (rec) {
    playMiniTrack({
      url: API.library(rec.path),
      title: rec.title || item.title || 'Música',
      artist: rec.artist || item.artist || '',
      cover: rec.cover_url || item.thumbnail || '',
      ytId: rec.yt_id || item.id,
    });
    return;
  }
  playMiniTrack({
    url: API.library(item.id), // stub — 404 se não baixada
    title: item.title || 'Música',
    artist: item.artist || '',
    cover: item.thumbnail || '',
    ytId: item.id,
    _notDownloaded: true, // sinaliza o handler de erro do player
  });
}

// --------------------------------------------------- álbuns do artista

function artistViewHtml({ name, items }) {
  const albums = items || [];
  return `
    <section class="view sub-view" aria-label="Álbuns de ${escapeHtml(name)}">
      <header class="sub-header">
        <button type="button" class="icon-btn back-btn" id="back-btn" aria-label="Voltar">${ICONS.back}</button>
        <div class="sub-header-info">
          <h1 class="sub-heading">${escapeHtml(name)}</h1>
          <p class="sub-meta">${albums.length} ${albums.length === 1 ? 'álbum' : 'álbuns'}</p>
        </div>
      </header>
      ${
        albums.length === 0
          ? '<p class="empty-state">Nenhum álbum encontrado.</p>'
          : `<ul class="card-list">${albums
              .map((item, i) => cardHtml(item, 'album', i))
              .join('')}</ul>`
      }
    </section>`;
}

function bindArtistEvents() {
  const back = document.getElementById('back-btn');
  if (back) back.addEventListener('click', goBack);

  document.querySelectorAll('.card').forEach((card) => {
    card.addEventListener('click', (e) => {
      if (e.target.closest('.dl-btn, .dl-album-btn')) return; // download tem clique próprio
      onCardClick(card.dataset);
    });
    bindCardKeyboard(card, () => onCardClick(card.dataset));
  });

  bindDlButtons(document.querySelector('.sub-view'));
  syncDlButtons();
  syncPlayingCards();
}

// ------------------------------------------------------- tela do álbum

function albumViewHtml(album) {
  const tracks = album.tracks || [];
  const coverImg = album.cover_url
    ? `<img src="${escapeHtml(album.cover_url)}" alt="" class="album-hero-cover" />`
    : `<div class="album-hero-cover cover--album-hero">💿</div>`;

  return `
    <section class="view sub-view" aria-label="Álbum ${escapeHtml(album.title)}">
      <header class="sub-header">
        <button type="button" class="icon-btn back-btn" id="back-btn" aria-label="Voltar">${ICONS.back}</button>
        <div class="sub-header-info">
          <h1 class="sub-heading">${escapeHtml(album.title)}</h1>
          <p class="sub-meta">${escapeHtml(album.artist)} · ${tracks.length} ${tracks.length === 1 ? 'faixa' : 'faixas'}</p>
        </div>
      </header>

      <div class="album-hero">
        ${coverImg}
        <div class="album-hero-details">
          <h2 class="album-hero-title">${escapeHtml(album.title)}</h2>
          <p class="album-hero-artist">${escapeHtml(album.artist)}</p>
          <button type="button" class="btn btn-primary btn-block" id="download-album-btn">
            ${ICONS.download}
            Baixar tudo (${tracks.length} faixas)
          </button>
          <p class="album-hint">Formato atual: <strong>${formatLabel(state.format)}</strong></p>
        </div>
      </div>

      <ol class="track-list">
        ${tracks.map((track, i) => trackRowHtml(track, i)).join('')}
      </ol>
    </section>`;
}

function trackRowHtml(track, index) {
  const title = track.title || 'Sem título';
  // number (posição 1..N) vem no contrato; fallback para a posição no array
  const num = track.number ?? index + 1;
  return `
    <li class="track-row">
      <span class="track-num" aria-hidden="true">${String(num).padStart(2, '0')}</span>
      <div class="track-info">
        <span class="track-title">${escapeHtml(title)}</span>
        <span class="track-duration">${formatDuration(track.duration)}</span>
      </div>
      <button
        type="button"
        class="icon-btn track-dl"
        data-yt-id="${escapeHtml(track.yt_id)}"
        data-title="${escapeHtml(title)}"
        aria-label="Baixar ${escapeHtml(title)}"
      >${ICONS.download}</button>
    </li>`;
}

function bindAlbumEvents(album) {
  const back = document.getElementById('back-btn');
  if (back) back.addEventListener('click', goBack);

  const dlBtn = document.getElementById('download-album-btn');
  if (dlBtn) {
    dlBtn.addEventListener('click', () => downloadAlbum(album));
  }

  document.querySelectorAll('.track-dl').forEach((btn) => {
    btn.addEventListener('click', () => downloadTrack(btn.dataset.ytId, btn.dataset.title));
  });
}

async function downloadTrack(ytId, title) {
  if (!state.hasFfmpeg) {
    showToast('Servidor sem ffmpeg: downloads indisponíveis', 'error');
    return;
  }
  ensureNotificationPermission();
  const btn = document.querySelector(`.track-dl[data-yt-id="${CSS.escape(ytId)}"]`);
  if (btn) btn.disabled = true; // evita duplo-submit durante o POST
  try {
    const data = await postDownloadApi({ yt_id: ytId, formato: state.format });
    if (data.task) ingestTask(data.task);
    state.autoDownloadPending = true;
    showToast(`“${title}” adicionado à fila`, 'success');
    // Permanece na tela (toast + badge confirmam) — sem navegar nem limpar o backStack.
  } catch (err) {
    handleApiError(err, 'Não foi possível adicionar à fila.');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function downloadAlbum(album) {
  if (!state.hasFfmpeg) {
    showToast('Servidor sem ffmpeg: downloads indisponíveis', 'error');
    return;
  }
  warnIfLowDisk(); // alerta (não bloqueia) com pouco espaço no servidor
  const btn = document.getElementById('download-album-btn');
  if (btn) btn.disabled = true; // evita duplo-submit durante o POST
  ensureNotificationPermission();
  try {
    const data = await postDownloadApi({ album_id: album.id, formato: state.format });
    const count = data.tasks ? data.tasks.length : 0;
    state.autoDownloadPending = false; // álbum: sem flood de auto-downloads no navegador
    (data.tasks || []).forEach(ingestTask);
    showToast(
      `${count} ${count === 1 ? 'faixa adicionada' : 'faixas adicionadas'} à fila`,
      'success'
    );
    // Permanece na tela (toast + badge confirmam) — sem navegar nem limpar o backStack.
  } catch (err) {
    handleApiError(err, 'Não foi possível baixar o álbum.');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Baixar uma música direto da aba Músicas da busca: enfileira e PERMANECE na
// tela (o toast confirma) — sem pular para Downloads nem "reagir a tela toda".
async function downloadSingleTrack({ yt_id, title }) {
  if (!state.hasFfmpeg) {
    showToast('Servidor sem ffmpeg: downloads indisponíveis', 'error');
    return;
  }
  warnIfLowDisk(); // alerta (não bloqueia) com pouco espaço no servidor
  ensureNotificationPermission();
  const btn = document.querySelector(`.dl-btn[data-yt-id="${CSS.escape(yt_id)}"]`);
  if (btn) btn.disabled = true; // evita duplo-submit durante o POST
  try {
    const data = await postDownloadApi({ yt_id, formato: state.format });
    if (data.task) ingestTask(data.task);
    state.autoDownloadPending = true;
    showToast(`“${title || 'Música'}” adicionado à fila`, 'success');
  } catch (err) {
    handleApiError(err, 'Não foi possível adicionar à fila.');
  } finally {
    if (btn) btn.disabled = false;
    syncDlButtons(); // reflete o estado novo da task no botão (spinner/%) na hora
  }
}

// Baixa um álbum direto do card de busca (CTA .dl-album-btn). Cria N tasks de
// faixas (sem task do álbum) — o feedback do botão vem do syncDlButtons via
// correspondência por título do álbum nas tasks.
async function downloadSearchAlbum(album, btn) {
  if (!state.hasFfmpeg) {
    showToast('Servidor sem ffmpeg: downloads indisponíveis', 'error');
    return;
  }
  warnIfLowDisk(); // alerta (não bloqueia) com pouco espaço no servidor
  ensureNotificationPermission();
  if (btn) btn.disabled = true; // evita duplo-submit durante o POST
  try {
    const data = await postDownloadApi({ album_id: album.id, formato: state.format });
    const count = data.tasks ? data.tasks.length : 0;
    state.autoDownloadPending = false; // álbum: sem flood de auto-downloads no navegador
    (data.tasks || []).forEach(ingestTask);
    showToast(
      `${count} ${count === 1 ? 'faixa adicionada' : 'faixas adicionadas'} à fila`,
      'success'
    );
  } catch (err) {
    handleApiError(err, 'Não foi possível baixar o álbum.');
  } finally {
    if (btn) btn.disabled = false;
    syncDlButtons();
  }
}

// ----------------------------------------------------------------- feedback
// de download (3 estados)

// Última task conhecida para um yt_id (Map preserva ordem de inserção: o último
// é o mais recente — retry cria task nova com o mesmo yt_id).
function findTaskByYtId(ytId) {
  if (!ytId) return null;
  let found = null;
  state.tasks.forEach((t) => {
    if (t.yt_id === ytId) found = t;
  });
  return found;
}

// Sincroniza os 3 estados dos botões de download visíveis (não reconstrói
// cards): .dl-btn (música) e .dl-album-btn (álbum). Os .dl-btn de ação
// (armazenamento) têm data-action e são ignorados (não são botões de download).
function syncDlButtons() {
  document.querySelectorAll('.dl-btn:not([data-action])').forEach((btn) => {
    const ytId = btn.dataset.ytId;
    let task = findTaskByYtId(ytId);
    // Sessão anterior: task saiu da memória, mas o arquivo está no histórico.
    if (!task) {
      const rec = (state.history || []).find(
        (r) => r.yt_id === ytId && r.status === 'done' && r.path
      );
      if (rec) task = { status: 'done', progress: 100 };
    }
    const status = task ? task.status || 'pending' : null;
    if (status === 'done') setDlButtonState(btn, 'downloaded');
    else if (status === 'pending' || status === 'running')
      setDlButtonState(btn, 'downloading', task.progress);
    else setDlButtonState(btn, 'idle'); // sem task ou falhou/cancelado → volta ao ícone
  });

  document.querySelectorAll('.dl-album-btn').forEach(syncDlAlbumButton);
}

// Aplica o estado no .dl-btn (idle = só ícone; downloading = spinner + %;
// downloaded = check + "Baixado"). O aria-label acompanha o estado.
function setDlButtonState(btn, dlState, progress) {
  const pct = Math.round(Number(progress));
  const title = btn.dataset.title || '';
  if (dlState === 'downloading') {
    btn.classList.add('is-downloading');
    btn.classList.remove('is-downloaded');
    btn.setAttribute('aria-label', `Baixando: ${title}`);
    btn.innerHTML =
      '<span class="dl-spinner" aria-hidden="true"></span>' +
      (pct > 0 ? `<span class="dl-pct">${pct}%</span>` : '');
    return;
  }
  if (dlState === 'downloaded') {
    btn.classList.remove('is-downloading');
    btn.classList.add('is-downloaded');
    btn.setAttribute('aria-label', title ? `Baixado: ${title}` : 'Baixado');
    btn.innerHTML = `${ICONS.check}<span class="dl-label">Baixado</span>`;
    return;
  }
  btn.classList.remove('is-downloading', 'is-downloaded');
  btn.setAttribute('aria-label', title ? `Baixar ${title}` : 'Baixar');
  btn.innerHTML = ICONS.download;
}

// Estados do .dl-album-btn. Download de álbum cria N tasks de faixas (sem task
// do álbum) — o estado é derivado por correspondência de album/artista:
// todas done → "Baixado"; alguma ativa → spinner + %; senão → idle.
function syncDlAlbumButton(btn) {
  const title = btn.dataset.title || '';
  const artist = btn.dataset.artist || '';
  const count = Number(btn.dataset.count) || 0;
  const albumTasks = [...state.tasks.values()].filter(
    (t) => t.album === title && (!artist || t.artist === artist)
  );
  const idleLabel = `Baixar Álbum${count > 0 ? ` · ${count} ${count === 1 ? 'faixa' : 'faixas'}` : ''}`;

  if (albumTasks.length > 0 && albumTasks.every((t) => t.status === 'done')) {
    btn.classList.remove('is-downloading');
    btn.classList.add('is-downloaded');
    btn.setAttribute('aria-label', title ? `Álbum baixado: ${title}` : 'Álbum baixado');
    btn.innerHTML = `${ICONS.check}<span class="dl-label">Baixado</span>`;
    return;
  }
  const active = albumTasks.filter(
    (t) => t.status === 'pending' || t.status === 'running'
  );
  if (active.length > 0) {
    const progress = Math.max(...active.map((t) => Number(t.progress) || 0));
    btn.classList.add('is-downloading');
    btn.classList.remove('is-downloaded');
    btn.setAttribute('aria-label', `Baixando álbum: ${title}`);
    btn.innerHTML =
      '<span class="dl-spinner" aria-hidden="true"></span>' +
      (progress > 0 ? `<span class="dl-pct">${Math.round(progress)}%</span>` : '');
    return;
  }
  btn.classList.remove('is-downloading', 'is-downloaded');
  btn.setAttribute('aria-label', title ? `Baixar álbum ${title}` : 'Baixar álbum');
  btn.innerHTML = `${ICONS.download}<span class="dl-label">${idleLabel}</span>`;
}

// Adiciona uma task nova ao estado local na hora (POST ok) — alimenta o badge de
// downloads ativos e a Fila sem esperar o próximo snapshot do servidor.
function ingestTask(task) {
  if (!task || !task.task_id) return;
  state.tasks.set(task.task_id, task);
  state.queueSessionIds.add(task.task_id);
  updateDownloadsBadge();
}

// Contador de downloads ativos (pending/running) no botão Downloads da navegação.
function updateDownloadsBadge() {
  const badge = document.getElementById('downloads-badge');
  if (!badge) return;
  const count = [...state.tasks.values()].filter((t) => {
    const s = t.status || 'pending';
    return s === 'pending' || s === 'running';
  }).length;
  badge.hidden = count === 0;
  badge.textContent = count > 9 ? '9+' : String(count);
}

// Notificação nativa quando um download termina (ou falha) — pede permissão no
// primeiro download. Melhor-esforço: sem permissão, nada acontece.
function ensureNotificationPermission() {
  if (!('Notification' in window)) return false;
  if (Notification.permission === 'granted') return true;
  if (Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
  return Notification.permission === 'granted';
}

function notifyDownload(task) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  if (!task || (task.status !== 'done' && task.status !== 'failed')) return;
  if (state.notifiedTasks.has(task.task_id)) return; // sem spam por refresh/reconexão
  state.notifiedTasks.set(task.task_id, Date.now()); // entrada é podada por TTL
  try {
    new Notification(task.status === 'done' ? 'Download concluído ♪' : 'Download falhou', {
      body: task.title || 'Música',
      tag: task.task_id || 'musicbox',
    });
  } catch {
    // notificação é melhor-esforço
  }
}

// ------------------------------------------------------------ downloads

function downloadsViewHtml() {
  const m3uUrl = `/api/export.m3u${state.token ? `?token=${encodeURIComponent(state.token)}` : ''}`;
  return `
    <section class="view downloads-view" aria-label="Downloads">
      <header class="view-head">
        <h1 class="view-title">Downloads</h1>
        <div class="view-head-actions">
          <button type="button" class="btn btn-ghost btn-small" id="retry-failed-btn">Retentar Falhas</button>
          <a href="${m3uUrl}" download class="btn btn-ghost btn-small" id="export-m3u-btn">Exportar .M3U</a>
          <button type="button" class="btn btn-ghost btn-small" id="refresh-btn">Atualizar</button>
        </div>
      </header>

      <div id="storage-section" class="storage-section"></div>

      <div class="playlists-section">
        <h2 class="section-title">Playlists</h2>
        <form class="playlist-create" id="playlist-create-form">
          <input id="playlist-name-input" type="text" placeholder="Nome da nova playlist" aria-label="Nome da nova playlist" autocomplete="off" />
          <button type="submit" class="btn btn-primary btn-small">Criar</button>
        </form>
        <div id="playlists-list" class="playlists-list">
          <p class="empty-state">Nenhuma playlist ainda.</p>
        </div>
      </div>

      <div class="queue-section">
        <h2 class="section-title">Fila</h2>
        <div class="queue-actions">
          <button type="button" class="btn btn-ghost btn-small" id="pause-all-btn" hidden>Pausar tudo</button>
          <button type="button" class="btn btn-ghost btn-small" id="resume-all-btn" hidden>Retomar todos</button>
        </div>
        <div id="task-list" class="task-list">
          <p class="empty-state">Nenhum download na fila.</p>
        </div>
      </div>

      <div class="history-section">
        <h2 class="section-title">Histórico</h2>
        <div id="history-list" class="history-list">
          <p class="empty-state">Nenhum download ainda.</p>
        </div>
      </div>
    </section>`;
}

// --------------------------------------------------------- armazenamento

// Alerta de disco cheio no servidor (spec: free < max(10% do total, 2 GB)).
const DISK_LOW_RATIO = 0.10;
const DISK_LOW_MIN_BYTES = 2 * 1024 * 1024 * 1024; // 2 GB

// Alerta (não bloqueante) antes de iniciar um download com pouco espaço livre.
function warnIfLowDisk() {
  const d = state.storage && state.storage.disk;
  if (!d || !d.total) return;
  const limit = Math.max(DISK_LOW_RATIO * d.total, DISK_LOW_MIN_BYTES);
  if (d.free < limit) showToast('Atenção: pouco espaço no servidor', 'warning');
}

// Carrega dados de armazenamento: servidor (GET /api/storage) + dispositivo
// (navigator.storage.estimate + tamanho do cache offline) e renderiza a seção.
async function loadStorageData() {
  try {
    const [srv, dev] = await Promise.all([
      storageApi(),
      navigator.storage && navigator.storage.estimate
        ? navigator.storage.estimate().catch(() => null)
        : Promise.resolve(null),
    ]);
    const cache = await measureAudioCache();
    state.storage = {
      disk: srv && srv.disk ? srv.disk : null,
      librarySize: srv ? srv.library_size ?? 0 : 0,
      partialsSize: srv ? srv.partials_size ?? 0 : 0,
      partialsCount: srv ? srv.partials_count ?? 0 : 0,
      deviceQuota: dev && dev.quota ? dev.quota : null,
      deviceUsage: dev && dev.usage ? dev.usage : null,
      cacheBytes: cache.bytes,
    };
  } catch {
    state.storage = null;
  }
  renderStorageSection();
}

function renderStorageSection() {
  const container = document.getElementById('storage-section');
  if (!container) return;
  container.innerHTML = storageSectionHtml();
}

// HTML da seção de armazenamento (Servidor + Dispositivo). O container
// #storage-section é preenchido por renderStorageSection; o id NÃO é repetido
// no card interno para evitar id duplicado no DOM.
function storageSectionHtml() {
  const s = state.storage;
  if (!s) return '<div class="storage-card"><p>Armazenamento indisponível.</p></div>';
  const free = s.disk ? s.disk.free : 0;
  const total = s.disk ? s.disk.total : 0;
  const low = !!s.disk && free < Math.max(DISK_LOW_RATIO * total, DISK_LOW_MIN_BYTES);
  const diskPct = s.disk ? Math.min(100, (s.disk.used / Math.max(1, s.disk.total)) * 100) : 0;
  const devPct = s.deviceQuota ? Math.min(100, (s.deviceUsage / Math.max(1, s.deviceQuota)) * 100) : 0;
  return `
    <div class="storage-card${low ? ' storage-warn' : ''}">
      ${low ? `<p class="storage-alert">⚠ Pouco espaço no servidor — ${formatBytes(free)} livres.</p>` : ''}
      <div class="storage-block">
        <div class="storage-head"><strong>Servidor</strong>
          <button type="button" class="storage-refresh" data-action="refresh-storage" aria-label="Atualizar armazenamento">↻</button></div>
        ${
          s.disk
            ? `<div class="storage-bar"><div class="storage-fill" style="width:${diskPct}%"></div></div>
        <div class="storage-row"><span>${formatBytes(s.disk.used)} de ${formatBytes(s.disk.total)}</span><span>${formatBytes(free)} livres</span></div>`
            : ''
        }
        <div class="storage-row"><span>Biblioteca: ${formatBytes(s.librarySize)}</span>
          <span>Órfãos (.part): ${formatBytes(s.partialsSize)} (${s.partialsCount})</span></div>
        <div class="storage-actions">
          <button type="button" class="dl-btn ghost" data-action="cleanup-partials" ${s.partialsCount ? '' : 'disabled'}>Limpar órfãos</button>
        </div>
      </div>
      <div class="storage-block">
        <div class="storage-head"><strong>Dispositivo</strong></div>
        ${
          s.deviceQuota
            ? `<div class="storage-bar"><div class="storage-fill" style="width:${devPct}%"></div></div>
        <div class="storage-row"><span>${formatBytes(s.deviceUsage)} de ${formatBytes(s.deviceQuota)}</span></div>`
            : ''
        }
        <div class="storage-row"><span>Cache offline: ${formatBytes(s.cacheBytes)}</span></div>
        <div class="storage-actions">
          <button type="button" class="dl-btn ghost" data-action="clear-device-cache" ${s.cacheBytes ? '' : 'disabled'}>Limpar cache</button>
        </div>
      </div>
    </div>`;
}

// Ações de armazenamento/pausa. Toasts com contagem em pt-BR; refresh após ação.
async function pauseTasks(ids) {
  try {
    const r = await pauseTasksApi(ids);
    const n = (r.paused || []).length;
    showToast(`${n} ${n === 1 ? 'download pausado' : 'downloads pausados'}`, 'success');
    refreshDownloads();
  } catch (err) {
    handleApiError(err, 'Não foi possível pausar.');
  }
}

async function resumeTasks(ids) {
  try {
    const r = await resumeTasksApi(ids);
    const n = (r.resumed || []).length;
    showToast(`${n} ${n === 1 ? 'download retomado' : 'downloads retomados'}`, 'success');
    refreshDownloads();
  } catch (err) {
    handleApiError(err, 'Não foi possível retomar.');
  }
}

async function cleanupPartials() {
  try {
    const r = await storageCleanupApi();
    const freed = Number(r.freed_bytes) || 0;
    showToast(freed > 0 ? `${formatBytes(freed)} liberados` : 'Nada a limpar', freed > 0 ? 'success' : 'info');
    loadStorageData();
  } catch (err) {
    handleApiError(err, 'Não foi possível limpar órfãos.');
  }
}

async function clearDeviceCache() {
  try {
    if ('caches' in window) await caches.delete(AUDIO_CACHE_NAME);
    showToast('Cache offline limpo', 'success');
  } catch {
    showToast('Não foi possível limpar o cache.', 'error');
  }
  loadStorageData();
}

// Mostra/oculta os botões em lote ("Pausar tudo"/"Retomar todos") conforme há
// tasks ativas (pending/running) ou pausadas.
function updateBatchButtons() {
  const pauseAll = document.getElementById('pause-all-btn');
  const resumeAll = document.getElementById('resume-all-btn');
  if (!pauseAll && !resumeAll) return;
  const tasks = [...state.tasks.values()];
  const hasActive = tasks.some((t) => t.status === 'pending' || t.status === 'running');
  const hasPaused = tasks.some((t) => t.status === 'paused');
  if (pauseAll) pauseAll.hidden = !hasActive;
  if (resumeAll) resumeAll.hidden = !hasPaused;
}

// ------------------------------------------------------------- playlists

async function refreshPlaylists() {
  try {
    state.playlists = await apiFetch('/api/playlists');
  } catch (err) {
    state.playlists = [];
    handleApiError(err, 'Não foi possível carregar as playlists.');
  }
  renderPlaylists();
  renderPlayerPlaylistSelect();
}

function renderPlaylists() {
  const listEl = document.getElementById('playlists-list');
  if (!listEl) return;
  if (!state.playlists.length) {
    listEl.innerHTML = '<p class="empty-state">Nenhuma playlist ainda.</p>';
    return;
  }
  listEl.innerHTML = state.playlists
    .map(
      (pl) => `
    <div class="playlist-item" data-id="${escapeHtml(pl.id)}">
      <div class="playlist-head">
        <span class="playlist-name">${escapeHtml(pl.name)}</span>
        <span class="playlist-count">${pl.track_count} ${pl.track_count === 1 ? 'faixa' : 'faixas'}</span>
        <div class="playlist-actions">
          <button type="button" class="btn btn-ghost btn-small pl-play-btn">▶ Tocar</button>
          <a class="btn btn-ghost btn-small pl-export-btn" href="/api/playlists/${escapeHtml(pl.id)}/export.m3u${state.token ? `?token=${encodeURIComponent(state.token)}` : ''}" download>.M3U</a>
          <button type="button" class="icon-btn history-btn pl-toggle" aria-label="Abrir ${escapeHtml(pl.name)}">▾</button>
          <button type="button" class="icon-btn history-btn history-remove pl-delete" aria-label="Apagar ${escapeHtml(pl.name)}">✕</button>
        </div>
      </div>
      <div class="pl-tracks" hidden></div>
    </div>`
    )
    .join('');

  listEl.querySelectorAll('.pl-toggle').forEach((btn) => {
    btn.addEventListener('click', () => togglePlaylistTracks(btn.closest('.playlist-item')));
  });
  listEl.querySelectorAll('.pl-delete').forEach((btn) => {
    btn.addEventListener('click', () => deletePlaylist(btn.closest('.playlist-item')));
  });
  listEl.querySelectorAll('.pl-play-btn').forEach((btn) => {
    btn.addEventListener('click', () => playPlaylist(btn.closest('.playlist-item')));
  });
}

async function togglePlaylistTracks(item) {
  const tracksEl = item.querySelector('.pl-tracks');
  const id = item.dataset.id;
  if (!tracksEl.hidden) {
    tracksEl.hidden = true;
    return;
  }
  try {
    const pl = await apiFetch(`/api/playlists/${id}`);
    tracksEl.innerHTML = pl.tracks.length
      ? `<ul class="pl-track-list">${pl.tracks
          .map(
            (t) => `
            <li class="pl-track-row" data-yt-id="${escapeHtml(t.yt_id)}">
              <span class="pl-track-title${t.path ? '' : ' is-unplayable'}">${escapeHtml(t.title)}</span>
              ${
                t.path
                  ? '<button type="button" class="icon-btn history-btn pl-track-play" aria-label="Tocar">▶</button>'
                  : ''
              }
              <button type="button" class="icon-btn history-btn history-remove pl-track-remove" aria-label="Remover da playlist">✕</button>
            </li>`
          )
          .join('')}</ul>`
      : '<p class="empty-state">Playlist vazia.</p>';
    tracksEl.hidden = false;

    tracksEl.querySelectorAll('.pl-track-play').forEach((btn) => {
      btn.addEventListener('click', () => {
        const track = pl.tracks.find((t) => t.yt_id === btn.closest('.pl-track-row').dataset.ytId);
        if (track && track.path) playLibraryQueue([track], 0);
      });
    });
    tracksEl.querySelectorAll('.pl-track-remove').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const ytId = btn.closest('.pl-track-row').dataset.ytId;
        try {
          await apiFetch(`/api/playlists/${id}/tracks/${encodeURIComponent(ytId)}`, {
            method: 'DELETE',
          });
          showToast('Faixa removida', 'success');
          refreshPlaylists();
        } catch (err) {
          handleApiError(err, 'Não foi possível remover.');
        }
      });
    });
  } catch (err) {
    handleApiError(err, 'Não foi possível carregar a playlist.');
  }
}

async function deletePlaylist(item) {
  if (!window.confirm('Apagar esta playlist?')) return;
  try {
    await apiFetch(`/api/playlists/${item.dataset.id}`, { method: 'DELETE' });
    showToast('Playlist apagada', 'success');
    refreshPlaylists();
  } catch (err) {
    handleApiError(err, 'Não foi possível apagar.');
  }
}

function playPlaylist(item) {
  apiFetch(`/api/playlists/${item.dataset.id}`)
    .then((pl) => {
      const playable = pl.tracks.filter((t) => t.path);
      if (!playable.length) {
        showToast('Playlist sem faixas baixadas.', 'error');
        return;
      }
      playLibraryQueue(playable, 0);
    })
    .catch((err) => handleApiError(err, 'Não foi possível tocar.'));
}

// Replica a lista de playlists no <select> da tela do player (salvar faixa atual).
function renderPlayerPlaylistSelect() {
  const select = document.getElementById('pv-playlist-select');
  if (!select) return;
  const current = select.value;
  select.innerHTML =
    `<option value="">Salvar na playlist…</option>` +
    state.playlists.map((pl) => `<option value="${escapeHtml(pl.id)}">${escapeHtml(pl.name)}</option>`).join('');
  select.value = state.playlists.some((pl) => String(pl.id) === current) ? current : '';
}

function bindDownloadsEvents() {
  const refresh = document.getElementById('refresh-btn');
  if (refresh) refresh.addEventListener('click', refreshDownloads);

  const createForm = document.getElementById('playlist-create-form');
  if (createForm) {
    createForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = document.getElementById('playlist-name-input');
      const name = (input ? input.value : '').trim();
      if (!name) {
        if (input) input.focus();
        return;
      }
      try {
        await apiFetch('/api/playlists', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
        showToast('Playlist criada!', 'success');
        if (input) input.value = '';
        refreshPlaylists();
      } catch (err) {
        handleApiError(err, 'Não foi possível criar a playlist.');
      }
    });
  }

  const retryFailed = document.getElementById('retry-failed-btn');
  if (retryFailed) {
    retryFailed.addEventListener('click', async () => {
      try {
        const n = await retryFailedApi();
        showToast(`${n} ${n === 1 ? 'task re-enfileirada' : 'tasks re-enfileiradas'}`, 'success');
        refreshDownloads();
      } catch (err) {
        handleApiError(err, 'Não foi possível retentar falhas.');
      }
    });
  }

  // Ações da seção de armazenamento (delegate no container da aba).
  const viewEl = document.querySelector('.downloads-view');
  if (viewEl) {
    viewEl.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      if (action === 'refresh-storage') loadStorageData();
      else if (action === 'cleanup-partials') cleanupPartials();
      else if (action === 'clear-device-cache') clearDeviceCache();
    });
  }

  // Pausa/retoma em lote (todas as tasks ativas/pausadas).
  const pauseAll = document.getElementById('pause-all-btn');
  if (pauseAll) pauseAll.addEventListener('click', () => pauseTasks());
  const resumeAll = document.getElementById('resume-all-btn');
  if (resumeAll) resumeAll.addEventListener('click', () => resumeTasks());
}

// Fallback REST + histórico (usado ao abrir a aba e se o WS cair).
async function refreshDownloads() {
  try {
    const tasks = await listDownloadsApi();
    mergeTasks(tasks);
    renderTaskList();
    // Transferência automática: dispara para downloads avulsos concluídos
    // (gate em triggerAutoDownload evita flood ao baixar álbuns inteiros).
    tasks.filter((t) => t.status === 'done').forEach(triggerAutoDownload);
    tasks.forEach(notifyDownload); // avisa no celular mesmo chegando depois
    pruneTransientState();
  } catch (err) {
    handleApiError(err, 'Não foi possível carregar a fila.');
  }
  try {
    state.history = await historyApi();
    // Na aba Biblioteca renderiza a visão ativa (segmentos/filtros); na aba
    // Downloads, o histórico plano de sempre.
    if (state.currentView === 'biblioteca') renderLibrary();
    else renderHistory();
  } catch (err) {
    handleApiError(err, 'Não foi possível carregar o histórico.');
  }
  syncDlButtons(); // tasks/histórico mudaram → atualiza botões de download visíveis
  loadStorageData(); // números de armazenamento (aba Downloads + alerta de disco)
}

// Re-enfileira todas as tasks failed (mesma rota do botão "Retentar Falhas").
// Usada também pelo auto-retry ao voltar online (melhor esforço — o backend
// retenta sozinho de qualquer forma).
async function retryFailedApi() {
  const res = await apiFetch('/api/downloads/retry-failed', { method: 'POST' });
  return Number(res && res.retried_count) || 0; // pode vir undefined → evita "undefined tasks"
}

// Indicador de conexão: banner fixo no topo quando offline (criado como primeiro
// filho do body, sem duplicar), removido ao voltar online.
function updateConnBanner() {
  const body = document.body;
  if (!body) return;
  if (!state.online) {
    let banner = document.getElementById(OFFLINE_BANNER_ID);
    if (!banner) {
      banner = document.createElement('div');
      banner.id = OFFLINE_BANNER_ID;
      banner.setAttribute('role', 'status');
      banner.textContent = 'Você está offline — os downloads serão retomados automaticamente.';
      body.insertBefore(banner, body.firstChild); // primeiro filho do body
    }
    banner.classList.add('is-visible');
    return;
  }
  const banner = document.getElementById(OFFLINE_BANNER_ID);
  if (banner) {
    banner.classList.remove('is-visible');
    banner.remove();
  }
}

// Volta online: esconde o banner, avisa, atualiza a fila e re-enfileira falhas.
function onNetworkOnline() {
  state.online = true;
  updateConnBanner();
  showToast('Conexão restaurada — retomando downloads', 'success');
  refreshDownloads();
  const hasFailed = [...state.tasks.values()].some((t) => t.status === 'failed');
  if (hasFailed) {
    retryFailedApi().catch((err) => handleApiError(err, 'Não foi possível retentar falhas.'));
  }
}

// Caiu a conexão: apenas mostra o banner (o backend pausa/retoma sozinho).
function onNetworkOffline() {
  state.online = false;
  updateConnBanner();
}

// Mescla preservando a ordem de inserção (novos no fim, existentes atualizados).
function mergeTasks(tasks) {
  const next = new Map();
  state.tasks.forEach((task, id) => next.set(id, task));
  tasks.forEach((task) => {
    const existing = next.get(task.task_id);
    const merged = existing ? { ...existing, ...task } : task;
    stampTerminalAt(merged);
    next.set(task.task_id, merged);
  });
  state.tasks = next;
}

// Registra quando a task entrou em status terminal (usado pela poda por TTL).
function stampTerminalAt(task) {
  if (!task || !task.task_id) return;
  const s = task.status || 'pending';
  if (s === 'done' || s === 'failed' || s === 'skipped' || s === 'cancelled') {
    if (!task._terminalAt) task._terminalAt = Date.now();
  }
}

// Poda de estado transitório (TTL ~10 min): impede que notifiedTasks,
// autoDownloaded e a Fila (queueSessionIds/state.tasks) cresçam sem limite.
// Tasks em status terminal somem da Fila após o TTL — continuam no Histórico.
const TRANSIENT_TTL_MS = 10 * 60 * 1000;

function pruneTransientState() {
  const now = Date.now();
  const ttl = TRANSIENT_TTL_MS;

  for (const [id, ts] of state.notifiedTasks) {
    if (now - ts > ttl) state.notifiedTasks.delete(id);
  }
  for (const [id, ts] of state.autoDownloaded) {
    if (now - ts > ttl) state.autoDownloaded.delete(id);
  }

  for (const [id, task] of state.tasks) {
    const s = task.status || 'pending';
    const terminal = s === 'done' || s === 'failed' || s === 'skipped' || s === 'cancelled';
    if (!terminal) continue;
    if (!task._terminalAt) task._terminalAt = now; // sem carimbo → assume recente
    if (now - task._terminalAt > ttl) {
      state.queueSessionIds.delete(id);
      if (!state.taskEls.has(id)) state.tasks.delete(id); // some do estado, fica no Histórico
    }
  }
}

function renderTaskList() {
  const listEl = document.getElementById('task-list');
  if (!listEl) return;
  state.taskEls.clear();
  // A Fila mostra tasks ativas + pausadas + as concluídas/falhas DESTA sessão.
  // Cards "concluído" de sessões/restarts antigos ficam só no Histórico — era
  // isso que fazia "todas as músicas aparecerem concluídas" de uma vez.
  const active = ['pending', 'running', 'failed', 'cancelled', 'paused'];
  const tasks = [...state.tasks.values()].filter(
    (t) => active.includes(t.status || 'pending') || state.queueSessionIds.has(t.task_id)
  );

  updateDownloadsBadge();
  updateBatchButtons();

  if (tasks.length === 0) {
    listEl.innerHTML = '<p class="empty-state">Nenhum download na fila.</p>';
    return;
  }

  listEl.innerHTML = '';
  tasks.forEach((task) => {
    const el = document.createElement('div');
    el.className = 'task-card';
    updateTaskCard(el, task);
    listEl.appendChild(el);
    state.taskEls.set(task.task_id, el);
  });
}

// HTML completo do card para um dado estado da task. A atualização de progresso
// NÃO passa por aqui — updateTaskCard faz patch por nós (preserva foco de
// botões e a reprodução do <audio> de preview).
function taskCardHtml(task) {
  const status = task.status || 'pending';
  const stage = task.stage || 'queued';
  const progress = clampNumber(task.progress, 0, 100);
  const title = task.title || 'Música';

  const metaParts = [task.artist, task.album].filter(Boolean);
  const meta = metaParts.length ? escapeHtml(metaParts.join(' · ')) : '';
  const chip = task.format
    ? `<span class="chip">${escapeHtml(formatLabel(task.format))}</span>`
    : '';
  // Task failed com retry pendente (erro de rede): badge transitório de
  // reconexão — o scheduler do backend muda para pending/queued sozinho (WS).
  const badge =
    status === 'failed' && task.retry_count > 0
      ? `<span class="badge badge-retry">Reconectando · tentativa ${task.retry_count}/3</span>`
      : `<span class="badge ${STATUS_BADGE[status] || 'badge-pending'}">${STATUS_LABEL[status] || status}</span>`;

  let actions = '';
  if (status === 'done' && task.path) {
    const audioUrl = escapeHtml(API.library(task.path));
    actions = `
      <div class="audio-preview">
        <audio controls preload="none" src="${audioUrl}"></audio>
      </div>
      <div class="task-action-btns">
        <a
          class="btn btn-primary btn-small save-link"
          href="${audioUrl}"
          download
        >${ICONS.download} Salvar no celular</a>
        <button type="button" class="btn btn-ghost btn-small play-mini-btn" data-url="${audioUrl}" data-title="${escapeHtml(task.title || '')}" data-artist="${escapeHtml(task.artist || '')}" data-cover="${escapeHtml(task.cover_url || '')}" data-yt-id="${escapeHtml(task.yt_id || '')}">▶ Tocar</button>
        <button type="button" class="btn btn-ghost btn-small edit-meta-btn" data-yt-id="${escapeHtml(task.yt_id)}" data-title="${escapeHtml(task.title || '')}" data-artist="${escapeHtml(task.artist || '')}" data-album="${escapeHtml(task.album || '')}">Editar Tags</button>
      </div>`;
  } else if (status === 'pending' || status === 'running') {
    actions = `<button type="button" class="btn btn-ghost btn-small cancel-btn">Cancelar</button>`;
  } else if (status === 'paused') {
    // Pausado: progresso congelado (.part preservado) — Retomar ou Cancelar.
    actions = `
      <div class="task-action-btns">
        <button type="button" class="btn btn-primary btn-small" data-action="resume-task" aria-label="Retomar ${escapeHtml(title)}">Retomar</button>
        <button type="button" class="btn btn-ghost btn-small cancel-btn" data-action="cancel-task" aria-label="Cancelar ${escapeHtml(title)}">Cancelar</button>
      </div>`;
  } else if (status === 'failed') {
    actions = `
      ${task.error ? `<p class="task-error">${escapeHtml(task.error)}</p>` : ''}
      <button type="button" class="btn btn-ghost btn-small retry-btn">Tentar de novo</button>`;
  }

  return `
    <div class="task-head">
      <span class="task-title">${escapeHtml(title)}</span>
      ${badge}
    </div>
    <div class="task-meta">${meta ? meta + ' ' : ''}${chip}</div>
    <div class="progress-row">
      <div
        class="progress-track"
        role="progressbar"
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow="${Math.round(progress)}"
        aria-label="${escapeHtml(title)}"
      >
        <div class="progress-fill" style="width:${progress}%"></div>
      </div>
      <span class="stage-label">${STAGE_LABEL[stage] || stage} · ${Math.round(progress)}%</span>
    </div>
    ${actions ? `<div class="task-actions">${actions}</div>` : ''}
  `;
}

// Chave do bloco de ações — mudou ⇒ o card precisa ser reconstruído (não é só
// progresso). Cobre a transição done-sem-path → done-com-path (refreshTaskMeta).
function taskActionKey(task) {
  const status = task.status || 'pending';
  if (status === 'done' && task.path) return 'done';
  if (status === 'pending' || status === 'running') return 'running';
  if (status === 'failed') return 'failed';
  if (status === 'paused') return 'paused'; // chave própria (ações Retomar/Cancelar)
  return status;
}

// Liga os eventos dos botões do card (chamado após reconstruir o HTML).
function bindTaskCardEvents(el, task) {
  const playMini = el.querySelector('.play-mini-btn');
  if (playMini) {
    playMini.addEventListener('click', () => {
      playMiniTrack({
        url: playMini.dataset.url,
        title: playMini.dataset.title,
        artist: playMini.dataset.artist,
        cover: playMini.dataset.cover || '',
        ytId: playMini.dataset.ytId || '',
      });
    });
  }

  const cancelBtn = el.querySelector('.cancel-btn');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => cancelTask(task));
  }

  const resumeBtn = el.querySelector('[data-action="resume-task"]');
  if (resumeBtn) {
    resumeBtn.addEventListener('click', () => resumeTasks([task.task_id]));
  }

  const retryBtn = el.querySelector('.retry-btn');
  if (retryBtn) {
    retryBtn.addEventListener('click', () => retryTask(task));
  }

  const editBtn = el.querySelector('.edit-meta-btn');
  if (editBtn) {
    editBtn.addEventListener('click', () => openEditMetaModal(editBtn.dataset));
  }
}

// Atualiza o card de forma INCREMENTAL: cada tick de progresso faz patch só nos
// nós que mudaram (width do .progress-fill, texto do .stage-label e aria do
// progressbar) — nada de innerHTML a cada update do WS. A estrutura só é
// reconstruída quando o STATUS ou o bloco de ações muda, e nesse caso um
// <audio> de preview que esteja TOCANDO é reaproveitado em vez de recriado.
function updateTaskCard(el, task) {
  const status = task.status || 'pending';
  const stage = task.stage || 'queued';
  const progress = clampNumber(task.progress, 0, 100);
  const actionKey = taskActionKey(task);

  const needsRebuild = el.dataset.status !== status || el.dataset.actionKey !== actionKey;

  if (needsRebuild) {
    // Se um <audio> de preview está tocando, preserva o elemento ao reconstruir
    // (recriar mataria a reprodução em andamento).
    const oldAudio = el.querySelector('.audio-preview audio');
    const playing = oldAudio && !oldAudio.paused;

    el.innerHTML = taskCardHtml(task);
    el.dataset.status = status;
    el.dataset.actionKey = actionKey;
    bindTaskCardEvents(el, task);

    if (playing) {
      const wrap = el.querySelector('.audio-preview');
      if (wrap) {
        wrap.innerHTML = '';
        wrap.appendChild(oldAudio); // mantém o áudio tocando sem recriar
      }
    }
    return;
  }

  // Patch incremental de progresso — não toca no resto do card (foco, <audio>).
  const fill = el.querySelector('.progress-fill');
  if (fill) fill.style.width = `${progress}%`;
  const track = el.querySelector('.progress-track');
  if (track) track.setAttribute('aria-valuenow', String(Math.round(progress)));
  const stageLabel = el.querySelector('.stage-label');
  if (stageLabel) stageLabel.textContent = `${STAGE_LABEL[stage] || stage} · ${Math.round(progress)}%`;
}

async function retryTask(task) {
  try {
    // Re-POST com o mesmo yt_id e formato da task que falhou
    const data = await postDownloadApi({ yt_id: task.yt_id, formato: task.format });
    if (data.task) ingestTask(data.task);
    showToast('Tentando de novo…', 'success');
    // Remove o card falho e recarrega o snapshot (a nova task aparece na fila)
    state.tasks.delete(task.task_id);
    state.taskEls.delete(task.task_id);
    await refreshDownloads();
  } catch (err) {
    handleApiError(err, 'Não foi possível tentar de novo.');
  }
}

async function cancelTask(task) {
  try {
    await apiFetch(`/api/downloads/${encodeURIComponent(task.task_id)}`, { method: 'DELETE' });
    showToast('Download cancelado', 'success');
    // Aplica o estado local na hora (o WS/refresh confirma em seguida)
    task.status = 'cancelled';
    task.stage = 'cancelled';
    const el = state.taskEls.get(task.task_id);
    if (el) updateTaskCard(el, task);
    refreshDownloads();
  } catch (err) {
    handleApiError(err, 'Não foi possível cancelar.');
  }
}

function renderHistory(records) {
  const listEl = document.getElementById('history-list');
  if (!listEl) return;
  // `records` opcional: a Biblioteca (renderLibrary) passa a lista já filtrada
  // por formato/visão; sem argumento, usa state.history (view Downloads).
  const items = records || state.history || [];
  if (!items || items.length === 0) {
    listEl.innerHTML = '<p class="empty-state">Nenhum download ainda.</p>';
    return;
  }
  // playFromHistory indexa sobre TODAS as faixas tocáveis do histórico (a fila
  // do player é global) — o índice das linhas precisa ser o da lista completa,
  // mesmo quando a Biblioteca filtra por formato (items ≠ state.history).
  const fullPlayable = (state.history || []).filter((r) => r.status === 'done' && r.path);
  listEl.innerHTML = items
    .map((record) => {
      const status = record.status || 'pending';
      const idx = fullPlayable.indexOf(record);
      const playBtn = idx >= 0
        ? `<button type="button" class="icon-btn history-btn history-play" data-idx="${idx}" aria-label="Tocar ${escapeHtml(record.title || '')}">▶</button>`
        : '';
      const cover = record.cover_url
        ? `<img src="${escapeHtml(record.cover_url)}" alt="" class="history-cover" />`
        : `<span class="history-cover history-cover--vinyl" aria-hidden="true">♪</span>`;
      return `
        <div class="history-item">
          ${cover}
          <div class="history-main">
            <span class="history-title">${escapeHtml(record.title || 'Sem título')}</span>
            <span class="history-date">${formatDate(record.date)}${
              record.format ? ` · ${escapeHtml(formatLabel(record.format))}` : ''
            }</span>
          </div>
          <div class="history-actions">
            ${playBtn}
            <button type="button" class="icon-btn history-btn history-remove" data-yt-id="${escapeHtml(record.yt_id || '')}" aria-label="Remover ${escapeHtml(record.title || '')}">✕</button>
          </div>
          <span class="badge ${STATUS_BADGE[status] || 'badge-pending'}">${STATUS_LABEL[status] || status}</span>
        </div>`;
    })
    .join('');

  listEl.querySelectorAll('.history-play').forEach((btn) => {
    btn.addEventListener('click', () => playFromHistory(Number(btn.dataset.idx)));
  });
  listEl.querySelectorAll('.history-remove').forEach((btn) => {
    btn.addEventListener('click', () => removeHistoryItem(btn.dataset.ytId));
  });

  // Swipe (aceleração): esquerda → fila do player; direita → baixar rápido.
  listEl.querySelectorAll('.history-item').forEach((row, i) => {
    const record = items[i];
    if (!record) return;
    bindSwipe(
      row,
      () => addHistoryToQueue(record),
      () => downloadSingleTrack({ yt_id: record.yt_id, title: record.title })
    );
  });
}

async function removeHistoryItem(ytId) {
  if (!ytId) return;
  if (!window.confirm('Remover esta música da biblioteca? O arquivo será apagado do servidor.')) {
    return;
  }
  try {
    await apiFetch(`/api/history/${encodeURIComponent(ytId)}`, { method: 'DELETE' });
    showToast('Removido da biblioteca', 'success');
    refreshDownloads();
  } catch (err) {
    handleApiError(err, 'Não foi possível remover.');
  }
}

// ---------------------------------------------------------- mini-player

function playerEl(id) {
  return document.getElementById(id);
}

function currentTrack() {
  return state.playerQueue && state.playerQueue[state.playerIndex];
}

// Toca uma faixa avulsa (card de task concluída).
function playMiniTrack(track) {
  revokeLocalObjectUrls(); // fila do servidor substitui a local → libera URLs
  state.playerQueue = [track];
  state.playerIndex = 0;
  startPlayer();
}

// Toca a partir do histórico: a fila vira todas as faixas concluídas.
function playFromHistory(index) {
  const playable = state.history.filter((r) => r.status === 'done' && r.path);
  const item = playable[index];
  if (!item) return;
  revokeLocalObjectUrls(); // fila do servidor substitui a local → libera URLs
  state.playerQueue = playable.map((r) => ({
    url: API.library(r.path),
    title: r.title || 'Música',
    artist: r.artist || '',
    cover: r.cover_url || '',
    ytId: r.yt_id || '',
  }));
  state.playerIndex = index;
  startPlayer();
}

// Adiciona um registro do histórico à fila do player (sem tocar na faixa
// atual). Se a fila estiver vazia, já toca direto. Handler real do swipe
// esquerda (histórico/biblioteca).
function addHistoryToQueue(record) {
  if (!record || !record.yt_id) return;
  if (!record.path) {
    showToast('Faixa ainda não baixada.', 'error');
    return;
  }
  const track = {
    url: API.library(record.path),
    title: record.title || 'Música',
    artist: record.artist || '',
    cover: record.cover_url || '',
    ytId: record.yt_id || '',
  };
  if (state.playerQueue.some((t) => t.ytId === track.ytId)) {
    showToast(`“${track.title}” já está na fila`, 'info');
    return;
  }
  state.playerQueue.push(track);
  showToast(`“${track.title}” adicionado à fila`, 'success');
  if (state.playerQueue.length === 1) {
    state.playerIndex = 0; // nada tocando → toca direto
    startPlayer();
  }
}

// Gesto de swipe (touch) em linhas de histórico/biblioteca: esquerda → onLeft,
// direita → onRight. Aceleração — os botões/AC existentes continuam. Dominância
// horizontal (|dx| > |dy|) para não conflitar com o scroll vertical; deslocamento
// visual limitado a ±120px; respeita prefers-reduced-motion.
function bindSwipe(el, onLeft, onRight) {
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  let startX = 0;
  let startY = 0;
  let dragging = false;
  let dx = 0;
  const THRESHOLD = 60;

  el.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'touch') return;
    startX = e.clientX;
    startY = e.clientY;
    dragging = true;
    dx = 0;
    el.classList.add('row-swipe');
    try {
      el.setPointerCapture(e.pointerId);
    } catch {
      /* noop */
    }
  });

  el.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    dx = e.clientX - startX;
    // Dominância vertical (scroll) → aborta o gesto sem "puxar" a linha.
    if (Math.abs(e.clientY - startY) > Math.abs(dx)) {
      dragging = false;
      el.style.transform = '';
      return;
    }
    el.style.transform = `translateX(${Math.max(-120, Math.min(120, dx))}px)`;
  });

  const end = () => {
    if (!dragging) return;
    dragging = false;
    el.style.transform = '';
    el.classList.remove('row-swipe');
    if (dx <= -THRESHOLD && onLeft) onLeft(el);
    else if (dx >= THRESHOLD && onRight) onRight(el);
  };

  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);
}

function startPlayer() {
  const track = currentTrack();
  if (!track) return;
  const bar = playerEl('player-bar');
  if (bar) bar.hidden = false;
  const title = playerEl('player-title');
  const artist = playerEl('player-artist');
  if (title) title.textContent = track.title || 'Música';
  if (artist) artist.textContent = track.artist || 'MusicBox';
  const miniCover = playerEl('player-mini-cover');
  if (miniCover) {
    miniCover.innerHTML = track.cover
      ? `<img src="${escapeHtml(track.cover)}" alt="" />`
      : '<span aria-hidden="true">♪</span>';
  }
  const audio = playerEl('global-audio-element');
  if (audio) {
    if (state.crossfadeSeconds > 0 && audio.src) {
      // Crossfade ativo: a faixa atual sai com fade-out enquanto a próxima
      // entra com fade-in no elemento auxiliar (a troca central acontece em
      // swapToMain). No 'ended' natural o principal já silenciou — o fade-in
      // do auxiliar continua e cobre a transição.
      crossfadeTo(track, audio);
    } else {
      // Troca seca (1ª faixa, gapless ou crossfade desligado).
      state._discSuppressPause = true; // troca de faixa: o disco não decelera
      audio.src = track.url;
      audio.play()
        .then(() => {
          state._discSuppressPause = false;
          syncDiscState();
        })
        .catch(() => {
          state._discSuppressPause = false;
          stopDiscSpin();
        });
      if (state.crossfadeSeconds <= 0) preloadNextTrack(); // gapless: pré-decode
    }
  }
  updatePlayBtn();
  updateMediaSession();
  syncPlayerView();
  syncPlayingCards();
  syncLyricsForTrack(track); // invalida cache antigo; busca se o pane de letras está aberto
}

// ------------------------------------------------------------------ vinil

// Disco de vinil girando via requestAnimationFrame (33⅓ rpm ≈ 200°/s). O easing
// exponencial dá a aceleração ao tocar e a parada SUAVE ao pausar (~0.5s).
const DISC_DEG_PER_S = 200; // 33⅓ rpm — aparência de toca-discos real
const DISC_EASE_TAU = 0.22; // constante de tempo do easing (≈90% em ~0.5s)
const DISC_STOP_EPS = 0.4; // deg/s — abaixo disso o rAF encerra

function prefersReducedMotion() {
  return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

// Sincroniza vinil + agulha com o estado do <audio>. Chamado no play/pause e
// nas trocas de faixa. Com prefers-reduced-motion o disco NÃO gira (a agulha
// continua sendo alternada — a transição CSS vira instantânea pelo media query
// global do projeto).
function syncDiscState() {
  const audio = playerEl('global-audio-element');
  const playing = !!(audio && audio.src && !audio.paused);
  const tonearm = document.querySelector('.player-tonearm');
  if (tonearm) tonearm.classList.toggle('is-playing', playing);
  if (prefersReducedMotion()) return;
  if (playing) startDiscSpin();
  else if (!state._discSuppressPause) stopDiscSpin();
}

function startDiscSpin() {
  state.discTarget = DISC_DEG_PER_S;
  state.discDecelerating = false;
  if (state.discRaf === null) {
    state._discLastTs = null;
    state.discRaf = requestAnimationFrame(discFrame);
  }
}

// Pausa suave: continua o rAF decelerando até ~0 e então encerra.
function stopDiscSpin() {
  if (state.discRaf === null && !state.discDecelerating) return; // já parado
  state.discTarget = 0;
  state.discDecelerating = true;
}

// Para imediatamente (fechar o player) — sem easing.
function haltDiscSpin() {
  state.discTarget = 0;
  state.discVelocity = 0;
  state.discDecelerating = false;
  if (state.discRaf !== null) {
    cancelAnimationFrame(state.discRaf);
    state.discRaf = null;
  }
  applyDiscAngle();
}

function discFrame(ts) {
  if (prefersReducedMotion()) {
    state.discRaf = null; // preferência mudou no meio do voo — encerra
    return;
  }
  if (state._discLastTs === null) {
    state._discLastTs = ts;
    state.discRaf = requestAnimationFrame(discFrame);
    return;
  }
  const dt = Math.min(0.1, (ts - state._discLastTs) / 1000); // clamp p/ abas em background
  state._discLastTs = ts;
  const target = state.discTarget;
  state.discVelocity += (target - state.discVelocity) * (1 - Math.exp(-dt / DISC_EASE_TAU));
  state.discAngle = (state.discAngle + state.discVelocity * dt) % 360;
  applyDiscAngle();
  if (state.discDecelerating && Math.abs(state.discVelocity) < DISC_STOP_EPS) {
    state.discVelocity = 0;
    state.discDecelerating = false;
    state.discRaf = null; // encerra o loop (sem agendar o próximo frame)
    return;
  }
  state.discRaf = requestAnimationFrame(discFrame);
}

// Aplica o ângulo acumulado no disco. O transform é definido inline pelo rAF;
// a animação CSS antiga (.player-disc.is-playing) é desativada inline para o
// easing funcionar (senão os keyframes sobrescreveriam o transform).
function applyDiscAngle() {
  const disc = document.querySelector('.player-disc');
  if (!disc) return;
  disc.style.animation = 'none';
  disc.style.transform = `rotate(${state.discAngle.toFixed(2)}deg)`;
}

// --------------------------------------------------------- crossfade/gapless

const CROSSFADE_MAX = 12; // máx do slider de crossfade (segundos)

function loadCrossfade() {
  let v = 0;
  try {
    v = Number(localStorage.getItem(STORAGE_CROSSFADE_KEY));
  } catch {
    v = 0; // storage bloqueado → gapless (default)
  }
  if (!Number.isFinite(v)) v = 0;
  return clampNumber(Math.round(v), 0, CROSSFADE_MAX);
}

function saveCrossfade(v) {
  const val = clampNumber(Math.round(Number(v) || 0), 0, CROSSFADE_MAX);
  state.crossfadeSeconds = val;
  try {
    localStorage.setItem(STORAGE_CROSSFADE_KEY, String(val));
  } catch {
    // storage bloqueado — vale apenas para esta sessão
  }
  syncCrossfadeControl();
}

function crossfadeLabel(v) {
  const n = Number(v) || 0;
  return n === 0 ? 'Gapless' : `${n}s`;
}

// Slider + label na tela do player. Os elementos são recriados a cada render da
// view — por isso o bind real acontece no bindPlayerViewEvents (o init chama
// com guard; se os elementos não existirem, é no-op).
function bindCrossfadeControl() {
  const range = document.getElementById('crossfade-range');
  if (range) {
    range.addEventListener('input', () => saveCrossfade(Number(range.value)));
    range.addEventListener('change', () => saveCrossfade(Number(range.value)));
  }
  syncCrossfadeControl();
}

function syncCrossfadeControl() {
  const range = document.getElementById('crossfade-range');
  const label = document.getElementById('crossfade-label');
  if (range) range.value = String(state.crossfadeSeconds);
  if (label) label.textContent = crossfadeLabel(state.crossfadeSeconds);
}

function ensureAuxAudio() {
  if (!state._auxAudio && typeof Audio !== 'undefined') {
    state._auxAudio = new Audio();
    state._auxAudio.preload = 'auto';
  }
  return state._auxAudio;
}

function disposeAuxAudio() {
  if (state._auxAudio) {
    try {
      state._auxAudio.pause();
      state._auxAudio.removeAttribute('src');
      state._auxAudio.load();
    } catch {
      /* noop */
    }
    state._auxAudio = null;
  }
  state._auxUrl = null;
}

// Gapless (crossfade 0): pré-carrega a PRÓXIMA faixa no elemento auxiliar
// (preload auto + load) para o navegador já ter rede/decode aquecidos — no
// 'ended', a troca de src no principal fica quase instantânea (gap prático).
function preloadNextTrack() {
  if (state.playerQueue.length < 2) {
    disposeAuxAudio();
    return;
  }
  const next = state.playerQueue[(state.playerIndex + 1) % state.playerQueue.length];
  if (!next || !next.url) return;
  try {
    const aux = ensureAuxAudio();
    if (state._auxUrl !== next.url) {
      aux.src = next.url;
      state._auxUrl = next.url;
      aux.load();
    }
  } catch {
    /* melhor-esforço — sem auxiliar o fluxo segue normal */
  }
}

// Crossfade (1-12s): a faixa entrante toca no elemento auxiliar com fade-in
// enquanto a principal faz fade-out. Ao fim (ou se o auxiliar falhar), o
// principal assume o src da nova faixa — sem repetir o início (continua da
// posição onde o auxiliar estava) — e o auxiliar é descartado.
function crossfadeTo(track, audio) {
  if (state._crossfading) abortCrossfade(); // próximo/prev durante o fade
  const seq = (state._crossfadeSeq || 0) + 1;
  state._crossfadeSeq = seq; // token: invalida fades substituídos/abortados
  const seconds = clampNumber(state.crossfadeSeconds, 1, CROSSFADE_MAX);
  const base = audio ? audio.volume : 1;
  state._crossfadeBaseVolume = base;
  const aux = ensureAuxAudio();
  if (!aux) {
    swapToMain(track, audio, base); // sem suporte a Audio → troca seca
    return;
  }
  aux.volume = 0;
  aux.src = track.url;
  state._auxUrl = track.url;
  try {
    aux.currentTime = 0;
  } catch {
    /* noop */
  }
  aux.play()
    .then(() => {
      state._crossfading = true;
      const start = performance.now();
      const tick = () => {
        if (state._crossfadeSeq !== seq) return; // fade substituído — sai sem agir
        const t = Math.min(1, (performance.now() - start) / (seconds * 1000));
        if (aux.error) {
          swapToMain(track, audio, base); // fallback: troca seca, sem toast
          return;
        }
        if (audio) audio.volume = base * (1 - t);
        aux.volume = base * t;
        if (t < 1) requestAnimationFrame(tick);
        else swapToMain(track, audio, base);
      };
      tick();
    })
    .catch(() => swapToMain(track, audio, base));
}

// O principal assume a faixa nova (posição herdada do auxiliar quando há) e o
// auxiliar é descartado. Troca seca nos dois casos (fim do fade ou falha).
function swapToMain(track, audio, baseVolume) {
  const aux = state._auxAudio;
  let pos = 0;
  if (aux && Number.isFinite(aux.currentTime) && aux.currentTime > 0) {
    pos = aux.currentTime; // continua do ponto onde o fade parou (sem repetir)
  }
  state._crossfading = false;
  state._crossfadeBaseVolume = undefined;
  if (audio) {
    state._discSuppressPause = true;
    audio.src = track.url;
    try {
      audio.currentTime = pos;
    } catch {
      /* noop */
    }
    audio.volume = baseVolume;
    audio.play()
      .then(() => {
        state._discSuppressPause = false;
        syncDiscState();
      })
      .catch(() => {
        state._discSuppressPause = false;
        stopDiscSpin();
      });
  }
  disposeAuxAudio();
}

// Cancela um crossfade em andamento (ex.: novo next/prev durante o fade):
// restaura o volume do usuário, descarta o auxiliar e invalida o fade (token).
function abortCrossfade() {
  state._crossfadeSeq = (state._crossfadeSeq || 0) + 1; // mata o tick pendente
  const audio = playerEl('global-audio-element');
  if (audio) audio.volume = state._crossfadeBaseVolume !== undefined ? state._crossfadeBaseVolume : 1;
  disposeAuxAudio();
  state._crossfading = false;
  state._crossfadeBaseVolume = undefined;
}

// Marca o card da música que está TOCANDO como .is-playing e injeta o
// equalizer (.eq-bars) no corpo do card + overlay de play (.card-play-overlay)
// sobre a capa. Remove tudo quando a faixa muda, PAUSA (decisão: card "tocando"
// = só tocando — o mini-player continua mostrando a faixa pausada) ou fecha.
// Os cards de busca (data-kind="song") são os únicos com mapeamento por yt_id.
function syncPlayingCards() {
  const audio = playerEl('global-audio-element');
  const playing = !!(audio && audio.src && !audio.paused);
  const track = currentTrack();
  const ytId = playing && track && track.ytId ? track.ytId : '';
  document.querySelectorAll('.card').forEach((card) => {
    const match = !!ytId && card.dataset.kind === 'song' && card.dataset.id === ytId;
    card.classList.toggle('is-playing', match);

    const body = card.querySelector('.card-body');
    const eq = body ? body.querySelector('.eq-bars') : null;
    if (match && !eq && body) {
      const bars = document.createElement('span');
      bars.className = 'eq-bars';
      bars.setAttribute('aria-hidden', 'true');
      bars.innerHTML = '<i></i><i></i><i></i>';
      body.appendChild(bars);
    } else if (!match && eq) {
      eq.remove();
    }

    let overlay = card.querySelector('.card-play-overlay');
    if (match && !overlay) {
      overlay = document.createElement('span');
      overlay.className = 'card-play-overlay';
      overlay.setAttribute('aria-hidden', 'true');
      overlay.innerHTML = ICONS.play;
      // Imediatamente após a capa (irmão seguinte) — o CSS posiciona sobre ela;
      // não pode ir DENTRO do <img> (elemento vazio não aceita filhos).
      const coverEl = card.querySelector('.cover');
      if (coverEl) coverEl.insertAdjacentElement('afterend', overlay);
      else card.appendChild(overlay);
    } else if (!match && overlay) {
      overlay.remove();
    }
  });
}

function togglePlayer() {
  const audio = playerEl('global-audio-element');
  if (!audio || !audio.src) return;
  if (audio.paused) audio.play().catch(() => {});
  else audio.pause();
}

function updatePlayBtn() {
  const audio = playerEl('global-audio-element');
  const playing = !!(audio && !audio.paused);
  const btn = playerEl('player-play-btn');
  if (btn) {
    btn.textContent = playing ? '⏸' : '▶';
    btn.setAttribute('aria-label', playing ? 'Pausar' : 'Reproduzir');
  }
  const pvBtn = playerEl('pv-play-btn');
  if (pvBtn) {
    pvBtn.textContent = playing ? '⏸' : '▶';
    pvBtn.setAttribute('aria-label', playing ? 'Pausar' : 'Reproduzir');
  }
  const disc = playerEl('pv-disc');
  if (disc) disc.classList.toggle('is-playing', playing);
}

function playerNext() {
  if (!state.playerQueue || state.playerQueue.length === 0) return;
  state.playerIndex = (state.playerIndex + 1) % state.playerQueue.length;
  startPlayer();
}

function playerPrev() {
  if (!state.playerQueue || state.playerQueue.length === 0) return;
  state.playerIndex = (state.playerIndex - 1 + state.playerQueue.length) % state.playerQueue.length;
  startPlayer();
}

function closePlayer() {
  const audio = playerEl('global-audio-element');
  const bar = playerEl('player-bar');
  if (audio) {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
  }
  if (bar) bar.hidden = true;
  state._discSuppressPause = false;
  haltDiscSpin(); // para o vinil imediatamente (sem easing — fechou o player)
  state._crossfadeSeq = (state._crossfadeSeq || 0) + 1; // invalida fade em andamento
  disposeAuxAudio();
  state._crossfading = false;
  state._crossfadeBaseVolume = undefined;
  revokeLocalObjectUrls(); // libera as object URLs dos arquivos locais
  updateMediaSession(); // sem faixa atual → metadata limpo
  syncPlayingCards(); // nenhuma faixa atual → remove .is-playing/eq/overlay dos cards
  if (state.currentView === 'player') showView('player', {});
}

// Media Session: metadata + seekbar da notificação/tela de bloqueio. Os action
// handlers são registrados UMA vez no init (bindMediaSessionActions); aqui só
// atualiza o metadata da faixa atual (ou null quando não há faixa).
function updateMediaSession() {
  if (!('mediaSession' in navigator) || !('MediaMetadata' in window)) return;
  const track = currentTrack();
  try {
    navigator.mediaSession.metadata = track
      ? new MediaMetadata({
          title: track.title || 'Música',
          artist: track.artist || 'MusicBox',
          album: track.album || 'MusicBox',
        })
      : null;
  } catch {
    /* media session é melhor-esforço */
  }
}

// Registra os handlers de controle (play/pause/anterior/próxima/seekto) uma
// única vez no init. Usa as funções reais de next/prev do app.
function bindMediaSessionActions() {
  if (!('mediaSession' in navigator) || typeof navigator.mediaSession.setActionHandler !== 'function') return;
  try {
    navigator.mediaSession.setActionHandler('play', () => {
      const a = playerEl('global-audio-element');
      if (a && a.paused) a.play().catch(() => {});
    });
    navigator.mediaSession.setActionHandler('pause', () => {
      const a = playerEl('global-audio-element');
      if (a && !a.paused) a.pause();
    });
    navigator.mediaSession.setActionHandler('previoustrack', playerPrev);
    navigator.mediaSession.setActionHandler('nexttrack', playerNext);
    navigator.mediaSession.setActionHandler('seekto', (details) => {
      const a = playerEl('global-audio-element');
      if (a && details && Number.isFinite(details.seekTime)) a.currentTime = details.seekTime;
    });
  } catch {
    /* media session é melhor-esforço */
  }
}

function formatClock(sec) {
  if (!Number.isFinite(sec) || sec < 0) return '0:00';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function updateSeekUI() {
  const audio = playerEl('global-audio-element');
  if (!audio) return;
  // Durante um crossfade, o progresso exibido segue a faixa ENTRANTE (auxiliar).
  const active =
    state._crossfading && state._auxAudio && !state._auxAudio.paused ? state._auxAudio : audio;
  const dur = active.duration || 0;
  const cur = active.currentTime || 0;
  const value = Number.isFinite(dur) && dur > 0 ? String(Math.round((cur / dur) * 1000)) : '0';
  const label = `${formatClock(cur)} / ${formatClock(dur)}`;
  // Atualiza o seek do mini-player E o da tela do player (ambos podem existir).
  // Enquanto o usuário ARRASTA o slider (state.seekDragging), o timeupdate não
  // sobrescreve o value — senão o thumb "briga" com a mão do usuário.
  if (!state.seekDragging) {
    const seek = playerEl('player-seek');
    if (seek) seek.value = value;
    const pvSeek = playerEl('pv-seek');
    if (pvSeek) pvSeek.value = value;
  }
  const time = playerEl('player-time');
  if (time) time.textContent = label;
  const pvTime = playerEl('pv-time');
  if (pvTime) pvTime.textContent = label;
  // Media Session: posição da faixa na notificação/tela de bloqueio (seekbar).
  if (Number.isFinite(dur) && dur > 0 && 'mediaSession' in navigator && typeof navigator.mediaSession.setPositionState === 'function') {
    try {
      navigator.mediaSession.setPositionState({
        duration: dur,
        playbackRate: active.playbackRate || 1,
        position: Number.isFinite(cur) && cur > 0 ? cur : 0,
      });
    } catch {
      // navegadores sem suporte a setPositionState — melhor-esforço
    }
  }
  // Karaokê: destaca a linha ativa no painel de letras quando ele está aberto.
  // O auto-scroll só roda quando a linha muda (guard dentro de syncLyricLine).
  if (
    state.playerPane === 'lyrics' &&
    state._lyricsCache &&
    state._lyricsCache.timed &&
    state._lyricsCache.timed.length > 0
  ) {
    syncLyricLine(cur);
  }
}

function bindPlayer() {
  const audio = playerEl('global-audio-element');
  const playBtn = playerEl('player-play-btn');
  const prevBtn = playerEl('player-prev-btn');
  const nextBtn = playerEl('player-next-btn');
  const closeBtn = playerEl('player-close-btn');
  const seek = playerEl('player-seek');

  if (playBtn) playBtn.addEventListener('click', togglePlayer);
  if (prevBtn) prevBtn.addEventListener('click', playerPrev);
  if (nextBtn) nextBtn.addEventListener('click', playerNext);
  if (closeBtn) closeBtn.addEventListener('click', closePlayer);

  if (audio) {
    audio.addEventListener('play', () => {
      state._discSuppressPause = false;
      updatePlayBtn();
      syncPlayingCards(); // tocou → marca o card correspondente como .is-playing
      updateMediaSession();
      syncDiscState(); // vinil acelera até a velocidade-alvo
    });
    audio.addEventListener('pause', () => {
      updatePlayBtn();
      syncPlayingCards(); // pausou → remove .is-playing (decisão: card "tocando" = só tocando)
      updateMediaSession();
      syncDiscState(); // vinil decelera suavemente (exceto em troca de faixa)
    });
    audio.addEventListener('timeupdate', updateSeekUI);
    audio.addEventListener('loadedmetadata', updateSeekUI);
    audio.addEventListener('ended', () => {
      if (state._crossfading) return; // o crossfade está conduzindo a transição
      playerNext();
    });
    // Faixa da busca ainda não baixada: o <audio> falha (404) no stub
    // /api/library/{yt_id} → aviso claro. Faixas baixadas não mudam de
    // comportamento (sem toast novo para erros que já aconteciam). O guard de
    // src evita toast quando o closePlayer limpa o src (erro de "emptied").
    audio.addEventListener('error', () => {
      state._discSuppressPause = false;
      stopDiscSpin(); // faixa morreu → o vinil para de girar
      const t = currentTrack();
      if (t && t._notDownloaded && audio.getAttribute('src')) {
        showToast('Baixe a faixa para ouvir', 'error');
      }
    });
  }
  if (seek) {
    // Dragging: sinaliza para o timeupdate não sobrescrever o value do slider.
    seek.addEventListener('pointerdown', () => {
      state.seekDragging = true;
    });
    seek.addEventListener('input', () => {
      state.seekDragging = true;
      const a = playerEl('global-audio-element');
      if (!a || !a.duration) return;
      a.currentTime = (Number(seek.value) / 1000) * a.duration;
    });
    seek.addEventListener('pointerup', () => {
      state.seekDragging = false;
    });
    seek.addEventListener('change', () => {
      state.seekDragging = false;
    });
  }

  // Clicar no corpo do mini-player (fora dos controles/seek) abre a tela cheia.
  const barOpen = playerEl('player-bar-open');
  if (barOpen) {
    // role/tabindex/aria-label são aplicados via JS (o elemento vive no
    // index.html; aqui garantimos acessibilidade de teclado no mesmo padrão
    // das linhas da fila do player).
    barOpen.setAttribute('role', 'button');
    barOpen.setAttribute('tabindex', '0');
    barOpen.setAttribute('aria-label', 'Abrir tela do player');
    const openPlayer = () => {
      if (state.currentView === 'player') return;
      openPlayerTab();
    };
    barOpen.addEventListener('click', (e) => {
      if (e.target.closest('.player-controls') || e.target.closest('.player-seek')) return;
      openPlayer();
    });
    barOpen.addEventListener('keydown', (e) => {
      if (e.target !== barOpen) return;
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openPlayer();
      }
    });
  }
}

// -------------------------------------------------- tela dedicada do player

// Tela "now playing": capa grande (disco girando), seek, controles e fila.
function playerViewHtml() {
  const track = currentTrack();
  if (!track) {
    return `
      <section class="view player-view" aria-label="Player">
        <div class="player-empty">
          <div class="player-empty-disc" aria-hidden="true">♪</div>
          <h1 class="view-title">Player</h1>
          <p class="empty-state">Nada tocando ainda — abra o Histórico ou um download concluído e toque uma música.</p>
        </div>
      </section>`;
  }
  const audio = playerEl('global-audio-element');
  const volume = audio ? Math.round((audio.volume || 1) * 100) : 100;
  // Pane ativo refletido no markup (a view é recriada a cada render).
  const showLyrics = state.playerPane === 'lyrics';
  return `
    <section class="view player-view" id="player-view" aria-label="Player">
      <div class="player-drag-handle" role="button" tabindex="0" aria-label="Minimizar player"></div>
      <div class="player-stage">
        <div class="player-tonearm" aria-hidden="true"></div>
        <div class="player-disc${audio && !audio.paused ? ' is-playing' : ''}" id="pv-disc">
          <div class="player-cover" id="pv-cover">
            ${track.cover ? `<img src="${escapeHtml(track.cover)}" alt="" />` : '<span aria-hidden="true">♪</span>'}
          </div>
        </div>
        <div class="player-stage-info">
          <h1 id="pv-title" class="player-stage-title">${escapeHtml(track.title || 'Música')}</h1>
          <p id="pv-artist" class="player-stage-artist">${escapeHtml(track.artist || 'MusicBox')}</p>
        </div>
        <div class="player-stage-seek">
          <input id="pv-seek" type="range" min="0" max="1000" value="0" aria-label="Posição da música" />
          <span id="pv-time" class="player-time">0:00 / 0:00</span>
        </div>
        <div class="player-stage-controls">
          <button type="button" id="pv-prev-btn" class="icon-btn player-nav-btn" aria-label="Anterior">⏮</button>
          <button type="button" id="pv-play-btn" class="play-toggle-btn play-toggle-btn--lg" aria-label="Reproduzir/Pausar">▶</button>
          <button type="button" id="pv-next-btn" class="icon-btn player-nav-btn" aria-label="Próxima">⏭</button>
        </div>
        <div class="player-stage-volume">
          <span class="volume-label" aria-hidden="true">${ICONS.volume}</span>
          <input id="pv-volume" type="range" min="0" max="100" value="${volume}" aria-label="Volume" />
        </div>
        <div class="player-stage-extras">
          <button type="button" class="btn btn-ghost btn-small" id="pv-offline-btn">💾 Salvar offline</button>
          <select id="pv-playlist-select" class="pl-select" aria-label="Playlist para salvar a faixa atual">
            <option value="">Salvar na playlist…</option>
            ${state.playlists.map((pl) => `<option value="${escapeHtml(pl.id)}">${escapeHtml(pl.name)}</option>`).join('')}
          </select>
          <button type="button" class="btn btn-ghost btn-small" id="pv-save-playlist-btn">+ Playlist</button>
        </div>
        <div class="crossfade-control">
          <label for="crossfade-range">Crossfade</label>
          <input id="crossfade-range" type="range" min="0" max="${CROSSFADE_MAX}" step="1" value="${state.crossfadeSeconds}" aria-label="Crossfade entre faixas" />
          <span id="crossfade-label">${crossfadeLabel(state.crossfadeSeconds)}</span>
        </div>
      </div>
      <div class="queue-section">
        <div class="player-pane-switch" role="tablist" aria-label="Visão do player">
          <button type="button" class="pane-btn${showLyrics ? '' : ' is-active'}" role="tab" data-pane="queue" aria-selected="${showLyrics ? 'false' : 'true'}">Fila</button>
          <button type="button" class="pane-btn${showLyrics ? ' is-active' : ''}" role="tab" data-pane="lyrics" aria-selected="${showLyrics ? 'true' : 'false'}">Letras</button>
        </div>
        <div id="player-queue" role="tabpanel" ${showLyrics ? 'hidden' : ''}>
          <h2 class="section-title">Na fila (${state.playerQueue.length})</h2>
          <ol class="player-queue" id="pv-queue">
            ${state.playerQueue.map((t, i) => playerQueueRowHtml(t, i)).join('')}
          </ol>
        </div>
        <div id="player-lyrics" class="lyrics-view" role="tabpanel" ${showLyrics ? '' : 'hidden'}></div>
      </div>
    </section>`;
}

function playerQueueRowHtml(track, index) {
  const cover = track.cover
    ? `<img src="${escapeHtml(track.cover)}" alt="" class="queue-cover" />`
    : `<span class="queue-cover queue-cover--vinyl" aria-hidden="true">♪</span>`;
  const current = index === state.playerIndex;
  return `
    <li class="player-queue-row${current ? ' is-current' : ''}" data-idx="${index}" role="button" tabindex="0" aria-current="${current ? 'true' : 'false'}" aria-label="Tocar ${escapeHtml(track.title || 'Música')}">
      ${cover}
      <div class="queue-row-info">
        <span class="queue-row-title">${escapeHtml(track.title || 'Música')}</span>
        <span class="queue-row-artist">${escapeHtml(track.artist || 'MusicBox')}</span>
      </div>
    </li>`;
}

// ---------------------------------------------------------- letras (LRC)

// Busca as letras (texto puro) de GET /api/library/{yt_id}/lyrics. NÃO passa
// pelo apiFetch (que tenta parsear JSON) — o corpo aqui é text/plain. Segue o
// mesmo padrão de auth do app (header X-MusicBox-Token; erro de rede vira
// ApiError com isNetwork).
async function lyricsApi(ytId) {
  let res;
  try {
    const headers = new Headers();
    if (state.token) headers.set('X-MusicBox-Token', state.token);
    res = await fetch(API.lyrics(ytId), { headers });
  } catch {
    const err = new ApiError(0, 'Sem conexão com o servidor');
    err.isNetwork = true;
    throw err;
  }
  if (res.status === 401) {
    showToast('Token de acesso necessário', 'error');
    openTokenModal();
    throw new ApiError(401, 'Token de acesso necessário');
  }
  if (res.status === 404) throw new ApiError(404, 'Sem letra');
  if (!res.ok) throw new ApiError(res.status, `Erro HTTP ${res.status}`);
  return res.text();
}

// Parse de letras LRC: `[mm:ss.xx]texto` → timed; linhas sem timestamp → plain
// (modo estático). `frac` com 1 dígito = décimos, 2 = centésimos, 3 = ms.
// Tags de metadados do LRC ([ti:], [ar:], [offset:] etc.) são descartadas.
function parseLrc(text) {
  const timed = [];
  const plain = [];
  if (!text) return { timed, plain };
  const re = /^\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\](.*)$/;
  const metaRe = /^\[[A-Za-z]+:[^\]]*\]\s*$/; // [ti:…], [ar:…], [offset:…]…
  for (const raw of String(text).split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const m = re.exec(line);
    if (!m) {
      if (!metaRe.test(line)) plain.push(line);
      continue;
    }
    const fracRaw = m[3];
    let frac = 0;
    if (fracRaw !== undefined && fracRaw !== '') {
      frac =
        fracRaw.length === 1
          ? Number(fracRaw) / 10
          : fracRaw.length === 2
          ? Number(fracRaw) / 100
          : Number(fracRaw) / 1000;
    }
    timed.push({ time: Number(m[1]) * 60 + Number(m[2]) + frac, text: m[4].trim() });
  }
  return { timed, plain };
}

// Renderiza as letras da faixa atual no painel #player-lyrics, com cache por
// yt_id (refetch só quando a faixa muda). Guards de corrida: a resposta pode
// chegar depois de o usuário trocar de faixa/aba — nesse caso o resultado é
// apenas armazenado (o DOM não é tocado com dados obsoletos).
async function renderLyricsPane(ytId) {
  if (!ytId) return;
  const container = document.getElementById('player-lyrics');
  if (!container) return;

  // Já resolvido para esta faixa (com letras, sem letras ou com erro).
  if (state._lyricsFor === ytId) {
    if (state._lyricsCache) renderLyricsHtml(container, state._lyricsCache);
    else renderLyricsError(container, state._lyricsError || 404);
    return;
  }
  if (state._lyricsFetching === ytId) return; // fetch em andamento para esta faixa

  state._lyricsFetching = ytId;
  container.innerHTML = '<div class="lyrics-empty">Carregando letras…</div>';

  let parsed = null;
  let status = null;
  try {
    parsed = parseLrc(await lyricsApi(ytId));
  } catch (err) {
    parsed = null;
    status = err && err.status ? err.status : 0;
  }
  if (state._lyricsFetching === ytId) state._lyricsFetching = null;

  // Armazena sempre (mesmo se o DOM não for tocado agora).
  state._lyricsFor = ytId;
  state._lyricsError = status;
  state._lyricsCache = parsed;

  // Guards de corrida: só renderiza se a faixa atual ainda é ytId e o pane
  // ainda é 'lyrics' (e o container existe na view atual).
  const track = currentTrack();
  if (!track || track.ytId !== ytId || state.playerPane !== 'lyrics') return;
  const fresh = document.getElementById('player-lyrics');
  if (!fresh) return;
  if (parsed && (parsed.timed.length || parsed.plain.length)) renderLyricsHtml(fresh, parsed);
  else renderLyricsError(fresh, status || 404);
}

// Corpo do painel: linhas sincronizadas (karaokê) ou parágrafos estáticos.
function renderLyricsHtml(container, parsed) {
  state._lyricsActiveIdx = -1; // novo conteúdo → força novo destaque/scroll
  const timed = parsed && parsed.timed ? parsed.timed : [];
  const plain = parsed && parsed.plain ? parsed.plain : [];
  if (timed.length > 0) {
    container.innerHTML = timed
      .map(
        (l, i) => `<div class="lyric-line" data-idx="${i}">${escapeHtml(l.text)}</div>`
      )
      .join('');
  } else if (plain.length > 0) {
    container.innerHTML = plain
      .map((p) => `<p class="lyric-line lyric-line--static">${escapeHtml(p)}</p>`)
      .join('');
  } else {
    container.innerHTML = '<div class="lyrics-empty">Sem letras para esta faixa</div>';
  }
}

// Estados vazio (404) e de erro (rede/outros) com botão de nova tentativa.
function renderLyricsError(container, status) {
  if (status === 404) {
    container.innerHTML = '<div class="lyrics-empty">Sem letras para esta faixa</div>';
    return;
  }
  container.innerHTML =
    '<div class="lyrics-empty">Não foi possível carregar as letras.</div>' +
    '<div class="lyrics-retry">' +
    '<button type="button" class="btn btn-ghost btn-small" data-action="lyrics-retry">Tentar novamente</button>' +
    '</div>';
}

// Karaokê: destaca a última linha com time <= currentTime e faz auto-scroll
// suave SÓ quando a linha ativa muda (não scrolla a cada tick do timeupdate).
function syncLyricLine(currentTime) {
  const container = document.getElementById('player-lyrics');
  if (!container) return;
  const timed = state._lyricsCache && state._lyricsCache.timed;
  if (!timed || timed.length === 0) return;

  let idx = -1;
  for (let i = 0; i < timed.length; i++) {
    if (timed[i].time <= currentTime) idx = i;
    else break;
  }
  if (idx === state._lyricsActiveIdx) return; // mesma linha — sem scroll

  const prev = container.querySelector('.lyric-line.is-active');
  if (prev) prev.classList.remove('is-active');
  state._lyricsActiveIdx = idx;
  if (idx >= 0) {
    const line = container.querySelector(`.lyric-line[data-idx="${idx}"]`);
    if (line) {
      line.classList.add('is-active');
      if (typeof line.scrollIntoView === 'function') {
        line.scrollIntoView({ block: 'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
      }
    }
  }
}

// Alterna entre as visões Fila | Letras do player (bind dos .pane-btn).
function setPlayerPane(pane) {
  state.playerPane = pane === 'lyrics' ? 'lyrics' : 'queue';
  const queueEl = document.getElementById('player-queue');
  const lyricsEl = document.getElementById('player-lyrics');
  if (queueEl) queueEl.hidden = state.playerPane !== 'queue';
  if (lyricsEl) lyricsEl.hidden = state.playerPane !== 'lyrics';
  document.querySelectorAll('.player-pane-switch .pane-btn').forEach((b) => {
    const active = b.dataset.pane === state.playerPane;
    b.classList.toggle('is-active', active);
    b.setAttribute('aria-selected', String(active));
  });
  if (state.playerPane === 'lyrics') {
    const t = currentTrack();
    if (t && t.ytId) renderLyricsPane(t.ytId); // cache-hit → renderiza; senão busca
  }
}

// Ao trocar de faixa: invalida o cache da faixa anterior; se o pane de letras
// estiver aberto, já busca as letras da nova faixa.
function syncLyricsForTrack(track) {
  if (!track || !track.ytId) return;
  if (state._lyricsFor !== track.ytId) {
    state._lyricsFor = null;
    state._lyricsCache = null;
    state._lyricsError = null;
    state._lyricsActiveIdx = -1;
  }
  if (state.playerPane === 'lyrics') renderLyricsPane(track.ytId);
}

// Botão "Tentar novamente" do estado de erro: força o refetch da faixa atual.
function retryLyrics() {
  const t = currentTrack();
  if (!t || !t.ytId) return;
  state._lyricsFor = null;
  state._lyricsError = null;
  state._lyricsCache = null;
  renderLyricsPane(t.ytId);
}

// ------------------------------------------------- bottom sheet (arraste)

// Elemento da tela cheia do player (a lane de design usa #player-view.is-dragging).
function playerViewEl() {
  return document.getElementById('player-view') || document.querySelector('.player-view');
}

// Minimizar: fecha a tela cheia do player mantendo a REPRODUÇÃO (o mini-player
// #player-bar já está visível). Não usa closePlayer — ele pararia a faixa.
function minimizePlayer() {
  const view = playerViewEl();
  if (view) {
    view.style.transform = '';
    view.classList.remove('is-dragging');
  }
  if (state.currentView === 'player') openSearchTab();
}

// Gesto de arraste no .player-drag-handle (único gatilho — não conflita com o
// seek). Soltou com ≥30% da altura da viewport → minimiza; senão, snap de volta
// (a transição CSS do estado normal faz o retorno suave).
function bindPlayerDragHandle() {
  const view = playerViewEl();
  const handle = document.querySelector('.player-drag-handle');
  if (!view || !handle) return;
  let startY = 0;
  let dragging = false;
  let dy = 0;

  handle.addEventListener('pointerdown', (e) => {
    if (state.seekDragging) return; // nunca captura durante o arraste do seek
    startY = e.clientY;
    dy = 0;
    dragging = true;
    try {
      handle.setPointerCapture(e.pointerId);
    } catch {
      /* noop */
    }
  });

  handle.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    dy = Math.max(0, e.clientY - startY);
    if (dy > 0) view.classList.add('is-dragging');
    view.style.transform = `translateY(${dy}px)`;
  });

  const end = () => {
    if (!dragging) return;
    dragging = false;
    view.style.transform = '';
    view.classList.remove('is-dragging');
    const h = window.innerHeight || 0;
    if (h > 0 && dy >= h * 0.3) minimizePlayer();
  };

  handle.addEventListener('pointerup', end);
  handle.addEventListener('pointercancel', end);

  // AC: o handle é um botão (role="button") — Enter/Espaço minimizam (padrão
  // dos demais botões acessíveis do app; Escape não fecha player por design).
  handle.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      minimizePlayer();
    }
  });
}

function bindPlayerViewEvents() {
  const prev = document.getElementById('pv-prev-btn');
  const next = document.getElementById('pv-next-btn');
  const play = document.getElementById('pv-play-btn');
  const seek = document.getElementById('pv-seek');
  const volume = document.getElementById('pv-volume');
  const offlineBtn = document.getElementById('pv-offline-btn');
  const savePlBtn = document.getElementById('pv-save-playlist-btn');
  const plSelect = document.getElementById('pv-playlist-select');

  if (prev) prev.addEventListener('click', playerPrev);
  if (next) next.addEventListener('click', playerNext);
  if (play) play.addEventListener('click', togglePlayer);
  if (offlineBtn) {
    offlineBtn.addEventListener('click', () => {
      const t = currentTrack();
      if (!t || !t.url) return;
      saveTrackOffline(t.url);
    });
  }
  if (savePlBtn && plSelect) {
    savePlBtn.addEventListener('click', async () => {
      const t = currentTrack();
      const pid = plSelect.value;
      if (!pid) {
        showToast('Escolha uma playlist.', 'error');
        return;
      }
      if (!t || !t.ytId) {
        showToast('Faixa atual não identificada.', 'error');
        return;
      }
      try {
        await apiFetch(`/api/playlists/${pid}/tracks`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ yt_id: t.ytId }),
        });
        showToast('Adicionada à playlist!', 'success');
        refreshPlaylists();
      } catch (err) {
        handleApiError(err, 'Não foi possível adicionar.');
      }
    });
  }
  if (seek) {
    // Mesmo padrão do mini-player: durante o arraste o timeupdate não mexe no value.
    seek.addEventListener('pointerdown', () => {
      state.seekDragging = true;
    });
    seek.addEventListener('input', () => {
      state.seekDragging = true;
      const a = playerEl('global-audio-element');
      if (!a || !a.duration) return;
      a.currentTime = (Number(seek.value) / 1000) * a.duration;
    });
    seek.addEventListener('pointerup', () => {
      state.seekDragging = false;
    });
    seek.addEventListener('change', () => {
      state.seekDragging = false;
    });
  }
  if (volume) {
    volume.addEventListener('input', () => {
      const a = playerEl('global-audio-element');
      if (a) a.volume = Number(volume.value) / 100;
    });
  }
  document.querySelectorAll('#pv-queue .player-queue-row').forEach((row) => {
    const playRow = () => {
      const idx = Number(row.dataset.idx);
      if (!Number.isNaN(idx) && state.playerQueue[idx]) {
        state.playerIndex = idx;
        startPlayer();
      }
    };
    row.addEventListener('click', playRow);
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        playRow();
      }
    });
  });
  // Fase 3: arraste para minimizar + slider de crossfade (guard de existência —
  // os elementos são recriados a cada render da view).
  bindPlayerDragHandle();
  bindCrossfadeControl();

  // Fase 4: panes Fila | Letras + retry das letras (delegação no container —
  // o botão "Tentar novamente" é criado dinamicamente pelo estado de erro).
  document.querySelectorAll('.player-pane-switch .pane-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (btn.dataset.pane === state.playerPane) return;
      setPlayerPane(btn.dataset.pane);
    });
  });
  const pvView = playerViewEl();
  if (pvView) {
    pvView.addEventListener('click', (e) => {
      if (e.target.closest('[data-action="lyrics-retry"]')) retryLyrics();
    });
  }
  // A view foi recriada: re-renderiza o conteúdo do pane ativo (letras vêm do
  // cache quando a faixa já foi resolvida; senão, busca).
  const trk = currentTrack();
  if (trk && trk.ytId && state.playerPane === 'lyrics') renderLyricsPane(trk.ytId);

  syncPlayerView();
}

// Sincroniza a tela do player com a faixa atual (título, artista, capa e botões).
function syncPlayerView() {
  const track = currentTrack();
  const title = document.getElementById('pv-title');
  const artist = document.getElementById('pv-artist');
  const coverBox = document.getElementById('pv-cover');
  if (title) title.textContent = track ? track.title || 'Música' : '';
  if (artist) artist.textContent = track ? track.artist || 'MusicBox' : '';
  if (coverBox) {
    coverBox.innerHTML = track && track.cover
      ? `<img src="${escapeHtml(track.cover)}" alt="" />`
      : '<span aria-hidden="true">♪</span>';
  }
  applyDiscAngle(); // disco recém-renderizado herda o ângulo acumulado
  updatePlayBtn();
}

// ----------------------------------------------------------- biblioteca

async function loadBiblioteca() {
  try {
    state.biblioteca = await apiFetch('/api/browse');
  } catch (err) {
    state.biblioteca = [];
    handleApiError(err, 'Não foi possível carregar a biblioteca.');
  }
  if (state.currentView === 'biblioteca') showView('biblioteca', {});
}

function bibliotecaViewHtml() {
  return `
    <section class="view" aria-label="Biblioteca">
      <header class="view-head">
        <h1 class="view-title">Biblioteca</h1>
      </header>
      <div class="library-controls">
        <div class="library-segments" id="library-segments" role="group" aria-label="Visão da biblioteca">
          <button type="button" class="segment-btn" data-view="historico" aria-pressed="true">Histórico</button>
          <button type="button" class="segment-btn" data-view="artistas" aria-pressed="false">Artistas</button>
          <button type="button" class="segment-btn" data-view="albuns" aria-pressed="false">Álbuns</button>
          <button type="button" class="segment-btn" data-view="local" aria-pressed="false">Local</button>
        </div>
        <div class="library-filters" id="library-filters" role="group" aria-label="Filtrar por formato">
          <button type="button" class="filter-chip" data-fmt="all" aria-pressed="true">Todos</button>
          <button type="button" class="filter-chip" data-fmt="mp3" aria-pressed="false">MP3 320</button>
          <button type="button" class="filter-chip" data-fmt="opus" aria-pressed="false">Opus 160</button>
        </div>
      </div>
      <div id="history-list" class="history-list library-content"></div>
    </section>`;
}

// Liga os controles da Biblioteca (segmentos + filtros) e renderiza a visão.
// Os estados vêm de state.libraryView/libraryFmt (persistidos em localStorage).
function bindBibliotecaEvents() {
  document.querySelectorAll('#library-segments .segment-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.libraryView = btn.dataset.view;
      try {
        localStorage.setItem(STORAGE_LIB_VIEW_KEY, state.libraryView);
      } catch {
        // storage bloqueado — vale apenas para esta sessão
      }
      renderLibrary();
    });
  });
  document.querySelectorAll('#library-filters .filter-chip').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.libraryFmt = btn.dataset.fmt;
      try {
        localStorage.setItem(STORAGE_LIB_FMT_KEY, state.libraryFmt);
      } catch {
        // storage bloqueado — vale apenas para esta sessão
      }
      renderLibrary();
    });
  });
  renderLibrary();
}

function loadLibraryView() {
  const allowed = ['historico', 'artistas', 'albuns', 'local'];
  try {
    const v = localStorage.getItem(STORAGE_LIB_VIEW_KEY);
    return allowed.includes(v) ? v : 'historico';
  } catch {
    return 'historico';
  }
}

function loadLibraryFmt() {
  const allowed = ['all', 'mp3', 'opus'];
  try {
    const v = localStorage.getItem(STORAGE_LIB_FMT_KEY);
    return allowed.includes(v) ? v : 'all';
  } catch {
    return 'all';
  }
}

// Estado ativo dos controles (is-active + aria-pressed) conforme o estado.
function updateLibraryControls() {
  document.querySelectorAll('#library-segments .segment-btn').forEach((btn) => {
    const active = btn.dataset.view === state.libraryView;
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-pressed', String(active));
  });
  document.querySelectorAll('#library-filters .filter-chip').forEach((btn) => {
    const active = btn.dataset.fmt === state.libraryFmt;
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-pressed', String(active));
  });
}

// Ponto único de renderização da Biblioteca: aplica o filtro de formato a
// TODAS as visões e delega para o renderizador da visão ativa.
function renderLibrary() {
  const listEl = document.getElementById('history-list');
  if (!listEl) return;
  const fmt = state.libraryFmt || 'all';
  const records =
    fmt === 'all'
      ? state.history || []
      : (state.history || []).filter((r) => r.format === fmt);
  updateLibraryControls();

  const view = state.libraryView || 'historico';
  if (view === 'artistas') renderLibraryArtists(records);
  else if (view === 'albuns') renderLibraryAlbums(records);
  else if (view === 'local') renderLibraryLocal();
  else renderHistory(records); // historico → comportamento atual reutilizado
}

// Visão "Artistas": agrupa as faixas baixadas (done) por record.artist.
function renderLibraryArtists(records) {
  const listEl = document.getElementById('history-list');
  if (!listEl) return;
  const playable = records.filter((r) => r.status === 'done' && r.path);
  if (!playable.length) {
    listEl.innerHTML = '<p class="empty-state">Nenhuma música na biblioteca ainda.</p>';
    return;
  }
  const groups = new Map();
  playable.forEach((r) => {
    const name = r.artist || 'Desconhecido';
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(r);
  });
  const names = [...groups.keys()].sort((a, b) => a.localeCompare(b, 'pt-BR'));
  listEl.innerHTML = names
    .map((name) => {
      const items = groups.get(name);
      return `
        <div class="lib-group">
          <div class="lib-group-head">
            <span class="lib-group-name">${escapeHtml(name)}</span>
            <span class="lib-group-count">${items.length} ${items.length === 1 ? 'item' : 'itens'}</span>
          </div>
          <ul class="lib-group-items">
            ${items.map((r) => libRowHtml(r)).join('')}
          </ul>
        </div>`;
    })
    .join('');
  bindLibraryRowEvents(listEl);
}

// Visão "Álbuns": agrupa as faixas baixadas por record.album.
function renderLibraryAlbums(records) {
  const listEl = document.getElementById('history-list');
  if (!listEl) return;
  const playable = records.filter((r) => r.status === 'done' && r.path);
  if (!playable.length) {
    listEl.innerHTML = '<p class="empty-state">Nenhuma música na biblioteca ainda.</p>';
    return;
  }
  const groups = new Map();
  playable.forEach((r) => {
    const key = r.album || 'Desconhecido';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  });
  const keys = [...groups.keys()].sort((a, b) => a.localeCompare(b, 'pt-BR'));
  listEl.innerHTML = keys
    .map((name) => {
      const items = groups.get(name);
      const first = items[0];
      const year = first && first.year ? ` · ${escapeHtml(String(first.year))}` : '';
      const artistLine = first && first.artist ? escapeHtml(first.artist) : '';
      return `
        <div class="lib-group">
          <div class="lib-group-head">
            <span class="lib-group-name">${escapeHtml(name)}${year}</span>
            <span class="lib-group-meta">${artistLine ? `${artistLine} · ` : ''}${items.length} ${items.length === 1 ? 'faixa' : 'faixas'}</span>
          </div>
          <ul class="lib-group-items">
            ${items.map((r) => libRowHtml(r, true)).join('')}
          </ul>
        </div>`;
    })
    .join('');
  bindLibraryRowEvents(listEl);
}

// Linha de item dentro de um grupo (cover 42px na visão artistas; 48px na de
// álbuns via modificador .lib-row--album).
function libRowHtml(record, albumModifier) {
  const cover = record.cover_url
    ? `<img src="${escapeHtml(record.cover_url)}" alt="" class="lib-row-cover" loading="lazy" />`
    : `<span class="lib-row-cover lib-row-cover--vinyl" aria-hidden="true">♪</span>`;
  const subtitle = `${record.album || 'Álbum desconhecido'} · ${escapeHtml(formatLabel(record.format || state.format))}`;
  return `
    <li class="lib-row${albumModifier ? ' lib-row--album' : ''}">
      ${cover}
      <div class="lib-row-meta">
        <span class="lib-row-title">${escapeHtml(record.title || 'Sem título')}</span>
        <span class="lib-row-sub">${subtitle}</span>
      </div>
      <button
        type="button"
        class="lib-row-play"
        data-yt-id="${escapeHtml(record.yt_id || '')}"
        aria-label="Tocar ${escapeHtml(record.title || 'Música')}"
      >${ICONS.play}</button>
    </li>`;
}

function bindLibraryRowEvents(container) {
  container.querySelectorAll('.lib-row-play').forEach((btn) => {
    btn.addEventListener('click', () => {
      const record = (state.history || []).find(
        (h) => h.yt_id === btn.dataset.ytId && h.status === 'done' && h.path
      );
      if (record) playLibraryHistoryRecord(record);
    });
  });

  // Swipe (aceleração): esquerda → fila do player; direita → baixar rápido.
  container.querySelectorAll('.lib-row').forEach((row) => {
    const playBtn = row.querySelector('.lib-row-play');
    const ytId = playBtn ? playBtn.dataset.ytId : '';
    const record = (state.history || []).find(
      (h) => h.yt_id === ytId && h.status === 'done' && h.path
    );
    if (!record) return;
    bindSwipe(
      row,
      () => addHistoryToQueue(record),
      () => downloadSingleTrack({ yt_id: record.yt_id, title: record.title })
    );
  });
}

// Toca um registro do histórico a partir da posição dele na fila GLOBAL de
// tocáveis (o índice do filtro de formato da Biblioteca não serve para o
// player, que monta a fila com tudo).
function playLibraryHistoryRecord(record) {
  const playable = (state.history || []).filter((r) => r.status === 'done' && r.path);
  const idx = playable.findIndex((r) => r.yt_id === record.yt_id);
  if (idx < 0) return;
  playFromHistory(idx);
}

// ------------------------------------------------- biblioteca local (arquivos)

// Visão "Local": lista os arquivos de áudio do dispositivo (sem upload).
// Vazio → cassete + convite; com arquivos → busca por título/álbum + linhas.
function renderLibraryLocal() {
  const listEl = document.getElementById('history-list');
  if (!listEl) return;
  if (!state.localFiles || state.localFiles.length === 0) {
    listEl.innerHTML = `
      <div class="local-empty">
        <svg viewBox="0 0 24 24" width="170" height="170" fill="currentColor" aria-hidden="true"><path d="M9 17V5l12-2v12"/><circle cx="7" cy="17" r="2"/><circle cx="17" cy="15" r="2"/></svg>
        <p class="empty-state">Adicione uma pasta com suas músicas</p>
        <p class="local-hint">Selecione uma pasta do dispositivo — os arquivos não saem dele.</p>
        <button type="button" class="btn-add-folder">Adicionar pasta</button>
      </div>`;
    bindLocalFolderButton(listEl);
    return;
  }
  // Sessão nova (metadados do IndexedDB sem file) → aviso de re-seleção.
  const needsReselect = state.localIndexed && !state.localFiles.some((f) => f.file);
  listEl.innerHTML = `
    <div class="local-search">
      <input id="local-search-input" type="search" placeholder="Buscar nas músicas locais…" value="${escapeHtml(state.localQuery || '')}" aria-label="Buscar nas músicas locais" autocomplete="off" />
    </div>
    ${
      needsReselect
        ? `<div class="local-actions">
            <p class="local-hint">Re-selecione a pasta para tocar as músicas</p>
            <button type="button" class="btn-add-folder">Adicionar pasta</button>
          </div>`
        : ''
    }
    <div id="local-list"></div>
  `;
  const input = document.getElementById('local-search-input');
  if (input) {
    // Filtro por título/álbum: re-renderiza SÓ a lista (o input não é recriado,
    // então o foco/cursor são preservados a cada tecla).
    input.addEventListener('input', () => {
      state.localQuery = input.value;
      renderLocalListItems();
    });
  }
  bindLocalFolderButton(listEl);
  renderLocalListItems();
}

// Lista de itens locais filtrada pela busca (título/álbum, lowercase includes).
function renderLocalListItems() {
  const listEl = document.getElementById('local-list');
  if (!listEl) return;
  const q = (state.localQuery || '').trim().toLowerCase();
  const items = (state.localFiles || []).filter(
    (f) =>
      !q ||
      (f.title || '').toLowerCase().includes(q) ||
      (f.album || '').toLowerCase().includes(q)
  );
  listEl.innerHTML =
    items.length === 0
      ? '<p class="empty-state">Nenhuma música local encontrada.</p>'
      : `<ul class="lib-group-items">${items.map(localRowHtml).join('')}</ul>`;
  bindLocalRowEvents(listEl);
}

// Linha de arquivo local (reusa o padrão .lib-row; badge "LOCAL" + .is-local).
function localRowHtml(item) {
  const title = item.title || item.name || 'Música';
  return `
    <li class="lib-row is-local" data-local-id="${escapeHtml(item.id)}">
      <span class="lib-row-cover lib-row-cover--vinyl" aria-hidden="true">♪</span>
      <div class="lib-row-meta">
        <span class="lib-row-title">${escapeHtml(title)}</span>
        <span class="lib-row-sub">${escapeHtml(item.album || 'Músicas locais')} · <span class="badge">LOCAL</span></span>
      </div>
      <button
        type="button"
        class="lib-row-play"
        data-local-id="${escapeHtml(item.id)}"
        aria-label="Tocar ${escapeHtml(title)}"
      >${ICONS.play}</button>
    </li>`;
}

// Liga play (clique) e swipe esquerda (fila) das linhas locais. Swipe direita
// (download) NÃO se aplica a arquivos locais (não têm yt_id/servidor).
function bindLocalRowEvents(container) {
  container.querySelectorAll('.lib-row-play[data-local-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const item = (state.localFiles || []).find((f) => f.id === btn.dataset.localId);
      if (item) playLocalTrack(item);
    });
  });
  container.querySelectorAll('.lib-row.is-local').forEach((row) => {
    const btn = row.querySelector('.lib-row-play');
    const id = btn ? btn.dataset.localId : '';
    const item = (state.localFiles || []).find((f) => f.id === id);
    if (!item) return;
    bindSwipe(row, () => addLocalToQueue(item), null);
  });
}

// Liga o botão "Adicionar pasta" (estado vazio e aviso de re-seleção).
function bindLocalFolderButton(container) {
  const btn = container.querySelector('.btn-add-folder');
  if (btn) btn.addEventListener('click', () => addLocalFolder());
}

// Pasta pai imediata a partir do caminho relativo do DIRETÓRIO que contém o
// arquivo (ex.: 'Rock/Sub' → 'Sub'); vazio (raiz da pasta selecionada) → fallback.
function localParentAlbum(relPath) {
  if (!relPath) return 'Músicas locais';
  const parts = String(relPath).split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : 'Músicas locais';
}

// Abre o seletor de pasta: showDirectoryPicker (desktop Chromium) com fallback
// <input webkitdirectory multiple> (Android/web). AbortError (cancelado) é
// silencioso; erros reais viram toast.
async function addLocalFolder() {
  if (typeof window.showDirectoryPicker === 'function') {
    try {
      const dir = await window.showDirectoryPicker({ mode: 'read' });
      const items = [];
      // relPath = caminho do DIRETÓRIO que contém o arquivo (vazio = raiz da
      // pasta selecionada); o nome da raiz não vira "álbum".
      const walk = async (handle, relPath) => {
        for await (const entry of handle.values()) {
          if (entry.kind === 'directory') {
            await walk(entry, relPath ? `${relPath}/${entry.name}` : entry.name);
          } else if (entry.kind === 'file') {
            const name = entry.name || '';
            const ext = '.' + (name.split('.').pop() || '').toLowerCase();
            if (!LOCAL_EXTENSIONS.has(ext)) continue;
            const file = await entry.getFile();
            items.push({
              id: crypto.randomUUID ? crypto.randomUUID() : `${name}:${file.size}`,
              name,
              title: name.replace(/\.[^.]+$/, ''),
              album: localParentAlbum(relPath),
              size: file.size || 0,
              type: file.type || '',
              file,
              handle: entry,
            });
          }
        }
      };
      await walk(dir, '');
      finalizeLocalFiles(items);
    } catch (err) {
      if (err && err.name === 'AbortError') return; // usuário cancelou → silencioso
      showToast('Não foi possível ler a pasta', 'error');
    }
    return;
  }

  // Fallback: input webkitdirectory (Android/WebKit) — re-seleção a cada sessão.
  const input = document.createElement('input');
  input.type = 'file';
  input.setAttribute('webkitdirectory', '');
  input.multiple = true;
  input.hidden = true;
  input.style.display = 'none';
  document.body.appendChild(input);
  input.addEventListener('change', () => {
    const items = [];
    const files = input.files ? Array.from(input.files) : [];
    for (const file of files) {
      const name = file.name || '';
      const ext = '.' + (name.split('.').pop() || '').toLowerCase();
      if (!LOCAL_EXTENSIONS.has(ext)) continue;
      // webkitRelativePath: 'Raiz/Pasta/arquivo.mp3' → álbum = pasta interna
      // (segunda à última parte); arquivo direto na raiz → 'Músicas locais'.
      const rel = file.webkitRelativePath || name;
      const relParts = rel.split('/').filter(Boolean);
      const album =
        relParts.length > 2 ? relParts[relParts.length - 2] : 'Músicas locais';
      items.push({
        id: crypto.randomUUID ? crypto.randomUUID() : `${name}:${file.size || 0}`,
        name,
        title: name.replace(/\.[^.]+$/, ''),
        album,
        size: file.size || 0,
        type: file.type || '',
        file,
      });
    }
    finalizeLocalFiles(items);
    input.remove();
  });
  input.click();
}

// Ordena (álbum + título, localeCompare pt-BR), aplica em state e persiste
// metadados no IndexedDB (sem file/handle).
function finalizeLocalFiles(items) {
  items.sort(
    (a, b) =>
      (a.album || '').localeCompare(b.album || '', 'pt-BR') ||
      (a.title || '').localeCompare(b.title || '', 'pt-BR')
  );
  state.localFiles = items;
  saveLocalFiles(items); // persistência parcial — melhor-esforço
  renderLibrary();
}

// Revoga as object URLs da fila local vigente (só blob:). Chamado ao fechar o
// player e quando a fila local é substituída (nova seleção / faixa do servidor).
function revokeLocalObjectUrls() {
  if (!state._localObjectUrls) return;
  state._localObjectUrls.forEach((u) => {
    try {
      if (u && u.startsWith('blob:')) URL.revokeObjectURL(u);
    } catch {
      /* noop */
    }
  });
  state._localObjectUrls.clear();
  state._localObjectUrl = null;
}

// Toca um arquivo local: monta a fila com TODOS os arquivos locais tocáveis
// (object URLs) e inicia na faixa clicada — mesmo fluxo do playFromHistory.
function playLocalTrack(item) {
  if (!item || !item.file) {
    showToast('Re-selecione a pasta para tocar esta música.', 'error');
    return;
  }
  revokeLocalObjectUrls();
  const playable = (state.localFiles || []).filter((f) => f.file);
  const idx = playable.findIndex((f) => f.id === item.id);
  if (idx < 0) return;
  state.playerQueue = playable.map((f) => {
    const url = URL.createObjectURL(f.file);
    state._localObjectUrls.add(url);
    return {
      url,
      title: f.title || f.name || 'Música',
      artist: f.album || 'Local',
      cover: '',
      ytId: `local:${f.id}`,
      _local: true, // marca para a limpeza de URLs
    };
  });
  state._localObjectUrl = state.playerQueue[idx].url; // a faixa atual
  state.playerIndex = idx;
  startPlayer();
}

// Swipe esquerda (fila) em arquivo local: adiciona à fila do player sem tocar
// na faixa atual (mesmo fluxo do addHistoryToQueue).
function addLocalToQueue(item) {
  if (!item || !item.file) {
    showToast('Re-selecione a pasta para tocar esta música.', 'error');
    return;
  }
  const url = URL.createObjectURL(item.file);
  const track = {
    url,
    title: item.title || item.name || 'Música',
    artist: item.album || 'Local',
    cover: '',
    ytId: `local:${item.id}`,
    _local: true,
  };
  if ((state.playerQueue || []).some((t) => t.ytId === track.ytId)) {
    showToast(`“${track.title}” já está na fila`, 'info');
    return;
  }
  state.playerQueue.push(track);
  state._localObjectUrls.add(url);
  showToast(`“${track.title}” adicionado à fila`, 'success');
  if (state.playerQueue.length === 1) {
    state.playerIndex = 0;
    state._localObjectUrl = url;
    startPlayer();
  }
}

// ------------------------------------------------- IndexedDB (metadados locais)

function openLocalDb() {
  return new Promise((resolve, reject) => {
    if (!('indexedDB' in window)) return reject(new Error('indexedDB indisponível'));
    const req = window.indexedDB.open(LOCAL_DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(LOCAL_DB_STORE)) {
        db.createObjectStore(LOCAL_DB_STORE, { keyPath: 'id' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error('falha ao abrir IndexedDB'));
  });
}

// Persiste SÓ os metadados (id/name/title/album/size/type) — file/handle não
// sobrevivem à sessão. Limpa a store e regrava a seleção atual.
async function saveLocalFiles(items) {
  try {
    const db = await openLocalDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(LOCAL_DB_STORE, 'readwrite');
      const store = tx.objectStore(LOCAL_DB_STORE);
      store.clear();
      (items || []).forEach((it) => {
        store.put({
          id: it.id,
          name: it.name,
          title: it.title,
          album: it.album,
          size: it.size,
          type: it.type,
        });
      });
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error('falha ao salvar'));
    });
    db.close();
  } catch {
    // IndexedDB indisponível — persistência é melhor-esforço (silencioso)
  }
}

// Restaura os metadados da seleção anterior (sem file). Em sessão nova a lista
// aparece com o aviso de re-seleção (.local-hint).
async function loadLocalFiles() {
  try {
    const db = await openLocalDb();
    const rows = await new Promise((resolve, reject) => {
      const tx = db.transaction(LOCAL_DB_STORE, 'readonly');
      const req = tx.objectStore(LOCAL_DB_STORE).getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error || new Error('falha ao carregar'));
    });
    db.close();
    if (Array.isArray(rows) && rows.length) {
      state.localFiles = rows; // metadados apenas — sem file/handle
      state.localIndexed = true;
      if (state.currentView === 'biblioteca') renderLibrary();
    }
  } catch {
    // IndexedDB indisponível — segue sem lista local (silencioso)
  }
}

function libArtistViewHtml({ artist }) {
  const albums = artist.albums || [];
  return `
    <section class="view sub-view" aria-label="Álbuns de ${escapeHtml(artist.name)}">
      <header class="sub-header">
        <button type="button" class="icon-btn back-btn" id="back-btn" aria-label="Voltar">${ICONS.back}</button>
        <div class="sub-header-info">
          <h1 class="sub-heading">${escapeHtml(artist.name)}</h1>
          <p class="sub-meta">${albums.length} ${albums.length === 1 ? 'álbum' : 'álbuns'} na biblioteca</p>
        </div>
      </header>
      ${
        albums.length === 0
          ? '<p class="empty-state">Nenhum álbum na biblioteca.</p>'
          : `<ul class="card-list">${albums
              .map(
                (album, i) => `
            <li>
              <div class="card" role="button" tabindex="0" aria-label="Abrir álbum ${escapeHtml(album.name)}" data-album="${escapeHtml(album.name)}" style="animation-delay:${Math.min(i * 40, 400)}ms">
                ${
                  album.cover_url
                    ? `<img src="${escapeHtml(album.cover_url)}" alt="" class="cover" loading="lazy" />`
                    : '<span class="cover cover--album" aria-hidden="true">💿</span>'
                }
                <span class="card-body">
                  <span class="card-title">${escapeHtml(album.name)}</span>
                  <span class="card-kind">${album.tracks.length} ${album.tracks.length === 1 ? 'faixa' : 'faixas'}</span>
                </span>
                ${ICONS.chevron}
              </div>
            </li>`
              )
              .join('')}</ul>`
      }
    </section>`;
}

function bindLibArtistEvents({ artist }) {
  const back = document.getElementById('back-btn');
  if (back) back.addEventListener('click', goBack);
  document.querySelectorAll('.card[data-album]').forEach((card) => {
    const openAlbum = () => {
      const album = artist.albums.find((a) => a.name === card.dataset.album);
      if (!album) return;
      state.backStack.push({ name: 'lib-artist', data: { artist } });
      showView('lib-album', { album });
    };
    card.addEventListener('click', openAlbum);
    bindCardKeyboard(card, openAlbum);
  });
}

function libAlbumViewHtml({ album }) {
  const tracks = album.tracks || [];
  return `
    <section class="view sub-view" aria-label="Álbum ${escapeHtml(album.name)}">
      <header class="sub-header">
        <button type="button" class="icon-btn back-btn" id="back-btn" aria-label="Voltar">${ICONS.back}</button>
        <div class="sub-header-info">
          <h1 class="sub-heading">${escapeHtml(album.name)}</h1>
          <p class="sub-meta">${tracks.length} ${tracks.length === 1 ? 'faixa' : 'faixas'}</p>
        </div>
      </header>
      <button type="button" class="btn btn-primary btn-block" id="lib-play-album-btn">▶ Tocar álbum (${tracks.length})</button>
      <ol class="track-list">
        ${tracks.map((track, i) => libTrackRowHtml(track, i)).join('')}
      </ol>
    </section>`;
}

function libTrackRowHtml(track, index) {
  return `
    <li class="track-row track-row--play" role="button" tabindex="0" aria-label="Tocar ${escapeHtml(track.title)}" data-yt-id="${escapeHtml(track.yt_id)}">
      <span class="track-num" aria-hidden="true">${String(index + 1).padStart(2, '0')}</span>
      <div class="track-info">
        <span class="track-title">${escapeHtml(track.title)}</span>
        <span class="track-duration">${escapeHtml(track.format ? formatLabel(track.format) : '')}</span>
      </div>
      <span class="track-dl" aria-hidden="true">▶</span>
    </li>`;
}

function bindLibAlbumEvents({ album }) {
  const back = document.getElementById('back-btn');
  if (back) back.addEventListener('click', goBack);
  const playAlbum = document.getElementById('lib-play-album-btn');
  if (playAlbum) playAlbum.addEventListener('click', () => playLibraryQueue(album.tracks, 0));
  document.querySelectorAll('.track-row--play').forEach((row) => {
    const playRow = () => {
      const idx = album.tracks.findIndex((t) => t.yt_id === row.dataset.ytId);
      if (idx >= 0) playLibraryQueue(album.tracks, idx);
    };
    row.addEventListener('click', playRow);
    bindCardKeyboard(row, playRow);
  });
}

// Toca faixas da biblioteca/playlist no player (fila completa do álbum).
function playLibraryQueue(tracks, index) {
  const playable = tracks.filter((t) => t.path);
  if (!playable.length) {
    showToast('Nenhuma faixa baixada para tocar.', 'error');
    return;
  }
  revokeLocalObjectUrls(); // fila do servidor substitui a local → libera URLs
  state.playerQueue = playable.map((t) => ({
    url: API.library(t.path),
    title: t.title || 'Música',
    artist: t.artist || '',
    cover: t.cover_url || '',
    ytId: t.yt_id || '',
  }));
  state.playerIndex = Math.min(Math.max(index, 0), state.playerQueue.length - 1);
  startPlayer();
}

// Salva a faixa atual no cache de áudio do Service Worker (reprodução offline).
// O SW também cacheia sozinho quando a faixa é reproduzida ou baixada.
const AUDIO_CACHE_NAME = 'musicbox-audio-v1';
const AUDIO_CACHE_MAX_BYTES = 500 * 1024 * 1024; // teto fixo (~500 MB)
const AUDIO_CACHE_MAX_RATIO = 0.8; // ou 80% da quota estimada do navegador

// Mede o cache de áudio do Service Worker: {count, bytes, entries}. `entries`
// carrega metadados por entrada (url/lastModified) reutilizados pelo eviction
// LRU. Melhor-esforço: falha de medição retorna 0/0 sem derrubar a chamada.
async function measureAudioCache() {
  try {
    if (!('caches' in window)) return { count: 0, bytes: 0, entries: [] };
    const cache = await caches.open(AUDIO_CACHE_NAME);
    const requests = await cache.keys();
    const entries = [];
    let bytes = 0;
    for (const req of requests) {
      try {
        const res = await cache.match(req);
        if (!res) continue;
        const blob = await res.clone().blob();
        entries.push({
          req,
          size: blob.size,
          url: req.url,
          lastModified: res.headers.get('last-modified') || res.headers.get('date') || '',
        });
        bytes += blob.size;
      } catch {
        // entrada ilegível — conta como 0 e fica (será podada nas próximas)
      }
    }
    return { count: requests.length, bytes, entries };
  } catch {
    return { count: 0, bytes: 0, entries: [] };
  }
}

async function saveTrackOffline(url) {
  if (!('caches' in window)) {
    showToast('Navegador sem suporte a cache.', 'error');
    return;
  }
  try {
    const cache = await caches.open(AUDIO_CACHE_NAME);
    await cache.add(url);
    await evictAudioCache(cache, url); // eviction LRU antes de confirmar
    showToast('Música salva para ouvir offline!', 'success');
  } catch {
    showToast('Não foi possível salvar offline (sem conexão?).', 'error');
  }
}

// Eviction LRU simples do cache de áudio: mede o tamanho total (via
// measureAudioCache, mesmo código usado no storage manager) e, se passar do
// limite (80% da quota estimada ou o teto fixo de ~500 MB, o que for menor),
// apaga as entradas mais antigas até caber. É melhor-esforço: falha de medição
// não derruba o save.
async function evictAudioCache(cache, keepUrl) {
  try {
    let quotaBytes = Infinity;
    try {
      if (navigator.storage && navigator.storage.estimate) {
        const est = await navigator.storage.estimate();
        if (est && est.quota) quotaBytes = est.quota;
      }
    } catch {
      quotaBytes = Infinity;
    }
    const limit = Math.min(AUDIO_CACHE_MAX_BYTES, quotaBytes * AUDIO_CACHE_MAX_RATIO);

    // Reusa a medição do cache (entries + total) — uma única fonte de medida.
    const { bytes: total, entries } = await measureAudioCache();
    if (total <= limit) return;

    // "Mais antiga primeiro": usa Last-Modified/Date quando disponível e cai
    // para a ordem de inserção do Cache (aproximação LRU) quando não há.
    const byTime = (a, b) => (a.lastModified ? new Date(a.lastModified).getTime() : 0) - (b.lastModified ? new Date(b.lastModified).getTime() : 0);
    const oldestFirst = entries.slice().sort((a, b) => byTime(a, b) || entries.indexOf(a) - entries.indexOf(b));

    let removed = 0;
    for (const e of oldestFirst) {
      if (total - removed <= limit) break;
      if (e.url === keepUrl) continue; // nunca apaga a que acabou de salvar
      await cache.delete(e.req);
      removed += e.size;
    }
  } catch {
    // eviction é melhor-esforço — não derruba o save offline
  }
}

// ---------------------------------------------------------- WebSocket

// Backoff da reconexão automática: 3s → 6s → 12s → … → 30s (máx).
const WS_RECONNECT_BASE_MS = 3000;
const WS_RECONNECT_MAX_MS = 30000;

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const base = `${proto}://${location.host}/ws`;
  return state.token ? `${base}?token=${encodeURIComponent(state.token)}` : base;
}

// Conecta ao /ws (sempre ligado: alimenta o badge e as notificações mesmo fora
// da aba Downloads). Reconexão automática com backoff progressivo — a guarda
// `if (state.ws) return` evita conexões duplicadas, então reconectar após o
// timer é seguro mesmo que o close/error tenha chegado mais de uma vez.
function connectWS() {
  if (state.ws) return; // já conectado
  if (state.authRequired && !state.token) return; // sem token o servidor fecha 4401

  const ws = new WebSocket(wsUrl());
  state.ws = ws;

  ws.addEventListener('open', () => {
    state.wsReconnectDelay = null; // conexão ok → backoff volta ao valor base
  });

  ws.addEventListener('message', (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if (msg.type === 'snapshot' && Array.isArray(msg.tasks)) {
      // Estado inicial da fila (substitui a lista por completo)
      state.tasks = new Map(msg.tasks.map((t) => [t.task_id, t]));
      renderTaskList();
      syncDlButtons(); // snapshot do servidor → atualiza estados dos .dl-btn
    } else if (msg.type === 'update') {
      applyUpdate(msg);
    }
  });

  ws.addEventListener('close', () => {
    state.ws = null;
    if (!state.wsReconnectTimer) {
      showToast('Conexão em tempo real perdida. Reconectando…', 'error');
      refreshDownloads(); // fallback REST para o estado inicial
    }
    scheduleWsReconnect();
  });

  ws.addEventListener('error', () => {
    state.ws = null;
    try {
      ws.close(); // o handler de close cuida do fallback/reconnect
    } catch {
      /* noop */
    }
  });
}

// Agenda a reconexão com backoff progressivo (dobra a cada tentativa, até 30s;
// o 'open' da conexão reseta o backoff para o valor base).
function scheduleWsReconnect() {
  const delay = state.wsReconnectDelay || WS_RECONNECT_BASE_MS;
  clearTimeout(state.wsReconnectTimer);
  state.wsReconnectTimer = setTimeout(() => {
    state.wsReconnectTimer = null;
    // A próxima tentativa dobra, respeitando o teto de 30s.
    state.wsReconnectDelay = Math.min(
      (state.wsReconnectDelay || WS_RECONNECT_BASE_MS) * 2,
      WS_RECONNECT_MAX_MS
    );
    connectWS();
  }, delay);
}

// Update chega sem title/format; preserva os dados já conhecidos da task.
function applyUpdate(msg) {
  state.queueSessionIds.add(msg.task_id);
  const existing = state.tasks.get(msg.task_id);
  if (existing) {
    Object.assign(existing, msg);
  } else {
    state.tasks.set(msg.task_id, {
      task_id: msg.task_id,
      status: msg.status,
      progress: msg.progress,
      stage: msg.stage,
      title: '',
      format: undefined,
    });
  }

  const el = state.taskEls.get(msg.task_id);
  if (el) {
    updateTaskCard(el, state.tasks.get(msg.task_id));
  } else {
    renderTaskList();
  }

  stampTerminalAt(state.tasks.get(msg.task_id));
  pruneTransientState(); // mantém notifiedTasks/autoDownloaded/Fila sob controle

  // O contrato WS (fixo) não entrega path/error: em status terminal, busca o
  // snapshot REST e enriquece a task — link "Salvar no celular" no done e o
  // motivo no failed só aparecem com o merge dos campos do REST.
  if (msg.status === 'done' || msg.status === 'failed') {
    notifyDownload(state.tasks.get(msg.task_id));
    refreshTaskMeta(msg.task_id);
  }
  updateDownloadsBadge();
  syncDlButtons(); // progresso/status da task mudou → atualiza .dl-btn/.dl-album-btn
}

// Dispara o download automático no dispositivo do cliente quando o arquivo fica
// pronto — apenas para downloads AVULSOS (gate evita flood ao baixar álbuns).
function triggerAutoDownload(task) {
  if (!state.autoDownloadPending) return;
  if (!task || !task.path || task.status !== 'done') return;
  if (state.autoDownloaded.has(task.task_id)) return;
  state.autoDownloaded.set(task.task_id, Date.now()); // entrada é podada por TTL
  const a = document.createElement('a');
  a.href = API.library(task.path);
  a.download = '';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  showToast(`Baixando “${task.title || 'Música'}” no seu dispositivo…`, 'success');
}

// Busca o snapshot REST e faz merge dos campos que o update WS não carrega.
async function refreshTaskMeta(taskId) {
  try {
    const tasks = await listDownloadsApi();
    mergeTasks(tasks);
    const enriched = state.tasks.get(taskId);
    if (enriched) {
      const el = state.taskEls.get(taskId);
      if (el) updateTaskCard(el, enriched);
      else renderTaskList();
      if (enriched.status === 'done' && enriched.path) {
        triggerAutoDownload(enriched);
      }
    }
  } catch {
    // Fallback REST falhou — o card mantém o estado do update WS (sem path/error).
  }
}

// ------------------------------------------------- token de acesso (auth)

// Gestão de foco dos <dialog>: guarda quem tinha o foco ao abrir, foca o
// primeiro input depois (requestAnimationFrame) e restaura no close (qualquer
// via: Esc nativo, botão fechar ou submit do formulário).
let _modalFocusReturn = null;

function openDialog(modal, focusSelector) {
  if (!modal) return;
  _modalFocusReturn = document.activeElement;
  const opened = typeof modal.showModal === 'function' ? !modal.open : !modal.hasAttribute('open');
  if (typeof modal.showModal === 'function') {
    if (!modal.open) modal.showModal();
  } else {
    modal.setAttribute('open', 'true');
  }
  if (opened) {
    // O dialog <dialog> nativo dispara 'close' ao fechar; no fallback sem
    // showModal/close, closeDialog() restaura o foco manualmente.
    modal.addEventListener('close', restoreModalFocus, { once: true });
  }
  if (focusSelector) {
    const input = modal.querySelector(focusSelector);
    if (input) requestAnimationFrame(() => input.focus());
  }
}

function closeDialog(modal) {
  if (!modal) return;
  if (typeof modal.close === 'function') modal.close();
  else {
    modal.removeAttribute('open');
    restoreModalFocus();
  }
}

function restoreModalFocus() {
  const target = _modalFocusReturn;
  _modalFocusReturn = null;
  if (target && typeof target.focus === 'function') {
    try {
      target.focus();
    } catch {
      /* noop */
    }
  }
}

function loadToken() {
  try {
    return localStorage.getItem(STORAGE_TOKEN_KEY) || '';
  } catch {
    return ''; // storage bloqueado (ex.: modo privado)
  }
}

function openTokenModal() {
  const modal = document.getElementById('token-modal');
  if (!modal) return;
  const input = document.getElementById('token-input');
  if (state.token && input) input.value = state.token;
  // Foco no input ao abrir e restaura o foco anterior ao fechar.
  openDialog(modal, '#token-input');
}

function bindTokenModal() {
  const modal = document.getElementById('token-modal');
  const form = document.getElementById('token-form');

  if (form) {
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const input = document.getElementById('token-input');
      const value = (input ? input.value : '').trim();
      if (!value) {
        if (input) input.focus();
        return;
      }
      state.token = value;
      try {
        localStorage.setItem(STORAGE_TOKEN_KEY, value);
      } catch {
        // storage bloqueado — token vale apenas para esta sessão
      }
      if (modal) closeDialog(modal); // restaura o foco do elemento anterior
      showToast('Token salvo!', 'success');
      // Retoma o que estava em andamento
      if (state.lastQuery) runSearch(state.lastQuery);
      if (state.currentView === 'downloads') refreshDownloads();
      connectWS(); // o WS ficou de fora enquanto não havia token
    });
  }
}

// ---------------------------------------------------------------- init

// Carrega /api/config: sem ffmpeg no servidor, exibe banner persistente e
// desabilita downloads (spec: "banner/aviso na UI").
async function loadConfig() {
  try {
    const config = await apiFetch(API.config());
    state.hasFfmpeg = config.has_ffmpeg !== false;
    state.authRequired = config.auth_required === true;
    if (state.authRequired && !state.token) {
      openTokenModal();
    }
  } catch {
    // Sem resposta na inicialização (servidor caindo): mantém otimista e libera
    // os downloads — o toast de rede já cobre a indisponibilidade.
    state.hasFfmpeg = true;
  }
  applyFfmpegState();
}

// Aplica o estado de ffmpeg no DOM: banner persistente + botões desabilitados.
function applyFfmpegState() {
  const banner = document.getElementById('ffmpeg-banner');
  if (banner) banner.hidden = state.hasFfmpeg;

  const dlAlbumBtn = document.getElementById('download-album-btn');
  if (dlAlbumBtn) dlAlbumBtn.disabled = !state.hasFfmpeg;
  document.querySelectorAll('.track-dl').forEach((btn) => {
    btn.disabled = !state.hasFfmpeg;
  });
  document.querySelectorAll('.dl-btn:not([data-action]), .dl-album-btn').forEach((btn) => {
    btn.disabled = !state.hasFfmpeg;
  });
  const dlNav = document.querySelector('.nav-btn[data-tab="downloads"]');
  if (dlNav) dlNav.disabled = !state.hasFfmpeg;
}

function bindConnectModal() {
  const modal = document.getElementById('connect-modal');
  const btn = document.getElementById('connect-btn');
  const closeBtn = document.getElementById('close-modal-btn');
  const copyBtn = document.getElementById('copy-url-btn');

  if (btn && modal) {
    btn.addEventListener('click', () => {
      if (typeof modal.showModal === 'function') modal.showModal();
      else modal.setAttribute('open', 'true');
    });
  }

  if (closeBtn && modal) {
    closeBtn.addEventListener('click', () => {
      if (typeof modal.close === 'function') modal.close();
      else modal.removeAttribute('open');
    });
  }

  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      const urlCode = document.getElementById('modal-server-url');
      if (!urlCode) return;
      const text = urlCode.textContent || '';
      // Em origens inseguras (HTTP) navigator.clipboard é undefined — e mesmo
      // quando existe, writeText pode rejeitar (permissão/contexto).
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        try {
          await navigator.clipboard.writeText(text);
          showToast('URL copiada para a área de transferência!', 'success');
          return;
        } catch {
          // cai no fallback abaixo
        }
      }
      // Fallback: textarea oculta + execCommand('copy') (funciona em HTTP).
      let copied = false;
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        copied = document.execCommand('copy');
        document.body.removeChild(ta);
      } catch {
        copied = false;
      }
      showToast(
        copied ? 'URL copiada para a área de transferência!' : 'Não foi possível copiar.',
        copied ? 'success' : 'error'
      );
    });
  }
}

function loadFormat() {
  let saved = null;
  try {
    saved = localStorage.getItem(STORAGE_FORMAT_KEY);
  } catch {
    saved = null; // storage bloqueado (ex.: modo privado) → DEFAULT_FORMAT
  }
  // Respeita a preferência salva (mp3 OU opus); sem valor, usa o default.
  if (saved === 'mp3' || saved === 'opus') return saved;
  return DEFAULT_FORMAT;
}

// ------------------------------------------------------------ tema claro/escuro

function loadTheme() {
  let saved = null;
  try {
    saved = localStorage.getItem(STORAGE_THEME_KEY);
  } catch {
    saved = null; // storage bloqueado → claro (padrão na 1ª visita)
  }
  return saved === 'dark' ? 'dark' : 'light';
}

// Aplica o tema no <html> (dataset.theme), no meta theme-color (barra do
// navegador), no estado e no aria-pressed do toggle. O CSS esconde o ícone
// sun/moon do tema não ativo — o JS só alterna data-theme.
function applyTheme(theme) {
  state.theme = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = state.theme;
  const toggle = document.getElementById('theme-toggle');
  if (toggle) toggle.setAttribute('aria-pressed', String(state.theme === 'dark'));
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = THEME_COLORS[state.theme] || THEME_COLORS.light;
  try {
    localStorage.setItem(STORAGE_THEME_KEY, state.theme);
  } catch {
    // storage bloqueado — tema vale apenas para esta sessão
  }
}

function bindThemeToggle() {
  const toggle = document.getElementById('theme-toggle');
  if (!toggle) return;
  toggle.addEventListener('click', () => {
    applyTheme(state.theme === 'dark' ? 'light' : 'dark');
  });
}

// Estado ativo dos botões de formato no header (fonte da verdade: state.format).
function applyFormatButtons() {
  document.querySelectorAll('.fmt-btn').forEach((b) => {
    const active = b.dataset.format === state.format;
    b.classList.toggle('is-active', active);
    b.setAttribute('aria-pressed', String(active));
  });
}

function bindHeader() {
  document.querySelectorAll('.fmt-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.format = btn.dataset.format === 'opus' ? 'opus' : 'mp3';
      try {
        localStorage.setItem(STORAGE_FORMAT_KEY, state.format);
      } catch {
        // storage bloqueado — formato vale apenas para esta sessão
      }
      applyFormatButtons();
    });
  });
}

function bindNav() {
  document.querySelector('.bottom-nav').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-tab]');
    if (!btn) return;
    if (btn.dataset.tab === 'buscar') openSearchTab();
    else if (btn.dataset.tab === 'biblioteca') openBibliotecaTab();
    else if (btn.dataset.tab === 'player') openPlayerTab();
    else openDownloadsTab();
  });
}

function openEditMetaModal(data) {
  const modal = document.getElementById('edit-meta-modal');
  const inputYtId = document.getElementById('edit-yt-id');
  const inputTitle = document.getElementById('edit-title');
  const inputArtist = document.getElementById('edit-artist');
  const inputAlbum = document.getElementById('edit-album');

  if (!modal || !inputYtId) return;

  inputYtId.value = data.ytId || '';
  inputTitle.value = data.title || '';
  inputArtist.value = data.artist || '';
  inputAlbum.value = data.album || '';

  // Foco no primeiro input ao abrir e restaura o foco anterior ao fechar.
  openDialog(modal, '#edit-yt-id');
}

function bindEditMetaModal() {
  const modal = document.getElementById('edit-meta-modal');
  const closeBtn = document.getElementById('close-edit-modal-btn');
  const form = document.getElementById('edit-meta-form');

  if (closeBtn && modal) {
    closeBtn.addEventListener('click', () => {
      closeDialog(modal); // restaura o foco do elemento anterior
    });
  }

  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const ytId = document.getElementById('edit-yt-id').value;
      const title = document.getElementById('edit-title').value;
      const artist = document.getElementById('edit-artist').value;
      const album = document.getElementById('edit-album').value;

      try {
        await apiFetch(`/api/history/${encodeURIComponent(ytId)}/metadata`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, artist, album }),
        });
        showToast('Metadados e tags atualizadas!', 'success');
        closeDialog(modal); // restaura o foco do elemento anterior
        refreshDownloads();
      } catch (err) {
        handleApiError(err, 'Não foi possível salvar os metadados.');
      }
    });
  }
}

function registerServiceWorker() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
  }
}

function init() {
  state.format = loadFormat();
  state.token = loadToken();
  state.theme = loadTheme();
  state.libraryView = loadLibraryView();
  state.libraryFmt = loadLibraryFmt();
  state.crossfadeSeconds = loadCrossfade();
  state.mainEl = document.getElementById('app');
  state.toastRegion = document.getElementById('toast-region');
  // Estado da conexão + banner (navigator.onLine pode ser undefined em alguns
  // ambientes → assume online).
  state.online = typeof navigator !== 'undefined' ? navigator.onLine : true;
  updateConnBanner();
  window.addEventListener('online', onNetworkOnline);
  window.addEventListener('offline', onNetworkOffline);

  bindHeader();
  bindNav();
  bindConnectModal();
  bindEditMetaModal();
  bindTokenModal();
  bindPlayer();
  bindThemeToggle();
  bindMediaSessionActions(); // handlers de play/pause/prev/next/seekto (uma vez)
  bindPlayerDragHandle(); // guard: só liga quando a view do player existir
  bindCrossfadeControl(); // guard: idem (rebind real no bindPlayerViewEvents)
  registerServiceWorker();

  // Aplica tema e formato ANTES da primeira view (fonte da verdade no init).
  applyTheme(state.theme);
  applyFormatButtons();

  loadConfig();

  // Pré-carrega o histórico: cards de música da busca tocam o arquivo baixado
  // (playSearchTrack) e a Biblioteca já abre populada, sem depender de abrir a
  // aba Downloads antes. Melhor-esforço — falha aqui não bloqueia o app.
  historyApi()
    .then((h) => {
      state.history = h || [];
      if (state.currentView === 'biblioteca') renderLibrary();
    })
    .catch(() => {});

  // Biblioteca local: restaura os metadados da seleção anterior (sem file) —
  // silencioso se o IndexedDB estiver indisponível.
  loadLocalFiles();

  // Poda periódica do estado transitório (notificações/auto-downloads/Fila).
  setInterval(pruneTransientState, 2 * 60 * 1000);

  // WS sempre ligado: alimenta o badge de downloads ativos e as notificações
  // de fim de download mesmo fora da aba Downloads.
  connectWS();

  showView('search', {});
}

document.addEventListener('DOMContentLoaded', init);
