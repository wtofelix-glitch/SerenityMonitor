var CACHE='serenity-v7';
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
  var url=new URL(e.request.url);
  // API: 同源 /api/** 强制 network-only + 绕过浏览器 HTTP cache（双保险：Flask 同时设 no-store）
  if(url.origin===self.location.origin && url.pathname.startsWith('/api/')){
    e.respondWith(fetch(e.request,{cache:'no-store'}));
    return;
  }
  // HTML 导航 + 静态 CSS/JS：网络优先，失败回退缓存（保留离线打开看板的能力）
  e.respondWith(fetch(e.request).then(function(response){
    // 只缓存成功响应，避免部署期间 404/500 被缓存为可用资产
    if(response && response.ok){
      var copy=response.clone();
      caches.open(CACHE).then(function(cache){cache.put(e.request,copy)});
    }
    return response;
  }).catch(function(){return caches.match(e.request)}));
});
