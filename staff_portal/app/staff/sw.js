// Required for "Install app" on Android. Always goes to the network so staff never see stale
// device states; if the network is down, the app shows its own "Can't connect" screen.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
