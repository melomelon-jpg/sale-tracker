# -*- coding: utf-8 -*-
"""買い時判定ロジック。

現在価格と過去最安値だけを使う純粋関数。取得（fetch）時に呼び出して
結果を latest.json に埋め込み、サイト生成（build）側は表示するだけにする。
外部依存なしなので単体テストが容易。
"""

# 現在価格と過去最安値の関係を、事実に即した4段階で表す。
#   code  : プログラム/CSSで扱う識別子
#   label : サイトに出す日本語ラベル（差額等の動的な数字は含めない。
#           含める場合は build_site.py 側のキャプション文言に出す）
#
# 「底値」（株式相場の用語）は使わず「最安値」に統一する。また
# 「現在価格が過去最安と同額（tied_low）」を「過去最安値を更新（new_low）」と
# 混同しない（同額は"更新"ではない、という誤認を避ける）。
# 赤色は本日終了などの緊急性表示にのみ使うため、バッジ側の色は
# 緑（new_low/tied_low）／グレー（near_low/above_low）の2系統に絞る。
NEAR_LOW_MAX_GAP = 0.05  # 「最安値に近い」とみなす上振れ率の上限（過去最安からの差）

VERDICT_RULES = [
    {"code": "new_low",   "label": "過去最安値を更新"},
    {"code": "tied_low",  "label": "過去最安値と同じ価格"},
    {"code": "near_low",  "label": "最安値に近い"},
    {"code": "above_low", "label": "最安値より高い"},
]
_LABELS = {r["code"]: r["label"] for r in VERDICT_RULES}

UNKNOWN_VERDICT = {"code": "unknown", "label": "判定不可", "gap_pct": None}


def judge(current_amount, lowest_amount):
    """現在価格と過去最安値から、事実に即した4段階の関係を返す。

    引数:
        current_amount: 現在価格（数値）。None の場合は判定不可。
        lowest_amount:  過去最安値（数値）。None/0以下 の場合は判定不可。

    戻り値:
        {"code": str, "label": str, "gap_pct": float | None}
        gap_pct は過去最安からの上振れ率（％）。負なら過去最安を下回っている
        （= new_low）。
    """
    if current_amount is None or lowest_amount is None or lowest_amount <= 0:
        return dict(UNKNOWN_VERDICT)

    gap = (current_amount - lowest_amount) / lowest_amount
    gap_pct = round(gap * 100, 1)

    if current_amount < lowest_amount:
        code = "new_low"
    elif current_amount == lowest_amount:
        code = "tied_low"
    elif gap <= NEAR_LOW_MAX_GAP:
        code = "near_low"
    else:
        code = "above_low"

    return {"code": code, "label": _LABELS[code], "gap_pct": gap_pct}


if __name__ == "__main__":
    # 簡易セルフテスト
    cases = [
        (4200, 4400, "new_low"),      # 過去最安を下回る＝真の更新
        (4400, 4400, "tied_low"),     # 過去最安と同額
        (4600, 4400, "near_low"),     # +4.5%
        (4630, 4400, "above_low"),    # +5.2%（5%超え）
        (6000, 4400, "above_low"),    # +36.4%
        (7000, 4400, "above_low"),    # +59%
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
