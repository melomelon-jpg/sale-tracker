# -*- coding: utf-8 -*-
"""コンソール版（Nintendo Switch）価格の記録専用スクリプト。

【目的】この段階では「表示」は作らない。過去最安値を出すには数ヶ月分の
履歴が要るため、まず記録だけを毎日積み上げる（fetch_data.py/build_site.py
とは完全に独立。既存のSteamパイプラインには一切触れない）。

【対象】config/console_games.json に手動登録したタイトルのみ。
セール中タイトルの自動発見はしない（Nintendo側の検索APIはrobots.txtで
非Googlebotに対して明示的に禁止されているため使わない。詳細は
nintendo_client.py のコメント参照）。

【アクセス方針（利用規約がグレーであることを踏まえた自主ルール）】
  - 1日1回、GitHub Actionsからのみ実行する想定
  - リクエスト間隔を十分に空ける（_THROTTLE_SECONDS）
  - 身元を隠さないUser-Agent（nintendo_client.UA参照）
  - ブロックの兆候（429/403）を検知したら即座に全体を中断し、
    残りのタイトルへはリトライで押し込まない
  - 表示には使わない。記録のみ

【将来のPlayStation対応・商用データ提供元への切替に備えた設計】
  1タイトル分のレコード形式 {"date","regular","current","on_sale","sale_end"}
  はストアに依存しない共通の形にしてある。PlayStation対応時は
  playstation_client.py を追加し、この関数と同じ形のレコードを
  data/playstation/history/<slug>.json に書けば、record_store()の
  ロジックはそのまま使い回せる（現時点ではストアが1つしかないため、
  抽象クラス等の大掛かりな構造は導入しない）。

実行:
    python src/fetch_console_data.py
出力:
    data/nintendo/latest.json         … 登録タイトルの最新スナップショット
    data/nintendo/history/<slug>.json … タイトル別の日次価格履歴（1日1行）
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nintendo_client

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "console_games.json"
NINTENDO_DIR = ROOT / "data" / "nintendo"
NINTENDO_HISTORY_DIR = NINTENDO_DIR / "history"

_THROTTLE_SECONDS = 3.0  # リクエスト間隔（十分に空ける）


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_str():
    return datetime.now(timezone.utc).date().isoformat()


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_history(slug):
    path = NINTENDO_HISTORY_DIR / f"{slug}.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("history", [])
    except Exception:
        return []


def save_history(slug, title, nsuid, history):
    path = NINTENDO_HISTORY_DIR / f"{slug}.json"
    payload = {
        "slug": slug, "title": title, "nsuid": nsuid,
        "updated_at": now_iso(), "history": history,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def record_day(history, day, regular, current, on_sale, sale_end):
    """当日分を history に追記する（同じ日付が既にあれば上書き=再実行しても重複しない）。"""
    row = {"date": day, "regular": regular, "current": current}
    if on_sale:
        row["on_sale"] = True
        if sale_end:
            row["sale_end"] = sale_end
    if history and history[-1].get("date") == day:
        history[-1] = row
    else:
        history.append(row)
    return history


def main():
    cfg = load_config()
    ncfg = cfg.get("nintendo") or {}
    if not ncfg.get("enabled"):
        print("config/console_games.json: nintendo.enabled が false のため何もしません。")
        return
    games = ncfg.get("games") or []
    if not games:
        print("config/console_games.json: nintendo.games が空です。")
        return

    NINTENDO_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    day = today_str()
    ts = now_iso()

    latest = []
    blocked = False
    print(f"Nintendo eShop(JP) 価格記録: 対象{len(games)}件")
    for i, game in enumerate(games):
        slug, title, nsuid = game["slug"], game["title"], game["nsuid"]
        try:
            result = nintendo_client.price(nsuid)
        except nintendo_client.NintendoBlockedError as e:
            print(f"  [BLOCKED] {title}: {e}")
            print("  ブロックの兆候を検知したため、残りのタイトルの取得を中断します。")
            blocked = True
            break
        except nintendo_client.NintendoError as e:
            print(f"  [NG] {title}: {type(e).__name__}: {e}")
            if i < len(games) - 1:
                time.sleep(_THROTTLE_SECONDS)
            continue

        if result is None:
            print(f"  [NG] {title}: nsuid={nsuid} が見つかりません（登録ミスの可能性）")
        else:
            history = load_history(slug)
            history = record_day(
                history, day, result["regular"], result["current"],
                result["on_sale"], result.get("sale_end"),
            )
            save_history(slug, title, nsuid, history)
            latest.append({
                "slug": slug, "title": title, "nsuid": nsuid,
                "regular": result["regular"], "current": result["current"],
                "on_sale": result["on_sale"],
            })
            sale_txt = "セール中" if result["on_sale"] else "通常価格"
            print(f"  [OK] {title}: 現在={result['current']}円 定価={result['regular']}円 ({sale_txt})")

        if i < len(games) - 1:
            time.sleep(_THROTTLE_SECONDS)

    (NINTENDO_DIR / "latest.json").write_text(
        json.dumps({"generated_at": ts, "currency": nintendo_client.CURRENCY, "games": latest},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"保存完了: data/nintendo/latest.json ({len(latest)}件)")
    if blocked:
        print("[WARN] ブロック検知により今回は途中終了しました。次回実行時に再試行されます。")


if __name__ == "__main__":
    main()
