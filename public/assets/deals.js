
(function () {
  "use strict";
  var grid = document.getElementById("grid");
  if (!grid) return; // このUIを持たないページ（トップページ等）では何もしない

  // スマホ幅では初回だけ絞り込み行を畳んでおく（以降はユーザーの開閉操作を尊重する）
  var filterDetails = document.querySelector(".filter-details");
  if (filterDetails && window.innerWidth < 720) {
    filterDetails.removeAttribute("open");
  }

  var cards = Array.prototype.slice.call(grid.querySelectorAll(".row"));
  var qInput = document.getElementById("q");
  var sortSel = document.getElementById("f-sort");
  var cutSel = document.getElementById("f-cut");
  var priceSel = document.getElementById("f-price");
  var reviewsSel = document.getElementById("f-reviews");
  var genreSel = document.getElementById("f-genre");
  var jpChk = document.getElementById("f-jp");
  var onsaleChk = document.getElementById("f-onsale");
  var resultCount = document.getElementById("result-count");
  var emptyMsg = document.getElementById("empty");
  var loadMoreBtn = document.getElementById("load-more");
  var resetBtn = document.getElementById("reset-filters");
  var resetBtnEmpty = document.getElementById("reset-filters-empty");

  var PAGE_SIZE = 24;
  var page = 1;

  var SORTERS = {
    cut_desc: function (a, b) { return (+b.dataset.cut) - (+a.dataset.cut); },
    price_asc: function (a, b) { return (+a.dataset.price) - (+b.dataset.price); },
    reviews_desc: function (a, b) { return (+b.dataset.reviews) - (+a.dataset.reviews); },
    verdict: function (a, b) { return (+a.dataset.verdictRank) - (+b.dataset.verdictRank); },
    expiry: function (a, b) { return (+a.dataset.expiryDays) - (+b.dataset.expiryDays); },
  };

  function priceInBucket(price, bucket) {
    if (!bucket) return true;
    var parts = bucket.split("-");
    var lo = parts[0], hi = parts[1];
    if (lo && price < +lo) return false;
    if (hi && price > +hi) return false;
    return true;
  }

  function parseParams() {
    var p = new URLSearchParams(location.search);
    if (p.has("q")) qInput.value = p.get("q");
    if (p.has("sort")) sortSel.value = p.get("sort");
    if (p.has("cut")) cutSel.value = p.get("cut");
    if (p.has("price")) priceSel.value = p.get("price");
    if (p.has("reviews")) reviewsSel.value = p.get("reviews");
    if (p.has("genre")) genreSel.value = p.get("genre");
    if (p.has("jp")) jpChk.checked = p.get("jp") === "1";
    if (p.has("onsale")) onsaleChk.checked = p.get("onsale") === "1";
  }

  function syncUrl() {
    var p = new URLSearchParams();
    if (qInput.value) p.set("q", qInput.value);
    if (sortSel.value !== "cut_desc") p.set("sort", sortSel.value);
    if (cutSel.value !== "0") p.set("cut", cutSel.value);
    if (priceSel.value) p.set("price", priceSel.value);
    if (reviewsSel.value !== "0") p.set("reviews", reviewsSel.value);
    if (genreSel.value) p.set("genre", genreSel.value);
    if (jpChk.checked) p.set("jp", "1");
    if (onsaleChk.checked) p.set("onsale", "1");
    var qs = p.toString();
    history.replaceState(null, "", qs ? ("?" + qs) : location.pathname);
  }

  function render() {
    var q = qInput.value.trim().toLowerCase();
    var minCut = +cutSel.value || 0;
    var bucket = priceSel.value;
    var minReviews = +reviewsSel.value || 0;
    var genre = genreSel.value;
    var jpOnly = jpChk.checked;
    var onsaleOnly = onsaleChk.checked;

    var matched = cards.filter(function (c) {
      if (q && c.dataset.title.indexOf(q) === -1) return false;
      if (minCut && (+c.dataset.cut) < minCut) return false;
      if (bucket && !priceInBucket(+c.dataset.price, bucket)) return false;
      if (minReviews && (+c.dataset.reviews) < minReviews) return false;
      if (genre && (c.dataset.genres || "").split(",").indexOf(genre) === -1) return false;
      if (jpOnly && c.dataset.jp !== "1") return false;
      if (onsaleOnly && c.dataset.onsale !== "1") return false;
      return true;
    });

    matched.sort(SORTERS[sortSel.value] || SORTERS.cut_desc);

    cards.forEach(function (c) { c.hidden = true; });
    var visibleCount = Math.min(matched.length, page * PAGE_SIZE);
    for (var i = 0; i < visibleCount; i++) {
      matched[i].hidden = false;
      grid.appendChild(matched[i]);
    }

    resultCount.textContent = matched.length + "件ヒット" +
      (matched.length > visibleCount ? "（" + visibleCount + "件表示中）" : "");
    emptyMsg.hidden = matched.length !== 0;
    loadMoreBtn.hidden = matched.length <= visibleCount;

    syncUrl();
  }

  function onFilterChange() { page = 1; render(); }

  var debounceId;
  qInput.addEventListener("input", function () {
    clearTimeout(debounceId);
    debounceId = setTimeout(onFilterChange, 200);
  });
  [sortSel, cutSel, priceSel, reviewsSel, genreSel, jpChk, onsaleChk].forEach(function (el) {
    el.addEventListener("change", onFilterChange);
  });
  loadMoreBtn.addEventListener("click", function () { page += 1; render(); });

  function resetAll() {
    qInput.value = ""; sortSel.value = "cut_desc"; cutSel.value = "0";
    priceSel.value = ""; reviewsSel.value = "0"; genreSel.value = "";
    jpChk.checked = false; onsaleChk.checked = false;
    onFilterChange();
  }
  if (resetBtn) resetBtn.addEventListener("click", resetAll);
  if (resetBtnEmpty) resetBtnEmpty.addEventListener("click", resetAll);

  parseParams();
  render();
})();
