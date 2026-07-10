var CACHE='serenity-v6';
var URLS=['/static/manifest.json'];
self.addEventListener('install',function(e){
  e.waitUntil(caches.open(CACHE).then(function(c){return c.addAll(URLS)}).then(function(){return self.skipWaiting()}));
});
self.addEventListener('activate',function(e){
  e.waitUntil(caches.keys().then(function(keys){
    return Promise.all(keys.filter(function(key){return key!==CACHE}).map(function(key){return caches.delete(key)}));
  }).then(function(){return self.clients.claim()}));
});
self.addEventListener('fetch',function(e){
  if(e.request.method!=='GET') return;
  e.respondWith(fetch(e.request).then(function(response){
    if(response && response.ok){
      var copy=response.clone();
      caches.open(CACHE).then(function(cache){cache.put(e.request,copy)});
    }
    return response;
  }).catch(function(){return caches.match(e.request)}));
});
