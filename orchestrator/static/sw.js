// Service worker de Chati: solo recibe los avisos (push.py) y los enseña,
// aunque Chati este cerrado. No guarda nada en cache.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: "Chati", body: event.data && event.data.text() }; }
  // el iPhone exige enseñar un aviso por cada push (si no, quita el permiso)
  event.waitUntil(self.registration.showNotification(data.title || "Chati", {
    body: data.body || "", tag: data.tag || "chati", icon: "/static/icon-192.png", badge: "/static/icon-192.png",
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if ("focus" in w) return w.focus();
    }
    return self.clients.openWindow(url);
  })());
});
