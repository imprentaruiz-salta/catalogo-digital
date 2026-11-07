const CACHE='lector-qr-independent-v2';
const SHELL=['/lector-qr','/lector-qr/manifest.webmanifest','/lector-qr/html5-qrcode.min.js','/static/lector-qr-192.png','/static/lector-qr-512.png'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('lector-qr-independent-')&&k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{const r=e.request,u=new URL(r.url);if(r.method!=='GET'||u.origin!==self.location.origin)return;if(SHELL.includes(u.pathname)){e.respondWith(fetch(r).then(res=>{if(res.ok)caches.open(CACHE).then(c=>c.put(r,res.clone()));return res}).catch(()=>caches.match(r).then(c=>c||Response.error())));return;}});
