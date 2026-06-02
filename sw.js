// ── Erik's Memory Palace · Service Worker ──
// 监听推送事件，弹通知；监听点击事件，打开页面。

self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(
    self.registration.showNotification(data.title || 'Erik', {
      body: data.body || '',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      vibrate: [80, 40, 80],
      data: { url: data.url || '/' },
      tag: data.tag || 'erik-push',
      renotify: true
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      // 如果已经有窗口打开了，聚焦它
      for (const client of windowClients) {
        if (client.url.includes(self.registration.scope) && 'focus' in client) {
          return client.focus();
        }
      }
      // 否则打开新窗口
      return clients.openWindow(event.notification.data?.url || '/');
    })
  );
});

// 基础缓存策略（PWA 离线兼容）
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});
