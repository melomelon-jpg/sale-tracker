# -*- coding: utf-8 -*-
"""①データ取得: ITAD(主)/Steam(補助) から価格を取り、data/ にJSON保存する。

1日1回実行する想定。ここだけがAPIを叩く。サイト生成(build_site.py)は
このスクリプトが吐いた data/ しか見ない。

実行:
    python src/fetch_data.py
必要:
    .env または環境変数 ITAD_API_KEY
出力:
    data/latest.json         … 全ゲームの最新スナップショット
    data/history/<slug>.json … ゲーム別 価格履歴
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# src/ を import パスに追加（単体実行でも動くように）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import steam_client
import verdict
from itad_client import ITADClient, ITADError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "games.json"
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
DISCOVERED_PATH = DATA_DIR / "discovered.json"  # 自動収集プールの永続化
CURRENCY = "JPY"


def _safe_slug(s):
    """ファイル名に使える slug に正規化（英小文字・数字・ハイフンのみ）。"""
    s = re.sub(r"[^a-z0-9-]+", "-", (s or "").lower()).strip("-")
    return s or "game"


def load_env():
    """.env があれば読み込んで os.environ に反映（KEY=VALUE の素朴なパーサ）。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_games():
    return load_config().get("games", [])


def load_discovery_config():
    return load_config().get("discovery", {}) or {}


def load_discovered():
    """前回の自動収集プールを {itad_id: entry} で返す。無ければ空。"""
    if not DISCOVERED_PATH.exists():
        return {}
    try:
        data = json.loads(DISCOVERED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {e["id"]: e for e in data.get("items", []) if e.get("id")}


def save_discovered(items, ts):
    payload = {"updated_at": ts, "items": list(items.values())}
    DISCOVERED_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def discover(client, cfg, manual_slugs, manual_titles, previous_items):
    """/deals/v2 からセール中候補を集め、条件で絞った自動プールを返す。

    フィルタ: 本編ゲームのみ（allowed_types）/ mature除外 / 割引率しきい値。
    プールは last_on_sale を更新しつつ prune_days を超えた項目を剪定し、
    max_auto 件に制限する（追跡総数=レート消費を有界にするため）。
    戻り値: {itad_id: {"id","slug","title","first_seen","last_on_sale"}}
    """
    today = date.today().isoformat()
    allowed_types = set(cfg.get("allowed_types") or ["game"])
    exclude_mature = cfg.get("exclude_mature", True)
    min_cut = cfg.get("min_cut_pct", 60)
    min_price = cfg.get("min_price_jpy", 1)   # ¥0(無料配布/デモ)を除外
    limit = cfg.get("fetch_limit", 200)
    max_auto = cfg.get("max_auto", 20)
    prune_days = cfg.get("prune_days", 14)

    def qualifies(r):
        if not r.get("id"):
            return False
        if allowed_types and r.get("type") not in allowed_types:
            return False            # DLC/サントラ/パッケージ/type不明を除外
        if exclude_mature and r.get("mature"):
            return False            # アダルト向けを除外
        slug = _safe_slug(r.get("slug") or r.get("title"))
        if slug in manual_slugs or (r.get("title") or "").lower() in manual_titles:
            return False            # 手動登録と重複するものは除外
        if (r.get("cut") or 0) < min_cut:
            return False
        price = r.get("price")
        if price is None or price < min_price:
            return False            # ¥0(無料配布/デモ)や価格不明を除外
        return True

    # 各sortの順位を保ったまま、資格のある候補だけを残す
    per_sort = []
    for sort in cfg.get("sorts") or ["-cut"]:
        try:
            rows = client.deals(sort=sort, limit=limit)
        except Exception as e:
            print(f"  [WARN] deals(sort={sort}) 失敗: {type(e).__name__}: {e}")
            rows = []
        client._sleep()
        per_sort.append([r for r in rows if qualifies(r)])

    # ラウンドロビンで交互に採用し、割引順(-cut)と人気順(-trending)を両立させる
    interleaved = []
    seen = set()
    depth = max((len(p) for p in per_sort), default=0)
    for i in range(depth):
        for plist in per_sort:
            if i < len(plist) and plist[i]["id"] not in seen:
                seen.add(plist[i]["id"])
                interleaved.append(plist[i])

    # 既存プールへマージ（last_on_sale を更新、新規は first_seen を記録）
    pool = dict(previous_items)
    order_today = []
    for r in interleaved:
        gid = r["id"]
        slug = _safe_slug(r.get("slug") or r.get("title"))
        entry = pool.get(gid, {})
        pool[gid] = {
            "id": gid,
            "slug": entry.get("slug") or slug,
            "title": entry.get("title") or r.get("title"),
            "first_seen": entry.get("first_seen") or today,
            "last_on_sale": today,
        }
        order_today.append(gid)

    # 剪定: prune_days を超えて確認できていない項目・手動重複を落とす
    cutoff = (date.today() - timedelta(days=prune_days)).isoformat()
    pool = {
        gid: e for gid, e in pool.items()
        if (e.get("last_on_sale") or "") >= cutoff
        and e.get("slug") not in manual_slugs
    }

    # 優先度: 今日のインターリーブ順を先頭に、残枠は直近セール日順の繰り越し
    today_ids = [gid for gid in order_today if gid in pool]
    today_set = set(today_ids)
    carry = sorted(
        (e for gid, e in pool.items() if gid not in today_set),
        key=lambda e: (e.get("last_on_sale") or "", e.get("first_seen") or ""),
        reverse=True,
    )
    ordered_ids = today_ids + [e["id"] for e in carry]
    return {gid: pool[gid] for gid in ordered_ids[:max_auto]}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_one(client, game):
    """1ゲーム分を取得し、latest用のレコードと履歴を返す。"""
    slug = game["slug"]
    title = game["title"]
    steam_appid = game.get("steam_appid")

    record = {
        "slug": slug,
        "title": title,
        "itad_id": None,
        "steam_appid": steam_appid,
        "source": game.get("source", "manual"),
        "jp_support": None,
        "assets": {},
        "appid": None,
        "regular": None,
        "current": None,
        "lowest": None,
        "verdict": dict(verdict.UNKNOWN_VERDICT),
        "on_sale": False,
    }
    history = []

    # 自動収集で既にIDが分かっている場合は lookup を省く（1リクエスト節約）
    itad_id = game.get("itad_id") or client.lookup_id(title)
    record["itad_id"] = itad_id

    if itad_id:
        ov = client.overview(itad_id)
        client._sleep()
        record["current"] = ov["current"]
        record["lowest"] = ov["lowest"]
        record["regular"] = ov["regular"]
        history = client.history(itad_id)
        client._sleep()
        # ゲーム情報（画像アセット・appid）を取得
        try:
            gi = client.info(itad_id)
            record["assets"] = gi.get("assets") or {}
            record["appid"] = gi.get("appid")
        except Exception:
            record["assets"] = {}
        client._sleep()

    # ITADで現在価格が欠けたら Steam で補完
    if (not record["current"] or record["current"].get("amount") is None) and steam_appid:
        s = steam_client.price_overview(steam_appid)
        if s and s.get("currency") == CURRENCY:
            record["current"] = {
                "amount": s["current"],
                "shop": "Steam",
                "discount_pct": s["discount_pct"],
                "url": f"https://store.steampowered.com/app/{steam_appid}/",
            }
            if not record["regular"]:
                record["regular"] = {"amount": s["regular"]}

    # 買い時判定
    cur_amt = (record["current"] or {}).get("amount")
    low_amt = (record["lowest"] or {}).get("amount")
    record["verdict"] = verdict.judge(cur_amt, low_amt)

    # セール中フラグ（割引率>0）
    disc = (record["current"] or {}).get("discount_pct") or 0
    record["on_sale"] = disc > 0

    # 日本語対応の判定（Steam appid が分かるときのみ・別ホストなので低コスト）
    if steam_appid:
        try:
            record["jp_support"] = steam_client.supports_japanese(steam_appid)
        except Exception:
            record["jp_support"] = None

    return record, history


def main():
    load_env()
    api_key = os.environ.get("ITAD_API_KEY", "").strip()
    if not api_key:
        print("エラー: ITAD_API_KEY が未設定です。.env を作成するか環境変数を設定してください。")
        print("  例(PowerShell): $env:ITAD_API_KEY = 'あなたのキー'")
        sys.exit(1)

    client = ITADClient(api_key)
    manual_games = load_games()
    if not manual_games:
        print("config/games.json に対象ゲームがありません。")
        sys.exit(1)

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    ts = now_iso()

    manual_slugs = {g["slug"] for g in manual_games}
    manual_titles = {(g.get("title") or "").lower() for g in manual_games}

    # --- 自動収集: セール中の候補をプールへ取り込む ---
    cfg = load_discovery_config()
    kept = load_discovered()
    if cfg.get("enabled"):
        print("自動収集: /deals/v2 から候補を取得中…")
        try:
            kept = discover(client, cfg, manual_slugs, manual_titles, kept)
        except Exception as e:
            print(f"  [WARN] 自動収集に失敗: {type(e).__name__}: {e}（前回プールを維持）")
        save_discovered(kept, ts)
        print(f"  自動枠 {len(kept)} 件（手動 {len(manual_games)} 件 + 自動）")

    # 追跡対象 = 手動（source=manual）+ 自動（source=auto, ITAD id 既知）
    targets = [dict(g, source="manual") for g in manual_games]
    for e in kept.values():
        targets.append({
            "slug": _safe_slug(e["slug"]),
            "title": e["title"],
            "itad_id": e["id"],
            "steam_appid": None,
            "source": "auto",
        })

    records = []
    print(f"取得開始: {len(targets)}件（手動{len(manual_games)} / 自動{len(kept)}）")
    for game in targets:
        try:
            record, history = fetch_one(client, game)
            records.append(record)
            # 履歴を個別ファイルに保存
            hist_path = HISTORY_DIR / f"{record['slug']}.json"
            hist_path.write_text(
                json.dumps(
                    {"slug": record["slug"], "updated_at": ts, "history": history},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            cur = (record["current"] or {}).get("amount")
            low = (record["lowest"] or {}).get("amount")
            print(f"  [OK] {game['title']} [{game['source']}]: 現在={cur} 最安={low} "
                  f"判定={record['verdict']['label']} 日本語={record['jp_support']} 履歴={len(history)}点")
        except Exception as e:
            print(f"  [NG] {game['title']}: {type(e).__name__}: {e}")

    # スナップショットを保存
    latest = {"generated_at": ts, "currency": CURRENCY, "games": records}
    (DATA_DIR / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"保存完了: data/latest.json ({len(records)}件)")


if __name__ == "__main__":
    main()
