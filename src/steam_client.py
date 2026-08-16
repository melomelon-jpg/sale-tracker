# -*- coding: utf-8 -*-
"""Steam appdetails (cc=jp) の補助クライアント。

ITAD が主データ源。Steam は ITAD 側で価格が欠けたときの補完や、
Steamストアの円建て現価/割引の裏取りに使う。依存は標準ライブラリのみ。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

UA = "sale-tracker/0.1 (+https://github.com/; daily price tracker)"


def price_overview(steam_appid, timeout=20):
    """Steamの円建て価格を返す。取れなければ None。

    戻り値: {"currency", "regular", "current", "discount_pct"} | None
    Steam の initial/final は最小単位（銭）なので /100 して円に直す。
    """
    if not steam_appid:
        return None
    params = urllib.parse.urlencode({
        "appids": steam_appid,
        "cc": "jp",
        "l": "japanese",
        "filters": "price_overview",
    })
    url = f"https://store.steampowered.com/api/appdetails?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None

    node = data.get(str(steam_appid), {})
    if not node.get("success"):
        return None
    po = (node.get("data") or {}).get("price_overview")
    if not po:
        return None
    return {
        "currency": po.get("currency"),
        "regular": po.get("initial", 0) / 100.0,
        "current": po.get("final", 0) / 100.0,
        "discount_pct": po.get("discount_percent", 0),
    }


def supports_japanese(steam_appid, timeout=20):
    """Steamの supported_languages に日本語が含まれるかを判定する。

    戻り値: True（日本語対応）/ False（非対応）/ None（判定不能）。
    appdetails の supported_languages は "English, Japanese<strong>*</strong>..."
    のようなHTML文字列。フィルタを付けるとこの項目が欠けることがあるため
    フィルタ無しで取得する（1リクエスト）。
    """
    if not steam_appid:
        return None
    params = urllib.parse.urlencode({
        "appids": steam_appid,
        "cc": "jp",
        "l": "english",
    })
    url = f"https://store.steampowered.com/api/appdetails?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None

    node = data.get(str(steam_appid), {})
    if not node.get("success"):
        return None
    langs = (node.get("data") or {}).get("supported_languages")
    if not isinstance(langs, str):
        return None
    return ("Japanese" in langs) or ("日本語" in langs)
