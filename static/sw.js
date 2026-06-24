var CACHE='serenity-v1';
var URLS=['/monitor','/static/css/monitor.css','/static/js/monitor.js','/static/manifest.json'];
self.addEventListener('install',function(e){e.waitUntil(caches.open(CACHE).then(function(c){return c.addAll(URLS)}))});
self.addEventListener('fetch',function(e){e.respondWith(caches.match(e.request).then(function(r){return r||fetch(e.request)}))});
