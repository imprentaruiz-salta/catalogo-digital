const CACHE='ruiz-lector-shell-v1';
const SHELL=['/consulta-qr','/lector-qr.webmanifest','/static/lector-qr-192.png','/static/lector-qr-512.png'];
self.addEventListener('install',event=>{event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting()))});
self.addEventListener('activate',event=>{event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',event=>{
 const request=event.request;const url=new URL(request.url);
 if(request.method!=='GET'||url.origin!==self.location.origin)return;
 if(url.pathname.startsWith('/api/consulta-qr/')){
  event.respondWith(fetch(request,{cache:'no-store'}).catch(()=>new Response(JSON.stringify({error:'Sin conexión. Conectate a internet para consultar el precio vigente.'}),{status:503,headers:{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'}})));
  return;
 }
 if(SHELL.includes(url.pathname)){
  event.respondWith(fetch(request).then(response=>{if(response.ok){const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(request,copy))}return response}).catch(()=>caches.match(request).then(cached=>cached||Response.error())));
 }
});
