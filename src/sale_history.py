# -*- coding: utf-8 -*-
"""セール履歴の傾向分析。

価格履歴（fetch_data.py が保存する data/history/<slug>.json）から、過去に
何回セールになったか・直近はいつだったかを機械的に検出するだけの純粋関数。
「次はいつ安くなるか」という予測は行わない。追跡期間がまだ2年に満たず、
複数年サイクルを断定できるだけの根拠がないため（CLAUDE.md「断定しない」）。
"""


def summarize_sale_history(history):
    """[{"date","amount"}, ...] から、検出したセール回数と直近開始日を返す。

    「セール中」の判定は、観測された最高値（＝定価とみなす）より5%以上安い
    区間とする。連続する安値区間は1回のセールとしてまとめる（同一セール内で
    割引率が変わり複数の変化点が記録されている場合を1件に潰すため）。

    戻り値: {"sale_count", "last_sale_date", "tracked_since"} | None
            （履歴が少なすぎる/定価が判定できない場合）
    """
    pts = [h for h in history if h.get("amount") is not None and h.get("date")]
    if len(pts) < 2:
        return None
    pts = sorted(pts, key=lambda h: h["date"])
    baseline = max(p["amount"] for p in pts)
    if baseline <= 0:
        return None
    threshold = baseline * 0.95

    sale_count = 0
    last_sale_date = None
    in_sale = False
    for p in pts:
        if p["amount"] < threshold:
            if not in_sale:
                sale_count += 1
                last_sale_date = p["date"]
                in_sale = True
        else:
            in_sale = False

    if sale_count < 1:
        return None
    return {
        "sale_count": sale_count,
        "last_sale_date": last_sale_date,
        "tracked_since": pts[0]["date"],
    }
