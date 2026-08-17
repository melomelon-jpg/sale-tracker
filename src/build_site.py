# -*- coding: utf-8 -*-
"""②サイト生成: data/ の保存済みJSONから public/ に静的HTMLを書き出す。

APIは一切叩かない。fetch_data.py が吐いた data/ だけを読む。
テンプレートは依存を増やさないため Python の文字列で組む。
価格履歴グラフは外部JS不要のインラインSVGで描く。

実行:
    python src/build_site.py
出力:
    public/index.html
    public/games/<slug>.html
    public/assets/style.css
"""
import functools
import html
import json
import math
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verdict  # noqa: E402  買い時判定の純粋関数（表示時に再計算して最新ルールを反映）
import sale_history  # noqa: E402  セール履歴の傾向（事実ベース、予測はしない）純粋関数

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
PUBLIC_DIR = ROOT / "public"
ASSETS_SRC = ROOT / "assets"  # 手書きCSSがあればここから、無ければ内蔵を使う
CONFIG_PATH = ROOT / "config" / "games.json"

# 「注目のセール」の足切り・スコアリング（無名の低品質ゲームを除外するため）。
# config/games.json の "featured" セクションで調整できる。
_FEATURED_DEFAULTS = {"min_reviews": 500, "min_price_jpy": 500, "popularity_weight": 15}


def load_featured_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("featured") or {}
    except Exception:
        cfg = {}
    return {**_FEATURED_DEFAULTS, **cfg}


STEAM_SALES_PATH = ROOT / "config" / "steam_sales.json"


def load_steam_sales():
    try:
        return json.loads(STEAM_SALES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def steam_sales_calendar_html(base=None):
    """Steam大型セールカレンダー。config/steam_sales.json（Valve公式アナウンスを
    手動転記したもの。取得APIが存在しないため自動化はしていない）を基に、
    開催中/今後の予定だけを表示する。終了済みの回は「今どうすべきか」という
    サイトの目的に合わないため一覧から外す。
    """
    data = load_steam_sales()
    sales = data.get("sales") or []
    today = base or date.today()
    items = []
    for s in sales:
        try:
            start = datetime.strptime(s["start"], "%Y-%m-%d").date()
            end = datetime.strptime(s["end"], "%Y-%m-%d").date()
        except Exception:
            continue
        if end < today:
            continue
        if start <= today <= end:
            status_html = '<span class="sales-cal-status is-live">開催中</span>'
            cls = "sales-cal-item is-live"
        else:
            days = (start - today).days
            status_html = f'<span class="sales-cal-status">あと{days}日</span>'
            cls = "sales-cal-item"
        date_txt = f"{start.month}月{start.day}日 〜 {end.month}月{end.day}日"
        items.append((start, f"""
    <li class="{cls}">
      <span class="sales-cal-name">{esc(s.get("name"))}</span>
      <span class="sales-cal-dates">{esc(date_txt)}</span>
      {status_html}
    </li>"""))
    if not items:
        return ""
    items.sort(key=lambda t: t[0])
    rows = "".join(row_html for _, row_html in items)
    updated_note = data.get("updated_at")
    note = f'（{esc(updated_note)}時点の情報）' if updated_note else ""
    return f"""
<h2 id="calendar">Steam大型セールカレンダー</h2>
<p class="meta">Valve公式アナウンスに基づく予定です。日程は変更される場合があります{note}。</p>
<ul class="sales-cal-list">{rows}</ul>"""


FEATURED_CFG = load_featured_config()


def popularity_score(review_count, discount_pct):
    """割引率(0-100目安) + 人気度(レビュー数の対数)を合成したスコア。

    レビュー数は桁が大きく開く（数百〜数百万）ため対数を取り、
    config の popularity_weight で割引率と釣り合う大きさに調整する。
    """
    reviews = review_count or 0
    pop = math.log10(reviews + 1) * FEATURED_CFG["popularity_weight"]
    return (discount_pct or 0) + pop


def is_featured_eligible(g):
    """「注目のセール」に出してよいか（無名・低額の掃き溜め対策）。"""
    cur_amt = (g.get("current") or {}).get("amount")
    reviews = g.get("review_count")
    if not g.get("on_sale"):
        return False
    if reviews is None or reviews < FEATURED_CFG["min_reviews"]:
        return False
    if cur_amt is None or cur_amt < FEATURED_CFG["min_price_jpy"]:
        return False
    return True

SITE_NAME = "ゲーム最安隊"
# 独自ドメインに移行する際はここだけ書き換えればよい（OGP/canonical/sitemap.xmlで使用）。
SITE_URL = "https://sale-tracker-368.pages.dev"
SITE_DESCRIPTION = (
    "Steamのセール情報を毎日自動収集し、過去最安値と比較した「買い時」判定バッジで"
    "今狙うべきセールが一目でわかる非公式の価格追跡サイト。"
)
# ストアはすべてSteamに統一されているため、行ごとにバッジを繰り返さず一覧の先頭で一度だけ明記する。
STEAM_NOTE = '<p class="meta">価格はすべて Steam ストアの表示です。</p>'

# 判定コード -> CSSクラス（色分け用）。過去最安に到達している2段階（new_low/tied_low）
# だけを緑にし、それ以外（near_low/above_low/unknown）は中立グレーにする
# （色は意味のためだけに使うルール。赤は緊急性表示専用）。
VERDICT_CLASS = {
    "new_low": "v-record",
    "tied_low": "v-record",
    "near_low": "v-near",
    "above_low": "v-watch",
    "unknown": "v-unknown",
}

# ストアバッジの配色（各社ブランドカラーに近い色。ロゴ画像は使わずテキストバッジのみ）。
# 未知のストア（掲載していないキーショップ等）は _STORE_DEFAULT の中立グレーにフォールバック
# するので、ここに無いストアが増えても表示が壊れることはない。
STORE_STYLES = {
    "Steam":             {"bg": "#1b2838", "fg": "#66c0f4", "border": "#2a475e"},
    "GOG":                {"bg": "#2b1f3d", "fg": "#c592ff", "border": "#5b3b8c"},
    "Epic Game Store":   {"bg": "#0d0d0d", "fg": "#f3f3f3", "border": "#3a3a3a"},
    "Fanatical":          {"bg": "#3a2410", "fg": "#ffab3d", "border": "#8a5a1e"},
    "Humble Store":      {"bg": "#3a1414", "fg": "#ff8d7d", "border": "#8a3a30"},
    "GreenManGaming":    {"bg": "#12331e", "fg": "#6fd88a", "border": "#2e6b45"},
    "Microsoft Store":   {"bg": "#0f2e14", "fg": "#7fd06a", "border": "#2e6b32"},
    "Ubisoft Store":     {"bg": "#0e2338", "fg": "#7fb8f0", "border": "#2a5788"},
}
_STORE_DEFAULT = {"bg": "#20262f", "fg": "#a7b1c2", "border": "#39414f"}


def store_badge(shop):
    """ストア名を色分けバッジで返す。未指定なら空文字。"""
    if not shop:
        return ""
    style = STORE_STYLES.get(shop, _STORE_DEFAULT)
    return (f'<span class="store-badge" style="background:{style["bg"]};'
            f'color:{style["fg"]};border-color:{style["border"]}">{esc(shop)}</span>')


def other_store_html(g):
    """Steamより安い他ストアがあるときだけ、控えめな補足行を返す（主役はSteam価格）。"""
    other = g.get("other_store") or {}
    amt = other.get("amount")
    if amt is None:
        return ""
    return f'<div class="row-other-store">他ストア最安 {yen(amt)}（{esc(other.get("shop") or "")}）</div>'


# ジャンル別ページ（/genre/<slug>.html）用の日本語→URLスラッグ対応表。
# 対象ジャンルは現状データで観測される11種のみだが、将来増えても _genre_slug() が
# 英数字以外を除去した簡易スラッグにフォールバックするため生成自体は壊れない。
GENRE_SLUGS = {
    "アクション": "action",
    "アドベンチャー": "adventure",
    "インディー": "indie",
    "RPG": "rpg",
    "シミュレーション": "simulation",
    "ストラテジー": "strategy",
    "カジュアル": "casual",
    "レース": "racing",
    "スポーツ": "sports",
    "早期アクセス": "early-access",
    "MM（Massively Multiplayer）": "mmo",
}


@functools.lru_cache(maxsize=None)
def _genre_slug(genre):
    slug = GENRE_SLUGS.get(genre)
    if slug:
        return slug
    fallback = "".join(c.lower() if c.isascii() and c.isalnum() else "-" for c in genre).strip("-")
    return fallback or "genre"


def genre_href(genre, prefix=""):
    return f"{prefix}genre/{_genre_slug(genre)}.html"


def genre_tag_links(genres, prefix="", limit=None):
    names = genres[:limit] if limit else genres
    return "".join(
        f'<a class="tag-genre" href="{esc(genre_href(g, prefix))}">{esc(g)}</a>' for g in names
    )


@functools.lru_cache(maxsize=None)
def load_history(slug):
    hist_path = HISTORY_DIR / f"{slug}.json"
    if not hist_path.exists():
        return []
    try:
        return json.loads(hist_path.read_text(encoding="utf-8")).get("history", [])
    except Exception:
        return []


def tied_low_verdict_text(slug, low_amount):
    """「過去最安値と同じ価格」(tied_low) バッジの一言。過去に何回この価格へ
    到達したかで「珍しいのか、毎回来る価格なのか」を伝える。

    tied_low は「過去最安と同額」であり定義上すでに1回以上の到達実績がある
    （＝それ自体が"初めて"ではあり得ない）。過去の到達回数は実データで強い
    分散が確認できた軸（例: 179件中47件が初到達、一方で20回以上到達した
    例もある）ので、それをそのままバッジの根拠として言語化する。
    new_low（真の更新）はバッジ自体が「更新」を明言しているため、
    この関数は呼ばない（重複説明をしない）。
    観測点が3点未満（追跡を始めたばかり等）は判断材料が薄いため何も言わない。
    """
    pts = [h for h in load_history(slug) if h.get("amount") is not None]
    if len(pts) < 3 or low_amount is None:
        return ""
    times = sum(1 for h in pts if h["amount"] <= low_amount + 0.01)
    if times <= 1:
        return ""
    return f"この価格になるのは{times}回目"


def esc(s):
    return html.escape(str(s if s is not None else ""))


def display_title(g):
    """表示用タイトル。Steam(cc=jp)の日本語名キャッシュがあればそれを、無ければ英語名を使う。"""
    return g.get("title_jp") or g.get("title") or ""


def yen(amount):
    if amount is None:
        return "—"
    return f"¥{amount:,.0f}"


# 見出し/バッジの装飾は絵文字をやめ、currentColor で色を継承するインラインSVGに統一する
# （個人の実験ではなく「ちゃんとしたサービス」に見せるため）。
ICON_TAG = ('<svg class="brand-icon" viewBox="0 0 24 24" width="20" height="20" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            'aria-hidden="true"><path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 '
            '2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 '
            '0-3.42Z"/><circle cx="7.5" cy="7.5" r=".6" fill="currentColor"/></svg>')

ICON_CLOCK = ('<svg class="icon-clock" viewBox="0 0 24 24" width="13" height="13" fill="none" '
              'stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" '
              'aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>')

ICON_CHEVRON_UP = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
                    'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                    '<polyline points="18 15 12 9 6 15"/></svg>')
ICON_CHEVRON_LEFT = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
                      'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                      '<polyline points="15 18 9 12 15 6"/></svg>')
ICON_CHEVRON_RIGHT = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
                       'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                       '<polyline points="9 6 15 12 9 18"/></svg>')

# テーマ切替ボタンのアイコン。data-theme に応じてCSS側で片方だけ表示する。
ICON_SUN = ('<svg class="icon-sun" viewBox="0 0 24 24" width="17" height="17" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            'aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41'
            'M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>')
ICON_MOON = ('<svg class="icon-moon" viewBox="0 0 24 24" width="17" height="17" fill="none" '
             'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
             'aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/></svg>')


JST = timezone(timedelta(hours=9))


def fmt_dt(iso):
    """ISO文字列（UTC）を日本時間の 'YYYY年M月D日 HH:MM（日本時間）' に整形。失敗時はそのまま返す。"""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST)
        # %-m はWindowsで使えないため手組みで整形する
        return f"{dt.year}年{dt.month}月{dt.day}日 {dt.hour:02d}:{dt.minute:02d}（日本時間）"
    except Exception:
        return iso


def fmt_date_jp(date_str):
    """'2025-07-01' を '2025年7月1日' に整形。失敗時は元の文字列（Noneは空）。"""
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        return f"{d.year}年{d.month}月{d.day}日"
    except Exception:
        return date_str or ""


def relative_date_jp(date_str, base=None):
    """'2025-07-01' を今日基準の相対表現（例: '1年前' '先月' '3日前'）にする。

    「2年前の最安」と「先月の最安」を一目で区別できるようにするための表示用。
    失敗時は空文字を返す。
    """
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except Exception:
        return ""
    today = base or date.today()
    days = (today - d).days
    if days < 0:
        return ""
    if days == 0:
        return "今日"
    if days < 7:
        return f"{days}日前"
    if days < 30:
        return f"{days // 7}週間前"
    if days < 60:
        return "先月"
    if days < 365:
        return f"{days // 30}か月前"
    return f"{days // 365}年前"


def buy_timing_text(current_amount, lowest_amount, code=None):
    """現在価格と過去最安値の差を金額ベースの一言で表す（near_low/above_low バッジの補足）。

    現在価格が過去最安値以下（= new_low/tied_low バッジ）のときは、バッジの文言と
    意味が重複するためここでは何も返さない（呼び出し側もバッジと二重表示しない）。
    near_low（最安値に近い）は「あと◯円」、above_low（最安値より高い）は
    「過去最安より◯円高い」と、判定と矛盾しない言い方に分ける。
    """
    if current_amount is None or lowest_amount is None:
        return ""
    diff = current_amount - lowest_amount
    if diff <= 0:
        return ""
    if code == "near_low":
        return f"あと {yen(diff)}"
    return f"過去最安より {yen(diff)} 高い"


def page(title, body, rel_root=".", active="", path="", description=None,
         og_title=None, og_image=None, twitter_card="summary_large_image"):
    """共通HTMLシェル。

    rel_root: assets等への相対パス。 active: 現在ページのnavハイライト。
    path:     サイトルートからの相対パス（例 "games/elden-ring.html"、トップは ""）。
              canonical/og:url/sitemap用の絶対URL組み立てに使う。
    description/og_title/og_image: 未指定ならサイト共通のデフォルトにフォールバックする。
    """
    nav_featured = "active" if active == "featured" else ""
    nav_all = "active" if active == "all" else ""
    nav_about = "active" if active == "about" else ""
    full_title = f"{SITE_NAME}｜{esc(title)}" if active == "featured" else f"{esc(title)}｜{SITE_NAME}"
    # all.htmlは検索欄自体を持つため、ヘッダー検索は他ページにのみ出す（二重にしない）
    header_search = "" if active == "all" else f"""
    <form class="site-search" action="{rel_root}/all.html" method="get" role="search">
      <label class="sr-only" for="header-q">ゲーム名で検索</label>
      <input type="search" id="header-q" name="q" placeholder="ゲーム名で検索…" autocomplete="off" aria-label="ゲーム名で検索">
    </form>"""

    canonical_url = f"{SITE_URL}/{path}" if path else f"{SITE_URL}/"
    desc = description or SITE_DESCRIPTION
    og_t = og_title or full_title
    og_img = og_image or f"{SITE_URL}/og-default.png"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#ffffff" id="theme-color-meta">
<script>
// FOUC防止: CSSが読み込まれる前に保存済みテーマを <html> に反映する。
// 未保存（初回訪問）なら何もせず、CSS側の prefers-color-scheme に委ねる
// （ライトを既定値としつつOSのダーク設定も尊重するため）。
(function () {{
  try {{
    var saved = localStorage.getItem("sale-tracker:theme");
    if (saved === "light" || saved === "dark") {{
      document.documentElement.setAttribute("data-theme", saved);
      var m = document.getElementById("theme-color-meta");
      if (m) m.setAttribute("content", saved === "dark" ? "#0d1014" : "#ffffff");
    }}
  }} catch (e) {{ /* ignore */ }}
}})();
</script>
<title>{full_title}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{esc(canonical_url)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{esc(SITE_NAME)}">
<meta property="og:locale" content="ja_JP">
<meta property="og:title" content="{esc(og_t)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{esc(canonical_url)}">
<meta property="og:image" content="{esc(og_img)}">
<meta name="twitter:card" content="{esc(twitter_card)}">
<meta name="twitter:title" content="{esc(og_t)}">
<meta name="twitter:description" content="{esc(desc)}">
<meta name="twitter:image" content="{esc(og_img)}">
<link rel="icon" href="{rel_root}/favicon.svg" type="image/svg+xml">
<link rel="icon" href="{rel_root}/favicon.png" sizes="48x48" type="image/png">
<link rel="apple-touch-icon" href="{rel_root}/apple-touch-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=Noto+Sans+JP:wght@400;500;700;900&display=swap">
<link rel="stylesheet" href="{rel_root}/assets/style.css">
</head>
<body>
<header class="site-header">
  <div class="header-inner">
    <a class="brand" href="{rel_root}/index.html">{ICON_TAG} <span>{esc(SITE_NAME)}</span></a>
    {header_search}
    <nav class="site-nav">
      <a href="{rel_root}/index.html" class="{nav_featured}">注目のセール</a>
      <a href="{rel_root}/index.html#popular">人気</a>
      <a href="{rel_root}/index.html#discount">値下げ率</a>
      <a href="{rel_root}/index.html#ending">まもなく終了</a>
      <a href="{rel_root}/all.html" class="{nav_all}">すべてのセール</a>
      <a href="{rel_root}/about.html" class="{nav_about}">このサイトについて</a>
    </nav>
    <button type="button" id="theme-toggle" class="theme-toggle" aria-pressed="false" aria-label="ダークモードに切り替え">
      {ICON_SUN}{ICON_MOON}
    </button>
  </div>
</header>
<main class="container">
{body}
</main>
<footer class="site-footer">
  <p>価格データ提供: <a href="https://isthereanydeal.com/" target="_blank" rel="noopener noreferrer">IsThereAnyDeal</a>（Steamほか各ストアの価格情報を集約）</p>
  <p>本サイトはValve/Steamおよび各ストアの公式サイトではありません。掲載価格は保存済みデータのため、購入前に必ずストア側の価格をご確認ください。</p>
  <p>毎朝6時ごろ自動更新 ・ <a href="{rel_root}/about.html">このサイトについて</a></p>
</footer>
<button type="button" id="back-to-top" class="back-to-top" aria-label="ページの先頭に戻る">{ICON_CHEVRON_UP}</button>
<!-- Cloudflare Web Analytics --><script type='module' src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{{"token": "c8c81e274f13457d80d3e8484503fdf2"}}'></script><!-- End Cloudflare Web Analytics -->
<script src="{rel_root}/assets/favorites.js" defer></script>
<script src="{rel_root}/assets/deals.js" defer></script>
<script>
(function () {{
  "use strict";
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // --- テーマ切替: クリックのたびにライト/ダークをトグルし、localStorageに保存する。
  //     初回訪問（保存なし）はOSの現在の実効テーマを起点に反転させる。 ---
  var themeBtn = document.getElementById("theme-toggle");
  var themeMeta = document.getElementById("theme-color-meta");
  var THEME_KEY = "sale-tracker:theme";
  function effectiveTheme() {{
    var saved = null;
    try {{ saved = localStorage.getItem(THEME_KEY); }} catch (e) {{ /* ignore */ }}
    if (saved === "light" || saved === "dark") return saved;
    return (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  }}
  function applyTheme(theme) {{
    document.documentElement.setAttribute("data-theme", theme);
    if (themeMeta) themeMeta.setAttribute("content", theme === "dark" ? "#0d1014" : "#ffffff");
    if (themeBtn) themeBtn.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
  }}
  if (themeBtn) {{
    themeBtn.addEventListener("click", function () {{
      var next = effectiveTheme() === "dark" ? "light" : "dark";
      try {{ localStorage.setItem(THEME_KEY, next); }} catch (e) {{ /* ignore */ }}
      applyTheme(next);
    }});
    applyTheme(effectiveTheme());
  }}

  // --- 固定ヘッダーの実測高さをCSS変数に反映（フィルタバーのtop位置・アンカーの
  //     scroll-margin-topが正しく効くようにするため。検索欄の折り返しで高さが
  //     変わるスマホでも常に正しい値を保つ） ---
  var header = document.querySelector(".site-header");
  function setHeaderHeight() {{
    if (header) document.documentElement.style.setProperty("--header-h", header.offsetHeight + "px");
  }}
  setHeaderHeight();
  window.addEventListener("resize", setHeaderHeight);
  window.addEventListener("orientationchange", setHeaderHeight);
  // Webフォント読み込み完了でヘッダーの実高さが微妙に変わることがあるため再計測
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(setHeaderHeight);

  // --- トップへ戻るボタン ---
  var btn = document.getElementById("back-to-top");
  if (btn) {{
    function onScroll() {{ btn.classList.toggle("show", window.scrollY > 400); }}
    window.addEventListener("scroll", onScroll, {{ passive: true }});
    btn.addEventListener("click", function () {{
      window.scrollTo({{ top: 0, behavior: reduceMotion ? "auto" : "smooth" }});
    }});
    onScroll();
  }}

  // --- 横スクロールカルーセル（本日イチ押し等）の矢印ボタン。ネイティブの
  //     overflow-x scrollで動くのでJSはボタン操作の補助のみ（スマホのスワイプは
  //     このJS無しでも動く）。ページに無ければ何もしない。 ---
  Array.prototype.slice.call(document.querySelectorAll(".carousel-wrap")).forEach(function (wrap) {{
    var track = wrap.querySelector(".carousel-track");
    var prev = wrap.querySelector(".carousel-arrow.prev");
    var next = wrap.querySelector(".carousel-arrow.next");
    if (!track || !prev || !next) return;
    function step() {{
      var card = track.querySelector(":scope > *");
      return card ? card.getBoundingClientRect().width + 14 : track.clientWidth * 0.9;
    }}
    function updateArrows() {{
      prev.disabled = track.scrollLeft <= 4;
      next.disabled = track.scrollLeft >= track.scrollWidth - track.clientWidth - 4;
    }}
    prev.addEventListener("click", function () {{
      track.scrollBy({{ left: -step(), behavior: reduceMotion ? "auto" : "smooth" }});
    }});
    next.addEventListener("click", function () {{
      track.scrollBy({{ left: step(), behavior: reduceMotion ? "auto" : "smooth" }});
    }});
    track.addEventListener("scroll", function () {{
      if (!wrap._ticking) {{
        wrap._ticking = true;
        window.requestAnimationFrame(function () {{ updateArrows(); wrap._ticking = false; }});
      }}
    }}, {{ passive: true }});
    updateArrows();
  }});

  // --- 期間切り替え（1年/全期間）: ページ内に複数あってもよいよう、ボタン群ごとに
  //     data-period-target で対応する表示ブロックを出し分ける。 ---
  Array.prototype.slice.call(document.querySelectorAll("[data-period-buttons]")).forEach(function (group) {{
    var buttons = Array.prototype.slice.call(group.querySelectorAll("[data-period]"));
    buttons.forEach(function (btn) {{
      btn.addEventListener("click", function () {{
        var period = btn.getAttribute("data-period");
        buttons.forEach(function (b) {{ b.setAttribute("aria-pressed", b === btn ? "true" : "false"); }});
        var targets = document.querySelectorAll("[data-period-panel='" + group.getAttribute("data-period-buttons") + "']");
        Array.prototype.slice.call(targets).forEach(function (panel) {{
          panel.hidden = panel.getAttribute("data-period") !== period;
        }});
        scrollChartsToLatest();
      }});
    }});
  }});

  // --- 価格グラフは観測点が多いほど実ピクセル幅を広げて横スクロール可にしているため
  //     （.chart-scroll）、初期表示は左端（最古）ではなく右端（現在価格）を見せる。
  //     「今が買い時か」の判断に要る情報を最初のスクロール操作なしで見せるため。 ---
  function scrollChartsToLatest() {{
    Array.prototype.slice.call(document.querySelectorAll(".chart-scroll")).forEach(function (el) {{
      if (!el.offsetParent) return; // hidden panel
      el.scrollLeft = el.scrollWidth;
    }});
  }}
  scrollChartsToLatest();

  // --- スクロールスパイ: 表示中のセクションに応じてナビの強調表示を切り替える ---
  var navLinks = Array.prototype.slice.call(document.querySelectorAll(".site-nav a"));
  var topLink = document.querySelector(".site-nav a.active");
  var sectionMap = [];
  navLinks.forEach(function (a) {{
    var hash = (a.getAttribute("href") || "").split("#")[1];
    if (!hash) return;
    var el = document.getElementById(hash);
    if (el) sectionMap.push({{ link: a, el: el }});
  }});
  if (sectionMap.length && topLink) {{
    var ticking = false;
    function updateSpy() {{
      ticking = false;
      var headerH = header ? header.offsetHeight : 0;
      var offset = headerH + 24;
      var current = null;
      sectionMap.forEach(function (s) {{
        if (s.el.getBoundingClientRect().top <= offset) current = s;
      }});
      navLinks.forEach(function (a) {{ a.classList.remove("active"); }});
      (current ? current.link : topLink).classList.add("active");
    }}
    window.addEventListener("scroll", function () {{
      if (!ticking) {{ window.requestAnimationFrame(updateSpy); ticking = true; }}
    }}, {{ passive: true }});
    updateSpy();
  }}
}})();
</script>
</body>
</html>
"""


def verdict_desc(code):
    """判定バッジの根拠（過去最安との差のしきい値）を説明する短文。ツールチップ/凡例で共用。"""
    near_pct = int(verdict.NEAR_LOW_MAX_GAP * 100)
    texts = {
        "new_low": "現在価格が過去のどの記録よりも安い（過去最安値を更新）",
        "tied_low": "現在価格が過去最安値とちょうど同額",
        "near_low": f"過去最安値との差が{near_pct}%以内",
        "above_low": f"過去最安値より{near_pct}%を超えて高い（それ以上の優劣は付けない中立表示）",
        "unknown": "価格データ不足のため判定できません",
    }
    return texts.get(code, "")


def verdict_badge(v):
    cls = VERDICT_CLASS.get(v.get("code"), "v-unknown")
    desc = verdict_desc(v.get("code"))
    return f'<span class="badge {cls}" title="{esc(desc)}">{esc(v.get("label"))}</span>'


def jp_mark(jp):
    """日本語対応の表示。True のときだけ🇯🇵バッジを出す。

    False は「Steam公式の対応言語一覧に日本語が見つからなかった」に過ぎず、
    非公式Fan翻訳や表記ゆれで実際には対応しているケースもあり「日本語なし」と
    断定できない。誤った断定を避けるため、False/None(不明)はどちらも何も表示しない。
    """
    if jp is True:
        return '<span class="jp" title="日本語対応">🇯🇵 日本語</span>'
    return ""


def best_asset_url(assets, sizes):
    """assets から指定サイズ優先順で最初に見つかったURLを返す。無ければ空文字。"""
    if isinstance(assets, dict):
        for s in sizes:
            if assets.get(s):
                return assets[s]
    return ""


def game_image(assets, sizes, cls, alt, dims=None, lazy=True):
    """assets から最初に見つかったサイズの画像を <img> で返す。

    無ければ絵文字プレースホルダー（同じクラス）でレイアウト崩れを防ぐ。
    画像はITADのURLを直接参照する（自前保存はしない）。
    dims=(width, height) を渡すとwidth/height属性を付け、画像読み込み前でも
    ブラウザがアスペクト比を確保できるようにする（レイアウトシフト防止）。
    ITADのbanner系アセットは実寸约600x344（比率約1.74:1）で統一されている。

    lazy: True なら loading="lazy" を付ける。all.html の一覧はJSで大半の行を
          hidden にして段階表示するため lazy が有効（初期読み込みを絞れる）。
          一方、トップページの各セクションやジャンル別ページはJSで隠さず全行を
          常時DOMに描画するため、native lazy loading の実装差（低速回線での
          先読み距離が極端に短くなる等）でスクロール前に画像が空白のまま
          になる不具合が実機で確認された。そのためこれらは lazy=False にして
          常に即時読み込みする。
    """
    url = best_asset_url(assets, sizes)
    if url:
        size_attr = f'width="{dims[0]}" height="{dims[1]}" ' if dims else ""
        loading_attr = 'loading="lazy" ' if lazy else ""
        return (f'<img class="{cls}" src="{esc(url)}" alt="{esc(alt)}" {size_attr}'
                f'{loading_attr}referrerpolicy="no-referrer">')
    # 画像が無いときはタイトルを表示するプレースホルダー（何のゲームか分かるように）
    return (f'<div class="{cls} img-ph" role="img" aria-label="{esc(alt)}">'
            f'<span class="img-ph-icon">🎮</span><span class="img-ph-title">{esc(alt)}</span></div>')


def expiry_info(iso, base=None):
    """セール終了日時を表示用に整形。expiry が無ければ None。

    戻り値: {"date_full","date_short","days_left","urgent","sort_ts"} | None
    urgent は残り3日以内（当日含む）で True。
    sort_ts は時刻まで含む比較用のUNIX秒（同日中の複数終了が「本日終了」に
    まとめられて順序が失われないよう、日単位のdays_leftとは別に持つ）。
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return None
    d = dt.date()
    today = base or date.today()
    days = (d - today).days
    return {
        "date_full": f"{d.year}年{d.month}月{d.day}日",
        "date_short": f"{d.month}月{d.day}日",
        "days_left": days,
        "urgent": 0 <= days <= 3,
        "sort_ts": dt.timestamp(),
    }


def days_left_text(days):
    """残り日数の日本語表現。"""
    if days < 0:
        return "まもなく終了"
    if days == 0:
        return "本日終了"
    return f"あと{days}日"


# ---------------------------------------------------------------------------
# 判定の並び順（良い順）: 一覧の既定ソートやトップページの厳選に使う
# ---------------------------------------------------------------------------
VERDICT_ORDER = {"new_low": 0, "tied_low": 1, "near_low": 2, "above_low": 3, "unknown": 4}

FEATURED_COUNT = 30


# ---------------------------------------------------------------------------
# ゲームカード（トップページ・全件ページ共通）
# ---------------------------------------------------------------------------
def game_row(g, rank=None, link_prefix="", big_expiry=False, lazy=True):
    """1ゲーム分の一覧行HTML。data-* 属性は all.html の検索/並び替え/絞り込みJS用。

    Steamのセールチャートのような固定カラムのテーブル構造（順位・画像・タイトル・
    買い時・割引・価格）。全行で同じ位置に同じ種類の情報が来るよう、列の並びは
    game_row() が唯一の生成元となり、CSS側の .row グリッドと1対1で対応する。
    判定を一覧の主役にするため、過去最安に到達している行（new_low/tied_low）だけ
    左端のアクセントバーで強調する（行の明るさ自体は全行で揃え、above_low を沈める処理はしない）。

    rank: 指定するとSteamチャート風の順位数字を左端に表示する（ランキング専用）。
    link_prefix: ゲーム/ジャンルへのリンクの相対パス接頭辞（詳細ページ内の関連ゲーム欄など
                 サイトルート以外から呼ぶ場合は "../" を渡す）。
    big_expiry: 「まもなく終了」セクション用。残り日数を買い時列に大きく常時表示し、
                3日以内なら行の左端を警告色でハイライトする。
    """
    cur = g.get("current") or {}
    low = g.get("lowest") or {}
    reg = g.get("regular") or {}
    v = g["verdict"]
    cur_amt = cur.get("amount")
    low_amt = low.get("amount")
    reg_amt = reg.get("amount")
    disc = cur.get("discount_pct")
    on_sale = bool(g.get("on_sale"))
    title = display_title(g)
    thumb = game_image(g.get("assets"),
                        ["banner400", "banner300", "banner145", "boxart"],
                        "row-thumb-img", title, dims=(400, 230), lazy=lazy)

    exp = expiry_info(cur.get("expiry"))

    # 買い時列の中身: バッジ→（終了間近なら残り日数）→補足の一言、の順に積む。
    # tied_low は「珍しいのか毎回来る価格なのか」を過去の到達回数で、
    # near_low/above_low は過去最安との差額で、new_low はバッジ自体で完結する
    # ため何も足さない（バッジと意味が重ならないよう排他にする）。
    code = v.get("code")
    if code == "tied_low":
        timing_txt = tied_low_verdict_text(g["slug"], low_amt)
    elif code == "new_low":
        timing_txt = ""
    else:
        timing_txt = buy_timing_text(cur_amt, low_amt, code=code)
    # 緑は過去最安に到達している行（new_low/tied_low）だけに使う（色は意味だけに使うルール）
    timing_cls = "row-timing save" if code in ("new_low", "tied_low") else "row-timing"

    timing_bits = [verdict_badge(v)]
    if big_expiry and exp:
        cls = "tag-expiry big urgent" if exp["urgent"] else "tag-expiry big"
        timing_bits.append(f'<span class="{cls}">{ICON_CLOCK} '
                            f'{esc(days_left_text(exp["days_left"]))}</span>')
    elif exp and exp["urgent"]:
        timing_bits.append(f'<span class="tag-expiry urgent">{ICON_CLOCK} '
                            f'{esc(days_left_text(exp["days_left"]))}</span>')
    if timing_txt:
        timing_bits.append(f'<span class="{timing_cls}">{esc(timing_txt)}</span>')

    # 価格ブロック上段: 定価(取り消し線)＋割引バッジ。定価が現在価格より高いときだけ
    # 出す（同額/不明なら「セットで1つの情報」として何も足さない＝断定しない）。
    cut_txt = f"-{disc}%" if (on_sale and disc) else ""
    price_top_bits = []
    if on_sale and reg_amt is not None and cur_amt is not None and reg_amt > cur_amt:
        price_top_bits.append(f'<span class="row-regular"><span class="row-regular-label">定価</span>'
                               f'<span class="row-regular-price">{yen(reg_amt)}</span></span>')
    if cut_txt:
        price_top_bits.append(f'<span class="row-cut">{esc(cut_txt)}</span>')
    price_top_html = f'<div class="row-amount-top">{"".join(price_top_bits)}</div>' if price_top_bits else ""

    # JS用データ属性。値が不明なものは昇順ソートで末尾に回るよう大きな値にする。
    # ジャンル・レビュー数・日本語対応は行内表示からは外したが、all.htmlの絞り込みは
    # このdata属性で動くため引き続き埋め込む。
    d_price = cur_amt if cur_amt is not None else 999999999
    d_cut = disc if disc else 0
    d_rank = VERDICT_ORDER.get(v.get("code"), 9)
    d_expiry = exp["sort_ts"] if (exp and exp.get("sort_ts") is not None) else 9999999999
    d_jp = "1" if g.get("jp_support") is True else ("0" if g.get("jp_support") is False else "")
    d_onsale = "1" if on_sale else "0"
    d_reviews = g.get("review_count") if g.get("review_count") is not None else 0
    d_genres = esc(",".join(g.get("genres") or []))
    d_shop = esc(cur.get("shop") or "")
    title_norm = esc(f"{g.get('title') or ''} {g.get('title_jp') or ''}".strip().lower())

    href = f"{link_prefix}games/{esc(g['slug'])}.html"
    row_cls = f"row row-{VERDICT_CLASS.get(v.get('code'), 'v-unknown')}"
    if big_expiry and exp and exp["urgent"]:
        row_cls += " row-urgent"

    return f"""
<li class="{row_cls}" data-slug="{esc(g['slug'])}" data-title="{title_norm}" data-cut="{d_cut}" data-price="{d_price}"
  data-verdict-rank="{d_rank}" data-expiry-ts="{d_expiry}" data-jp="{d_jp}" data-onsale="{d_onsale}"
  data-reviews="{d_reviews}" data-genres="{d_genres}" data-shop="{d_shop}">
  <div class="row-rank">{rank or ''}</div>
  <div class="row-thumb">
    {thumb}
    <button type="button" class="fav-btn" data-fav-slug="{esc(g['slug'])}" aria-pressed="false" aria-label="お気に入りに追加" title="お気に入りに追加">★</button>
  </div>
  <h3 class="row-title"><a href="{href}" class="stretched-link">{esc(title)}</a></h3>
  <div class="row-timing-col">{''.join(timing_bits)}</div>
  <div class="row-amount">
    {price_top_html}
    <span class="row-cur">{yen(cur_amt)}</span>
  </div>
</li>"""


LIST_HEAD_HTML = (
    '<div class="list-head" aria-hidden="true">'
    '<span class="lh-rank"></span><span class="lh-thumb"></span>'
    '<span class="lh-title">ゲーム</span><span class="lh-timing">買い時</span>'
    '<span class="lh-amount">価格</span>'
    '</div>'
)


def featured_pool(games):
    """レビュー数・最低価格のしきい値（config/games.json の featured セクション）で
    無名・低品質なゲームを除外した「注目のセール」候補プール。"""
    return [g for g in games if is_featured_eligible(g)]


def _all_link(params):
    """all.html への絞り込み済みリンクを組み立てる（トップページの「もっと見る」用）。"""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"all.html?{qs}"


def _reviews_bucket():
    """f-reviews セレクトの選択肢（0/500/5000/50000）から、featured設定のmin_reviews以下で
    最大のものを選ぶ。「もっと見る」リンクの絞り込み条件をトップページの基準と揃えるため。"""
    for b in (50000, 5000, 500):
        if b <= FEATURED_CFG["min_reviews"]:
            return b
    return 0


# ---------------------------------------------------------------------------
# トップページ: トップ3ヒーロー / 人気ゲームのセール / 値下げ率ランキング / まもなく終了
# （Steamのチャートページのような構成）。無名の低品質ゲームは featured_pool() の時点で除外。
# ---------------------------------------------------------------------------
def _record_low_count(picks):
    # 「過去最安値」を名乗ってよいのは実際に過去最安と同額/それ以下の2コードのみ。
    # near_low（最安値に近いだけで同額ではない）を含めると誤認になるため含めない。
    return sum(1 for g in picks if g["verdict"]["code"] in ("new_low", "tied_low"))


def _popular_lead(picks):
    """「人気ゲームのセール」の冒頭一言。データから自動生成する。"""
    n = len(picks)
    record_n = _record_low_count(picks)
    if record_n:
        return f"人気タイトル{n}本のうち{record_n}本が過去最安値。"
    return f"レビュー数の多い定番タイトル{n}本が値下げ中。"


def _discount_lead(picks):
    """「値下げ率ランキング」の冒頭一言。"""
    n = len(picks)
    max_cut = max(((g.get("current") or {}).get("discount_pct") or 0 for g in picks), default=0)
    record_n = _record_low_count(picks)
    return f"最大-{max_cut}%、{n}本中{record_n}本が過去最安値で購入可能。"


def _ending_lead(picks):
    """「まもなく終了」の冒頭一言。最短の残り日数を実データから算出する。"""
    n = len(picks)
    days_list = [
        exp["days_left"] for g in picks
        if (exp := expiry_info((g.get("current") or {}).get("expiry")))
    ]
    if not days_list:
        return f"セール終了が近いタイトル{n}本。"
    min_days = min(days_list)
    if min_days <= 0:
        return f"本日中に終了するセールが{n}本。買うなら今日。"
    return f"最短であと{min_days}日でセールが終了する{n}本。"


def _chart_section(anchor, title, desc, picks, more_href, ranked=True, big_expiry=False):
    if not picks:
        return ""
    if ranked:
        rows = "".join(game_row(g, rank=i, big_expiry=big_expiry, lazy=False) for i, g in enumerate(picks, 1))
    else:
        rows = "".join(game_row(g, big_expiry=big_expiry, lazy=False) for g in picks)
    list_cls = "list ranked" if ranked else "list"
    return f"""
<section class="chart-section" id="{anchor}">
  <div class="section-head">
    <h2>{esc(title)}</h2>
    <p class="meta">{esc(desc)}</p>
  </div>
  {LIST_HEAD_HTML}
  <ul class="{list_cls}">{rows}</ul>
  <p class="see-all"><a class="btn-outline" href="{esc(more_href)}">もっと見る →</a></p>
</section>"""


def hero_carousel_section(picks):
    """トップページ最上部の横スクロールカルーセル（総合スコア上位10本）。
    カードは全て同一サイズにし（1枚だけ拡大しない）、情報の階層は色数を増やさず
    文字サイズ・太さで表現する（現在価格が最大・最太、定価は最小・細字）。
    ネイティブの overflow-x + scroll-snap でスマホのスワイプに対応し、
    左右の矢印ボタンは補助（動作は共通スクリプト側の .carousel-* 汎用ロジック）。"""
    if not picks:
        return ""
    cards = []
    for i, g in enumerate(picks, 1):
        cur = g.get("current") or {}
        reg = g.get("regular") or {}
        v = g["verdict"]
        title = display_title(g)
        thumb = game_image(g.get("assets"), ["banner400", "banner300", "boxart"],
                            "hero3-thumb-img", title, dims=(400, 230), lazy=False)
        cur_amt, reg_amt = cur.get("amount"), reg.get("amount")
        cut = cur.get("discount_pct")
        on_sale = bool(g.get("on_sale"))
        price_top_bits = []
        if on_sale and reg_amt is not None and cur_amt is not None and reg_amt > cur_amt:
            price_top_bits.append(f'<span class="row-regular"><span class="row-regular-label">定価</span>'
                               f'<span class="row-regular-price">{yen(reg_amt)}</span></span>')
        if on_sale and cut:
            price_top_bits.append(f'<span class="row-cut">-{cut}%</span>')
        price_top_html = f'<div class="row-amount-top">{"".join(price_top_bits)}</div>' if price_top_bits else ""
        cards.append(f"""
    <li class="hero3-card">
      <span class="hero3-rank">{i}</span>
      <a href="games/{esc(g['slug'])}.html" class="hero3-link">
        <div class="hero3-thumb">{thumb}</div>
        <div class="hero3-body">
          <h3 class="hero3-title">{esc(title)}</h3>
          <div class="hero3-tags">{verdict_badge(v)}</div>
          <div class="hero3-price">
            {price_top_html}
            <span class="row-cur">{yen(cur_amt)}</span>
          </div>
        </div>
      </a>
    </li>""")
    return f"""
<section class="hero-top3">
  <div class="section-head">
    <h2 class="hero-top3-heading">本日イチ押しのセール</h2>
    <p class="meta">割引率とレビュー数（人気度）から算出したスコアが高い順</p>
  </div>
  <div class="carousel-wrap">
    <button type="button" class="carousel-arrow prev" aria-label="前へ">{ICON_CHEVRON_LEFT}</button>
    <ul class="carousel-track">{''.join(cards)}</ul>
    <button type="button" class="carousel-arrow next" aria-label="次へ">{ICON_CHEVRON_RIGHT}</button>
  </div>
</section>"""



def _price_chips():
    """トップページの価格帯導線。all.html の既存フィルタ(#f-price)のバケットに合わせる。"""
    buckets = [
        ("999円以下", "0-999"),
        ("1,000〜2,999円", "1000-2999"),
        ("3,000〜4,999円", "3000-4999"),
        ("5,000円〜", "5000-"),
    ]
    chips = "".join(
        f'<a class="chip" href="{esc(_all_link({"price": val, "onsale": 1}))}">{esc(label)}</a>'
        for label, val in buckets
    )
    return f'<div class="chip-row"><span class="chip-row-label">価格帯から探す</span>{chips}</div>'


def _genre_chips(games):
    """トップページのジャンル導線。全ゲーム中の出現頻度順。"""
    counts = {}
    for g in games:
        for genre in g.get("genres") or []:
            counts[genre] = counts.get(genre, 0) + 1
    if not counts:
        return ""
    ordered = sorted(counts, key=lambda name: -counts[name])
    chips = "".join(
        f'<a class="chip" href="{esc(genre_href(genre))}">{esc(genre)}（{counts[genre]}）</a>'
        for genre in ordered
    )
    return f'<div class="chip-row"><span class="chip-row-label">ジャンルから探す</span>{chips}</div>'


# 買い時の核となる3つの判定（過去最安値±5%以内）。above_low/unknownは含めない
# （「買い時」を名乗る以上、根拠の薄いものまで水増ししない）。
BUY_NOW_CODES = ("new_low", "tied_low", "near_low")


def _hero_stat_section(games, sale_count, pool_count, updated):
    """導入部: 「何本を毎朝チェックしていて、何本が今買い時か」を、数字そのものを
    ページ内最大の要素として見せる。数字の大きさだけで語らせ、罫線・背景色・
    アイコンなどの装飾は足さない（見出しはこの数字ブロック自体がh1を兼ねる）。
    """
    total = len(games)
    buy_now = sum(1 for g in games if g.get("on_sale") and g["verdict"]["code"] in BUY_NOW_CODES)
    return f"""
<section class="hero hero-stat">
  <h1 class="hero-stat-row">
    <span class="hero-stat-item">
      <span class="hero-stat-num">{total}</span>
      <span class="hero-stat-label">本のセールを<br class="hero-stat-br">毎朝チェック</span>
    </span>
    <span class="hero-stat-item">
      <span class="hero-stat-num">{buy_now}</span>
      <span class="hero-stat-label">本が今<span class="hero-stat-accent">買い時</span></span>
    </span>
  </h1>
  <p class="meta hero-stat-sub">Steamでセール中の人気タイトルを毎朝自動収集。
    過去最安値と同額か、その5%以内まで値下がりしている{buy_now}本を「買い時」としています。</p>
  <p class="meta hero-stat-meta">セール中 {sale_count} / {total} 本（厳選対象 {pool_count} 本） ・
    最終更新 {esc(updated)} ・ <a href="about.html#verdict">買い時判定の基準について</a></p>
  {STEAM_NOTE}
</section>"""


def _explore_section(games):
    """ジャンル・価格帯チップを、導入部から切り離した独立セクションとして表示する。"""
    body = _genre_chips(games) + _price_chips()
    if not body.strip():
        return ""
    return f"""
<section class="explore-section">
  <div class="section-head">
    <h2>ジャンル・価格帯から探す</h2>
  </div>
  {body}
</section>"""


def build_featured(latest):
    games = latest.get("games", [])
    pool = featured_pool(games)
    updated = fmt_dt(latest.get("generated_at", ""))
    sale_count = sum(1 for g in games if g.get("on_sale"))
    mr = _reviews_bucket()

    # 本日イチ押しカルーセルに使うゲームは、下の3セクション（人気/値下げ率/まもなく終了）の
    # 割り当てから除外する。以降「まもなく終了 ＞ 人気ゲームのセール ＞ 値下げ率ランキング」
    # の優先度で1ゲーム1セクションに割り当てる（表示順は人気→値下げ率→まもなく終了）。
    top10 = sorted(pool, key=lambda g: -popularity_score(g.get("review_count"),
                                                           (g.get("current") or {}).get("discount_pct")))[:10]
    used = {g["slug"] for g in top10}

    ending_all = [g for g in pool if g["slug"] not in used and expiry_info((g.get("current") or {}).get("expiry"))]
    # 終了日時の秒単位(sort_ts)で真の近い順に並べる。同一セール群で終了時刻が
    # 完全一致し「本日終了」ばかりに見えるケースは、レビュー数が多い（＝知名度が
    # 高くユーザーの関心を引きやすい）ものを優先して並べ、一覧としての情報量を確保する。
    ending_all.sort(key=lambda g: (
        expiry_info((g.get("current") or {}).get("expiry"))["sort_ts"],
        -(g.get("review_count") or 0),
    ))
    ending = ending_all[:10]
    used |= {g["slug"] for g in ending}

    popular_all = sorted(pool, key=lambda g: -(g.get("review_count") or 0))
    popular = [g for g in popular_all if g["slug"] not in used][:10]
    used |= {g["slug"] for g in popular}

    discounted_all = sorted(
        pool, key=lambda g: -((g.get("current") or {}).get("discount_pct") or 0)
    )
    discounted = [g for g in discounted_all if g["slug"] not in used][:10]

    # セクション下の一言は毎回のビルドでデータから再計算される（＝生成日ごとに変わりうる）ため、
    # いつ時点の集計かを明記する（データと文言が乖離して見えないように）。
    date_suffix = f"（{updated}時点）"
    sections = (
        _chart_section("popular", "人気ゲームのセール",
                       _popular_lead(popular) + date_suffix, popular,
                       _all_link({"sort": "reviews_desc", "reviews": mr, "onsale": 1})) +
        _chart_section("discount", "値下げ率ランキング",
                       _discount_lead(discounted) + date_suffix, discounted,
                       _all_link({"sort": "cut_desc", "reviews": mr, "onsale": 1})) +
        _chart_section("ending", "まもなく終了",
                        _ending_lead(ending) + date_suffix, ending,
                        _all_link({"sort": "expiry", "reviews": mr, "onsale": 1}),
                        ranked=False, big_expiry=True)
    )
    empty = ('<p class="empty">現在、条件に合うセールはありません。'
             'しきい値は<a href="about.html#verdict">このサイトについて</a>のページで確認できます。</p>') if not pool else ""

    body = f"""
{_hero_stat_section(games, sale_count, len(pool), updated)}
{_explore_section(games)}
{hero_carousel_section(top10)}
{sections}
{empty}
<p class="see-all"><a class="btn-outline" href="all.html">すべてのセール（{len(games)}本）を見る →</a></p>
"""
    description = (
        f"{SITE_NAME}がレビュー数と割引率から選ぶ、今買うべきSteamセール。"
        f"毎朝自動更新、セール中 {sale_count} / {len(games)} 本。"
    )
    (PUBLIC_DIR / "index.html").write_text(
        page("今日の注目セール", body, active="featured", path="", description=description),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 全件ページ: すべてのセール（検索・並び替え・絞り込みはJS/deals.js）
# ---------------------------------------------------------------------------
def build_all(latest):
    games = latest.get("games", [])
    # 初期表示は人気順（レビュー数順）。無名の激安ゲームが先頭に来ないようにするため。
    # 割引率順はユーザーがソートを選んだ時だけ（JS無効時のフォールバックも同じ基準に揃える）。
    games_sorted = sorted(games, key=lambda g: -(g.get("review_count") or 0))
    rows = "".join(game_row(g) for g in games_sorted)
    updated = fmt_dt(latest.get("generated_at", ""))

    # 出現頻度順のジャンル一覧（絞り込みセレクトの選択肢）
    genre_counts = {}
    for g in games:
        for genre in g.get("genres") or []:
            genre_counts[genre] = genre_counts.get(genre, 0) + 1
    top_genres = sorted(genre_counts, key=lambda name: -genre_counts[name])[:24]
    genre_options = "".join(
        f'<option value="{esc(genre)}">{esc(genre)}（{genre_counts[genre]}）</option>' for genre in top_genres
    )

    # 出現頻度順のストア一覧（絞り込みセレクトの選択肢）
    shop_counts = {}
    for g in games:
        shop = (g.get("current") or {}).get("shop")
        if shop:
            shop_counts[shop] = shop_counts.get(shop, 0) + 1
    top_shops = sorted(shop_counts, key=lambda name: -shop_counts[name])
    shop_options = "".join(
        f'<option value="{esc(shop)}">{esc(shop)}（{shop_counts[shop]}）</option>' for shop in top_shops
    )

    body = f"""
<section class="hero">
  <h1>すべてのセール</h1>
  <p class="meta">全 {len(games)} 本 ・ 最終更新 {esc(updated)}</p>
  <p class="meta"><a href="about.html#verdict">買い時判定の基準について</a></p>
  {STEAM_NOTE}
</section>
<noscript><p class="js-warn">検索・並び替え・絞り込みには JavaScript が必要です（一覧自体は表示されています）。</p></noscript>
<div class="filter-bar">
  <label class="sr-only" for="q">ゲーム名で検索</label>
  <input type="search" id="q" placeholder="ゲーム名で検索…" autocomplete="off" aria-label="ゲーム名で検索">
  <details class="filter-details" open>
    <summary class="filter-toggle">絞り込み・並び替え</summary>
    <div class="filter-row">
      <select id="f-sort" aria-label="並び替え">
        <option value="reviews_desc" selected>人気順（レビュー数）</option>
        <option value="cut_desc">割引率が高い順</option>
        <option value="price_asc">価格が安い順</option>
        <option value="verdict">買い時順</option>
        <option value="expiry">終了が近い順</option>
      </select>
      <select id="f-cut" aria-label="割引率で絞り込み">
        <option value="0">割引率: すべて</option>
        <option value="50">50%以上</option>
        <option value="70">70%以上</option>
        <option value="90">90%以上</option>
      </select>
      <select id="f-price" aria-label="価格帯で絞り込み">
        <option value="">価格帯: すべて</option>
        <option value="0-999">〜999円</option>
        <option value="1000-2999">1,000〜2,999円</option>
        <option value="3000-4999">3,000〜4,999円</option>
        <option value="5000-">5,000円〜</option>
      </select>
      <select id="f-reviews" aria-label="人気度で絞り込み">
        <option value="0">人気度: すべて</option>
        <option value="500">レビュー500件以上</option>
        <option value="5000">レビュー5,000件以上</option>
        <option value="50000">レビュー50,000件以上</option>
      </select>
      <select id="f-genre" aria-label="ジャンルで絞り込み">
        <option value="">ジャンル: すべて</option>
        {genre_options}
      </select>
      <select id="f-shop" aria-label="ストアで絞り込み">
        <option value="">ストア: すべて</option>
        {shop_options}
      </select>
      <label class="chk"><input type="checkbox" id="f-jp"> 日本語対応のみ</label>
      <label class="chk"><input type="checkbox" id="f-onsale"> セール中のみ</label>
      <label class="chk"><input type="checkbox" id="f-fav"> お気に入りのみ</label>
      <button type="button" id="reset-filters" class="btn-text">条件をリセット</button>
    </div>
  </details>
  <p class="result-count" id="result-count"></p>
</div>
{LIST_HEAD_HTML}
<ul class="list" id="grid">{rows}</ul>
<div class="empty" id="empty" hidden>
  <p>条件に合うゲームがありません。</p>
  <button type="button" id="reset-filters-empty" class="btn-outline">条件をリセット</button>
</div>
<p class="load-more-wrap"><button type="button" id="load-more" class="btn-outline" hidden>もっと見る</button></p>
"""
    description = f"Steamで配信中のセール全{len(games)}本を検索・絞り込みで一覧表示。価格帯・割引率・日本語対応の有無で絞り込めます。"
    (PUBLIC_DIR / "all.html").write_text(
        page("すべてのセール", body, active="all", path="all.html", description=description),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# ジャンル別ページ（/genre/<slug>.html）: SEOの受け皿。JSフィルタなしのSSR一覧。
# ---------------------------------------------------------------------------
def build_genre_pages(latest):
    games = latest.get("games", [])
    genre_dir = PUBLIC_DIR / "genre"
    genre_dir.mkdir(parents=True, exist_ok=True)

    genres = {}
    for g in games:
        for genre in g.get("genres") or []:
            genres.setdefault(genre, []).append(g)

    valid_slugs = set()
    for genre, members in genres.items():
        slug = _genre_slug(genre)
        valid_slugs.add(slug)
        members_sorted = sorted(members, key=lambda g: -((g.get("current") or {}).get("discount_pct") or 0))
        rows = "".join(game_row(g, link_prefix="../") for g in members_sorted)
        on_sale_n = sum(1 for g in members if g.get("on_sale"))
        updated = fmt_dt(latest.get("generated_at", ""))

        body = f"""
<nav class="crumbs"><a href="../index.html">注目のセール</a> ・ <a href="../all.html">すべてのセール</a></nav>
<section class="hero">
  <h1>{esc(genre)}のセール一覧</h1>
  <p class="meta">全 {len(members)} 本（セール中 {on_sale_n} 本） ・ 最終更新 {esc(updated)}</p>
  {STEAM_NOTE}
</section>
{LIST_HEAD_HTML}
<ul class="list">{rows}</ul>
<p class="see-all"><a class="btn-outline" href="../all.html?genre={esc(quote(genre))}">すべてのセールで絞り込み表示 →</a></p>
"""
        description = f"Steamで配信中の{genre}ジャンルのセールを{len(members)}本掲載。割引率が高い順に一覧表示します。"
        (genre_dir / f"{slug}.html").write_text(
            page(f"{genre}のセール一覧", body, rel_root="..", active="", path=f"genre/{slug}.html",
                 description=description),
            encoding="utf-8",
        )

    for path in genre_dir.glob("*.html"):
        if path.stem not in valid_slugs:
            path.unlink()

    return sorted(valid_slugs)


# ---------------------------------------------------------------------------
# このサイトについて（データ出所・買い時判定の仕組みを開示する信頼性ページ）
# ---------------------------------------------------------------------------
def build_about(latest):
    updated = fmt_dt(latest.get("generated_at", ""))

    legend_rows = []
    for rule in verdict.VERDICT_RULES:
        cls = VERDICT_CLASS.get(rule["code"], "v-unknown")
        legend_rows.append(f"""
    <li class="legend-row">
      <span class="badge {cls}">{esc(rule["label"])}</span>
      <span class="legend-desc">{esc(verdict_desc(rule["code"]))}</span>
    </li>""")
    legend_rows.append(f"""
    <li class="legend-row">
      <span class="badge v-unknown">判定不可</span>
      <span class="legend-desc">{esc(verdict_desc("unknown"))}</span>
    </li>""")

    body = f"""
<section class="hero">
  <h1>このサイトについて</h1>
  <p class="meta">最終更新 {esc(updated)}</p>
</section>

<h2>「{esc(SITE_NAME)}」とは</h2>
<p>{esc(SITE_NAME)}は、Steamで配信されているゲームのセール情報を毎日自動で収集し、
「今が買い時かどうか」を過去の最安値と比較して一目でわかるようにする非公式の価格追跡サイトです。
セール中のタイトルを厳選して紹介するトップページと、全タイトルを検索・絞り込みできる一覧ページを用意しています。</p>

<h2>データの出所</h2>
<p><strong>掲載価格はSteamストアの価格を基準にしています。</strong>
価格データは <a href="https://isthereanydeal.com/" target="_blank" rel="noopener noreferrer">IsThereAnyDeal</a>
のAPIをSteamストアに絞り込んで取得し、ゲーム情報の一部はSteam公式APIで補っています。
過去最安値・価格履歴もSteamストアの価格のみで算出しており、サードパーティのキーショップ
（他社ストア）の価格とは混在させていません。Steamより安い他ストアの価格がある場合のみ、
一覧・詳細ページに「他ストア最安」として控えめに参考表示します。
表示価格はリアルタイムではなく、<strong>毎朝6時ごろ（日本時間）に自動更新される保存済みデータ</strong>です。
実際の購入前には、必ずストア側の最新価格をご確認ください。</p>

{steam_sales_calendar_html()}

<h2 id="verdict">買い時判定の仕組み</h2>
<p>各ゲームに付くバッジは、「現在価格」と「過去に記録された最安値」の差（％）だけを使って機械的に判定しています。
セールの煽り文句や独自の主観は入れず、しきい値は以下の通りです（バッジにカーソルを合わせても同じ説明が出ます）。</p>
<ul class="legend-list">{''.join(legend_rows)}</ul>
<p class="muted">※ 過去最安値は、このサイトが観測してきた範囲内での最安値です。観測期間より前の一時的な特売などは反映されない場合があります。</p>

<h2>免責事項</h2>
<p>本サイトはValve/Steamおよび各ストアの公式サイトではありません。掲載している価格・セール情報の正確性・最新性は保証できません。
購入の判断は、必ずリンク先のストアで表示される価格を確認した上で行ってください。</p>
"""
    description = f"{SITE_NAME}のデータ出所（IsThereAnyDeal/Steam）と、買い時判定バッジのしきい値を公開しています。"
    (PUBLIC_DIR / "about.html").write_text(
        page("このサイトについて", body, active="about", path="about.html", description=description),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# robots.txt / sitemap.xml（検索エンジン向け）
# ---------------------------------------------------------------------------
def build_robots():
    content = f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}/sitemap.xml\n"
    (PUBLIC_DIR / "robots.txt").write_text(content, encoding="utf-8")


def build_sitemap(latest, genre_slugs=()):
    games = latest.get("games", [])
    today = date.today().isoformat()
    paths = (["", "all.html", "about.html"]
             + [f"genre/{slug}.html" for slug in genre_slugs]
             + [f"games/{g['slug']}.html" for g in games])

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path in paths:
        loc = f"{SITE_URL}/{path}" if path else f"{SITE_URL}/"
        parts.append(f"  <url><loc>{esc(loc)}</loc><lastmod>{today}</lastmod></url>")
    parts.append("</urlset>")
    (PUBLIC_DIR / "sitemap.xml").write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# 価格履歴のインラインSVGスパークライン
# ---------------------------------------------------------------------------
def _short_ym(date_str):
    """'2025-07-01' を 'YYYY/M' に。失敗時は空。X軸ラベル用。"""
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        return f"{d.year}/{d.month}"
    except Exception:
        return ""


def related_games(game, all_games, n=4):
    """同じジャンルを1つ以上共有し、割引率・人気スコアが高い順の関連ゲーム。"""
    genres = set(game.get("genres") or [])
    if not genres:
        return []
    candidates = [
        g for g in all_games
        if g["slug"] != game["slug"] and genres & set(g.get("genres") or [])
    ]
    candidates.sort(key=lambda g: -popularity_score(g.get("review_count"),
                                                      (g.get("current") or {}).get("discount_pct")))
    return candidates[:n]


def _nice_axis_max(value, divisions=4):
    """value以上になる「キリのいい」Y軸上限と、divisions等分したときの目盛り幅を返す。
    例: value=4300, divisions=4 -> (5000, 1250) ではなく (6000, 1500) のように
    1/2/5×10^n に丸めた見やすい目盛りにする。"""
    if value <= 0:
        return 100, 25
    raw_step = value / divisions
    magnitude = 10 ** math.floor(math.log10(raw_step))
    residual = raw_step / magnitude
    if residual > 5:
        nice = 10
    elif residual > 2:
        nice = 5
    elif residual > 1:
        nice = 2
    else:
        nice = 1
    step = nice * magnitude
    return step * divisions, step


def _price_chart_svg(pts, current_amount=None):
    """1期間分の価格履歴ステップ（階段）チャートSVGを描く。

    価格は「変わるまで一定」のデータであり、観測点を斜め線で結ぶ折れ線では
    「徐々に値下がりした」ように誤って見えるため使わない。水平線が価格の
    維持期間、垂直線が値下げ/値上げの瞬間を表すステップチャートにする。

    動的な長さのテキストラベル（現在価格・最安値など）はSVG内に置かない
    （線や他の点と重なる原因になるため）。それらは呼び出し側がSVGの外の
    HTML凡例として表示する。SVG内は軸・グリッド線・線・エリア塗り・点のみ。
    """
    n = len(pts)
    amounts = [p["amount"] for p in pts]
    hist_low = min(amounts)
    has_cur = isinstance(current_amount, (int, float))
    scale_max_val = max(amounts + ([current_amount] if has_cur else []))
    axis_max, axis_step = _nice_axis_max(scale_max_val)

    # 観測点が多いほど横に詰まって「バーコード化」するため、点数に応じて実ピクセル幅を
    # 広げる（呼び出し側が overflow-x:auto のラッパーで横スクロール可能にする）。
    width = max(760, 22 * n)
    height = 260
    pad_left, pad_right, pad_top, pad_bottom = 64, 16, 14, 28
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    def x(i):
        return pad_left + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

    def y(a):
        return pad_top + plot_h * (1 - a / axis_max)

    # Y軸グリッド線・ラベル（¥0を含め、キリのいい間隔で3〜4本＋上限の計4〜5本）
    n_lines = max(1, round(axis_max / axis_step))
    grid_bits = []
    for i in range(n_lines + 1):
        val = i * axis_step
        gy = y(val)
        grid_bits.append(f'<line x1="{pad_left}" y1="{gy:.1f}" x2="{width-pad_right}" y2="{gy:.1f}" class="grid-line"/>')
        grid_bits.append(f'<text x="{pad_left-10}" y="{gy+4:.1f}" class="axis-y-label" text-anchor="end">{esc(yen(val))}</text>')

    # ステップパス: 各区間を「水平線（前の価格を維持）→垂直線（変化の瞬間）」で繋ぐ
    step_d = f"M {x(0):.1f},{y(pts[0]['amount']):.1f}"
    for i in range(1, n):
        step_d += f" L {x(i):.1f},{y(pts[i-1]['amount']):.1f} L {x(i):.1f},{y(pts[i]['amount']):.1f}"

    # エリア塗り: ステップパスの下（¥0の基準線まで）を薄く塗り、価格帯を読み取りやすくする
    baseline_y = y(0)
    area_d = f"{step_d} L {x(n-1):.1f},{baseline_y:.1f} L {x(0):.1f},{baseline_y:.1f} Z"

    low_idx = amounts.index(hist_low)   # 最安を付けた点
    dots = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(p["amount"]):.1f}" r="2.5" class="pt"/>'
        for i, p in enumerate(pts) if i != low_idx
    )
    low_dot = f'<circle cx="{x(low_idx):.1f}" cy="{y(hist_low):.1f}" r="4" class="pt-low"/>'

    cur_dot = ""
    if has_cur:
        cur_dot = f'<circle cx="{x(n-1):.1f}" cy="{y(current_amount):.1f}" r="4" class="pt-current"/>'

    # X軸の日付ラベル: 最初と最後は必ず、間を含め等間隔に最大6点まで
    n_ticks = min(6, n)
    tick_idxs = sorted({round(i * (n - 1) / (n_ticks - 1)) for i in range(n_ticks)}) if n_ticks > 1 else [0]
    axis_x_bits = []
    for i in tick_idxs:
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        axis_x_bits.append(f'<text x="{x(i):.1f}" y="{height-8:.1f}" class="axis-x-label" '
                            f'text-anchor="{anchor}">{esc(_short_ym(pts[i]["date"]))}</text>')

    return f"""<svg class="chart" viewBox="0 0 {width} {height}" style="min-width:{width}px" role="img"
     aria-label="価格履歴グラフ（ステップチャート）">
  {''.join(grid_bits)}
  <path d="{area_d}" class="price-area"/>
  <path d="{step_d}" class="price-line"/>
  {dots}{low_dot}{cur_dot}{''.join(axis_x_bits)}
</svg>"""


def price_chart_html(history, current_amount=None, lowest_amount=None, regular_amount=None):
    """価格推移グラフ一式（HTML凡例＋期間切り替え＋SVG）を返す。

    現在価格は overview 由来の値（current_amount）を使う。履歴の最終点は
    「定価復帰」等で現在価格と一致しないため、最終点をそのまま現在扱いにしない。
    """
    pts = [h for h in history if h.get("amount") is not None and h.get("date")]
    if len(pts) < 2:
        return '<p class="muted">履歴データが不足しています。</p>'
    pts.sort(key=lambda p: p["date"])

    legend_bits = []
    if isinstance(current_amount, (int, float)):
        legend_bits.append(f'<span class="chart-legend-item"><span class="chart-legend-swatch cur"></span>現在 {esc(yen(current_amount))}</span>')
    if isinstance(lowest_amount, (int, float)):
        legend_bits.append(f'<span class="chart-legend-item"><span class="chart-legend-swatch low"></span>最安 {esc(yen(lowest_amount))}</span>')
    if isinstance(regular_amount, (int, float)):
        legend_bits.append(f'<span class="chart-legend-item">定価 {esc(yen(regular_amount))}</span>')
    legend_html = f'<p class="chart-legend">{"".join(legend_bits)}</p>' if legend_bits else ""

    # 直近1年分だけの絞り込み表示（点数が多い長期履歴は詰まって読みにくいため既定はこちら）。
    # 1年以内に収まる履歴しか無いゲームは切り替えボタン自体を出さない。
    today = date.today()
    try:
        one_year_pts = [p for p in pts if (today - datetime.strptime(p["date"][:10], "%Y-%m-%d").date()).days <= 365]
    except Exception:
        one_year_pts = pts
    show_toggle = len(one_year_pts) >= 2 and len(one_year_pts) < len(pts)

    if not show_toggle:
        return legend_html + f'<div class="table-wrap chart-scroll">{_price_chart_svg(pts, current_amount)}</div>'

    toggle_html = f"""
<div class="chart-period-toggle" data-period-buttons="price-chart">
  <button type="button" class="chart-period-btn" data-period="1y" aria-pressed="true">1年</button>
  <button type="button" class="chart-period-btn" data-period="all" aria-pressed="false">全期間</button>
</div>"""
    panel_1y = (f'<div class="table-wrap chart-scroll" data-period-panel="price-chart" data-period="1y">'
                f'{_price_chart_svg(one_year_pts, current_amount)}</div>')
    panel_all = (f'<div class="table-wrap chart-scroll" data-period-panel="price-chart" data-period="all" hidden>'
                 f'{_price_chart_svg(pts, current_amount)}</div>')
    return legend_html + toggle_html + panel_1y + panel_all


# ---------------------------------------------------------------------------
# ゲーム個別ページ
# ---------------------------------------------------------------------------
def sale_history_block_html(summary):
    """詳細ページの「セール履歴の傾向」。事実（回数・直近日）だけを示し、
    「次はいつ」という予測はしない（sale_history.py 参照）。"""
    if not summary:
        return ""
    since_txt = fmt_date_jp(summary["tracked_since"])
    last_txt = fmt_date_jp(summary["last_sale_date"])
    return f"""
<h2>セール履歴の傾向</h2>
<p>追跡開始（{esc(since_txt)}〜）以降、{summary['sale_count']}回のセールを確認。
直近は{esc(last_txt)}からです。</p>
<p class="muted">※ 予測ではなく、記録された価格変化から検出した事実のみを表示しています。</p>"""


def build_game_page(game, latest):
    slug = game["slug"]
    hist_path = HISTORY_DIR / f"{slug}.json"
    history = []
    if hist_path.exists():
        history = json.loads(hist_path.read_text(encoding="utf-8")).get("history", [])

    cur = (game.get("current") or {})
    low = (game.get("lowest") or {})
    reg = (game.get("regular") or {})
    v = game["verdict"]
    sale_summary = sale_history.summarize_sale_history(history)

    shop = cur.get("shop")
    url = cur.get("url")
    buy_link = f'<a class="buy" href="{esc(url)}" target="_blank" rel="noopener">{esc(shop or "ストアで見る")}で見る →</a>' if url else ""

    # 過去最安値カードの補足: いつ記録した最安かを絶対日付+相対で併記
    low_when = ""
    if low.get("date"):
        rel = relative_date_jp(low.get("date"))
        abs_d = fmt_date_jp(low.get("date"))
        low_when = f"{abs_d}（{rel}）" if rel else abs_d

    # 買い時の主説明（一覧行と同じロジック: tied_lowは到達回数、new_lowは無し、それ以外は差額）
    game_verdict_code = v.get("code")
    if game_verdict_code == "tied_low":
        timing_txt = tied_low_verdict_text(slug, low.get("amount"))
    elif game_verdict_code == "new_low":
        timing_txt = ""
    else:
        timing_txt = buy_timing_text(cur.get("amount"), low.get("amount"), code=game_verdict_code)

    title = display_title(game)

    # ファーストビューで画像・タイトル・現在価格・買い時・購入ボタンが横並びで
    # 一度に見えるよう、バナーは縮小して game-hero-media に収める
    banner = game_image(game.get("assets"),
                        ["banner600", "banner400", "banner300", "boxart"],
                        "hero-banner", title, dims=(600, 344), lazy=False)

    # セール終了日時（個別ページは日付＋残り日数の両方）。単独の浮遊段落にはせず、
    # 価格ブロックの一部として表示する。
    exp = expiry_info(cur.get("expiry"))
    if exp:
        exp_cls = "expiry-detail urgent" if exp["urgent"] else "expiry-detail"
        expiry_detail = (f'<p class="{exp_cls}">{ICON_CLOCK} セール終了：{esc(exp["date_full"])}'
                         f'（{esc(days_left_text(exp["days_left"]))}）</p>')
    elif cur.get("discount_pct"):
        expiry_detail = '<p class="expiry-detail">セール終了日：未定</p>'
    else:
        expiry_detail = ""

    # 定価からの節約額・割引率（現在価格の周りに添える）
    cur_amt, reg_amt = cur.get("amount"), reg.get("amount")
    disc = cur.get("discount_pct")
    on_sale = bool(game.get("on_sale"))
    save_txt = ""
    if cur_amt is not None and reg_amt is not None and reg_amt > cur_amt:
        pct = f"（{disc}%オフ）" if disc else ""
        save_txt = f"定価より {yen(reg_amt - cur_amt)} お得{pct}"
    cut_html = f'<span class="card-cut">-{disc}%</span>' if (on_sale and disc) else ""

    # 履歴テーブル（直近20件を新しい順）。ストアは全件Steamのため列を出さない。
    # 直近5件だけ常時表示し、残りは<details>で展開する（20行以上の常時表示は冗長なため）。
    # all.htmlの絞り込み欄で既に使っている<details>と同じ、JS不要の折りたたみパターン。
    all_rows = [f'<tr><td>{esc(h.get("date"))}</td><td>{yen(h.get("amount"))}</td></tr>'
                for h in list(reversed(history))]
    hist_table = ""
    if all_rows:
        visible_rows, rest_rows = all_rows[:5], all_rows[5:]
        rest_html = (f'<details class="hist-more"><summary>もっと見る（残り{len(rest_rows)}件）</summary>'
                     f'<table class="history"><tbody>{"".join(rest_rows)}</tbody></table></details>') if rest_rows else ""
        hist_table = f"""
<h2>価格履歴（直近）</h2>
<div class="table-wrap">
<table class="history">
  <thead><tr><th>日付</th><th>価格</th></tr></thead>
  <tbody>{''.join(visible_rows)}</tbody>
</table>
</div>
{rest_html}"""

    updated = fmt_dt(latest.get("generated_at", ""))
    genre_tags = genre_tag_links(game.get("genres") or [], prefix="../")

    related = related_games(game, latest.get("games", []), n=4)
    related_html = ""
    if related:
        primary_genre = (game.get("genres") or [None])[0]
        more_link = (f'<p class="see-all"><a class="btn-outline" href="{esc(genre_href(primary_genre, "../"))}">'
                     f'{esc(primary_genre)}のセールをもっと見る →</a></p>') if primary_genre else ""
        related_html = f"""
<h2>このゲームと同じジャンルのセール</h2>
{LIST_HEAD_HTML}
<ul class="list">{''.join(game_row(g, link_prefix="../", lazy=False) for g in related)}</ul>
{more_link}"""

    body = f"""
<nav class="crumbs"><a href="../index.html">注目のセール</a> ・ <a href="../all.html">すべてのセール</a></nav>
<section class="game-hero">
  <div class="game-hero-media">{banner}</div>
  <div class="game-hero-info">
    <div class="game-hero-title-row">
      <h1>{esc(title)}</h1>
      <button type="button" class="fav-btn icon" data-fav-slug="{esc(slug)}" aria-pressed="false" aria-label="お気に入りに追加" title="お気に入りに追加">★</button>
    </div>
    <div class="game-head-tags">
      {verdict_badge(v)}
      {jp_mark(game.get('jp_support'))}
      {genre_tags}
    </div>
    <div class="price-current">
      <div class="price-current-value">{yen(cur.get('amount'))}{cut_html}</div>
      {f'<p class="verdict-detail">{esc(timing_txt)}</p>' if timing_txt else ''}
      {expiry_detail}
      <div class="price-current-meta">
        {store_badge(shop)}
        {f'<span class="card-sub save">{esc(save_txt)}</span>' if save_txt else ''}
        {other_store_html(game)}
      </div>
    </div>
    {buy_link}
  </div>
</section>

<section class="price-cards-secondary">
  <div class="stat-card">
    <div class="card-label">過去最安値</div>
    <div class="card-value">{yen(low.get('amount'))}</div>
    <div class="card-sub">{esc(low_when)}</div>
  </div>
  <div class="stat-card">
    <div class="card-label">定価</div>
    <div class="card-value">{yen(reg.get('amount'))}</div>
    <div class="card-sub"></div>
  </div>
</section>

<h2>価格の推移</h2>
{price_chart_html(history, current_amount=cur.get('amount'), lowest_amount=low.get('amount'), regular_amount=reg.get('amount'))}

{sale_history_block_html(sale_summary)}

{hist_table}

{related_html}

<p class="meta">最終更新: {esc(updated)}</p>
"""
    cut_txt = f"（-{disc}%）" if (on_sale and disc) else ""
    og_title = f"{title} {yen(cur_amt)}{cut_txt}"
    description = (
        f"{title}の価格推移・過去最安値をチェック。"
        f"現在価格{yen(cur_amt)}{cut_txt}、過去最安値{yen(low.get('amount'))}。"
    )
    og_image = best_asset_url(game.get("assets"), ["banner600", "banner400", "banner300", "boxart"])

    out = PUBLIC_DIR / "games" / f"{slug}.html"
    out.write_text(
        page(f"{title}の最安値・セール履歴", body, rel_root="..", path=f"games/{slug}.html",
             description=description, og_title=og_title, og_image=og_image or None),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
CSS = """
/* テーマ設計の原則: ライトを既定値として無条件の :root に定義する
   （日本の価格比較サイトの標準に合わせる）。ダークは prefers-color-scheme と
   手動トグル（<html data-theme="dark|light">）の両方から上書きし、後者が
   常にOS設定より優先されるよう `:root:not([data-theme="light"])` で
   ガードする。ライト/ダークは単純な反転ではなく別トークンセットとして
   個別にチューニングする（中間グレーを使わず、本文は常に「ほぼ黒」か
   「ほぼ白」の1色、補助テキストも1色に統一する）。 */
:root {
  --header-h: 64px;

  /* ページ背景はわずかに温かみのあるオフホワイトにし、カード面(--surface-1)を
     純白にすることで「背景は沈み、カードが浮く」階層を作る（純白背景の眩しさ対策）。 */
  --bg: #fafaf9;
  --surface-1: #ffffff;
  --surface-2: #f2f1ee;
  --surface-3: #e7e5e0;
  --panel: var(--surface-1);
  --panel2: var(--surface-2);
  --row-hover: var(--surface-2);
  --header-glass: rgba(250, 250, 249, .86);

  --text: #14171c;
  --text-strong: #14171c;
  --muted: #5c6470;
  --line: #e3e6eb;
  --line-strong: #cdd2da;

  /* 色は意味だけに使う: コーラル=主要アクション(購入等)のみ、
     緑=過去最安値に到達（new_low/tied_low）のみ、赤=セール終了間近のみ。それ以外のUIは
     全て上の無彩色トークン（text/muted/surface/line）で表現する。
     good/bad はライト/ダークそれぞれ白地・黒地で4.5:1以上のコントラストに
     なるよう個別に調整済み（薄い版をそのまま反転しただけだと不合格になる）。 */
  --accent: #ff7a59;
  --on-accent: #1c0f09;
  --good: #0c6b3d;
  --good-bg: rgba(12, 107, 61, .08);
  --good-border: rgba(12, 107, 61, .3);
  --bad: #c22f2f;
  --bad-bg: rgba(194, 47, 47, .08);
  --bad-border: rgba(194, 47, 47, .32);
  /* バッジの「塗り」専用トークン。背景色と同系色の文字を重ねる中途半端な表現を
     禁止し、背景を塗るバッジは常に不透明色+白文字のこの組み合わせだけを使う
     （ライト/ダーク共通の固定値。実測コントラスト比: 緑6.6:1 / 赤5.6:1、
     白文字でWCAG AA基準の4.5:1を満たす）。 */
  --good-solid-bg: #0c6b3d;
  --good-solid-fg: #ffffff;
  --bad-solid-bg: #c22f2f;
  --bad-solid-fg: #ffffff;
  --font: "Inter", "Noto Sans JP", system-ui, "Hiragino Kaku Gothic ProN", Meiryo, sans-serif;

  /* タイポグラフィスケール: 見出し/本文の差を広く取って階層を作る。
     日本語は行間を広め（1.7前後）に取り、和欧混植でも詰まって見えないようにする。 */
  --text-display: 800 2.25rem/1.2 var(--font);
  --text-h2: 700 1.5rem/1.3 var(--font);
  --text-title: 600 1rem/1.4 var(--font);
  --text-price: 800 1.375rem/1 var(--font);
  --text-body: 400 .875rem/1.7 var(--font);
  --text-meta: 400 .75rem/1.6 var(--font);
  --text-micro: 500 .6875rem/1.5 var(--font);
  --text-label: 700 .78rem/1 var(--font);

  /* スペーシング（4pxベース）。sp-9/sp-10 はセクション間の余白を
     広く取るために追加した段階（行の高さ自体は変えない）。 */
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px;
  --sp-5: 24px; --sp-6: 32px; --sp-7: 48px; --sp-8: 64px;
  --sp-9: 96px; --sp-10: 128px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0d1014;
    --surface-1: #161a20;
    --surface-2: #1c2129;
    --surface-3: #232a33;
    --header-glass: rgba(13, 16, 20, .88);
    --text: #f2f4f7;
    --text-strong: #f2f4f7;
    --muted: #98a0ac;
    --line: #262b33;
    --line-strong: #333b46;
    --good: #6fbf8b;
    --good-bg: rgba(111, 191, 139, .14);
    --good-border: rgba(111, 191, 139, .4);
    --bad: #ef6b6b;
    --bad-bg: rgba(239, 107, 107, .14);
    --bad-border: rgba(239, 107, 107, .42);
  }
}
:root[data-theme="dark"] {
  --bg: #0d1014;
  --surface-1: #161a20;
  --surface-2: #1c2129;
  --surface-3: #232a33;
  --header-glass: rgba(13, 16, 20, .88);
  --text: #f2f4f7;
  --text-strong: #f2f4f7;
  --muted: #98a0ac;
  --line: #262b33;
  --line-strong: #333b46;
  --good: #6fbf8b;
  --good-bg: rgba(111, 191, 139, .14);
  --good-border: rgba(111, 191, 139, .4);
  --bad: #ef6b6b;
  --bad-bg: rgba(239, 107, 107, .14);
  --bad-border: rgba(239, 107, 107, .42);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: var(--font);
  font-feature-settings: "palt" 1;
  line-height: 1.7;
  /* .site-header は position:fixed でフローから外れているため、
     その分の余白をここで確保する（--header-h はJSで実測して更新） */
  padding-top: var(--header-h);
}
h1, h2, h3 { color: var(--text-strong); font-weight: 800; }
/* リンクは色ではなく下線で示す（アクセントは購入ボタン等のCTA専用のため）。
   パンくず・本文リンクが誤ってエラー表示のような色に見える問題もこれで解消する。 */
a { color: var(--text-strong); text-decoration: underline; text-decoration-color: var(--line-strong); text-underline-offset: 2px; }
a:hover { text-decoration-color: currentColor; }
.container { max-width: 1120px; margin: 0 auto; padding: var(--sp-6) var(--sp-5) var(--sp-9); }
@media (min-width: 720px) {
  .container { padding-left: var(--sp-8); padding-right: var(--sp-8); }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after {
    animation-duration: .001ms !important; animation-iteration-count: 1 !important;
    transition-duration: .001ms !important; scroll-behavior: auto !important;
  }
}
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}

/* ヘッダー / ナビ: 常時画面上部に固定し、下にコンテンツが透けるガラス調に。
   以前は position:sticky + backdrop-filter で実装していたが、Windows版Edgeに限らず
   スクロール後にヘッダーが一覧の途中に浮いて見える不具合が実機でも再発したため、
   スクロール挙動がブラウザ間で安定している position:fixed に切り替えた
   （フローから外れる分は body の padding-top で補っている）。
   ぼかし背景は引き続き::beforeの絶対配置レイヤーに分離し、本体は背景合成をしない。 */
.site-header {
  position: fixed; top: 0; left: 0; right: 0; z-index: 50;
  border-bottom: 1px solid var(--line);
  padding: 10px 14px;
}
.site-header::before {
  content: ""; position: absolute; inset: 0; z-index: -1;
  background: var(--header-glass);
  backdrop-filter: blur(10px) saturate(1.2);
  -webkit-backdrop-filter: blur(10px) saturate(1.2);
}
.header-inner {
  max-width: 1120px; margin: 0 auto; display: flex; align-items: center;
  justify-content: space-between; gap: 10px 16px; flex-wrap: wrap;
}
.brand {
  display: inline-flex; align-items: center; gap: 6px;
  font-weight: 800; font-size: 1.1rem; letter-spacing: .01em; color: var(--text-strong);
  text-decoration: none;
}
.brand:hover { text-decoration: none; }
.brand-icon { color: var(--accent); flex-shrink: 0; } /* ロゴマークのみブランド識別として例外的にアクセントを使う */
.site-search { flex: 1 1 200px; max-width: 320px; }
.site-search input {
  width: 100%; padding: 7px 12px; border-radius: 7px; border: 1px solid var(--line);
  background: var(--panel2); color: var(--text-strong); font-size: .85rem;
  transition: border-color .15s ease;
}
.site-search input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
/* 横に収まらないときは折り返さず横スクロールにして、固定ヘッダーの高さを一定に保つ
   （スマホでヘッダーが画面を圧迫しないようにするため） */
.site-nav {
  display: flex; gap: 4px; flex-wrap: nowrap; overflow-x: auto;
  scrollbar-width: none; -ms-overflow-style: none; max-width: 100%;
}
.site-nav::-webkit-scrollbar { display: none; }
.site-nav a {
  padding: 6px 12px; border-radius: 6px; font-size: .82rem; font-weight: 700;
  color: var(--muted); white-space: nowrap; text-decoration: none;
  transition: background .15s ease, color .15s ease;
}
.site-nav a:hover { color: var(--text-strong); }
.site-nav a.active { background: var(--panel2); color: var(--text-strong); }
/* テーマ切替。アイコンのみのゴーストボタン。データ属性でサン/ムーンの表示を切り替える。 */
.theme-toggle {
  display: inline-flex; align-items: center; justify-content: center;
  width: 32px; height: 32px; border-radius: 50%; border: 1px solid var(--line);
  background: none; color: var(--muted); cursor: pointer; flex-shrink: 0;
  transition: color .15s ease, border-color .15s ease, background .15s ease;
}
.theme-toggle:hover { color: var(--text-strong); border-color: var(--line-strong); background: var(--panel2); }
.theme-toggle .icon-moon { display: none; }
:root[data-theme="dark"] .theme-toggle .icon-sun { display: none; }
:root[data-theme="dark"] .theme-toggle .icon-moon { display: block; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .theme-toggle .icon-sun { display: none; }
  :root:not([data-theme="light"]) .theme-toggle .icon-moon { display: block; }
}
.site-footer {
  text-align: center; color: var(--muted); font-size: .78rem;
  padding: 24px; border-top: 1px solid var(--line);
}
.site-footer p { margin: 4px 0; }
.site-footer a { color: var(--muted); }
.site-footer a:hover { color: var(--text-strong); }

.hero {
  padding: var(--sp-6) var(--sp-7) var(--sp-7); border-radius: 16px; border: 1px solid var(--line);
  background: var(--panel);
}
.hero h1, .game-hero-title-row h1 { margin: 0 0 4px; font-weight: 800; }
/* 見出しの装飾は色ではなく無彩色の左ボーダー＋サイズ差で示す（色は意味のみに使うため） */
.hero h1 { padding-left: 14px; border-left: 3px solid var(--line-strong); font: var(--text-display); }
.meta { color: var(--muted); font-size: .85rem; margin: 2px 0; }

/* 導入部（トップページ冒頭）: 「何本を毎朝チェックしていて、何本が今買い時か」を
   数字そのものをページ内最大の要素として見せる。罫線・背景色・アイコンなどの
   装飾は足さず、数字の大きさと数字/説明文のサイズ差だけで語らせる。 */
.hero-stat-row {
  display: flex; flex-direction: column; gap: var(--sp-6);
  margin: 0; padding: 0; border: none; font: inherit;
}
@media (min-width: 640px) {
  .hero-stat-row { flex-direction: row; align-items: baseline; flex-wrap: wrap; gap: var(--sp-9); }
}
.hero-stat-item { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; }
.hero-stat-num {
  display: block; font-weight: 800; letter-spacing: -.03em; line-height: .92;
  font-size: clamp(3.4rem, 16vw, 6.5rem);
  color: var(--text-strong); font-variant-numeric: tabular-nums;
}
.hero-stat-label { font-size: 1rem; font-weight: 700; color: var(--muted); }
@media (min-width: 640px) { .hero-stat-label { font-size: 1.15rem; } .hero-stat-br { display: none; } }
/* サイトの核となる価値（買い時）だけ、購入導線と同じアクセントカラーを例外的に使う */
.hero-stat-accent { color: var(--accent); font-weight: 800; }
.hero-stat-meta { margin-top: var(--sp-5); }

/* ジャンル / 価格帯チップ（導入部から独立した「探す」セクション） */
.explore-section { margin: var(--sp-8) 0 0; }
.chip-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 12px 0 0; padding-left: 14px; }
.chip-row-label { color: var(--muted); font-size: .78rem; font-weight: 700; margin-right: 2px; }
.chip {
  display: inline-block; padding: 4px 11px; border-radius: 999px; border: 1px solid var(--line);
  background: var(--panel2); color: var(--text); font-size: .78rem; font-weight: 600;
  text-decoration: none;
  transition: border-color .15s ease, color .15s ease, background .15s ease;
}
.chip:hover, .chip:focus-visible {
  border-color: var(--line-strong); color: var(--text-strong);
  background: var(--surface-3);
}

/* トップ3ヒーロー */
.hero-top3 { margin: var(--sp-9) 0 0; }
.hero-top3 .section-head { margin: 0 0 var(--sp-5); }

/* 汎用の横スクロールカルーセル（本日イチ押し・詳細ページのグラフ期間切替でも使う
   ため用途非依存の命名にする）。ネイティブの overflow-x + scroll-snap で
   スマホのスワイプに対応し、矢印ボタンはその補助として左右に添える。 */
.carousel-wrap { display: flex; align-items: stretch; gap: var(--sp-3); }
.carousel-track {
  list-style: none; margin: 0; padding: 2px 2px 10px; display: flex; gap: var(--sp-4);
  overflow-x: auto; scroll-snap-type: x mandatory; -webkit-overflow-scrolling: touch;
}
.carousel-track > li { scroll-snap-align: start; flex: 0 0 auto; }
.carousel-arrow {
  flex: 0 0 auto; display: none; align-items: center; justify-content: center;
  width: 36px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel2);
  color: var(--text-strong); cursor: pointer;
  transition: background .15s ease, border-color .15s ease, opacity .15s ease;
}
@media (min-width: 720px) { .carousel-arrow { display: flex; } }
.carousel-arrow:hover { background: var(--surface-2); border-color: var(--line-strong); }
.carousel-arrow:disabled { opacity: .35; cursor: default; }
.carousel-arrow:disabled:hover { background: var(--panel2); border-color: var(--line); }

/* カードは全て同一サイズ（1枚だけ拡大しない＝10枚が同格）。情報の階層は色数を
   増やさず文字サイズ・太さで表す: 現在価格が最大・最太、タイトルは中間、
   定価は最小・細字（row-cur/row-regularの既定スタイルをそのまま使う）。 */
.hero3-card {
  position: relative; width: 220px; background: var(--surface-1); border: 1px solid var(--line-strong);
  border-radius: 14px; overflow: hidden;
  box-shadow: 0 1px 3px rgba(0,0,0,.07), 0 1px 2px rgba(0,0,0,.05);
  transition: background .16s ease, border-color .16s ease, box-shadow .16s ease;
}
.hero3-card:hover { background: var(--surface-2); border-color: var(--text-strong); box-shadow: 0 4px 12px rgba(0,0,0,.1); }
.hero3-link { display: block; color: inherit; text-decoration: none; }
.hero3-rank {
  position: absolute; top: 10px; left: 10px; z-index: 1;
  width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
  background: rgba(16,19,26,.78); border: 1px solid rgba(255,255,255,.5); color: #fff;
  font-weight: 800; font-size: .95rem; font-variant-numeric: tabular-nums;
}
.hero3-thumb { width: 100%; aspect-ratio: 400 / 230; background: var(--panel2); overflow: hidden; }
.hero3-thumb-img { width: 100%; height: 100%; object-fit: cover; display: block; }
.hero3-body { padding: var(--sp-4) var(--sp-5) var(--sp-5); }
.hero3-title {
  margin: 0 0 8px; font-size: .92rem; font-weight: 700; color: var(--text-strong);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.hero3-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.hero3-price { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; }
.hero3-price .row-cur { font-size: 1.2rem; }

/* まもなく終了: 横スクロールカードは一覧性が悪いため使わず、他セクションと同じ
   縦リストのまま「残り日数の表示サイズ」で差別化する。赤は本日〜3日以内の
   緊急表示にのみ使う（それ以外の判定色は使わない）。 */
.tag-expiry.big {
  padding: 2px 8px; border-radius: 4px; font-size: .82rem; font-weight: 800;
  color: var(--text-strong); background: var(--panel2); border: 1px solid var(--line);
  white-space: nowrap;
}
.tag-expiry.big.urgent { color: var(--bad-solid-fg); background: var(--bad-solid-bg); border-color: var(--bad-solid-bg); }
/* 緊急度（残り3日以内）はセクション/行の背景に色を敷かず、左端のアクセントラインと
   「本日終了」バッジの2箇所だけで表現する（背景着色はダークテーマで濁って見えるため
   廃止）。.row.row-urgent で詳細度を1段上げ、new_low/tied_low等の左バー色より確実に勝つ。 */
.row.row-urgent::before { background: var(--bad); width: 4px; }

/* トップページのチャートセクション（人気/値下げ率/まもなく終了） */
.chart-section, #verdict { scroll-margin-top: calc(var(--header-h) + 14px); }
.chart-section { margin: var(--sp-9) 0 0; }
.section-head { margin: 0 0 var(--sp-6); }
.section-head h2 {
  margin: 0 0 4px; font: var(--text-h2); letter-spacing: -.01em;
  padding-left: 12px; border-left: 3px solid var(--line-strong);
}
.section-head .meta { margin: 0; padding-left: 12px; }
.empty { text-align: center; color: var(--muted); padding: 36px 12px; }
.empty p { margin: 0 0 14px; }
.js-warn {
  background: var(--panel2); border: 1px solid var(--line); padding: 10px 14px;
  border-radius: 10px; color: var(--muted); font-size: .85rem;
}

/* 検索・並び替え・絞り込みバー（全件ページ）: 固定ヘッダーの下端に貼り付ける。
   ガラス調（backdrop-filter）は固定ヘッダーのみに限定し、ここは不透明な背景にする。 */
.filter-bar {
  position: sticky; top: var(--header-h); z-index: 5;
  background: var(--surface-1);
  padding: var(--sp-3) 0 var(--sp-3); margin-bottom: var(--sp-1); border-bottom: 1px solid var(--line);
}
#q {
  width: 100%; padding: 11px 14px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--panel); color: var(--text-strong); font-size: 1rem; margin-bottom: 8px;
  transition: border-color .15s ease;
}
#q:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.filter-toggle {
  display: none; cursor: pointer; padding: 8px 2px; margin-top: 2px;
  font-weight: 700; font-size: .84rem; color: var(--text-strong);
}
.filter-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding-top: 6px; }
.filter-row select {
  padding: 7px 10px; border-radius: 6px; border: 1px solid var(--line);
  background: var(--panel2); color: var(--text); font-size: .82rem;
  transition: border-color .15s ease;
}
.filter-row select:hover { border-color: var(--line-strong); }
.filter-row select:focus-visible { border-color: var(--accent); outline: 2px solid var(--accent); outline-offset: 1px; }
/* チェックボックスはピル状にして、選択状態が一目で分かるようにする（色ではなく背景の濃さで示す） */
.chk {
  display: inline-flex; align-items: center; gap: 6px; font-size: .8rem; color: var(--muted);
  padding: 5px 10px 5px 8px; border-radius: 999px; border: 1px solid var(--line); background: var(--panel2);
  transition: border-color .15s ease, color .15s ease, background .15s ease;
}
.chk input { accent-color: var(--text-strong); }
.chk:has(input:checked) {
  color: var(--text-strong); border-color: var(--line-strong); background: var(--surface-3);
}
.btn-text { background: none; border: none; color: var(--text-strong); text-decoration: underline; font-size: .8rem; cursor: pointer; padding: 4px 0; }
.result-count { margin: 8px 0 0; font-size: .78rem; color: var(--muted); }
@media (max-width: 719px) {
  /* 画面が狭いうちは絞り込み行を畳んでおき、検索窓の直下を圧迫しないようにする */
  .filter-toggle { display: block; }
}

/* ゲーム一覧（トップ・全件ページ共通）: Steamのセールチャートのような固定カラムの
   表構造。全行・全セクションで「順位・画像・タイトル・買い時・割引・価格」が同じ
   位置に揃うグリッドにする（デスクトップ）。判定の強調は色数を増やさず、
   過去最安に到達している行（new_low/tied_low）だけを左端の細いアクセントバーで
   示す方式に一本化し、それ以外の行の明るさ・文字色は全行で統一する（above_lowを沈める処理はしない）。 */
.list {
  list-style: none; margin: var(--sp-5) 0; padding: 0;
  background: var(--surface-1); border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
}

/* 列見出し行。一覧の先頭に1回だけ出し、各行での説明を省く。
   帯状の背景は使わず、細い罫線と小さめの文字だけで控えめに示す（安っぽく見えないように）。
   モバイルは行そのものが3要素構成に変わるため見出しごと隠す。 */
.list-head { display: none; }
@media (min-width: 720px) {
  .list-head {
    display: grid; grid-template-columns: 40px 128px minmax(140px, 1fr) 170px 188px;
    column-gap: var(--sp-4); align-items: center;
    padding: 5px var(--sp-8); border-bottom: 1px solid var(--line);
  }
  .list-head span { font: var(--text-micro); color: var(--muted); letter-spacing: .03em; }
  .lh-amount { text-align: right; }
}

/* 行1件。スマホは [画像][タイトル＋買い時][価格] の3要素・2段組に圧縮する。
   行の高さ（min-height）と縦paddingは余白拡張の対象外（縦に間延びさせないため）。
   横paddingだけ広く取る。 */
.row {
  position: relative;
  display: grid; grid-template-columns: 56px 1fr auto;
  grid-template-areas: "thumb title amount" "thumb timing amount";
  align-items: center; column-gap: var(--sp-3); row-gap: 1px;
  padding: 6px var(--sp-5); border-bottom: 1px solid var(--line);
  cursor: pointer; transition: background .16s ease; min-height: 56px;
}
/* 行全体をクリック可能にする（タイトルのリンクを行全体に拡張する「stretched link」）。
   行内の個別リンク（もしあれば）は position:relative + z-index で上に出す。 */
.row-title a.stretched-link::after { content: ""; position: absolute; inset: 0; z-index: 1; }
.list .row:last-child { border-bottom: none; }
.list .row[hidden] { display: none; }
/* 左端のアクセントバー: 過去最安に到達している行(new_low/tied_low)だけに出す。行の背景・文字色は
   判定によらず全行で統一し、バー1本だけで買い時を目立たせる。ホバーは無彩色
   （アクセントは購入等のCTA専用のため、単なるホバー表示には使わない）。 */
.row::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background: transparent; transition: background .12s ease;
}
.row:hover { background: var(--surface-2); }
.row:hover::before { background: var(--line-strong); }
.row-v-record::before { background: var(--good); }
.row-v-record:hover::before { background: var(--good); }

@media (min-width: 720px) {
  .row {
    grid-template-columns: 40px 128px minmax(140px, 1fr) 170px 188px;
    grid-template-areas: "rank thumb title timing amount";
    align-items: center; column-gap: var(--sp-4); row-gap: 0;
    padding: 6px var(--sp-8); min-height: 84px;
  }
}

.row-rank {
  grid-area: rank; display: none;
  font-size: 1rem; font-weight: 800; color: var(--muted);
  text-align: center; font-variant-numeric: tabular-nums;
}
@media (min-width: 720px) { .row-rank { display: block; } }
.list.ranked li:nth-child(-n+3) .row-rank { color: var(--text-strong); }

/* お気に入り★トグル。サムネの右上に重ねて表示する（stretched-linkより手前）。
   常時表示すると主要4情報の主張が弱まるため、ホバー/キーボードフォーカス時のみ現れる
   （既にお気に入り済みのものは、行を探せるよう常時表示のままにする）。 */
.row-thumb {
  grid-area: thumb; position: relative; display: block; width: 100%; max-width: 56px;
  aspect-ratio: 16 / 9; border-radius: 6px; overflow: hidden; background: var(--surface-2);
}
@media (min-width: 720px) { .row-thumb { max-width: 128px; aspect-ratio: 128 / 72; } }
.row-thumb-img { width: 100%; height: 100%; object-fit: cover; display: block; }
.fav-btn {
  position: absolute; top: 2px; right: 2px; z-index: 2;
  width: 22px; height: 22px; border-radius: 50%; border: 1px solid rgba(255,255,255,.4);
  background: rgba(9,11,16,.72); color: #fff; font-size: .76rem; line-height: 1;
  cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 0;
  opacity: 0; text-decoration: none;
  transition: opacity .15s ease, color .15s ease, border-color .15s ease, background .15s ease;
}
@media (min-width: 720px) { .fav-btn { top: 4px; right: 4px; width: 26px; height: 26px; font-size: .85rem; } }
.row:hover .fav-btn, .row:focus-within .fav-btn, .fav-btn:focus-visible, .fav-btn.is-fav { opacity: 1; }
/* サムネ画像に重なる常設オーバーレイのため、ページのテーマトークンではなく
   固定の白黒濃淡で状態を示す（色は使わず、塗り有無で「お気に入り済み」を表す）。 */
.fav-btn:hover { border-color: #fff; }
.fav-btn.is-fav { background: #fff; color: #14171c; border-color: #fff; }
/* お気に入り追加の瞬間だけ弾むマイクロインタラクション（操作フィードバックとして唯一残す動き） */
.fav-btn.fav-pop { animation: favPop .38s ease; }
@keyframes favPop {
  0% { transform: scale(1); }
  35% { transform: scale(1.35); }
  65% { transform: scale(.92); }
  100% { transform: scale(1); }
}
/* 詳細ページ用の小さいアイコンボタン（タイトル横）。全幅ボタンは廃止した。 */
.fav-btn.icon {
  position: static; opacity: 1; flex-shrink: 0;
  width: 36px; height: 36px; border-radius: 50%;
  border: 1px solid var(--line-strong); background: var(--surface-1); color: var(--muted);
  font-size: 1rem;
}
.fav-btn.icon:hover { color: var(--text-strong); border-color: var(--text-strong); }
.fav-btn.icon.is-fav { color: var(--text-strong); background: var(--surface-2); border-color: var(--text-strong); }

/* タイトル。行の高さを一定に保つため常に1行（超過分は省略記号）。 */
.row-title {
  grid-area: title; margin: 0; min-width: 0;
  font: var(--text-title); font-size: .92rem; color: var(--text-strong);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
@media (min-width: 720px) { .row-title { font-size: 1rem; } }
.row-title a { color: inherit; text-decoration: none; }
.row-title a:hover { text-decoration: underline; }

/* 買い時列: バッジを上、補足の一言をその直下の小さい文字で積む（中央に浮遊させない）。
   スマホは高さを抑えるため横並びの1行にする。 */
.row-timing-col {
  grid-area: timing; display: flex; flex-direction: row; flex-wrap: wrap;
  align-items: center; gap: 5px; min-width: 0; overflow: hidden;
}
@media (min-width: 720px) {
  .row-timing-col { flex-direction: column; align-items: flex-start; justify-content: center; gap: 3px; }
}
.row-timing { font: var(--text-meta); color: var(--muted); white-space: nowrap; }
/* 過去最安に到達（new_low/tied_low）のみ緑（色は意味だけに使うルールに合わせ、
   最安値に近い(near_low)は太字のみで示し色は使わない） */
.row-timing.save { color: var(--good); font-weight: 700; }
.row-other-store { font: var(--text-micro); color: var(--muted); opacity: .85; }
.tag-expiry.urgent {
  display: inline-flex; align-items: center; gap: 3px; color: var(--bad); font-weight: 600; white-space: nowrap;
}
.icon-clock { flex-shrink: 0; }

/* 割引・価格。右揃え＋tabular-numsで桁を揃える。固定幅にして全行・列見出しと
   ぴったり揃える（割引70px・価格110px）。 */
/* 価格ブロック: 定価(取り消し線)＋割引バッジを上段に、現在価格を下段の大きな数字で
   積む。3つを縦に近接させて「セットで1つの情報」に見せる（割引率だけを離れた
   列に置かない）。現在価格が最も大きく・太く＝主役、定価は小さく添える。 */
.row-amount {
  grid-area: amount; display: flex; flex-direction: column;
  align-items: flex-end; justify-content: center; gap: 2px;
}
.row-amount-top { display: flex; align-items: center; gap: 6px; }
/* 「これが定価だと分からない」対策として、取消線に加え小さな「定価」ラベルを前置する。
   ラベル自体には取消線を付けない（価格の部分だけに掛かるよう内側のspanに限定する）。 */
.row-regular {
  font-size: .76rem; color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap;
}
.row-regular-label { margin-right: 3px; }
.row-regular-price { text-decoration: line-through; text-decoration-color: var(--line-strong); }
/* 割引率は色ではなく無彩色の塗りバッジで強調する（割引率専用の意味色は割り当てない） */
.row-cut {
  display: inline-flex; align-items: center; padding: 1px 6px; border-radius: 4px;
  background: var(--surface-3); border: 1px solid var(--line-strong);
  font: var(--text-label); color: var(--text-strong); font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.row-cur {
  font: var(--text-price); font-size: 1.05rem; color: var(--text-strong); letter-spacing: -.01em;
  font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap;
}
@media (min-width: 720px) {
  .row-cur { font-size: 1.3rem; }
}

.see-all { text-align: center; margin: 22px 0 8px; }
.load-more-wrap { text-align: center; margin: 20px 0; }
.btn-outline {
  display: inline-block; padding: 10px 22px; border: 1px solid var(--line-strong); color: var(--text-strong);
  border-radius: 8px; font-weight: 700; background: none; cursor: pointer; font-size: .9rem;
  text-decoration: none;
  transition: background .15s ease, border-color .15s ease, transform .15s ease;
}
.btn-outline:hover { background: var(--surface-2); border-color: var(--muted); }
.btn-outline[hidden] { display: none; }

.back-to-top {
  position: fixed; right: 18px; bottom: 18px; z-index: 20;
  display: flex; align-items: center; justify-content: center;
  width: 44px; height: 44px; border-radius: 50%;
  background: var(--panel2); border: 1px solid var(--line); color: var(--text-strong);
  cursor: pointer; opacity: 0; pointer-events: none; transform: translateY(6px);
  box-shadow: 0 2px 8px -2px rgba(0,0,0,.4);
  transition: opacity .18s ease, transform .18s ease, background .15s ease;
}
.back-to-top.show { opacity: 1; pointer-events: auto; transform: translateY(0); }
.back-to-top.show:hover { background: var(--surface-3); }

/* 詳細ページ */
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; margin: 16px 0; }
th, td { text-align: left; padding: 10px 20px 10px 0; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-size: .8rem; font-weight: 600; }
.verdict-detail { margin: 8px 0 0; font-size: 1.05rem; font-weight: 700; color: var(--text-strong); }
.jp { color: var(--muted); font-size: .75rem; font-weight: 700; white-space: nowrap; }
.tag-genre {
  display: inline-block; padding: 1px 7px; border-radius: 4px;
  background: var(--panel2); color: var(--muted); font-size: .7rem; white-space: nowrap;
  border: 1px solid var(--line); position: relative; z-index: 2; text-decoration: none;
}
a.tag-genre:hover { color: var(--text-strong); border-color: var(--line-strong); }
/* 画像が取得できないときのプレースホルダー: 真っ黒/無地にせず、ゲーム名で判別できるようにする */
.img-ph {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 4px; padding: 6px; text-align: center;
  background: var(--surface-2); color: var(--muted);
}
.img-ph-icon { font-size: 1.3em; line-height: 1; opacity: .7; }
.img-ph-title {
  font-size: .72rem; line-height: 1.25; font-weight: 600; color: var(--text);
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.row-thumb-img.img-ph .img-ph-icon { font-size: 1.1em; }
.row-thumb-img.img-ph .img-ph-title { font-size: .66rem; -webkit-line-clamp: 2; }

.hero-banner {
  width: 100%; max-width: 100%; height: auto; aspect-ratio: 600 / 344; display: block;
  border-radius: 12px; background: var(--panel2); object-fit: cover;
}
.hero-banner.img-ph { height: auto; aspect-ratio: 600 / 344; }
.hero-banner.img-ph .img-ph-icon { font-size: 2.6rem; }
.hero-banner.img-ph .img-ph-title { font-size: 1.1rem; max-width: 80%; }
/* セール終了日は「本日〜3日以内」のときだけ赤にする（それ以外は無彩色）。
   「未定」も含め、単独の浮遊段落ではなく価格ブロック内の1行として表示する。 */
.expiry-detail {
  display: flex; align-items: center; gap: 5px;
  margin: 6px 0 0; font-size: .9rem; font-weight: 600; color: var(--muted);
}
.expiry-detail.urgent { color: var(--bad); }
.expiry-detail .icon-clock { flex-shrink: 0; }
.badge {
  display: inline-flex; align-items: center; padding: 2px 9px; border-radius: 5px;
  font-size: .78rem; font-weight: 700; border: 1px solid transparent; cursor: help;
  white-space: nowrap;
}
/* 一覧行の判定バッジはサイトの核となる情報なので、他の用途（凡例・ヒーロー枠）より
   一段大きく太くして主役級に見せる */
.row-timing-col .badge { padding: 3px 9px; font-size: .78rem; font-weight: 800; border-radius: 6px; }
.legend-list {
  list-style: none; margin: 14px 0; padding: 0; display: flex; flex-direction: column; gap: 10px;
}
.legend-row {
  display: flex; align-items: center; gap: 12px; padding: 16px 20px;
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
}
.legend-row .badge { flex-shrink: 0; }
.legend-desc { color: var(--text); font-size: .86rem; }

/* Steam大型セールカレンダー（about.html）。開催中は不透明な塗りバッジ+白文字で
   目立たせ、それ以外は無彩色の中立バッジで「あと◯日」を示す。 */
.sales-cal-list { list-style: none; margin: 14px 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.sales-cal-item {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 12px 16px; background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
}
.sales-cal-name { font-weight: 700; color: var(--text-strong); flex: 1 1 auto; min-width: 120px; }
.sales-cal-dates { color: var(--muted); font-size: .85rem; font-variant-numeric: tabular-nums; }
.sales-cal-status {
  display: inline-flex; align-items: center; padding: 2px 9px; border-radius: 5px;
  font-size: .78rem; font-weight: 700; background: var(--surface-2); border: 1px solid var(--line);
  color: var(--text-strong); white-space: nowrap;
}
.sales-cal-status.is-live { background: var(--good-solid-bg); color: var(--good-solid-fg); border-color: var(--good-solid-bg); }
.sales-cal-item.is-live { border-color: var(--good-solid-bg); }
/* 判定バッジの色は過去最安に到達（new_low/tied_low）だけに使う。それ以外（最安値に近い/
   最安値より高い/判定不可）は全て同じ無彩色チップにし、太字の有無だけで軽い差を付ける
   （色は意味だけに使うルールをバッジにも適用する）。 */
.v-record { color: var(--good-solid-fg); background: var(--good-solid-bg); border-color: var(--good-solid-bg); font-weight: 800; }
.v-near, .v-watch, .v-unknown {
  color: var(--text-strong); background: var(--surface-2); border-color: var(--line);
}
.v-near { font-weight: 800; }
.v-watch, .v-unknown { color: var(--muted); font-weight: 700; }
.crumbs { margin-bottom: 12px; font-size: .85rem; }
.game-head-tags { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: var(--sp-3) 0 0; }

/* ファーストビュー: 画像を縮小し、右側にタイトル・価格・買い時・購入ボタンを
   横並びで配置してスクロールなしで見えるようにする。 */
.game-hero {
  display: flex; flex-direction: column; gap: var(--sp-5); margin: 0 0 var(--sp-7);
}
@media (min-width: 720px) {
  .game-hero { flex-direction: row; align-items: flex-start; gap: var(--sp-7); }
  .game-hero-media { flex: 0 0 320px; max-width: 320px; }
  .game-hero-info { flex: 1 1 auto; min-width: 0; }
}
.game-hero-title-row { display: flex; align-items: flex-start; gap: var(--sp-3); }
.game-hero-title-row h1 { margin: 0; flex: 1 1 auto; font-size: 1.5rem; line-height: 1.3; }
.price-current { margin: var(--sp-4) 0 0; }
.price-current-value {
  display: flex; align-items: baseline; gap: var(--sp-3); flex-wrap: wrap;
  font-size: 2.5rem; font-weight: 800; color: var(--text-strong);
  font-variant-numeric: tabular-nums; letter-spacing: -.01em;
}
.price-current-meta { margin-top: var(--sp-2); display: flex; flex-direction: column; gap: 4px; }
.store-badge {
  display: inline-flex; align-items: center; align-self: flex-start; padding: 2px 9px;
  border-radius: 5px; font-size: .78rem; font-weight: 700; border: 1px solid transparent;
  white-space: nowrap;
}
.buy {
  display: inline-block; margin: var(--sp-4) 0 0; padding: 12px 22px;
  background: var(--accent); color: var(--on-accent); border-radius: 8px; font-weight: 700;
  text-decoration: none;
  transition: opacity .15s ease;
}
.buy:hover { opacity: .9; }

/* 過去最安・定価はページ下部の控えめな2枚カードにする（現在価格はヒーローに直書き）。 */
.price-cards-secondary { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-5); margin: 0 0 var(--sp-6); }
@media (max-width: 640px) { .price-cards-secondary { grid-template-columns: 1fr; } }
.stat-card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: var(--sp-6);
  transition: border-color .16s ease, background .16s ease;
}
.stat-card:hover { border-color: var(--line-strong); background: var(--surface-2); }
.card-label { color: var(--muted); font-size: .8rem; }
.card-value { font-size: 1.4rem; font-weight: 700; color: var(--text-strong); font-variant-numeric: tabular-nums; }
.card-cut {
  font-size: 1rem; font-weight: 800; color: var(--text-strong);
  background: var(--surface-2); border: 1px solid var(--line-strong);
  padding: 2px 8px; border-radius: 5px;
}
.card-sub { color: var(--muted); font-size: .8rem; min-height: 1.2em; }
.card-sub.save { color: var(--good); font-weight: 600; }

/* 価格履歴チャート: 「価格は変わるまで一定」を正しく表現するステップ（階段）
   グラフ。斜め線で結ぶ折れ線は徐々に値下がりしたように誤読させるため使わない。
   動的な文字ラベル（現在/最安/定価）はSVGの外（.chart-legend）に出し、線や
   グリッドと重ならないようにする。観測点が多いほど.chart自体の実ピクセル幅を
   広げて.table-wrapで横スクロールさせるため、幅は100%固定にしない。 */
.chart { display: block; width: 100%; height: auto; background: var(--panel); border-radius: 10px; padding: 8px; }
.grid-line { stroke: var(--line); stroke-width: 1; }
.axis-y-label, .axis-x-label { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.price-area { fill: var(--text-strong); opacity: .06; stroke: none; }
.price-line { stroke: var(--text-strong); stroke-width: 2; fill: none; }
.pt { fill: var(--muted); opacity: .7; }
.pt-low { fill: var(--good); stroke: var(--bg); stroke-width: 1.5; }
.pt-current { fill: var(--text-strong); stroke: var(--bg); stroke-width: 2; }

/* グラフ上部の凡例（SVG外のHTML）。現在/最安の色はドットの色と揃える。 */
.chart-legend { display: flex; flex-wrap: wrap; gap: var(--sp-5); margin: 0 0 8px; font-size: .82rem; color: var(--text-strong); font-weight: 600; }
.chart-legend-item { display: inline-flex; align-items: center; gap: 5px; }
.chart-legend-swatch { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); display: inline-block; }
.chart-legend-swatch.cur { background: var(--text-strong); }
.chart-legend-swatch.low { background: var(--good); }

/* 期間切り替え（1年/全期間）。トグルは中立チップ2つで、選択中だけ濃く塗る
   （色は意味だけに使うルールに沿い、緑等の意味色は使わない）。 */
.chart-period-toggle { display: inline-flex; gap: 6px; margin: 0 0 10px; }
.chart-period-btn {
  padding: 4px 12px; border-radius: 999px; border: 1px solid var(--line); background: var(--panel2);
  color: var(--muted); font-size: .8rem; font-weight: 700; cursor: pointer;
  transition: background .15s ease, color .15s ease, border-color .15s ease;
}
.chart-period-btn:hover { border-color: var(--line-strong); }
.chart-period-btn[aria-pressed="true"] { background: var(--surface-3); color: var(--text-strong); border-color: var(--line-strong); }

/* 価格履歴テーブルの「もっと見る」展開（JS不要のdetails/summary）。 */
.hist-more summary { cursor: pointer; color: var(--text-strong); font-weight: 700; font-size: .85rem; margin: 4px 0 8px; }
.hist-more table { margin-top: 0; }

.muted { color: var(--muted); }
"""

# ---------------------------------------------------------------------------
# 全件ページの検索・並び替え・絞り込み（クライアントサイドJS）
# ---------------------------------------------------------------------------
DEALS_JS = """
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
  var shopSel = document.getElementById("f-shop");
  var jpChk = document.getElementById("f-jp");
  var onsaleChk = document.getElementById("f-onsale");
  var favChk = document.getElementById("f-fav");
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
    expiry: function (a, b) { return (+a.dataset.expiryTs) - (+b.dataset.expiryTs); },
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
    if (p.has("shop")) shopSel.value = p.get("shop");
    if (p.has("jp")) jpChk.checked = p.get("jp") === "1";
    if (p.has("onsale")) onsaleChk.checked = p.get("onsale") === "1";
  }

  function syncUrl() {
    var p = new URLSearchParams();
    if (qInput.value) p.set("q", qInput.value);
    if (sortSel.value !== "reviews_desc") p.set("sort", sortSel.value);
    if (cutSel.value !== "0") p.set("cut", cutSel.value);
    if (priceSel.value) p.set("price", priceSel.value);
    if (reviewsSel.value !== "0") p.set("reviews", reviewsSel.value);
    if (genreSel.value) p.set("genre", genreSel.value);
    if (shopSel && shopSel.value) p.set("shop", shopSel.value);
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
    var shop = shopSel ? shopSel.value : "";
    var jpOnly = jpChk.checked;
    var onsaleOnly = onsaleChk.checked;
    var favOnly = favChk && favChk.checked;
    var favSet = (favOnly && window.SaleTrackerFavorites) ? window.SaleTrackerFavorites.getAll() : null;

    var matched = cards.filter(function (c) {
      if (q && c.dataset.title.indexOf(q) === -1) return false;
      if (minCut && (+c.dataset.cut) < minCut) return false;
      if (bucket && !priceInBucket(+c.dataset.price, bucket)) return false;
      if (minReviews && (+c.dataset.reviews) < minReviews) return false;
      if (genre && (c.dataset.genres || "").split(",").indexOf(genre) === -1) return false;
      if (shop && c.dataset.shop !== shop) return false;
      if (jpOnly && c.dataset.jp !== "1") return false;
      if (onsaleOnly && c.dataset.onsale !== "1") return false;
      if (favSet && favSet.indexOf(c.dataset.slug) === -1) return false;
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
  [sortSel, cutSel, priceSel, reviewsSel, genreSel, shopSel, jpChk, onsaleChk, favChk].forEach(function (el) {
    if (el) el.addEventListener("change", onFilterChange);
  });
  loadMoreBtn.addEventListener("click", function () { page += 1; render(); });
  document.addEventListener("favorites:change", function () {
    if (favChk && favChk.checked) render();
  });

  function resetAll() {
    qInput.value = ""; sortSel.value = "reviews_desc"; cutSel.value = "0";
    priceSel.value = ""; reviewsSel.value = "0"; genreSel.value = "";
    if (shopSel) shopSel.value = "";
    jpChk.checked = false; onsaleChk.checked = false;
    if (favChk) favChk.checked = false;
    onFilterChange();
  }
  if (resetBtn) resetBtn.addEventListener("click", resetAll);
  if (resetBtnEmpty) resetBtnEmpty.addEventListener("click", resetAll);

  parseParams();
  render();
})();
"""

# ---------------------------------------------------------------------------
# お気に入り（localStorage）: 全ページ共通で動く独立スクリプト。★ボタンのトグルと
# 「お気に入りのみ」フィルタ（deals.js側）の両方から使う。
# ---------------------------------------------------------------------------
FAVORITES_JS = """
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
"""


def main():
    if not (DATA_DIR / "latest.json").exists():
        raise SystemExit("data/latest.json がありません。先に fetch_data.py を実行してください。")

    latest = json.loads((DATA_DIR / "latest.json").read_text(encoding="utf-8"))

    # 買い時判定を表示時に再計算する。fetch を回さなくても VERDICT_RULES の
    # 変更（しきい値やラベル）が即サイトへ反映される。judge は純粋関数で軽い。
    for g in latest.get("games", []):
        cur_amt = (g.get("current") or {}).get("amount")
        low_amt = (g.get("lowest") or {}).get("amount")
        g["verdict"] = verdict.judge(cur_amt, low_amt)

    # 出力ディレクトリ準備
    (PUBLIC_DIR / "games").mkdir(parents=True, exist_ok=True)
    (PUBLIC_DIR / "assets").mkdir(parents=True, exist_ok=True)

    # CSS/JS: 手書きが assets/ にあればそれを使い、無ければ内蔵を書き出す
    custom_css = ASSETS_SRC / "style.css"
    if custom_css.exists():
        shutil.copyfile(custom_css, PUBLIC_DIR / "assets" / "style.css")
    else:
        (PUBLIC_DIR / "assets" / "style.css").write_text(CSS, encoding="utf-8")

    custom_js = ASSETS_SRC / "deals.js"
    if custom_js.exists():
        shutil.copyfile(custom_js, PUBLIC_DIR / "assets" / "deals.js")
    else:
        (PUBLIC_DIR / "assets" / "deals.js").write_text(DEALS_JS, encoding="utf-8")

    custom_fav_js = ASSETS_SRC / "favorites.js"
    if custom_fav_js.exists():
        shutil.copyfile(custom_fav_js, PUBLIC_DIR / "assets" / "favorites.js")
    else:
        (PUBLIC_DIR / "assets" / "favorites.js").write_text(FAVORITES_JS, encoding="utf-8")

    build_featured(latest)
    build_all(latest)
    build_about(latest)
    build_robots()
    genre_slugs = build_genre_pages(latest)
    build_sitemap(latest, genre_slugs)
    for game in latest.get("games", []):
        build_game_page(game, latest)
    removed = prune_orphan_game_pages(latest)

    print(f"生成完了: public/index.html, public/all.html, public/about.html, "
          f"public/robots.txt, public/sitemap.xml ほか {len(latest.get('games', []))} ページ"
          f"（ジャンルページ {len(genre_slugs)} 件・孤立ページ削除: {removed}件）")


def prune_orphan_game_pages(latest):
    """latest.json に存在しなくなったゲームの public/games/<slug>.html を削除する。

    対象から外れた（セール終了・掲載終了などで latest.json から消えた）ゲームの
    詳細ページが public/games/ に残り続けないようにするための後始末。
    """
    valid_slugs = {g["slug"] for g in latest.get("games", [])}
    games_dir = PUBLIC_DIR / "games"
    if not games_dir.exists():
        return 0
    removed = 0
    for path in games_dir.glob("*.html"):
        if path.stem not in valid_slugs:
            path.unlink()
            removed += 1
    return removed


if __name__ == "__main__":
    main()
