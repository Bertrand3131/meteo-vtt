// Service worker de l'appli Météo VTT.
// Toujours le réseau d'abord (pour avoir les dernières données) ; la copie en cache
// ne sert que si le téléphone est hors connexion.
const CACHE = 'meteo-vtt-v2';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;   // fonds de carte, API : non touchés
  e.respondWith(
    fetch(e.request, { cache: 'no-store' })
      .then(rep => { const copie = rep.clone(); caches.open(CACHE).then(c => c.put(e.request, copie)); return rep; })
      .catch(() => caches.match(e.request))
  );
});
