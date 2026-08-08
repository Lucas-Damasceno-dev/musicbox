/* MusicBox Service Worker — PWA & Offline Support */
const CACHE_NAME = 'musicbox-v4';
// Cache de áudio usado pelo app.js (saveTrackOffline). Não é populado pelo
// SW (evita 206 parciais); mantido aqui apenas como referência do nome.
const AUDIO_CACHE = 'musicbox-audio-v1';
const ASSETS = [
  '/',
  '/static/index.html',
  '/static/styles.css',
  '/static/app.js',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/fonts/inter-cyrillic-ext.woff2',
  '/static/fonts/inter-cyrillic.woff2',
  '/static/fonts/inter-greek-ext.woff2',
  '/static/fonts/inter-greek.woff2',
  '/static/fonts/inter-vietnamese.woff2',
  '/static/fonts/inter-latin-ext.woff2',
  '/static/fonts/inter-latin.woff2',
  '/static/fonts/outfit-latin-ext.woff2',
  '/static/fonts/outfit-latin.woff2'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // allSettled: um único asset com 404 (ex.: ícone/fonte removida) não
      // deve derrubar o install inteiro. Só o index.html é crítico — sem ele
      // não há app offline — e apenas nesse caso o install falha.
      Promise.allSettled(ASSETS.map((asset) => cache.add(asset))).then((results) => {
        const critical = ASSETS.find((asset, i) =>
          (asset === '/' || asset === '/static/index.html') && results[i].status === 'rejected'
        );
        if (critical) throw new Error(`Asset crítico falhou no precache: ${critical}`);
      })
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => {
          // Limpa caches do shell antigos (musicbox-v*).
          if (key.startsWith('musicbox-v') && key !== CACHE_NAME) return caches.delete(key);
          // O cache de ÁUDIO (musicbox-audio-v*) é apagado por segurança:
          // as entradas antigas foram auto-capturadas de respostas 206
          // parciais do <audio> (e acumulavam por token), ficando quebradas
          // offline. São recriáveis: o app.js salva as músicas completas via
          // saveTrackOffline no musicbox-audio-v1.
          if (key.startsWith('musicbox-audio')) return caches.delete(key);
        })
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);

  // Áudio da biblioteca: network-first com fallback ao cache. NÃO cacheia
  // aqui: respostas de /api/library/ podem ser 206 parciais (Range do
  // <audio>) e o cache acumularia entradas por token — offline tocaria só um
  // trecho. O cache offline é feito explicitamente pelo app.js
  // (saveTrackOffline -> musicbox-audio-v1), com a faixa completa.
  if (url.pathname.startsWith('/api/library/')) {
    event.respondWith(
      fetch(event.request).catch(() =>
        caches.match(event.request).then((cached) => cached || caches.match('/'))
      )
    );
    return;
  }

  if (url.pathname.startsWith('/api/')) return; // demais APIs dinâmicas sem cache

  // Shell/estático: cache-first.
  event.respondWith(
    caches.match(event.request).then((cached) => {
      return cached || fetch(event.request).then((response) => {
        return caches.open(CACHE_NAME).then((cache) => {
          cache.put(event.request, response.clone());
          return response;
        });
      });
    }).catch(() => caches.match('/'))
  );
});
