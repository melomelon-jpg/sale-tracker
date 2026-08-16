# -*- coding: utf-8 -*-
"""IsThereAnyDeal (ITAD) API の薄いラッパー。

api_check.py で検証済みの3エンドポイントを使う（すべて country=JP で円建て）:
  - games/lookup/v1   : タイトル -> ITADゲームID
  - games/overview/v2 : 現在価格 + 過去最安値
  - games/history/v2  : 価格履歴

依存は標準ライブラリのみ。レスポンス形式のブレに備えて防御的に読む。
"""
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.isthereanydeal.com"
UA = "sale-tracker/0.1 (+https://github.com/; daily price tracker)"
COUNTRY = "JP"
CURRENCY = "JPY"  # country=JP のときに期待する通貨コード

# 一時的な失敗として再試行するHTTPステータス（レート制限・サーバ側エラー）
_RETRY_STATUS = {429, 500, 502, 503, 504}


class ITADError(Exception):
    pass


def _open_with_retry(req, timeout, retries=4):
    """urlopen を指数バックオフ付きで実行しJSONを返す。

    429/5xx と一時的な接続エラーだけ再試行する。429 は Retry-After を尊重。
    レート制限に配慮しつつ、恒久的な失敗（4xxの多く）は即座に投げ直す。
    """
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            if e.code in _RETRY_STATUS and attempt < retries:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(retry_after) if retry_after else delay
                except (TypeError, ValueError):
                    wait = delay
                time.sleep(min(wait, 30) + random.uniform(0, 0.3))
                delay *= 2
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(delay + random.uniform(0, 0.3))
                delay *= 2
                continue
            raise


def _get(path, params, timeout=20):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return _open_with_retry(req, timeout)


def _post_json(path, params, payload, timeout=20):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"User-Agent": UA, "Content-Type": "application/json"},
    )
    return _open_with_retry(req, timeout)


def _amount(node):
    """{"price": {"amount": ...}} のような入れ子から amount を安全に取り出す。"""
    if not isinstance(node, dict):
        return None
    price = node.get("price") or {}
    return price.get("amount")


def _currency(node):
    """{"price": {"currency": ...}} から通貨コードを安全に取り出す。"""
    if not isinstance(node, dict):
        return None
    price = node.get("price") or {}
    return price.get("currency")


class ITADClient:
    def __init__(self, api_key, throttle=0.5):
        if not api_key:
            raise ITADError("ITAD_API_KEY が設定されていません")
        self.key = api_key
        self.throttle = throttle  # 連続リクエスト間の待機秒（レート配慮）

    def _sleep(self):
        if self.throttle:
            time.sleep(self.throttle)

    def lookup_id(self, title):
        """タイトルから ITADゲームID を返す。見つからなければ None。"""
        res = _get("/games/lookup/v1", {"key": self.key, "title": title})
        if not res.get("found"):
            return None
        return (res.get("game") or {}).get("id")

    def overview(self, game_id):
        """現在価格・過去最安値・定価を dict で返す。

        戻り値: {
          "current": {"amount", "shop", "discount_pct", "url"} | None,
          "lowest":  {"amount", "date"} | None,
          "regular": {"amount"} | None,
        }
        """
        params = {"key": self.key, "country": COUNTRY}
        res = _post_json("/games/overview/v2", params, [game_id])
        prices = res.get("prices") or []
        block = prices[0] if prices else {}

        cur_node = block.get("current") or {}
        low_node = block.get("lowest") or {}

        current = None
        if cur_node:
            shop = (cur_node.get("shop") or {}).get("name")
            current = {
                "amount": _amount(cur_node),
                "shop": shop,
                "discount_pct": cur_node.get("cut"),
                "url": cur_node.get("url"),
                "expiry": cur_node.get("expiry"),   # セール終了日時（null=期限なし/未定）
            }

        lowest = None
        if low_node:
            ts = low_node.get("timestamp")
            lowest = {
                "amount": _amount(low_node),
                "date": ts[:10] if isinstance(ts, str) else None,
            }

        # 定価は current.regular を優先、なければ block直下を探る
        regular_node = cur_node.get("regular") or block.get("regular") or {}
        regular = {"amount": regular_node.get("amount")} if regular_node else None

        return {"current": current, "lowest": lowest, "regular": regular}

    def history(self, game_id):
        """価格履歴を [{"date", "amount", "shop"}] の昇順リストで返す（円建てのみ）。

        history/v2 はショップごとに通貨が異なる点（例: 海外ストアはUSD/EUR）を
        返すことがある。JPY以外を混ぜるとグラフの最安値・スケールが壊れるため、
        通貨が判明していてJPYでない点は除外する。
        """
        params = {"key": self.key, "id": game_id, "country": COUNTRY}
        res = _get("/games/history/v2", params)
        if not isinstance(res, list):
            return []

        points = []
        for entry in res:
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp")
            deal = entry.get("deal") or {}
            amount = _amount(deal)
            currency = _currency(deal)
            if amount is None:
                # deal 直下に amount がある形式も一応拾う
                amount = deal.get("amount")
                currency = currency or deal.get("currency")
            # 通貨が判明していてJPYでなければ捨てる（不明なら後方互換で残す）
            if currency is not None and currency != CURRENCY:
                continue
            shop = (entry.get("shop") or {}).get("name")
            if ts and amount is not None:
                points.append({
                    "date": ts[:10] if isinstance(ts, str) else None,
                    "amount": amount,
                    "shop": shop,
                })

        points.sort(key=lambda p: p["date"] or "")
        return points

    def info(self, game_id):
        """ゲーム情報を返す。画像アセットと Steam appid を取り出す。

        戻り値: {"assets": {"boxart","banner145","banner300","banner400",
                            "banner600"}, "appid": int | None}
        assets は取得できなければ空dict。
        """
        res = _get("/games/info/v2", {"key": self.key, "id": game_id})
        if not isinstance(res, dict):
            return {"assets": {}, "appid": None}
        assets = res.get("assets")
        return {
            "assets": assets if isinstance(assets, dict) else {},
            "appid": res.get("appid"),
        }

    def deals(self, sort="-cut", limit=200, offset=0):
        """現在セール中のゲーム一覧を取得する（自動収集の候補源）。

        /deals/v2 は1リクエストで最大 limit 件を返すため候補集めが安価。
        各要素に ITAD の id が含まれるので、後段で lookup_id を省ける。

        引数:
            sort:   "-cut"（割引率降順）/ "-trending"（人気降順）等
            limit:  1回の取得件数（APIの上限に従う。既定200）
            offset: ページング用オフセット
        戻り値: [{"id","slug","title","type","mature","shop",
                   "price","currency","regular","cut"}]
        """
        params = {
            "key": self.key, "country": COUNTRY,
            "sort": sort, "limit": limit, "offset": offset,
        }
        res = _get("/deals/v2", params)
        # レスポンスは {"list": [...], "hasMore":..., "nextOffset":...} を想定
        lst = res.get("list") if isinstance(res, dict) else res
        if not isinstance(lst, list):
            return []

        out = []
        for item in lst:
            if not isinstance(item, dict):
                continue
            deal = item.get("deal") or {}
            price = deal.get("price") or {}
            regular = deal.get("regular") or {}
            shop = (deal.get("shop") or {}).get("name")
            out.append({
                "id": item.get("id"),
                "slug": item.get("slug"),
                "title": item.get("title"),
                "type": item.get("type"),          # "game" / "dlc" / "package" ...
                "mature": bool(item.get("mature")),
                "shop": shop,
                "price": price.get("amount"),
                "currency": price.get("currency"),
                "regular": regular.get("amount"),
                "cut": deal.get("cut"),
                "url": deal.get("url"),
            })
        return out
