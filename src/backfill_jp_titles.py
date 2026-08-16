# -*- coding: utf-8 -*-
"""既存の data/latest.json に日本語タイトル/ジャンル/レビュー数を一括で埋める一回限りのツール。

fetch_data.py は日々の実行時に新規ゲーム分（+ 旧スキーマ分）だけ Steam(cc=jp) へ
問い合わせて data/steam_info.json を育てていくが、それとは別に「今ある全件に
まとめてバックフィルしたい」ときのために本スクリプトを用意した。
ITAD_API_KEY は不要（Steamのみを叩く。ITADへは問い合わせない）。

実行:
    python src/backfill_jp_titles.py
"""
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import steam_client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
STEAM_INFO_PATH = DATA_DIR / "steam_info.json"


def load_steam_info():
    if not STEAM_INFO_PATH.exists():
        return {}
    try:
        return json.loads(STEAM_INFO_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_steam_info(cache):
    STEAM_INFO_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def main():
    latest = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    games = latest.get("games", [])
    steam_info = load_steam_info()

    new_calls = 0
    skipped_no_appid = 0
    for i, g in enumerate(games, 1):
        appid = g.get("steam_appid") or g.get("appid")
        key = str(appid) if appid else None
        if not key:
            g["title_jp"] = None
            g["genres"] = []
            g["categories"] = []
            g["review_count"] = None
            skipped_no_appid += 1
            continue

        cached = steam_info.get(key)
        if cached is None or "review_count" not in cached:
            info = steam_client.get_app_info(appid)
            steam_info[key] = {
                "name": info["name"] if info else None,
                "genres": info["genres"] if info else [],
                "categories": info["categories"] if info else [],
                "review_count": info["review_count"] if info else None,
                "checked_at": None,
            }
            new_calls += 1
            print(f"  [{i}/{len(games)}] appid={appid} title={g.get('title')!r} -> "
                  f"{steam_info[key]['name']!r} review_count={steam_info[key]['review_count']}")
            time.sleep(1.0)
            if new_calls % 20 == 0:
                save_steam_info(steam_info)

        cached = steam_info.get(key) or {}
        g["title_jp"] = cached.get("name")
        g["genres"] = cached.get("genres") or []
        g["categories"] = cached.get("categories") or []
        g["review_count"] = cached.get("review_count")

    save_steam_info(steam_info)
    LATEST_PATH.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完了: 新規取得 {new_calls} 件 / appid不明でスキップ {skipped_no_appid} 件 / "
          f"キャッシュ総数 {len(steam_info)} 件")


if __name__ == "__main__":
    main()
