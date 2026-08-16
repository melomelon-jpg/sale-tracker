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
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verdict  # noqa: E402  買い時判定の純粋関数（表示時に再計算して最新ルールを反映）

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
PUBLIC_DIR = ROOT / "public"
ASSETS_SRC = ROOT / "assets"  # 手書きCSSがあればここから、無ければ内蔵を使う

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


def yen(amount):
    if amount is None:
        return "—"
    return f"¥{amount:,.0f}"


def fmt_dt(iso):
    """ISO文字列を 'YYYY年M月D日 HH:MM UTC' に整形。失敗時はそのまま返す。"""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        # %-m はWindowsで使えないため手組みで整形する
        return f"{dt.year}年{dt.month}月{dt.day}日 {dt.hour:02d}:{dt.minute:02d} UTC"
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
    """現在価格と過去最安値の差を金額ベースの一言で表す（買い時の主説明）。

    ・現在 > 最安  → 「過去最安より ¥X 高い」
    ・現在 < 最安  → 「過去最安を更新中！」
    ・現在 = 最安  → 「過去最安と同額」
    どちらか欠けていれば空文字。
    """
    if current_amount is None or lowest_amount is None:
        return ""
    diff = current_amount - lowest_amount
    if diff > 0:
        return f"過去最安より {yen(diff)} 高い"
    if diff < 0:
        return "過去最安を更新中！"
    return "過去最安と同額"


def page(title, body, rel_root="."):
    """共通HTMLシェル。rel_root はassets等への相対パス。"""
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{rel_root}/assets/style.css">
</head>
<body>
<header class="site-header">
  <a class="brand" href="{rel_root}/index.html">🎮 セールトラッカー</a>
  <span class="tagline">PCゲームの価格を毎日チェック</span>
</header>
<main class="container">
{body}
</main>
<footer class="site-footer">
  <p>価格データ: IsThereAnyDeal / Steam ・ 表示は保存済みデータ（1日1回更新）</p>
</footer>
<!-- Cloudflare Web Analytics --><script type='module' src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{{"token": "c8c81e274f13457d80d3e8484503fdf2"}}'></script><!-- End Cloudflare Web Analytics -->
</body>
</html>
"""


def verdict_badge(v):
    cls = VERDICT_CLASS.get(v.get("code"), "v-unknown")
    return f'<span class="badge {cls}">{esc(v.get("label"))}</span>'


def jp_mark(jp):
    """日本語対応の表示。True=🇯🇵 / False=日本語なし / None(不明)=何も出さない。"""
    if jp is True:
        return '<span class="jp" title="日本語対応">🇯🇵 日本語</span>'
    if jp is False:
        return '<span class="jp-no" title="日本語表示なし">日本語なし</span>'
    return ""


def source_tag(source):
    """自動収集で追加されたゲームに小さなタグを付ける。"""
    if source == "auto":
        return '<span class="src-auto" title="セール自動収集で追加">自動</span>'
    return ""


def game_image(assets, sizes, cls, alt):
    """assets から最初に見つかったサイズの画像を <img> で返す。

    無ければ絵文字プレースホルダー（同じクラス）でレイアウト崩れを防ぐ。
    画像はITADのURLを直接参照する（自前保存はしない）。
    """
    url = ""
    if isinstance(assets, dict):
        for s in sizes:
            if assets.get(s):
                url = assets[s]
                break
    if url:
        return (f'<img class="{cls}" src="{esc(url)}" alt="{esc(alt)}" '
                f'loading="lazy" referrerpolicy="no-referrer">')
    return f'<div class="{cls} img-ph" role="img" aria-label="{esc(alt)}">🎮</div>'


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
# トップページ: 本日の値下げ一覧
# ---------------------------------------------------------------------------
def build_index(latest):
    games = latest.get("games", [])

    # セール中を上に、判定の良い順（最安更新中→…）で並べる
    order = {"record_low": 0, "near_low": 1, "decent": 2, "watch": 3, "high": 4, "unknown": 5}
    games_sorted = sorted(
        games,
        key=lambda g: (not g.get("on_sale", False), order.get(g["verdict"]["code"], 9)),
    )

    rows = []
    for g in games_sorted:
        cur = (g.get("current") or {}).get("amount")
        low = (g.get("lowest") or {}).get("amount")
        low_date = (g.get("lowest") or {}).get("date")
        reg = (g.get("regular") or {}).get("amount")
        disc = (g.get("current") or {}).get("discount_pct")

        disc_txt = f"-{disc}%" if disc else ""
        timing_txt = buy_timing_text(cur, low)
        low_when = relative_date_jp(low_date)

        thumb = game_image(g.get("assets"),
                           ["banner145", "banner300", "banner400", "boxart"],
                           "thumb", g["title"])

        # セール終了までの残り日数（一覧では残り日数のみ）
        exp = expiry_info((g.get("current") or {}).get("expiry"))
        if exp:
            exp_cls = "expiry urgent" if exp["urgent"] else "expiry"
            exp_html = f'<div class="{exp_cls}">セール終了 {esc(days_left_text(exp["days_left"]))}</div>'
        elif g.get("on_sale"):
            exp_html = '<div class="expiry undated">終了日未定</div>'
        else:
            exp_html = ""

        rows.append(f"""
    <tr>
      <td class="c-title"><div class="title-cell">{thumb}<div class="title-text"><a href="games/{esc(g['slug'])}.html">{esc(g['title'])}</a> {jp_mark(g.get('jp_support'))} {source_tag(g.get('source'))}</div></div></td>
      <td class="c-verdict">{verdict_badge(g['verdict'])}<div class="timing">{esc(timing_txt)}</div></td>
      <td class="c-price">{yen(cur)} <span class="disc">{esc(disc_txt)}</span>{exp_html}</td>
      <td class="c-regular">{yen(reg)}</td>
      <td class="c-low">{yen(low)} <span class="gap">{esc(low_when)}</span></td>
    </tr>""")

    updated = fmt_dt(latest.get("generated_at", ""))
    sale_count = sum(1 for g in games if g.get("on_sale"))

    body = f"""
<section class="hero">
  <h1>本日の値下げ一覧</h1>
  <p class="meta">最終更新: {esc(updated)} ・ セール中 {sale_count} / {len(games)} 本</p>
</section>
<div class="table-wrap">
<table class="deals">
  <thead>
    <tr>
      <th>タイトル</th><th>買い時</th><th>現在価格</th><th>定価</th><th>過去最安</th>
    </tr>
  </thead>
  <tbody>{''.join(rows)}
  </tbody>
</table>
</div>
"""
    (PUBLIC_DIR / "index.html").write_text(page("本日の値下げ一覧", body), encoding="utf-8")


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

    # 大きめのバナー画像（無ければプレースホルダー）
    banner = game_image(game.get("assets"),
                        ["banner600", "banner400", "banner300", "boxart"],
                        "hero-banner", game["title"])

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

    body = f"""
<nav class="crumbs"><a href="../index.html">← 一覧に戻る</a></nav>
<div class="game-banner">{banner}</div>
<section class="game-head">
  <h1>{esc(game['title'])}</h1>
  {verdict_badge(v)}
  {jp_mark(game.get('jp_support'))}
  {source_tag(game.get('source'))}
</section>
{f'<p class="verdict-detail">{esc(timing_txt)}</p>' if timing_txt else ''}
{expiry_detail}

<section class="price-cards">
  <div class="card">
    <div class="card-label">現在価格</div>
    <div class="card-value big">{yen(cur.get('amount'))}</div>
    <div class="card-sub">{esc(shop or '')}</div>
    <div class="card-sub save">{esc(save_txt)}</div>
  </div>
  <div class="card">
    <div class="card-label">過去最安値</div>
    <div class="card-value">{yen(low.get('amount'))}</div>
    <div class="card-sub">{esc(low_when)}</div>
  </div>
  <div class="card">
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
    out = PUBLIC_DIR / "games" / f"{slug}.html"
    out.write_text(page(f"{game['title']} の価格", body, rel_root=".."), encoding="utf-8")


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
CSS = """
:root {
  --bg: #0f1220; --panel: #1a1e33; --panel2: #232845;
  --text: #e8eaf5; --muted: #9aa1c4; --line: #2e3358;
  --accent: #6ea8fe; --record: #22c55e; --near: #84cc16;
  --decent: #eab308; --watch: #94a3b8; --high: #ef4444;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: system-ui, "Segoe UI", "Hiragino Kaku Gothic ProN", Meiryo, sans-serif;
  line-height: 1.6;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.container { max-width: 900px; margin: 0 auto; padding: 24px 16px 64px; }
.site-header {
  display: flex; align-items: baseline; gap: 12px;
  padding: 16px 24px; background: var(--panel); border-bottom: 1px solid var(--line);
}
.brand { font-weight: 700; font-size: 1.15rem; }
.tagline { color: var(--muted); font-size: .85rem; }
.site-footer {
  text-align: center; color: var(--muted); font-size: .8rem;
  padding: 24px; border-top: 1px solid var(--line);
}
.hero h1, .game-head h1 { margin: 0 0 4px; }
.meta { color: var(--muted); font-size: .85rem; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; margin: 16px 0; }
th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-size: .8rem; font-weight: 600; }
.deals .c-price { font-weight: 700; }
.deals .c-verdict { white-space: nowrap; }
.disc { color: var(--record); font-size: .8rem; margin-left: 4px; }
.gap { color: var(--muted); font-size: .8rem; margin-left: 4px; }
.timing { color: var(--muted); font-size: .78rem; margin-top: 4px; }
.verdict-detail { margin: 8px 0 0; font-size: 1.15rem; font-weight: 700; color: var(--text); }
.jp { color: var(--near); font-size: .75rem; font-weight: 700; white-space: nowrap; }
.jp-no { color: var(--muted); font-size: .72rem; }
.title-cell { display: flex; align-items: center; gap: 10px; }
.title-text { min-width: 0; }
.thumb {
  width: 72px; height: 34px; object-fit: cover; border-radius: 4px;
  flex: 0 0 auto; background: var(--panel2);
}
.img-ph {
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--panel2); color: var(--muted);
}
.thumb.img-ph { font-size: 16px; }
.expiry { font-size: .74rem; color: var(--muted); margin-top: 4px; }
.expiry.urgent { color: var(--high); font-weight: 700; }
.expiry.undated { color: var(--muted); opacity: .8; }
.game-banner { margin: 4px 0 16px; }
.hero-banner {
  width: 100%; max-width: 100%; height: auto; display: block; border-radius: 12px;
  background: var(--panel2);
}
.hero-banner.img-ph { height: 200px; font-size: 52px; }
.expiry-detail { margin: 8px 0 0; font-size: .95rem; font-weight: 600; color: var(--muted); }
.expiry-detail.urgent { color: var(--high); }
.src-auto {
  display: inline-block; padding: 0 6px; border-radius: 6px;
  background: var(--panel2); color: var(--muted); font-size: .7rem; font-weight: 600;
}
.badge {
  display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: .8rem; font-weight: 700; color: #0b0e18;
}
.v-record { background: var(--record); }
.v-near { background: var(--near); }
.v-decent { background: var(--decent); }
.v-watch { background: var(--watch); }
.v-high { background: var(--high); color: #fff; }
.v-unknown { background: #4b5170; color: var(--text); }
.crumbs { margin-bottom: 12px; }
.game-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.price-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 20px 0; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }
.card-label { color: var(--muted); font-size: .8rem; }
.card-value { font-size: 1.4rem; font-weight: 700; }
.card-value.big { font-size: 1.9rem; color: var(--accent); }
.card-sub { color: var(--muted); font-size: .8rem; min-height: 1.2em; }
.card-sub.save { color: var(--record); font-weight: 600; }
.buy {
  display: inline-block; margin: 4px 0 8px; padding: 10px 18px;
  background: var(--accent); color: #0b0e18; border-radius: 10px; font-weight: 700;
}
.buy:hover { text-decoration: none; opacity: .9; }
.chart { width: 100%; height: auto; background: var(--panel); border-radius: 12px; padding: 8px; }
.price-line { stroke: var(--accent); stroke-width: 2; }
.pt { fill: var(--accent); opacity: .55; }
.pt-low { fill: var(--record); stroke: var(--bg); stroke-width: 1.5; }
.pt-current { fill: #fff; stroke: var(--accent); stroke-width: 2.5; }
.cur-line { stroke: var(--accent); stroke-width: 1; stroke-dasharray: 2 3; opacity: .8; }
.cur-label { fill: var(--accent); font-size: 12px; font-weight: 700; }
.axis-label { fill: var(--muted); font-size: 11px; }
.low-line { stroke: var(--record); stroke-width: 1; stroke-dasharray: 4 4; }
.low-label { fill: var(--record); font-size: 11px; }
.muted { color: var(--muted); }
@media (max-width: 640px) {
  .price-cards { grid-template-columns: 1fr; }
}
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

    # CSS: 手書きが assets/style.css にあればそれを使い、無ければ内蔵を書き出す
    custom_css = ASSETS_SRC / "style.css"
    if custom_css.exists():
        shutil.copyfile(custom_css, PUBLIC_DIR / "assets" / "style.css")
    else:
        (PUBLIC_DIR / "assets" / "style.css").write_text(CSS, encoding="utf-8")

    build_index(latest)
    for game in latest.get("games", []):
        build_game_page(game, latest)

    print(f"生成完了: public/index.html ほか {len(latest.get('games', []))} ページ")


if __name__ == "__main__":
    main()
