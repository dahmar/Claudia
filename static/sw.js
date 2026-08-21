// Минимальный service worker — только чтобы браузер разрешил "Добавить на экран".
// Кэширования нет специально: Клавдии всегда нужен живой ответ от сервера.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());
self.addEventListener('fetch', () => {});
