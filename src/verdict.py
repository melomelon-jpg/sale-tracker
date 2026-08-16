# -*- coding: utf-8 -*-
"""買い時判定ロジック。

現在価格と過去最安値だけを使う純粋関数。取得（fetch）時に呼び出して
結果を latest.json に埋め込み、サイト生成（build）側は表示するだけにする。
外部依存なしなので単体テストが容易。
"""

# しきい値（過去最安からの上振れ率）と対応ラベル。上から順に評価する。
#   code       : プログラム/CSSで扱う識別子
#   label      : サイトに出す日本語ラベル
#   max_gap    : 「(現在 - 最安) / 最安」がこの値以下なら該当（None は上限なし）
VERDICT_RULES = [
    {"code": "record_low", "label": "過去最安更新中", "max_gap": 0.0},
    {"code": "near_low",   "label": "ほぼ最安",       "max_gap": 0.05},
    {"code": "decent",     "label": "まあ買い時",     "max_gap": 0.15},
    {"code": "watch",      "label": "様子見",         "max_gap": 0.5},
    {"code": "high",       "label": "割高",           "max_gap": None},
]

UNKNOWN_VERDICT = {"code": "unknown", "label": "判定不可", "gap_pct": None}


def judge(current_amount, lowest_amount):
    """現在価格と過去最安値から買い時判定を返す。

    引数:
        current_amount: 現在価格（数値）。None の場合は判定不可。
        lowest_amount:  過去最安値（数値）。None/0以下 の場合は判定不可。

    戻り値:
        {"code": str, "label": str, "gap_pct": float | None}
        gap_pct は過去最安からの上振れ率（％）。負なら最安を更新している。
    """
    if current_amount is None or lowest_amount is None or lowest_amount <= 0:
        return dict(UNKNOWN_VERDICT)

    gap = (current_amount - lowest_amount) / lowest_amount
    gap_pct = round(gap * 100, 1)

    for rule in VERDICT_RULES:
        if rule["max_gap"] is None or gap <= rule["max_gap"]:
            return {"code": rule["code"], "label": rule["label"], "gap_pct": gap_pct}

    # ここには到達しない（最後のルールが max_gap=None のため）
    return dict(UNKNOWN_VERDICT)


if __name__ == "__main__":
    # 簡易セルフテスト
    cases = [
        (4400, 4400, "record_low"),
        (4200, 4400, "record_low"),   # 最安割れ
        (4600, 4400, "near_low"),     # +4.5%
        (4900, 4400, "decent"),       # +11.4%
        (6000, 4400, "watch"),        # +36.4%
        (7000, 4400, "high"),         # +59% → 割高
        (None, 4400, "unknown"),
        (4400, None, "unknown"),
    ]
    ok = True
    for cur, low, expected in cases:
        got = judge(cur, low)
        mark = "OK" if got["code"] == expected else "NG"
        if got["code"] != expected:
            ok = False
        print(f"  [{mark}] cur={cur} low={low} -> {got}")
    print("すべてOK" if ok else "失敗あり")
