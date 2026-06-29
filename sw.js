const CACHE = 'assistant-v11';
const ASSETS = ['/', '/index.html', '/manifest.json'];

self.addEventListener('install', e => {
  // Cài từng asset riêng: một file 404 không làm hỏng toàn bộ install.
  e.waitUntil(
    caches.open(CACHE).then(c =>
      Promise.all(ASSETS.map(u =>
        fetch(u, { cache: 'no-store' }).then(r => r.ok ? c.put(u, r.clone()) : null).catch(() => null)
      ))
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});

// Push notification
self.addEventListener('push', e => {
  const data = e.data?.json() || {};
  const title = data.title || 'Trợ lý AI';
  const body  = data.body  || 'Bạn có tin nhắn mới';
  const icon  = data.icon  || '/static/icon-192.png';

  e.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon,
      badge: icon,
      vibrate: [200, 100, 200],
      data: { url: data.url || '/' },
      actions: [
        { action: 'open', title: 'Mở chat' },
        { action: 'close', title: 'Bỏ qua' }
      ]
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  if (e.action === 'close') return;
  e.waitUntil(
    clients.matchAll({ type: 'window' }).then(list => {
      for (const client of list) {
        if (client.url === '/' && 'focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow('/');
    })
  );
});
