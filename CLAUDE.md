# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

「ゲーム最安隊」— Steamゲームの価格を毎日追跡し、過去最安値と比較した「買い時」判定を表示する静的サイト。日本語のみ、依存は標準ライブラリのみ（`pip install` 不要）。

## Commands

```powershell
# ① データ取得（ITAD/Steam APIを叩く。1日1回想定）
python src/fetch_data.py

# ② サイト生成（data/ から public/ を書き出す。APIは叩かない。何度でも実行可）
python src/build_site.py

# verdict.py 単体の自己テスト（判定ロジックの境界値を確認）
python src/verdict.py

# 生成結果をブラウザで確認
start public/index.html
```

`.env` に `ITAD_API_KEY=...` が必要（`fetch_data.py` のみ）。`.env.example` からコピー。

自動実行は `.github/workflows/daily.yml`（毎日21:00 UTC = JST翌6:00、取得→生成→コミット→push）。

## Architecture

データの流れは一方向・厳密に分離されている:

```
config/games.json → fetch_data.py → data/*.json → build_site.py → public/*.html
   (監視対象)          (①取得/API)     (保存データ)     (②生成/APIなし)
```

- **`fetch_data.py`（取得専用）**: ここだけが外部APIを叩く。ITAD (isthereanydeal.com) が主データ源、Steam appdetails (`cc=jp`) が補助・裏取り。手動登録ゲーム（`config/games.json`）に加え、`/deals/v2` からセール中タイトルを自動収集するプール機構（`discover()`）を持つ。価格・履歴は Steam ショップID (`STEAM_SHOP_ID=61`) に絞り込んで取得する（鍵屋の投げ売りで判定が歪まないように、サイト全体の前提として一貫している）。`data/steam_info.json` と自動収集プール(`data/discovered.json`)内の assets/appid/lowest はAPI呼び出し削減のための永続キャッシュ — 一時的な失敗時にキャッシュへ書き込まないことで「取得失敗が永続的な情報欠損になる」不具合を防ぐ設計になっている（コード内コメントに経緯あり）。
- **`data/`**: `fetch_data.py` の出力であり `build_site.py` の入力。`latest.json`（全ゲームの最新スナップショット）と `history/<slug>.json`（ゲーム別価格履歴）。**手で編集しない** — 次回 fetch で上書きされる。
- **`build_site.py`（生成専用）**: `data/` の保存済みJSONのみを読み、外部通信は一切しない。テンプレートは依存を増やさないため素のPython文字列で組む（Jinja等のテンプレートエンジンは使わない）。価格履歴グラフは外部JS不要のインラインSVG（`sparkline_svg()`）。トップページ/全件ページ/ジャンル別ページ/ゲーム詳細ページ/aboutページ/sitemap/robots.txtを生成する。
- **`src/verdict.py`**: 買い時判定の純粋関数（`judge(current_amount, lowest_amount)`）。外部依存なし。判定しきい値（`VERDICT_RULES`）を変えると `fetch_data.py`（データ保存時）と `build_site.py`（表示時に再計算）の両方に効く。
- **`config/games.json`**: `games`（手動監視対象）、`discovery`（自動収集の閾値・上限）、`featured`（トップページ「注目のセール」の足切り基準）を持つ。

### 一覧行のレンダリング

`game_row()`（build_site.py）が全一覧（トップページ各セクション・all.html・ジャンル別ページ・関連ゲーム欄）で唯一のゲーム行HTML生成元。列の並び・data-*属性はここで一元管理されており、`public/assets/deals.js` の検索/並び替え/絞り込みJSはこのdata属性に依存する。

### フロントエンドJS（軽量・ビルドなし）

- `public/assets/deals.js`: all.html の検索・並び替え・絞り込み・段階表示（ページャなし、`load-more`ボタンで追加表示）
- `public/assets/favorites.js`: `localStorage` ベースのお気に入り機能
- テーマ切替（ライト/ダーク）は `page()` 関数内にインラインで実装、`localStorage` に保存

ビルドツール・バンドラは無し。JSは直接 `public/assets/` に置かれ、`build_site.py` から素通りで参照される。

# ゲーム最安隊 設計方針

## サイトの目的
日本のゲーマーが「今これを買っていいのか」を3秒で判断できること。
情報の網羅性より、判断の速さを優先する。

## デザイン原則
1. コントラストを最優先。中間の濁ったグレーを使わない。
   本文は「ほぼ黒」か「ほぼ白」。バッジの文字は背景と明確に区別できること
2. 色は意味のためだけに使う（緑=過去最安値に到達、赤=終了間近、コーラル=購入導線）。
   装飾で色を使わない。蛍光色・彩度の高い色は使わない
3. 詰め込まない。余白を惜しまない。1画面に情報を詰めるより、
   何を見ればいいかが分かることを優先
4. メリハリ。すべてを同じ強さで見せない。
   最も重要なもの（価格・買い時）を明確に大きく強く
5. 断定しない。データが不確実な場合は表示しない

## 禁止事項
- 装飾的なアニメーション（スクロール連動フェードイン等）
- 絵文字をアイコン代わりに使う
- 背景に色を敷いてセクションを区別する（濁って見える）
- 判定できない情報を推測で表示する

## 作業ルール
- 実装後は必ず実ブラウザのスクリーンショットで目視確認する
- 確認できない場合は「未確認」と明記して報告する
- 計算による見積もりだけで「完了」と報告しない
