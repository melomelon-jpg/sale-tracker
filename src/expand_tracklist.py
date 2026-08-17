# -*- coding: utf-8 -*-
"""追跡対象（人気タイトル）リストの拡張: ITADの人気ランキングから候補を集め、
data/tracked.json を作る/更新する。

fetch_data.py（毎日実行）とは別に、必要なとき手動実行する想定。
    python src/expand_tracklist.py

「セール中のものを集める」discover()（/deals/v2、fetch_data.py側）とは別の
データソース（/stats/most-popular等）を使い、セール中でない定価のゲームも
含めて「主要タイトル」を追跡対象に加えるための仕組み。

方針（CLAUDE.md参照）:
  - ここで作るのは「追跡対象リスト」（id/slug/title）だけ。価格・履歴・
    Steamジャンル/レビュー数は次回の fetch_data.py 実行時に通常の経路で取得する
    （data/tracked.json 自体は外部APIの価格情報を持たない）。
  - 既存の手動リスト（config/games.json games[]）と重複するものは除外する。
  - config/games.json の "track" セクションで有効化・件数上限・足切りを調整する。
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_data import load_env, load_games, load_config, _safe_slug  # noqa: E402
from itad_client import ITADClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TRACKED_PATH = DATA_DIR / "tracked.json"

_TRACK_DEFAULTS = {
    "enabled": False,
    "target_count": 1000,
    "sources": ["most-popular", "most-collected", "most-waitlisted"],
    "allowed_types": ["game"],
    "exclude_mature": True,
    "page_limit": 500,     # 1ソースあたりの1回のページング取得件数（API上限500）
    "max_pages_per_source": 10,  # 1ソースにつき最大何ページ集めるか（暴走防止）
}


def load_track_config():
    cfg = load_config().get("track") or {}
    return {**_TRACK_DEFAULTS, **cfg}


def load_tracked():
    """既存の data/tracked.json を {id: entry} で返す。無ければ空。"""
    if not TRACKED_PATH.exists():
        return {}
    try:
        data = json.loads(TRACKED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {e["id"]: e for e in data.get("items", []) if e.get("id")}


def save_tracked(items, ts):
    payload = {"updated_at": ts, "items": list(items.values())}
    TRACKED_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


_SOURCE_METHODS = {
    "most-popular": "stats_most_popular",
    "most-collected": "stats_most_collected",
    "most-waitlisted": "stats_most_waitlisted",
}


def collect_candidates(client, cfg):
    """設定されたソースをページングして、条件を満たす候補を人気順に集める。

    複数ソースをラウンドロビンで交互に採用する（discover()と同じ考え方）。
    1つのソースに偏らず、waitlist人気・collected人気の両方をバランス良く拾う。
    """
    allowed_types = set(cfg["allowed_types"])
    exclude_mature = cfg["exclude_mature"]
    page_limit = cfg["page_limit"]
    max_pages = cfg["max_pages_per_source"]

    def qualifies(r):
        if not r.get("id"):
            return False
        if allowed_types and r.get("type") not in allowed_types:
            return False
        if exclude_mature and r.get("mature"):
            return False
        return True

    per_source = []
    for name in cfg["sources"]:
        method_name = _SOURCE_METHODS.get(name)
        if not method_name:
            print(f"  [WARN] 不明なソース '{name}' をスキップ")
            continue
        method = getattr(client, method_name)
        rows = []
        for page in range(max_pages):
            offset = page * page_limit
            try:
                batch = method(offset=offset, limit=page_limit)
            except Exception as e:
                print(f"  [WARN] {name}(offset={offset}) 失敗: {type(e).__name__}: {e}")
                break
            if not batch:
                break
            rows.extend(r for r in batch if qualifies(r))
            print(f"  {name}: offset={offset} +{len(batch)}件 取得（条件通過 {len(rows)}件累計）")
            if len(batch) < page_limit:
                break  # ソース側の残りが尽きた
        per_source.append(rows)

    # ラウンドロビンで交互に採用（discover()と同じ発想: 1ソースに偏らせない）
    interleaved = []
    seen = set()
    depth = max((len(p) for p in per_source), default=0)
    for i in range(depth):
        for plist in per_source:
            if i < len(plist) and plist[i]["id"] not in seen:
                seen.add(plist[i]["id"])
                interleaved.append(plist[i])
    return interleaved


def main():
    load_env()
    import os
    api_key = os.environ.get("ITAD_API_KEY", "").strip()
    if not api_key:
        print("エラー: ITAD_API_KEY が未設定です。.env を作成するか環境変数を設定してください。")
        sys.exit(1)

    cfg = load_track_config()
    if not cfg["enabled"]:
        print("config/games.json の track.enabled が false です。何もせず終了します。")
        print('  例: "track": {"enabled": true, "target_count": 1000}')
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    client = ITADClient(api_key)

    manual_games = load_games()
    manual_slugs = {g["slug"] for g in manual_games}
    manual_titles = {(g.get("title") or "").lower() for g in manual_games}

    print(f"追跡対象リストを拡張します（目標 {cfg['target_count']} 本、"
          f"ソース: {', '.join(cfg['sources'])}）…")
    candidates = collect_candidates(client, cfg)
    print(f"候補 {len(candidates)} 件を収集")

    existing = load_tracked()
    today = date.today().isoformat()
    kept = dict(existing)  # 既存分は position/first_seen を保ったまま更新
    order = []
    for r in candidates:
        gid = r["id"]
        slug = _safe_slug(r.get("slug") or r.get("title"))
        if slug in manual_slugs or (r.get("title") or "").lower() in manual_titles:
            continue  # 手動登録済みと重複するものは除外（枠の無駄遣いを防ぐ）
        entry = kept.get(gid, {})
        kept[gid] = {
            "id": gid,
            "slug": entry.get("slug") or slug,
            "title": entry.get("title") or r.get("title"),
            "first_seen": entry.get("first_seen") or today,
            "popularity_rank": len(order),  # 今回の収集順（daily_history_top_n の優先度に使う）
            # 以下はfetch_data.py側が書き戻すキャッシュ欄。拡張時点では触らない。
            "assets": entry.get("assets"),
            "appid": entry.get("appid"),
            "info_fetched": entry.get("info_fetched", False),
            "lowest": entry.get("lowest"),
        }
        order.append(gid)

    # 目標件数までに切り詰める（今回の人気順を優先、既存で漏れた分は捨てる）
    final_ids = order[:cfg["target_count"]]
    trimmed = {gid: kept[gid] for gid in final_ids}

    dropped = len(kept) - len(trimmed)
    save_tracked(trimmed, today)
    print(f"保存完了: data/tracked.json ({len(trimmed)}件"
          f"{f'、対象外になった{dropped}件は削除' if dropped > 0 else ''})")


if __name__ == "__main__":
    main()
