'use strict';
/* bootstrap: 错误监听 + 启动看门狗。先于 app.js 加载。 */
(function () {
  function showFatal(message) {
    var el = document.getElementById('fatalError');
    if (!el) return;
    el.style.display = 'block';
    el.textContent = '前端加载失败：' + message + '（request_id=boot）';
  }
  window.__knowledgeBootError = null;
  window.addEventListener('error', function (ev) {
    window.__knowledgeBootError = String((ev && ev.message) || 'unknown error');
    try {
      var el = document.getElementById('fatalError');
      if (el && !window.__knowledgeAppReady) {
        showFatal(window.__knowledgeBootError);
      }
    } catch (e) {}
  });
  window.addEventListener('unhandledrejection', function (ev) {
    var msg = ev && ev.reason ? String((ev.reason && ev.reason.message) || ev.reason) : 'unhandled rejection';
    window.__knowledgeBootError = msg;
    try {
      var el = document.getElementById('fatalError');
      if (el && !window.__knowledgeAppReady) {
        showFatal(msg);
      }
    } catch (e) {}
  });
  window.__knowledgeWatchdog = setTimeout(function () {
    if (!window.__knowledgeAppReady) {
      showFatal(window.__knowledgeBootError || 'app.js 未在 5 秒内就绪');
    }
  }, 5000);
})();
