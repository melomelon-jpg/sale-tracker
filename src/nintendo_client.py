# -*- coding: utf-8 -*-
"""Nintendo eShop(JP)の非公式価格APIの薄いクライアント。

2026-08の調査に基づく設計判断:
  - api.ec.nintendo.com/v1/price?country=JP&ids=<nsuid>&lang=ja のみを使う。
    実測で動作確認済み・robots.txtによる明示的な禁止も確認できなかった。
  - タイトル名からnsuidを自動検索する経路(search.nintendo.jp)は
    robots.txt が `User-Agent: * / Disallow: /` を明示しているため使わない
    （Googlebotのみ例外的に許可）。nsuidは事前に人力で調べ
    config/console_games.json に登録する運用とする。
  - Nintendo/Sonyとも利用規約上「自動化されたアクセス」を明確に禁止しており、
    グレーゾーンであることを踏まえ、リトライで押し込まない・低頻度・
    身元を隠さないUser-Agentを徹底する（fetch_console_data.py側の
    呼び出し方針とあわせて設計）。

依存は標準ライブラリのみ。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.ec.nintendo.com/v1/price"
UA = ("sale-tracker-console-price-history/0.1 "
      "(+https://github.com/melomelon-jpg/sale-tracker; "
      "individual non-commercial daily price-history logger; contact via GitHub issues)")
COUNTRY = "JP"
CURRENCY = "JPY"


class NintendoError(Exception):
    """個別タイトルの取得失敗（この1件だけスキップすればよい）。"""


class NintendoBlockedError(Exception):
    """429/403等、ブロック・レート制限の兆候。呼び出し側は残りの取得を即座に中断すること。"""


def _parse_amount(node):
    """{"amount": "1,480円", "raw_value": "1480", ...} から数値を取り出す。"""
    if not isinstance(node, dict):
        return None
    raw = node.get("raw_value")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def price(nsuid, timeout=20):
    """1タイトル分の価格情報を返す。

    戻り値: {
      "regular": int|None,   # 定価(円)
      "current": int|None,   # 現在価格(円)。セール中でなければ regular と同額
      "on_sale": bool,       # discount_price が存在するか
      "sale_end": str|None,  # セール終了日時(ISO文字列、あれば)
    } | None（該当タイトルが見つからない場合）

    例外:
      NintendoBlockedError: HTTP 429/403（レート制限・ブロックの兆候）
      NintendoError: その他の失敗（ネットワークエラー・想定外レスポンス等）
    """
    params = urllib.parse.urlencode({
        "country": COUNTRY, "ids": nsuid, "lang": "ja",
    })
    url = f"{BASE}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code in (429, 403):
            raise NintendoBlockedError(f"HTTP {e.code}: レート制限/ブロックの可能性") from e
        raise NintendoError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        raise NintendoError(str(e)) from e

    prices = data.get("prices") or []
    if not prices:
        return None
    entry = prices[0]
    if entry.get("sales_status") == "not_found":
        return None

    regular_node = entry.get("regular_price") or {}
    if regular_node.get("currency") not in (None, CURRENCY):
        raise NintendoError(f"想定外の通貨: {regular_node.get('currency')}")
    regular = _parse_amount(regular_node)

    discount_node = entry.get("discount_price")
    current = regular
    on_sale = False
    sale_end = None
    if discount_node:
        cur = _parse_amount(discount_node)
        if cur is not None:
            current = cur
            on_sale = True
            sale_end = discount_node.get("end_datetime")

    return {"regular": regular, "current": current, "on_sale": on_sale, "sale_end": sale_end}
