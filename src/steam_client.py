# -*- coding: utf-8 -*-
"""Steam appdetails (cc=jp) の補助クライアント。

ITAD が主データ源。Steam は ITAD 側で価格が欠けたときの補完や、
Steamストアの円建て現価/割引の裏取りに使う。依存は標準ライブラリのみ。
"""
import json
import time
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


def get_japanese_name(steam_appid, timeout=20, retries=3):
    """Steamストアの日本語表示名を返す。取れなければ None。

    cc=jp&l=japanese で appdetails を叩く。ゲームが日本語タイトルを
    登録していない場合は英語名がそのまま返る（呼び出し側で英語のまま表示すればよい）。
    Steam側のレート制限が比較的厳しいため 429 は指数バックオフで再試行する
    （呼び出し側でも1件ごとにスリープを入れる想定）。
    """
    info = get_app_info(steam_appid, timeout=timeout, retries=retries)
    return info["name"] if info else None


def get_app_info(steam_appid, timeout=20, retries=3):
    """Steamストアの表示名・ジャンル・カテゴリ・レビュー数・日本語対応を1リクエストで返す。

    取れなければ None。cc=jp&l=japanese で appdetails を叩く（日本語タイトル取得と
    同じ経路）。filters="basic,genres,categories,recommendations" の "basic" には
    supported_languages も含まれるため、追加リクエストなしに人気度（レビュー数）・
    ジャンル情報に加えて日本語対応（supports_japanese と同じ判定）もまとめて取れる。
    Steam側のレート制限が比較的厳しいため 429 は指数バックオフで再試行する
    （呼び出し側でも1件ごとにスリープを入れる想定）。

    戻り値: {"name", "genres": [str], "categories": [str], "review_count": int|None,
              "jp_support": bool|None} | None
    """
    if not steam_appid:
        return None
    params = urllib.parse.urlencode({
        "appids": steam_appid,
        "cc": "jp",
        "l": "japanese",
        "filters": "basic,genres,categories,recommendations",
    })
    url = f"https://store.steampowered.com/api/appdetails?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})

    delay = 2.0
    data = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            return None
        except (urllib.error.URLError, json.JSONDecodeError):
            return None
    if data is None:
        return None

    node = data.get(str(steam_appid), {})
    if not node.get("success"):
        return None
    d = node.get("data") or {}
    genres = [g["description"] for g in (d.get("genres") or []) if g.get("description")]
    categories = [c["description"] for c in (d.get("categories") or []) if c.get("description")]
    review_count = (d.get("recommendations") or {}).get("total")
    langs = d.get("supported_languages")
    jp_support = (("Japanese" in langs) or ("日本語" in langs)) if isinstance(langs, str) else None
    return {
        "name": d.get("name") or None,
        "genres": genres,
        "categories": categories,
        "review_count": review_count if isinstance(review_count, int) else None,
        "jp_support": jp_support,
    }
