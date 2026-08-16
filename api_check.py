# -*- coding: utf-8 -*-
"""
PCゲーム セール情報サイト 技術検証スクリプト
検証項目:
  1. CheapShark API      : 現在セール中のゲーム一覧が取れるか
  2. IsThereAnyDeal API  : 特定ゲームの価格履歴・過去最安値が取れるか（要APIキー）
  3. 【最重要】日本円     : JPYで価格が取れるか（ITAD country=JP / Steam appdetails cc=jp）

依存: 標準ライブラリのみ（requests不要）
実行: python api_check.py
ITADキー: 環境変数 ITAD_API_KEY があれば使用。無ければ該当部分をスキップ。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

# 検証対象ゲーム（タイトル と Steam appid）
TARGET_GAMES = [
    {"title": "ELDEN RING",     "steam_appid": 1245620},
    {"title": "Hades",          "steam_appid": 1145360},
    {"title": "Stardew Valley", "steam_appid": 413150},
]

UA = "sale-tracker-poc/0.1 (tech verification; contact: neoregion222pas@gmail.com)"

# 検証結果を集約（最後に一覧表示）
RESULTS = []  # (項目, OK/NG/SKIP, 補足)


def record(item, status, note=""):
    RESULTS.append((item, status, note))


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return raw


def http_post_json(url, payload, headers=None, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    h = {"User-Agent": UA, "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return raw


def hr(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------------------
# 1. CheapShark API : 現在セール中の一覧（タイトル/定価/セール価格/割引率/ストア名）
# ---------------------------------------------------------------------------
def check_cheapshark():
    hr("[1] CheapShark API — 現在セール中のゲーム一覧")
    base = "https://www.cheapshark.com/api/1.0"
    try:
        # まずストアID→ストア名の対応表を取得
        stores_raw = http_get(f"{base}/stores")
        stores = json.loads(stores_raw)
        store_name = {s["storeID"]: s["storeName"] for s in stores}

        # セール一覧（割引率が高い順、上位10件）
        params = urllib.parse.urlencode({
            "pageSize": 10,
            "sortBy": "Savings",
            "desc": 1,
            "onSale": 1,
        })
        deals_raw = http_get(f"{base}/deals?{params}")
        deals = json.loads(deals_raw)

        if not deals:
            record("1. CheapShark セール一覧", "NG", "空のレスポンス")
            print("  取得0件")
            return

        print(f"  取得 {len(deals)} 件（上位表示）:")
        print(f"  {'タイトル':<38} {'定価':>7} {'セール':>7} {'割引':>5}  ストア")
        for d in deals[:10]:
            title = d.get("title", "")[:36]
            normal = float(d.get("normalPrice", 0))
            sale = float(d.get("salePrice", 0))
            savings = round(float(d.get("savings", 0)))
            sname = store_name.get(d.get("storeID"), "?")
            print(f"  {title:<38} ${normal:>6.2f} ${sale:>6.2f} {savings:>4}%  {sname}")

        # 通貨の確認（CheapSharkはUSDのみ）
        record("1. CheapShark セール一覧（title/定価/セール/割引/ストア）", "OK",
               f"{len(deals)}件取得。ストア名はstoresで名前解決")
        record("1b. CheapShark 通貨", "USDのみ", "JPY非対応（ドル固定）")
    except urllib.error.HTTPError as e:
        record("1. CheapShark セール一覧", "NG", f"HTTP {e.code}")
        print(f"  HTTPError: {e.code} {e.reason}")
    except Exception as e:
        record("1. CheapShark セール一覧", "NG", f"{type(e).__name__}: {e}")
        print(f"  エラー: {e}")


# ---------------------------------------------------------------------------
# 2 & 3(ITAD). IsThereAnyDeal API : 価格履歴・過去最安値・JPY対応
# ---------------------------------------------------------------------------
def check_itad():
    hr("[2] IsThereAnyDeal API — 価格履歴・過去最安値（+ JPY対応）")
    key = os.environ.get("ITAD_API_KEY", "").strip()
    if not key:
        print("  環境変数 ITAD_API_KEY が未設定のためスキップ。")
        print("  取得するには https://isthereanydeal.com/apps/ でアプリ登録→キー取得。")
        print("  設定例(PowerShell):  $env:ITAD_API_KEY = 'あなたのキー'")
        record("2. ITAD 価格履歴/過去最安値", "SKIP", "APIキー未設定（要登録・無料）")
        record("3b. ITAD JPY価格(country=JP)", "SKIP", "APIキー未設定")
        return

    base = "https://api.isthereanydeal.com"
    ok_hist = ok_low = ok_jpy = False
    for g in TARGET_GAMES:
        title = g["title"]
        try:
            # (a) タイトル→ITADゲームID
            q = urllib.parse.urlencode({"key": key, "title": title})
            look = json.loads(http_get(f"{base}/games/lookup/v1?{q}"))
            if not look.get("found"):
                print(f"  [{title}] ID解決できず")
                continue
            gid = look["game"]["id"]

            # (b) 概要: 現在価格 + 過去最安値（country=JP で円建てを試す）
            q2 = urllib.parse.urlencode({"key": key, "country": "JP"})
            overview = json.loads(
                http_post_json(f"{base}/games/overview/v2?{q2}", [gid])
            )
            price_block = None
            prices = overview.get("prices") or []
            if prices:
                price_block = prices[0]

            cur_cur = cur_amt = low_cur = low_amt = None
            if price_block:
                cur = price_block.get("current") or {}
                low = price_block.get("lowest") or {}
                cur_amt = (cur.get("price") or {}).get("amount")
                cur_cur = (cur.get("price") or {}).get("currency")
                low_amt = (low.get("price") or {}).get("amount")
                low_cur = (low.get("price") or {}).get("currency")

            # (c) 価格履歴
            q3 = urllib.parse.urlencode({"key": key, "id": gid, "country": "JP"})
            hist = json.loads(http_get(f"{base}/games/history/v2?{q3}"))
            hist_n = len(hist) if isinstance(hist, list) else 0

            print(f"  [{title}]  現在:{cur_amt} {cur_cur} / 過去最安:{low_amt} {low_cur} / 履歴点数:{hist_n}")
            if hist_n > 0:
                ok_hist = True
            if low_amt is not None:
                ok_low = True
            if (cur_cur == "JPY") or (low_cur == "JPY"):
                ok_jpy = True
            time.sleep(0.5)  # レート配慮
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            print(f"  [{title}] HTTP {e.code}: {body}")
        except Exception as e:
            print(f"  [{title}] エラー: {type(e).__name__}: {e}")

    record("2. ITAD 価格履歴", "OK" if ok_hist else "NG", "history/v2")
    record("2. ITAD 過去最安値", "OK" if ok_low else "NG", "overview/v2 lowest")
    record("3b. ITAD JPY価格(country=JP)", "OK" if ok_jpy else "NG",
           "overviewの通貨がJPYか")


# ---------------------------------------------------------------------------
# 2b. ITAD Deals List : 自動収集の候補源（/deals/v2）が使えるか
# ---------------------------------------------------------------------------
def check_itad_deals():
    hr("[2b] IsThereAnyDeal Deals List — 現在セール中の自動収集候補（/deals/v2）")
    key = os.environ.get("ITAD_API_KEY", "").strip()
    if not key:
        print("  環境変数 ITAD_API_KEY が未設定のためスキップ。")
        record("2b. ITAD Deals List(/deals/v2)", "SKIP", "APIキー未設定")
        return

    base = "https://api.isthereanydeal.com"
    try:
        q = urllib.parse.urlencode({
            "key": key, "country": "JP",
            "sort": "-cut", "limit": 20, "offset": 0,
        })
        res = json.loads(http_get(f"{base}/deals/v2?{q}"))
        lst = res.get("list") if isinstance(res, dict) else res
        if not isinstance(lst, list) or not lst:
            record("2b. ITAD Deals List(/deals/v2)", "NG", "list が空/想定外の形")
            print(f"  想定外レスポンス。トップレベルのキー: "
                  f"{list(res.keys()) if isinstance(res, dict) else type(res).__name__}")
            return

        # スキーマ確認: 先頭要素のキーと deal のキーを出す
        first = lst[0]
        print(f"  取得 {len(lst)} 件。要素のキー: {list(first.keys())}")
        print(f"  deal のキー: {list((first.get('deal') or {}).keys())}")

        jpy_ok = True
        print(f"  {'タイトル':<34} {'種別':<8} {'成人':<4} {'割引':>5} {'価格':>9} 通貨")
        for item in lst[:10]:
            deal = item.get("deal") or {}
            price = deal.get("price") or {}
            title = (item.get("title") or "")[:32]
            typ = str(item.get("type"))
            mature = "有" if item.get("mature") else "無"
            cut = deal.get("cut")
            amt = price.get("amount")
            cyc = price.get("currency")
            if cyc and cyc != "JPY":
                jpy_ok = False
            print(f"  {title:<34} {typ:<8} {mature:<4} {str(cut):>4}% {str(amt):>9} {cyc}")

        record("2b. ITAD Deals List(/deals/v2)", "OK",
               f"{len(lst)}件。type/mature/cut を確認")
        record("2b. Deals 通貨(JPY)", "OK" if jpy_ok else "要確認",
               "price.currency が JPY か")
        record("2b. Deals フィルタ材料(type/mature)", "OK",
               "本編ゲーム限定・mature除外の判定に使用可")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        record("2b. ITAD Deals List(/deals/v2)", "NG", f"HTTP {e.code}")
        print(f"  HTTP {e.code}: {body}")
    except Exception as e:
        record("2b. ITAD Deals List(/deals/v2)", "NG", f"{type(e).__name__}: {e}")
        print(f"  エラー: {e}")


# ---------------------------------------------------------------------------
# 3. Steam appdetails : cc=jp で日本円価格が取れるか（最重要フォールバック）
# ---------------------------------------------------------------------------
def check_steam_jpy():
    hr("[3] Steam appdetails (cc=jp) — 日本円価格の取得")
    ok = False
    for g in TARGET_GAMES:
        appid = g["steam_appid"]
        title = g["title"]
        try:
            params = urllib.parse.urlencode({
                "appids": appid,
                "cc": "jp",
                "l": "japanese",
                "filters": "price_overview",
            })
            url = f"https://store.steampowered.com/api/appdetails?{params}"
            data = json.loads(http_get(url))
            node = data.get(str(appid), {})
            if not node.get("success"):
                print(f"  [{title}] success=false（無料/地域制限/未発売の可能性）")
                continue
            po = (node.get("data") or {}).get("price_overview")
            if not po:
                print(f"  [{title}] price_overview なし（無料タイトルの可能性）")
                continue
            currency = po.get("currency")
            initial = po.get("initial", 0) / 100.0   # Steamは最小単位(銭)なので/100
            final = po.get("final", 0) / 100.0
            discount = po.get("discount_percent", 0)
            disp = po.get("final_formatted", "")
            print(f"  [{title}]  通貨:{currency}  定価:{initial:,.0f}  現価:{final:,.0f}  割引:{discount}%  表示:{disp}")
            if currency == "JPY":
                ok = True
            time.sleep(0.5)  # レート配慮
        except urllib.error.HTTPError as e:
            print(f"  [{title}] HTTP {e.code}")
        except Exception as e:
            print(f"  [{title}] エラー: {type(e).__name__}: {e}")

    record("3a. Steam appdetails JPY価格(cc=jp)", "OK" if ok else "NG",
           "price_overview.currency=JPY")


def print_summary():
    hr("検証結果サマリ（取れた◯ / 取れない✕ / 保留△）")
    mark = {"OK": "◯ 取得OK", "NG": "✕ 取得NG", "SKIP": "△ 保留(要キー)"}
    for item, status, note in RESULTS:
        label = mark.get(status, status)
        print(f"  [{label:<14}] {item}")
        if note:
            print(f"                   └ {note}")


def main():
    print("PCゲーム セール情報サイト 技術検証")
    print(f"Python {sys.version.split()[0]}  /  対象: " +
          ", ".join(g["title"] for g in TARGET_GAMES))
    check_cheapshark()
    check_itad()
    check_itad_deals()
    check_steam_jpy()
    print_summary()


if __name__ == "__main__":
    main()
