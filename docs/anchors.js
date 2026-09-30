/* 區塊標題的 # 連結（2026-10-01）。
 *
 * 每個有英文 id 的區塊標題旁邊放一個「#」：點了網址會帶上 #id（直接複製網址列就能分享），
 * 同時把完整網址複製到剪貼簿。拿到連結的人打開時會自動捲到那一區 —— 很多區塊不在頂列上，
 * 這是唯一能直接指到它們的方法。
 *
 * 錨點名稱寫死在 HTML 裡、一律英文：從中文標題自動產生的話，換個字連結就失效，
 * 而且中文網址在不少聊天軟體裡會被編碼成一長串 %E5%…。
 * 標題自己沒有 id 時，用它所在 section／article 的 id（前提是它是那一區的第一個標題）。
 */
(function () {
  var LABEL = { zh: '這一區的連結（點了會複製）', en: 'Link to this section (click to copy)', ja: 'このセクションへのリンク（クリックでコピー）' };
  var DONE = { zh: '已複製連結', en: 'Link copied', ja: 'リンクをコピーしました' };

  function lang() {
    var c = document.body.className;
    return /\blang-zh\b/.test(c) ? 'zh' : /\blang-ja\b/.test(c) ? 'ja' : 'en';
  }

  function targetId(h) {
    if (h.id) return h.id;
    var box = h.parentElement && h.parentElement.closest('section[id], article[id]');
    if (box && box.querySelector('h1, h2, h3, .grp') === h) return box.id;
    return null;
  }

  var css = document.createElement('style');
  css.textContent =
    '.hash-link{margin-left:.4em;padding:0 .15em;color:#94a3b8;font-weight:600;text-decoration:none;' +
    'opacity:.35;transition:opacity .15s,color .15s;white-space:nowrap}' +
    '.hash-link:hover,.hash-link:focus-visible{opacity:1;color:#18a058;outline:none}' +
    'h2:hover>.hash-link,h3:hover>.hash-link,.grp:hover>.hash-link{opacity:1}' +
    '.hash-link.copied{opacity:1;color:#18a058}' +
    /* 頂列是 sticky：捲過去時標題不要被它蓋住 */
    'section[id],article[id],h2[id],h3[id],.grp[id]{scroll-margin-top:80px}' +
    '@media(max-width:560px){section[id],article[id],h2[id],h3[id],.grp[id]{scroll-margin-top:124px}}';
  document.head.appendChild(css);

  function setup() {
    var heads = document.querySelectorAll('h2, h3, .grp');
    for (var i = 0; i < heads.length; i++) {
      var h = heads[i];
      var id = targetId(h);
      if (!id || h.querySelector(':scope > .hash-link')) continue;
      var a = document.createElement('a');
      a.className = 'hash-link';
      a.href = '#' + id;
      a.textContent = '#';
      a.setAttribute('aria-label', LABEL.en);
      a.addEventListener('mouseenter', function () { this.title = LABEL[lang()]; this.setAttribute('aria-label', LABEL[lang()]); });
      a.addEventListener('click', function () {
        var link = this;
        var url = location.href.split('#')[0] + link.getAttribute('href');
        try {
          navigator.clipboard.writeText(url).then(function () {
            link.classList.add('copied');
            link.title = DONE[lang()];
            setTimeout(function () { link.classList.remove('copied'); link.title = LABEL[lang()]; }, 1500);
          }, function () {});
        } catch (e) { /* 沒有剪貼簿權限就只換網址 */ }
      });
      h.appendChild(a);
    }
  }

  // 打開帶 #id 的網址時：頁面設了平滑捲動，從頂端一路滑下去要一兩秒；圖片晚一點才載完
  // 又會把版面往下推 → 載完後直接（不滑動）對準一次
  function realign() {
    var id = decodeURIComponent(location.hash.slice(1));
    var el = id && document.getElementById(id);
    if (el) el.scrollIntoView({ block: 'start', behavior: 'instant' });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup);
  else setup();
  window.addEventListener('load', function () { if (location.hash) setTimeout(realign, 0); });
})();
