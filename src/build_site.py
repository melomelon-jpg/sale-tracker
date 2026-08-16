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
import html
import json
import math
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verdict  # noqa: E402  買い時判定の純粋関数（表示時に再計算して最新ルールを反映）

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
SITE_TAGLINE = "Steamセールの買い時を毎朝チェック"
# 独自ドメインに移行する際はここだけ書き換えればよい（OGP/canonical/sitemap.xmlで使用）。
SITE_URL = "https://sale-tracker-368.pages.dev"
SITE_DESCRIPTION = (
    "Steamのセール情報を毎日自動収集し、過去最安値と比較した「買い時」判定バッジで"
    "今狙うべきセールが一目でわかる非公式の価格追跡サイト。"
)

# 判定コード -> CSSクラス（色分け用）
VERDICT_CLASS = {
    "record_low": "v-record",
    "near_low": "v-near",
    "decent": "v-decent",
    "watch": "v-watch",
    "high": "v-high",
    "unknown": "v-unknown",
}


def esc(s):
    return html.escape(str(s if s is not None else ""))


def display_title(g):
    """表示用タイトル。Steam(cc=jp)の日本語名キャッシュがあればそれを、無ければ英語名を使う。"""
    return g.get("title_jp") or g.get("title") or ""


def yen(amount):
    if amount is None:
        return "—"
    return f"¥{amount:,.0f}"


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


def buy_timing_text(current_amount, lowest_amount):
    """現在価格と過去最安値の差を金額ベースの一言で表す（買い時バッジの補足）。

    現在価格が過去最安値以下（= record_low バッジ）のときは、バッジの文言と
    意味が重複するためここでは何も返さない（呼び出し側もバッジと二重表示しない）。
    現在価格が過去最安値より高いときだけ、その差額を返す。
    """
    if current_amount is None or lowest_amount is None:
        return ""
    diff = current_amount - lowest_amount
    if diff > 0:
        return f"過去最安より {yen(diff)} 高い"
    return ""


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

    canonical_url = f"{SITE_URL}/{path}" if path else f"{SITE_URL}/"
    desc = description or SITE_DESCRIPTION
    og_t = og_title or full_title
    og_img = og_image or f"{SITE_URL}/og-default.png"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#11151c">
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
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@500;700;900&display=swap">
<link rel="stylesheet" href="{rel_root}/assets/style.css">
</head>
<body>
<header class="site-header">
  <div class="header-inner">
    <a class="brand" href="{rel_root}/index.html">🏷️ <span>{esc(SITE_NAME)}</span></a>
    <nav class="site-nav">
      <a href="{rel_root}/index.html" class="{nav_featured}">注目のセール</a>
      <a href="{rel_root}/all.html" class="{nav_all}">すべてのセール</a>
      <a href="{rel_root}/about.html" class="{nav_about}">このサイトについて</a>
    </nav>
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
<!-- Cloudflare Web Analytics --><script type='module' src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{{"token": "c8c81e274f13457d80d3e8484503fdf2"}}'></script><!-- End Cloudflare Web Analytics -->
<script src="{rel_root}/assets/deals.js" defer></script>
</body>
</html>
"""


def _verdict_thresholds():
    """verdict.VERDICT_RULES から code -> max_gap(%) の辞書を作る（凡例の数値を1箇所に集約）。"""
    return {r["code"]: r["max_gap"] for r in verdict.VERDICT_RULES}


def verdict_desc(code):
    """判定バッジの根拠（過去最安との差のしきい値）を説明する短文。ツールチップ/凡例で共用。"""
    t = _verdict_thresholds()
    texts = {
        "record_low": "現在価格が過去最安値と同じか、それを更新中",
        "near_low": f"過去最安値との差が{int(t['near_low'] * 100)}%以内",
        "decent": f"過去最安値との差が{int(t['decent'] * 100)}%以内",
        "watch": f"過去最安値との差が{int(t['watch'] * 100)}%以内",
        "high": f"過去最安値より{int(t['watch'] * 100)}%を超えて高い",
        "unknown": "価格データ不足のため判定できません",
    }
    return texts.get(code, "")


def verdict_badge(v):
    cls = VERDICT_CLASS.get(v.get("code"), "v-unknown")
    desc = verdict_desc(v.get("code"))
    return f'<span class="badge {cls}" title="{esc(desc)}">{esc(v.get("label"))}</span>'


def jp_mark(jp):
    """日本語対応の表示。True=🇯🇵 / False=日本語なし / None(不明)=何も出さない。"""
    if jp is True:
        return '<span class="jp" title="日本語対応">🇯🇵 日本語</span>'
    if jp is False:
        return '<span class="jp-no" title="日本語表示なし">日本語なし</span>'
    return ""


def best_asset_url(assets, sizes):
    """assets から指定サイズ優先順で最初に見つかったURLを返す。無ければ空文字。"""
    if isinstance(assets, dict):
        for s in sizes:
            if assets.get(s):
                return assets[s]
    return ""


def game_image(assets, sizes, cls, alt, dims=None):
    """assets から最初に見つかったサイズの画像を <img> で返す。

    無ければ絵文字プレースホルダー（同じクラス）でレイアウト崩れを防ぐ。
    画像はITADのURLを直接参照する（自前保存はしない）。
    dims=(width, height) を渡すとwidth/height属性を付け、画像読み込み前でも
    ブラウザがアスペクト比を確保できるようにする（レイアウトシフト防止）。
    ITADのbanner系アセットは実寸约600x344（比率約1.74:1）で統一されている。
    """
    url = best_asset_url(assets, sizes)
    if url:
        size_attr = f'width="{dims[0]}" height="{dims[1]}" ' if dims else ""
        return (f'<img class="{cls}" src="{esc(url)}" alt="{esc(alt)}" {size_attr}'
                f'loading="lazy" referrerpolicy="no-referrer">')
    # 画像が無いときはタイトルを表示するプレースホルダー（何のゲームか分かるように）
    return (f'<div class="{cls} img-ph" role="img" aria-label="{esc(alt)}">'
            f'<span class="img-ph-icon">🎮</span><span class="img-ph-title">{esc(alt)}</span></div>')


def expiry_info(iso, base=None):
    """セール終了日時を表示用に整形。expiry が無ければ None。

    戻り値: {"date_full","date_short","days_left","urgent"} | None
    urgent は残り3日以内（当日含む）で True。
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
VERDICT_ORDER = {"record_low": 0, "near_low": 1, "decent": 2, "watch": 3, "high": 4, "unknown": 5}

FEATURED_COUNT = 30


# ---------------------------------------------------------------------------
# ゲームカード（トップページ・全件ページ共通）
# ---------------------------------------------------------------------------
def game_row(g):
    """1ゲーム分の一覧行HTML。data-* 属性は all.html の検索/並び替え/絞り込みJS用。

    情報密度の高い横並びの行（Steamのストア一覧に近い見た目）。
    最も重要な「現在価格」と「買い時かどうか」を右側の専用カラムにまとめ、
    視線の流れ（サムネ→タイトル→判定→価格）が一定になるようにする。
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
                        "row-thumb-img", title, dims=(400, 230))

    exp = expiry_info(cur.get("expiry"))
    expiry_tag = ""
    if exp and exp["urgent"]:
        expiry_tag = f'<span class="tag-expiry urgent">⏳ {esc(days_left_text(exp["days_left"]))}</span>'

    timing_txt = buy_timing_text(cur_amt, low_amt)
    timing_cls = "row-timing"
    if cur_amt is not None and low_amt is not None and cur_amt <= low_amt:
        timing_cls += " save"
    low_when = relative_date_jp(low.get("date"))

    cut_html = f'<span class="row-cut">-{disc}%</span>' if (on_sale and disc) else ""
    reg_html = ""
    if reg_amt is not None and cur_amt is not None and reg_amt > cur_amt:
        reg_html = f'<span class="row-reg">{yen(reg_amt)}</span>'

    # JS用データ属性。値が不明なものは昇順ソートで末尾に回るよう大きな値にする。
    d_price = cur_amt if cur_amt is not None else 999999999
    d_cut = disc if disc else 0
    d_rank = VERDICT_ORDER.get(v.get("code"), 9)
    d_expiry = exp["days_left"] if (exp and exp.get("days_left") is not None) else 999999
    d_jp = "1" if g.get("jp_support") is True else ("0" if g.get("jp_support") is False else "")
    d_onsale = "1" if on_sale else "0"
    d_reviews = g.get("review_count") if g.get("review_count") is not None else 0
    d_genres = esc(",".join(g.get("genres") or []))
    title_norm = esc(f"{g.get('title') or ''} {g.get('title_jp') or ''}".strip().lower())

    href = f"games/{esc(g['slug'])}.html"
    genre_tags = "".join(f'<span class="tag-genre">{esc(genre)}</span>' for genre in (g.get("genres") or [])[:2])

    return f"""
<li class="row" data-title="{title_norm}" data-cut="{d_cut}" data-price="{d_price}"
  data-verdict-rank="{d_rank}" data-expiry-days="{d_expiry}" data-jp="{d_jp}" data-onsale="{d_onsale}"
  data-reviews="{d_reviews}" data-genres="{d_genres}">
  <a class="row-thumb" href="{href}" tabindex="-1" aria-hidden="true">{thumb}</a>
  <div class="row-main">
    <h3 class="row-title"><a href="{href}">{esc(title)}</a></h3>
    <div class="row-tags">{jp_mark(g.get('jp_support'))}{genre_tags}{expiry_tag}</div>
  </div>
  <div class="row-status">
    {verdict_badge(v)}
    {f'<span class="{timing_cls}">{esc(timing_txt)}</span>' if timing_txt else ''}
  </div>
  <div class="row-price">
    <div class="row-price-main">{cut_html}<span class="row-cur">{yen(cur_amt)}</span></div>
    {reg_html}
    <div class="row-low">過去最安 {yen(low_amt)}{f'・{esc(low_when)}' if low_when else ''}</div>
  </div>
</li>"""


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
# トップページ: 人気ゲームのセール / 値下げ率ランキング / まもなく終了（Steamのチャート
# ページのような構成）。無名の低品質ゲームは featured_pool() の時点で除外している。
# ---------------------------------------------------------------------------
def _chart_section(anchor, emoji, title, desc, picks, more_href):
    if not picks:
        return ""
    rows = "".join(game_row(g) for g in picks)
    return f"""
<section class="chart-section" id="{anchor}">
  <div class="section-head">
    <h2>{emoji} {esc(title)}</h2>
    <p class="meta">{esc(desc)}</p>
  </div>
  <ul class="list">{rows}</ul>
  <p class="see-all"><a class="btn-outline" href="{esc(more_href)}">もっと見る →</a></p>
</section>"""


def build_featured(latest):
    games = latest.get("games", [])
    pool = featured_pool(games)
    updated = fmt_dt(latest.get("generated_at", ""))
    sale_count = sum(1 for g in games if g.get("on_sale"))
    mr = _reviews_bucket()

    popular = sorted(pool, key=lambda g: -(g.get("review_count") or 0))[:10]

    discounted = sorted(
        pool, key=lambda g: -((g.get("current") or {}).get("discount_pct") or 0)
    )[:10]

    ending = [g for g in pool if expiry_info((g.get("current") or {}).get("expiry"))]
    ending.sort(key=lambda g: expiry_info((g.get("current") or {}).get("expiry"))["days_left"])
    ending = ending[:10]

    sections = (
        _chart_section("popular", "🔥", "人気ゲームのセール",
                       "レビュー数が多い定番タイトルが値下げ中", popular,
                       _all_link({"sort": "reviews_desc", "reviews": mr, "onsale": 1})) +
        _chart_section("discount", "💰", "値下げ率ランキング",
                       "人気タイトルの中で割引率が高い順", discounted,
                       _all_link({"sort": "cut_desc", "reviews": mr, "onsale": 1})) +
        _chart_section("ending", "⏳", "まもなく終了",
                       "セール終了が近いおすすめタイトル", ending,
                       _all_link({"sort": "expiry", "reviews": mr, "onsale": 1}))
    )
    empty = ('<p class="empty">現在、条件に合うセールはありません。'
             'しきい値は<a href="about.html#verdict">このサイトについて</a>のページで確認できます。</p>') if not pool else ""

    body = f"""
<section class="hero">
  <h1>🔥 注目のセール</h1>
  <p class="meta">知っているゲームが安くなっているものだけを厳選 ・ 最終更新 {esc(updated)}</p>
  <p class="meta">セール中 {sale_count} / {len(games)} 本（厳選対象 {len(pool)} 本） ・ <a href="about.html#verdict">買い時判定の基準について</a></p>
</section>
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
    # JS無効時のフォールバック順（割引率が高い順）
    games_sorted = sorted(games, key=lambda g: -((g.get("current") or {}).get("discount_pct") or 0))
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

    body = f"""
<section class="hero">
  <h1>すべてのセール</h1>
  <p class="meta">全 {len(games)} 本 ・ 最終更新 {esc(updated)}</p>
  <p class="meta"><a href="about.html#verdict">買い時判定の基準について</a></p>
</section>
<noscript><p class="js-warn">検索・並び替え・絞り込みには JavaScript が必要です（一覧自体は表示されています）。</p></noscript>
<div class="filter-bar">
  <label class="sr-only" for="q">ゲーム名で検索</label>
  <input type="search" id="q" placeholder="ゲーム名で検索…" autocomplete="off" aria-label="ゲーム名で検索">
  <details class="filter-details" open>
    <summary class="filter-toggle">絞り込み・並び替え</summary>
    <div class="filter-row">
      <select id="f-sort" aria-label="並び替え">
        <option value="cut_desc">割引率が高い順</option>
        <option value="price_asc">価格が安い順</option>
        <option value="reviews_desc">人気順（レビュー数）</option>
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
      <label class="chk"><input type="checkbox" id="f-jp"> 日本語対応のみ</label>
      <label class="chk"><input type="checkbox" id="f-onsale"> セール中のみ</label>
      <button type="button" id="reset-filters" class="btn-text">条件をリセット</button>
    </div>
  </details>
  <p class="result-count" id="result-count"></p>
</div>
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
<p>価格データは <a href="https://isthereanydeal.com/" target="_blank" rel="noopener noreferrer">IsThereAnyDeal</a>
のAPIを通じて取得し、ゲーム情報の一部はSteam公式APIで補っています。
表示価格はリアルタイムではなく、<strong>毎朝6時ごろ（日本時間）に自動更新される保存済みデータ</strong>です。
実際の購入前には、必ずストア側の最新価格をご確認ください。</p>

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


def build_sitemap(latest):
    games = latest.get("games", [])
    today = date.today().isoformat()
    paths = ["", "all.html", "about.html"] + [f"games/{g['slug']}.html" for g in games]

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


def sparkline_svg(history, current_amount=None, width=680, height=240, pad=36):
    """価格履歴の折れ線。最安ラインと、実際の現在価格の参照ラインを重ねる。

    現在価格は overview 由来の値（current_amount）を使う。履歴の最終点は
    「定価復帰」等で現在価格と一致しないため、最終点をそのまま現在扱いにしない。
    """
    pts = [h for h in history if h.get("amount") is not None and h.get("date")]
    if len(pts) < 2:
        return '<p class="muted">履歴データが不足しています。</p>'

    amounts = [p["amount"] for p in pts]
    hist_low = min(amounts)
    # 現在価格も含めて縦スケールを決める（参照線が枠外に出ないように）
    has_cur = isinstance(current_amount, (int, float))
    scale_vals = amounts + ([current_amount] if has_cur else [])
    lo, hi = min(scale_vals), max(scale_vals)
    span = (hi - lo) or 1
    n = len(pts)
    low_idx = amounts.index(hist_low)   # 最安を付けた点

    def x(i):
        return pad + (width - 2 * pad) * i / (n - 1)

    def y(a):
        return pad + (height - 2 * pad) * (1 - (a - lo) / span)

    coords = " ".join(f"{x(i):.1f},{y(p['amount']):.1f}" for i, p in enumerate(pts))

    # 通常点（最安点は別描画で強調するため除外）
    dots = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(p["amount"]):.1f}" r="2.5" class="pt"/>'
        for i, p in enumerate(pts) if i != low_idx
    )

    # 最安点を強調
    y_low = y(hist_low)
    low_dot = f'<circle cx="{x(low_idx):.1f}" cy="{y_low:.1f}" r="4" class="pt-low"/>'

    # 現在価格の参照ライン（実際の現在価格。最安ラインと同様の水平線）
    cur_ref = ""
    if has_cur:
        yc = y(current_amount)
        cur_ref = (
            f'<line x1="{pad}" y1="{yc:.1f}" x2="{width-pad}" y2="{yc:.1f}" class="cur-line"/>'
            f'<circle cx="{width-pad:.1f}" cy="{yc:.1f}" r="4" class="pt-current"/>'
            f'<text x="{width-pad}" y="{yc-8:.1f}" class="cur-label" text-anchor="end">'
            f'現在 {yen(current_amount)}</text>'
        )

    # X軸の日付ラベル（最初と最後）
    axis = (
        f'<text x="{pad}" y="{height-10:.1f}" class="axis-label">{esc(_short_ym(pts[0]["date"]))}</text>'
        f'<text x="{width-pad}" y="{height-10:.1f}" class="axis-label" text-anchor="end">'
        f'{esc(_short_ym(pts[-1]["date"]))}</text>'
    )

    return f"""<svg class="chart" viewBox="0 0 {width} {height}" role="img"
     aria-label="価格履歴グラフ">
  <line x1="{pad}" y1="{y_low:.1f}" x2="{width-pad}" y2="{y_low:.1f}" class="low-line"/>
  <text x="{pad}" y="{y_low-6:.1f}" class="low-label">最安 {yen(hist_low)}</text>
  <polyline points="{coords}" class="price-line" fill="none"/>
  {dots}{low_dot}{cur_ref}{axis}
</svg>"""


# ---------------------------------------------------------------------------
# ゲーム個別ページ
# ---------------------------------------------------------------------------
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

    shop = cur.get("shop")
    url = cur.get("url")
    buy_link = f'<a class="buy" href="{esc(url)}" target="_blank" rel="noopener">{esc(shop or "ストアで見る")}で見る →</a>' if url else ""

    # 過去最安値カードの補足: いつ記録した最安かを絶対日付+相対で併記
    low_when = ""
    if low.get("date"):
        rel = relative_date_jp(low.get("date"))
        abs_d = fmt_date_jp(low.get("date"))
        low_when = f"{abs_d}（{rel}）" if rel else abs_d

    # 買い時の主説明（金額ベースの一言）
    timing_txt = buy_timing_text(cur.get("amount"), low.get("amount"))

    title = display_title(game)

    # 大きめのバナー画像（無ければプレースホルダー）
    banner = game_image(game.get("assets"),
                        ["banner600", "banner400", "banner300", "boxart"],
                        "hero-banner", title, dims=(600, 344))

    # セール終了日時（個別ページは日付＋残り日数の両方）
    exp = expiry_info(cur.get("expiry"))
    if exp:
        exp_cls = "expiry-detail urgent" if exp["urgent"] else "expiry-detail"
        expiry_detail = (f'<p class="{exp_cls}">⏳ セール終了：{esc(exp["date_full"])}'
                         f'（{esc(days_left_text(exp["days_left"]))}）</p>')
    elif cur.get("discount_pct"):
        expiry_detail = '<p class="expiry-detail undated">セール終了日：未定</p>'
    else:
        expiry_detail = ""

    # 定価からの節約額（現在価格カードに添える）
    save_txt = ""
    cur_amt, reg_amt = cur.get("amount"), reg.get("amount")
    if cur_amt is not None and reg_amt is not None and reg_amt > cur_amt:
        disc = cur.get("discount_pct")
        pct = f"（{disc}%オフ）" if disc else ""
        save_txt = f"定価より {yen(reg_amt - cur_amt)} お得{pct}"

    # 履歴テーブル（直近20件を新しい順）
    rows = []
    for h in list(reversed(history))[:20]:
        rows.append(
            f'<tr><td>{esc(h.get("date"))}</td><td>{yen(h.get("amount"))}</td>'
            f'<td>{esc(h.get("shop"))}</td></tr>'
        )
    hist_table = ""
    if rows:
        hist_table = f"""
<h2>価格履歴（直近）</h2>
<div class="table-wrap">
<table class="history">
  <thead><tr><th>日付</th><th>価格</th><th>ストア</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
</div>"""

    updated = fmt_dt(latest.get("generated_at", ""))
    genre_tags = "".join(f'<span class="tag-genre">{esc(genre)}</span>' for genre in (game.get("genres") or []))

    body = f"""
<nav class="crumbs"><a href="../index.html">🔥 注目のセール</a> ・ <a href="../all.html">すべてのセール</a></nav>
<div class="game-banner">{banner}</div>
<section class="game-head">
  <h1>{esc(title)}</h1>
  <div class="game-head-tags">
    {verdict_badge(v)}
    {jp_mark(game.get('jp_support'))}
    {genre_tags}
  </div>
</section>
{f'<p class="verdict-detail">{esc(timing_txt)}</p>' if timing_txt else ''}
{expiry_detail}

<section class="price-cards">
  <div class="stat-card primary">
    <div class="card-label">現在価格</div>
    <div class="card-value big">{yen(cur.get('amount'))}{f'<span class="card-cut">-{cur.get("discount_pct")}%</span>' if (game.get('on_sale') and cur.get('discount_pct')) else ''}</div>
    <div class="card-sub">{esc(shop or '')}</div>
    <div class="card-sub save">{esc(save_txt)}</div>
  </div>
  <div class="stat-card secondary">
    <div class="card-label">過去最安値</div>
    <div class="card-value">{yen(low.get('amount'))}</div>
    <div class="card-sub">{esc(low_when)}</div>
  </div>
  <div class="stat-card secondary">
    <div class="card-label">定価</div>
    <div class="card-value">{yen(reg.get('amount'))}</div>
    <div class="card-sub"></div>
  </div>
</section>

{buy_link}

<h2>価格の推移</h2>
{sparkline_svg(history, current_amount=cur.get('amount'))}

{hist_table}

<p class="meta">最終更新: {esc(updated)}</p>
"""
    on_sale = bool(game.get("on_sale"))
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
:root {
  --bg: #11151c;
  --panel: #171c25;
  --panel2: #1e2530;
  --row-hover: #212836;
  --text: #c7d0dd;
  --text-strong: #e7ebf2;
  --muted: #7c8697;
  --line: #262d3a;
  --accent: #6ba3d6;
  --good: #7fa876;
  --decent: #c0a05f;
  --watch: #737d8e;
  --bad: #bd6f78;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: system-ui, "Segoe UI", "Hiragino Kaku Gothic ProN", Meiryo, sans-serif;
  line-height: 1.6;
}
h1, h2, h3, .brand, .row-cur { font-family: "Zen Kaku Gothic New", system-ui, "Hiragino Kaku Gothic ProN", Meiryo, sans-serif; }
h1, h2, h3 { color: var(--text-strong); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.container { max-width: 1120px; margin: 0 auto; padding: 20px 14px 64px; }
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}

/* ヘッダー / ナビ */
.site-header { background: var(--panel); border-bottom: 1px solid var(--line); padding: 12px 14px; }
.header-inner {
  max-width: 1120px; margin: 0 auto; display: flex; align-items: center;
  justify-content: space-between; gap: 12px; flex-wrap: wrap;
}
.brand { font-weight: 900; font-size: 1.1rem; letter-spacing: .02em; color: var(--text-strong); }
.brand:hover { text-decoration: none; }
.site-nav { display: flex; gap: 4px; }
.site-nav a { padding: 6px 12px; border-radius: 6px; font-size: .82rem; font-weight: 700; color: var(--muted); }
.site-nav a:hover { text-decoration: none; color: var(--text-strong); }
.site-nav a.active { background: var(--panel2); color: var(--accent); }
.site-footer {
  text-align: center; color: var(--muted); font-size: .78rem;
  padding: 24px; border-top: 1px solid var(--line);
}
.site-footer p { margin: 4px 0; }
.site-footer a { color: var(--muted); text-decoration: underline; }
.site-footer a:hover { color: var(--accent); }

.hero { padding: 6px 0 6px; }
.hero h1, .game-head h1 { margin: 0 0 4px; font-weight: 900; font-size: 1.5rem; }
.meta { color: var(--muted); font-size: .85rem; margin: 2px 0; }

/* トップページのチャートセクション（人気/値下げ率/まもなく終了） */
.chart-section { margin: 28px 0 0; }
.section-head { margin: 0 0 8px; }
.section-head h2 { margin: 0 0 2px; font-size: 1.15rem; font-weight: 800; }
.section-head .meta { margin: 0; }
.empty { text-align: center; color: var(--muted); padding: 36px 12px; }
.empty p { margin: 0 0 14px; }
.js-warn {
  background: var(--panel2); border: 1px solid var(--line); padding: 10px 14px;
  border-radius: 10px; color: var(--muted); font-size: .85rem;
}

/* 検索・並び替え・絞り込みバー（全件ページ） */
.filter-bar {
  position: sticky; top: 0; z-index: 5; background: var(--bg);
  padding: 10px 0 12px; margin-bottom: 4px; border-bottom: 1px solid var(--line);
}
#q {
  width: 100%; padding: 11px 14px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--panel); color: var(--text-strong); font-size: 1rem; margin-bottom: 8px;
}
#q:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.filter-toggle {
  display: none; cursor: pointer; padding: 8px 2px; margin-top: 2px;
  font-weight: 700; font-size: .84rem; color: var(--text-strong);
}
.filter-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding-top: 6px; }
.filter-row select {
  padding: 7px 10px; border-radius: 6px; border: 1px solid var(--line);
  background: var(--panel2); color: var(--text); font-size: .82rem;
}
.chk { display: inline-flex; align-items: center; gap: 5px; font-size: .8rem; color: var(--muted); }
.chk input { accent-color: var(--accent); }
.btn-text { background: none; border: none; color: var(--accent); font-size: .8rem; cursor: pointer; padding: 4px 0; }
.result-count { margin: 8px 0 0; font-size: .78rem; color: var(--muted); }
@media (max-width: 719px) {
  /* 画面が狭いうちは絞り込み行を畳んでおき、検索窓の直下を圧迫しないようにする */
  .filter-toggle { display: block; }
}

/* ゲーム一覧（トップ・全件ページ共通） */
.list {
  list-style: none; margin: 14px 0; padding: 0;
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
}

.row {
  display: grid; grid-template-columns: 108px 1fr; column-gap: 12px; row-gap: 6px;
  grid-template-areas: "thumb main" "thumb status" "thumb price";
  align-items: start; padding: 10px 14px; border-bottom: 1px solid var(--line);
}
.list .row:last-child { border-bottom: none; }
.list .row[hidden] { display: none; }
.row:hover { background: var(--row-hover); }

@media (min-width: 720px) {
  .row {
    /* main列の上限を狭めることで、短いタイトルの右側に不要な余白が
       できないようにし、ステータス/価格列をタイトルの近くに寄せる。 */
    grid-template-columns: 152px minmax(140px, 420px) 130px 170px;
    grid-template-areas: "thumb main status price";
    align-items: center; column-gap: 16px; row-gap: 0; padding: 10px 16px;
  }
}

.row-thumb {
  grid-area: thumb; display: block; width: 100%; aspect-ratio: 400 / 230;
  border-radius: 6px; overflow: hidden; background: var(--panel2);
}
.row-thumb-img { width: 100%; height: 100%; object-fit: cover; display: block; }

.row-main { grid-area: main; min-width: 0; }
.row-title { margin: 0; font-size: 1rem; line-height: 1.35; font-weight: 700; word-break: break-word; }
.row-title a { color: var(--text-strong); }
.row-title a:hover { color: var(--accent); text-decoration: none; }
.row-tags { margin-top: 5px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: .72rem; color: var(--muted); }

.row-status { grid-area: status; display: flex; flex-direction: column; gap: 4px; align-items: flex-start; }
.row-timing { font-size: .74rem; color: var(--muted); }
.row-timing.save { color: var(--good); font-weight: 600; }

/* 価格欄の情報階層: ①現在価格（最大・最強調）→②割引率→③定価/過去最安（控えめ） */
.row-price { grid-area: price; display: flex; flex-direction: column; gap: 3px; align-items: flex-start; }
@media (min-width: 720px) { .row-price { align-items: flex-end; text-align: right; } }
.row-price-main { display: flex; align-items: baseline; gap: 8px; }
.row-cut {
  font-size: .8rem; font-weight: 800; color: var(--good);
  background: rgba(127,168,118,.16); border: 1px solid rgba(127,168,118,.32);
  padding: 2px 7px; border-radius: 4px;
}
.row-cur { font-weight: 800; font-size: 1.4rem; color: var(--text-strong); letter-spacing: -.01em; }
.row-reg { color: var(--muted); font-size: .74rem; text-decoration: line-through; opacity: .8; }
.row-low { color: var(--muted); font-size: .72rem; opacity: .8; }

.tag-expiry.urgent { color: var(--bad); font-weight: 600; }

.see-all { text-align: center; margin: 22px 0 8px; }
.load-more-wrap { text-align: center; margin: 20px 0; }
.btn-outline {
  display: inline-block; padding: 10px 22px; border: 1px solid var(--accent); color: var(--accent);
  border-radius: 8px; font-weight: 700; background: none; cursor: pointer; font-size: .9rem;
}
.btn-outline:hover { background: rgba(107,163,214,.1); text-decoration: none; }
.btn-outline[hidden] { display: none; }

/* 詳細ページ */
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; margin: 16px 0; }
th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-size: .8rem; font-weight: 600; }
.verdict-detail { margin: 8px 0 0; font-size: 1.15rem; font-weight: 700; color: var(--text-strong); }
.jp { color: var(--good); font-size: .75rem; font-weight: 700; white-space: nowrap; }
.jp-no { color: var(--muted); font-size: .72rem; }
.tag-genre {
  display: inline-block; padding: 1px 7px; border-radius: 4px;
  background: var(--panel2); color: var(--muted); font-size: .7rem; white-space: nowrap;
}
/* 画像が取得できないときのプレースホルダー: 真っ黒/無地にせず、ゲーム名で判別できるようにする */
.img-ph {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 4px; padding: 6px; text-align: center;
  background: linear-gradient(160deg, var(--panel2), var(--panel)); color: var(--muted);
}
.img-ph-icon { font-size: 1.3em; line-height: 1; opacity: .7; }
.img-ph-title {
  font-size: .72rem; line-height: 1.25; font-weight: 600; color: var(--text);
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.row-thumb-img.img-ph .img-ph-icon { font-size: 1.1em; }
.row-thumb-img.img-ph .img-ph-title { font-size: .66rem; -webkit-line-clamp: 2; }

.game-banner { margin: 4px 0 16px; }
.hero-banner {
  width: 100%; max-width: 100%; height: auto; aspect-ratio: 600 / 344; display: block;
  border-radius: 10px; background: var(--panel2); object-fit: cover;
}
.hero-banner.img-ph { height: auto; aspect-ratio: 600 / 344; }
.hero-banner.img-ph .img-ph-icon { font-size: 2.6rem; }
.hero-banner.img-ph .img-ph-title { font-size: 1.1rem; max-width: 80%; }
.expiry-detail { margin: 8px 0 0; font-size: .95rem; font-weight: 600; color: var(--muted); }
.expiry-detail.urgent { color: var(--bad); }
.badge {
  display: inline-flex; align-items: center; padding: 2px 9px; border-radius: 5px;
  font-size: .78rem; font-weight: 700; border: 1px solid transparent; cursor: help;
}
.legend-list {
  list-style: none; margin: 14px 0; padding: 0; display: flex; flex-direction: column; gap: 10px;
}
.legend-row {
  display: flex; align-items: center; gap: 12px; padding: 10px 14px;
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
}
.legend-row .badge { flex-shrink: 0; }
.legend-desc { color: var(--text); font-size: .86rem; }
.v-record { color: var(--good); background: rgba(127,168,118,.16); border-color: rgba(127,168,118,.32); }
.v-near { color: var(--good); background: rgba(127,168,118,.11); border-color: rgba(127,168,118,.24); }
.v-decent { color: var(--decent); background: rgba(192,160,95,.14); border-color: rgba(192,160,95,.3); }
.v-watch { color: var(--watch); background: rgba(115,125,142,.14); border-color: rgba(115,125,142,.3); }
.v-high { color: var(--bad); background: rgba(189,111,120,.14); border-color: rgba(189,111,120,.3); }
.v-unknown { color: var(--muted); background: rgba(124,134,151,.1); border-color: var(--line); }
.crumbs { margin-bottom: 12px; font-size: .85rem; }
.game-head { display: flex; flex-direction: column; gap: 8px; }
.game-head-tags { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
/* 現在価格を最も強調し、過去最安/定価は控えめな補助情報として並べる */
.price-cards { display: grid; grid-template-columns: 1.4fr 1fr 1fr; gap: 12px; margin: 20px 0; }
.stat-card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }
.stat-card.primary { background: rgba(107,163,214,.08); border-color: rgba(107,163,214,.35); }
.stat-card.secondary { padding: 12px 16px; }
.stat-card.secondary .card-label { font-size: .74rem; }
.stat-card.secondary .card-value { font-size: 1.1rem; color: var(--text); font-weight: 600; }
.card-label { color: var(--muted); font-size: .8rem; }
.card-value { font-size: 1.4rem; font-weight: 700; color: var(--text-strong); }
.card-value.big { display: flex; align-items: baseline; gap: 10px; font-size: 2.1rem; color: var(--text-strong); }
.card-cut {
  font-size: 1rem; font-weight: 800; color: var(--good);
  background: rgba(127,168,118,.16); border: 1px solid rgba(127,168,118,.32);
  padding: 2px 8px; border-radius: 5px;
}
.card-sub { color: var(--muted); font-size: .8rem; min-height: 1.2em; }
.card-sub.save { color: var(--good); font-weight: 600; }
.buy {
  display: inline-block; margin: 4px 0 8px; padding: 10px 18px;
  background: var(--accent); color: #10161f; border-radius: 8px; font-weight: 700;
}
.buy:hover { text-decoration: none; opacity: .9; }
.chart { width: 100%; height: auto; background: var(--panel); border-radius: 10px; padding: 8px; }
.price-line { stroke: var(--accent); stroke-width: 2; }
.pt { fill: var(--accent); opacity: .55; }
.pt-low { fill: var(--good); stroke: var(--bg); stroke-width: 1.5; }
.pt-current { fill: var(--text-strong); stroke: var(--accent); stroke-width: 2.5; }
.cur-line { stroke: var(--accent); stroke-width: 1; stroke-dasharray: 2 3; opacity: .8; }
.cur-label { fill: var(--accent); font-size: 12px; font-weight: 700; }
.axis-label { fill: var(--muted); font-size: 11px; }
.low-line { stroke: var(--good); stroke-width: 1; stroke-dasharray: 4 4; }
.low-label { fill: var(--good); font-size: 11px; }
.muted { color: var(--muted); }
@media (max-width: 640px) {
  .price-cards { grid-template-columns: 1fr; }
}
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

    build_featured(latest)
    build_all(latest)
    build_about(latest)
    build_robots()
    build_sitemap(latest)
    for game in latest.get("games", []):
        build_game_page(game, latest)

    print(f"生成完了: public/index.html, public/all.html, public/about.html, "
          f"public/robots.txt, public/sitemap.xml ほか {len(latest.get('games', []))} ページ")


if __name__ == "__main__":
    main()
