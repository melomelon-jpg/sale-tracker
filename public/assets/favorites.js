
(function () {
  "use strict";
  var KEY = "sale-tracker:favorites";

  function readAll() {
    try {
      var raw = localStorage.getItem(KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (e) {
      return [];
    }
  }

  function writeAll(arr) {
    try { localStorage.setItem(KEY, JSON.stringify(arr)); } catch (e) { /* ignore */ }
  }

  function has(slug) { return readAll().indexOf(slug) !== -1; }

  function toggle(slug) {
    var arr = readAll();
    var i = arr.indexOf(slug);
    if (i === -1) { arr.push(slug); } else { arr.splice(i, 1); }
    writeAll(arr);
    document.dispatchEvent(new CustomEvent("favorites:change", { detail: { slug: slug } }));
    return i === -1; // true なら追加された
  }

  window.SaleTrackerFavorites = { has: has, toggle: toggle, getAll: readAll };

  function syncButton(btn) {
    var slug = btn.getAttribute("data-fav-slug");
    var on = has(slug);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.classList.toggle("is-fav", on);
    btn.title = on ? "お気に入りから削除" : "お気に入りに追加";
    btn.setAttribute("aria-label", btn.title);
  }

  function init() {
    var buttons = document.querySelectorAll("[data-fav-slug]");
    buttons.forEach(function (btn) {
      syncButton(btn);
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        var added = toggle(btn.getAttribute("data-fav-slug"));
        syncButton(btn);
        if (added) {
          // お気に入り追加時だけ弾むアニメーションを再生する
          btn.classList.remove("fav-pop");
          void btn.offsetWidth; // reflow でアニメーションを再始動させる
          btn.classList.add("fav-pop");
        }
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
