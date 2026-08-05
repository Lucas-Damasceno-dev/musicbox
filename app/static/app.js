/* MusicBox — frontend vanilla (sem dependências, offline).
 * Textos de UI/aria-labels/comentários em português; identificadores em inglês.
 * Contrato de API conforme .sdd/briefs/task-7-brief.md (valores exatos).
 */
'use strict';

// ------------------------------------------------------------- constantes

const STORAGE_FORMAT_KEY = 'musicbox.format';
const DEFAULT_FORMAT = 'mp3';

// Rótulos exibidos para cada formato (toggle do header).
const FORMAT_LABEL = { mp3: 'MP3 320', opus: 'Opus 160' };

// Rotas do contrato. `library` codifica cada segmento do path (tem barras).
const API = {
  config: () => '/api/config',
  search: (q) => `/api/search?q=${encodeURIComponent(q)}`,
  artistAlbums: (name) => `/api/artists/${encodeURIComponent(name)}/albums`,
  albumTracks: (browseId) => `/api/albums/${encodeURIComponent(browseId)}/tracks`,
  downloads: () => '/api/downloads',
  history: () => '/api/history',
  library: (relPath) =>
    `/api/library/${relPath.split('/').map(encodeURIComponent).join('/')}`,
};

// Rótulos PT dos status e etapas de DownloadTask (contrato).
const STATUS_LABEL = {
  pending: 'na fila',
  running: 'baixando',
  done: 'concluído',
  failed: 'erro',
  skipped: 'pulado',
};

const STAGE_LABEL = {
  queued: 'na fila',
  extracting: 'baixando',
  converting: 'convertendo',
  moving: 'movendo',
  done: 'concluído',
};

const STATUS_BADGE = {
  pending: 'badge-pending',
  running: 'badge-running',
  done: 'badge-done',
  failed: 'badge-failed',
  skipped: 'badge-skipped',
};

// Ícones inline (SVG — sem dependências externas).
const ICONS = {
  download:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v10m0 0 4-4m-4 4-4-4"/><path d="M5 20h14"/></svg>',
  back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>',
  chevron:
    '<svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>',
};

// ---------------------------------------------------------------- estado

const state = {
  format: DEFAULT_FORMAT, // formato ativo (mp3 | opus), persistido
  hasFfmpeg: true, // servidor com ffmpeg (config carregada no init)
  activeTab: 'artists', // aba da busca: 'artists' | 'albums'
  results: { artists: [], albums: [] }, // últimos resultados da busca
  lastQuery: '', // último termo buscado (restaurado ao voltar para a busca)
  currentView: 'search', // view renderizada (search|artist|album|downloads)
  currentData: {},
  backStack: [], // pilha de navegação interna (search → artist → album)
  tasks: new Map(), // task_id -> task (fila ao vivo, ordem de inserção)
  history: [],
  taskEls: new Map(), // task_id -> elemento do card (update in place)
  ws: null, // única conexão WebSocket
  wsActive: false,
  wsReconnectTimer: null,
  mainEl: null,
  toastRegion: null,
  autoDownloaded: new Set(),
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
  let res;
  try {
    res = await fetch(url, options);
  } catch {
    // TypeError: servidor inacessível (brief: toast "Sem conexão com o servidor")
    showToast('Sem conexão com o servidor', 'error');
    const err = new ApiError(0, 'Sem conexão com o servidor');
    err.isNetwork = true;
    throw err;
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

async function searchApi(q) {
  return apiFetch(API.search(q));
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

async function listDownloadsApi() {
  return apiFetch(API.downloads());
}

async function historyApi() {
  return apiFetch(API.history());
}

// Tratamento de erro HTTP genérico (toast já exibido para erro de rede).
function handleApiError(err, fallback) {
  if (err.isNetwork) return;
  if (err.status === 422) showToast(err.detail || 'Dados inválidos.', 'error');
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
}

function viewHtml(name, data) {
  if (name === 'artist') return artistViewHtml(data);
  if (name === 'album') return albumViewHtml(data.album);
  if (name === 'downloads') return downloadsViewHtml();
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

  // Reaplica a aba ativa (a view de busca nasce com Artistas selecionado)
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    const active = btn.dataset.tab === state.activeTab;
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-selected', String(active));
  });

  if (state.results.artists.length || state.results.albums.length) {
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
  disconnectWS();
  state.backStack = [];
  showView('search', {});
}

function openDownloadsTab() {
  setActiveTab('downloads');
  state.backStack = [];
  showView('downloads', {});
  refreshDownloads();
  connectWS();
}

function bindViewEvents(name, data) {
  if (name === 'search') bindSearchEvents();
  else if (name === 'artist') bindArtistEvents();
  else if (name === 'album') bindAlbumEvents(data.album);
  else if (name === 'downloads') bindDownloadsEvents();
}

// --------------------------------------------------------------- busca

function searchViewHtml() {
  return `
    <section class="view search-view" aria-label="Buscar">
      <form class="search-bar" id="search-form" role="search">
        <input
          id="search-input"
          type="search"
          placeholder="Artista ou álbum..."
          autocomplete="off"
          aria-label="Buscar artista ou álbum"
        />
        <button type="submit" class="btn btn-primary btn-search">Buscar</button>
      </form>
      <div class="tabbar" role="tablist" aria-label="Tipo de resultado">
        <button type="button" role="tab" data-tab="artists" class="tab-btn is-active" aria-selected="true">Artistas</button>
        <button type="button" role="tab" data-tab="albums" class="tab-btn" aria-selected="false">Álbuns</button>
      </div>
      <div id="results" class="results">
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
      // Busca sem termo → aviso "digite algo" (o backend responderia 422)
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

async function runSearch(q) {
  state.lastQuery = q;
  showSearchMessage('Buscando…');
  try {
    const data = await searchApi(q);
    state.results = { artists: data.artists || [], albums: data.albums || [] };
    renderResults();
  } catch (err) {
    state.results = { artists: [], albums: [] };
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
  const isArtistTab = state.activeTab === 'artists';
  const items = isArtistTab ? state.results.artists : state.results.albums;

  if (!items || items.length === 0) {
    showSearchMessage(isArtistTab ? 'Nenhum artista encontrado.' : 'Nenhum álbum encontrado.');
    return;
  }

  results.innerHTML = `<ul class="card-list">${items
    .map((item, i) => cardHtml(item, isArtistTab ? 'artist' : 'album', i))
    .join('')}</ul>`;

  results.querySelectorAll('.card').forEach((card) => {
    card.addEventListener('click', () => onCardClick(card.dataset));
  });
}

function cardHtml(item, kind, index) {
  const isArtist = kind === 'artist';
  const emoji = isArtist ? '🎤' : '💿';
  return `
    <li>
      <button
        type="button"
        class="card"
        data-kind="${escapeHtml(kind)}"
        data-id="${escapeHtml(item.id)}"
        data-title="${escapeHtml(item.title)}"
        style="animation-delay:${Math.min(index * 40, 400)}ms"
      >
        <span class="cover cover--${kind}" aria-hidden="true">${emoji}</span>
        <span class="card-body">
          <span class="card-title">${escapeHtml(item.title)}</span>
          <span class="card-kind">${isArtist ? 'Artista' : 'Álbum'}</span>
        </span>
        ${ICONS.chevron}
      </button>
    </li>`;
}

function onCardClick({ kind, id, title }) {
  if (kind === 'artist') {
    artistAlbumsApi(title)
      .then((items) => openArtist(title, items))
      .catch((err) => handleApiError(err, 'Não foi possível carregar os álbuns.'));
  } else {
    albumTracksApi(id)
      .then((album) => openAlbum(album))
      .catch((err) => handleApiError(err, 'Não foi possível carregar o álbum.'));
  }
}

// --------------------------------------------------- álbuns do artista

function artistViewHtml({ name, items }) {
  const albums = items || [];
  return `
    <section class="view sub-view" aria-label="Álbuns de ${escapeHtml(name)}">
      <header class="sub-header">
        <button type="button" class="icon-btn" id="back-btn" aria-label="Voltar">${ICONS.back}</button>
        <div class="sub-title">
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
    card.addEventListener('click', () => onCardClick(card.dataset));
  });
}

// ------------------------------------------------------- tela do álbum

function albumViewHtml(album) {
  const tracks = album.tracks || [];
  return `
    <section class="view sub-view" aria-label="Álbum ${escapeHtml(album.title)}">
      <header class="sub-header">
        <button type="button" class="icon-btn" id="back-btn" aria-label="Voltar">${ICONS.back}</button>
        <div class="sub-title">
          <h1 class="sub-heading">${escapeHtml(album.title)}</h1>
          <p class="sub-meta">${escapeHtml(album.artist)} · ${tracks.length} ${tracks.length === 1 ? 'faixa' : 'faixas'}</p>
        </div>
      </header>

      <div class="album-hero">
        <span class="cover cover--hero cover--album" aria-hidden="true">💿</span>
        <button type="button" class="btn btn-primary btn-block" id="download-album-btn">
          ${ICONS.download}
          Baixar álbum inteiro
        </button>
        <p class="album-hint">Formato atual: <strong>${formatLabel(state.format)}</strong></p>
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
  const btn = document.querySelector(`.track-dl[data-yt-id="${CSS.escape(ytId)}"]`);
  if (btn) btn.disabled = true; // evita duplo-submit durante o POST
  try {
    await postDownloadApi({ yt_id: ytId, formato: state.format });
    showToast(`“${title}” adicionado à fila`, 'success');
    openDownloadsTab();
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
  const btn = document.getElementById('download-album-btn');
  if (btn) btn.disabled = true; // evita duplo-submit durante o POST
  try {
    const data = await postDownloadApi({ album_id: album.id, formato: state.format });
    const count = data.tasks ? data.tasks.length : 0;
    showToast(
      `${count} ${count === 1 ? 'faixa adicionada' : 'faixas adicionadas'} à fila`,
      'success'
    );
    openDownloadsTab();
  } catch (err) {
    handleApiError(err, 'Não foi possível baixar o álbum.');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ------------------------------------------------------------ downloads

function downloadsViewHtml() {
  return `
    <section class="view downloads-view" aria-label="Downloads">
      <header class="view-head">
        <h1 class="view-title">Downloads</h1>
        <div class="view-head-actions">
          <button type="button" class="btn btn-ghost btn-small" id="retry-failed-btn">Retentar Falhas</button>
          <a href="/api/export.m3u" download class="btn btn-ghost btn-small" id="export-m3u-btn">Exportar .M3U</a>
          <button type="button" class="btn btn-ghost btn-small" id="refresh-btn">Atualizar</button>
        </div>
      </header>

      <div class="queue-section">
        <h2 class="section-title">Fila</h2>
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

function bindDownloadsEvents() {
  const refresh = document.getElementById('refresh-btn');
  if (refresh) refresh.addEventListener('click', refreshDownloads);

  const retryFailed = document.getElementById('retry-failed-btn');
  if (retryFailed) {
    retryFailed.addEventListener('click', async () => {
      try {
        const res = await apiFetch('/api/downloads/retry-failed', { method: 'POST' });
        showToast(`${res.retried_count} ${res.retried_count === 1 ? 'task re-enfileirada' : 'tasks re-enfileiradas'}`, 'success');
        refreshDownloads();
      } catch (err) {
        handleApiError(err, 'Não foi possível retentar falhas.');
      }
    });
  }
}

// Fallback REST + histórico (usado ao abrir a aba e se o WS cair).
async function refreshDownloads() {
  try {
    const tasks = await listDownloadsApi();
    mergeTasks(tasks);
    renderTaskList();
  } catch (err) {
    handleApiError(err, 'Não foi possível carregar a fila.');
  }
  try {
    state.history = await historyApi();
    renderHistory();
  } catch (err) {
    handleApiError(err, 'Não foi possível carregar o histórico.');
  }
}

// Mescla preservando a ordem de inserção (novos no fim, existentes atualizados).
function mergeTasks(tasks) {
  const next = new Map();
  state.tasks.forEach((task, id) => next.set(id, task));
  tasks.forEach((task) => {
    const existing = next.get(task.task_id);
    next.set(task.task_id, existing ? { ...existing, ...task } : task);
  });
  state.tasks = next;
}

function renderTaskList() {
  const listEl = document.getElementById('task-list');
  if (!listEl) return;
  state.taskEls.clear();
  const tasks = [...state.tasks.values()];

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

function updateTaskCard(el, task) {
  const status = task.status || 'pending';
  const stage = task.stage || 'queued';
  const progress = clampNumber(task.progress, 0, 100);
  const title = task.title || 'Música';

  const metaParts = [task.artist, task.album].filter(Boolean);
  const meta = metaParts.length ? escapeHtml(metaParts.join(' · ')) : '';
  const chip = task.format
    ? `<span class="chip">${escapeHtml(formatLabel(task.format))}</span>`
    : '';
  const badge = `<span class="badge ${STATUS_BADGE[status] || 'badge-pending'}">${STATUS_LABEL[status] || status}</span>`;

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
        <button type="button" class="btn btn-ghost btn-small edit-meta-btn" data-yt-id="${escapeHtml(task.yt_id)}" data-title="${escapeHtml(task.title || '')}" data-artist="${escapeHtml(task.artist || '')}" data-album="${escapeHtml(task.album || '')}">Editar Tags</button>
      </div>`;
  } else if (status === 'failed') {
    actions = `
      ${task.error ? `<p class="task-error">${escapeHtml(task.error)}</p>` : ''}
      <button type="button" class="btn btn-ghost btn-small retry-btn">Tentar de novo</button>`;
  }

  el.innerHTML = `
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

  const retryBtn = el.querySelector('.retry-btn');
  if (retryBtn) {
    retryBtn.addEventListener('click', () => retryTask(task));
  }

  const editBtn = el.querySelector('.edit-meta-btn');
  if (editBtn) {
    editBtn.addEventListener('click', () => openEditMetaModal(editBtn.dataset));
  }
}

async function retryTask(task) {
  try {
    // Re-POST com o mesmo yt_id e formato da task que falhou
    await postDownloadApi({ yt_id: task.yt_id, formato: task.format });
    showToast('Tentando de novo…', 'success');
    // Remove o card falho e recarrega o snapshot (a nova task aparece na fila)
    state.tasks.delete(task.task_id);
    state.taskEls.delete(task.task_id);
    await refreshDownloads();
  } catch (err) {
    handleApiError(err, 'Não foi possível tentar de novo.');
  }
}

function renderHistory() {
  const listEl = document.getElementById('history-list');
  if (!listEl) return;
  if (!state.history || state.history.length === 0) {
    listEl.innerHTML = '<p class="empty-state">Nenhum download ainda.</p>';
    return;
  }
  listEl.innerHTML = state.history
    .map((record) => {
      const status = record.status || 'pending';
      return `
        <div class="history-item">
          <div class="history-main">
            <span class="history-title">${escapeHtml(record.title || 'Sem título')}</span>
            <span class="history-date">${formatDate(record.date)}${
              record.format ? ` · ${escapeHtml(formatLabel(record.format))}` : ''
            }</span>
          </div>
          <span class="badge ${STATUS_BADGE[status] || 'badge-pending'}">${STATUS_LABEL[status] || status}</span>
        </div>`;
    })
    .join('');
}

// ---------------------------------------------------------- WebSocket

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}/ws`;
}

// Conecta ao /ws apenas enquanto a aba Downloads está aberta.
function connectWS() {
  if (state.ws) return; // já conectado
  state.wsActive = true;

  const ws = new WebSocket(wsUrl());
  state.ws = ws;

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
    } else if (msg.type === 'update') {
      applyUpdate(msg);
    }
  });

  ws.addEventListener('close', () => {
    state.ws = null;
    if (!state.wsActive) return;
    if (!state.wsReconnectTimer) {
      showToast('Conexão em tempo real perdida. Reconectando…', 'error');
      refreshDownloads(); // fallback REST para o estado inicial
    }
    scheduleWsReconnect();
  });

  ws.addEventListener('error', () => {
    try {
      ws.close(); // o handler de close cuida do fallback/reconnect
    } catch {
      /* noop */
    }
  });
}

function scheduleWsReconnect() {
  clearTimeout(state.wsReconnectTimer);
  state.wsReconnectTimer = setTimeout(() => {
    state.wsReconnectTimer = null;
    if (state.wsActive) connectWS();
  }, 3000);
}

// Encerra a única conexão ao trocar de aba (sem vazar conexões).
function disconnectWS() {
  state.wsActive = false;
  clearTimeout(state.wsReconnectTimer);
  state.wsReconnectTimer = null;
  if (state.ws) {
    try {
      state.ws.close();
    } catch {
      /* noop */
    }
    state.ws = null;
  }
}

// Update chega sem title/format; preserva os dados já conhecidos da task.
function applyUpdate(msg) {
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

  // O contrato WS (fixo) não entrega path/error: em status terminal, busca o
  // snapshot REST e enriquece a task — link "Salvar no celular" no done e o
  // motivo no failed só aparecem com o merge dos campos do REST.
  if (msg.status === 'done' || msg.status === 'failed') {
    refreshTaskMeta(msg.task_id);
  }
}

// Dispara o download automático no dispositivo do cliente quando o arquivo fica pronto.
function triggerAutoDownload(task) {
  if (!task || !task.path || task.status !== 'done') return;
  if (state.autoDownloaded.has(task.task_id)) return;
  state.autoDownloaded.add(task.task_id);
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

// ---------------------------------------------------------------- init

// Carrega /api/config: sem ffmpeg no servidor, exibe banner persistente e
// desabilita downloads (spec: "banner/aviso na UI").
async function loadConfig() {
  try {
    const config = await apiFetch(API.config());
    state.hasFfmpeg = config.has_ffmpeg !== false;
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
    copyBtn.addEventListener('click', () => {
      const urlCode = document.getElementById('modal-server-url');
      if (!urlCode) return;
      navigator.clipboard.writeText(urlCode.textContent).then(() => {
        showToast('URL copiada para a área de transferência!', 'success');
      }).catch(() => {
        showToast('Não foi possível copiar.', 'error');
      });
    });
  }
}

function loadFormat() {
  let saved = null;
  try {
    saved = localStorage.getItem(STORAGE_FORMAT_KEY);
  } catch {
    saved = null; // storage bloqueado (ex.: modo privado) → fallback mp3
  }
  return saved === 'opus' ? 'opus' : DEFAULT_FORMAT;
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
      document.querySelectorAll('.fmt-btn').forEach((b) => {
        const active = b.dataset.format === state.format;
        b.classList.toggle('is-active', active);
        b.setAttribute('aria-pressed', String(active));
      });
    });
  });
}

function bindNav() {
  document.querySelector('.bottom-nav').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-tab]');
    if (!btn) return;
    if (btn.dataset.tab === 'buscar') openSearchTab();
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

  if (typeof modal.showModal === 'function') modal.showModal();
  else modal.setAttribute('open', 'true');
}

function bindEditMetaModal() {
  const modal = document.getElementById('edit-meta-modal');
  const closeBtn = document.getElementById('close-edit-modal-btn');
  const form = document.getElementById('edit-meta-form');

  if (closeBtn && modal) {
    closeBtn.addEventListener('click', () => {
      if (typeof modal.close === 'function') modal.close();
      else modal.removeAttribute('open');
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
        if (typeof modal.close === 'function') modal.close();
        else modal.removeAttribute('open');
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
  state.mainEl = document.getElementById('app');
  state.toastRegion = document.getElementById('toast-region');

  bindHeader();
  bindNav();
  bindConnectModal();
  bindEditMetaModal();
  registerServiceWorker();

  loadConfig();

  document.querySelectorAll('.fmt-btn').forEach((b) => {
    const active = b.dataset.format === state.format;
    b.classList.toggle('is-active', active);
    b.setAttribute('aria-pressed', String(active));
  });

  showView('search', {});
}

document.addEventListener('DOMContentLoaded', init);
