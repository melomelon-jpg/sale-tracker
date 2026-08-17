# セールトラッカー

PCゲームの価格を毎日チェックし、過去最安値と比較して「買い時」を静的サイトで表示する。

- 価格データ源: **IsThereAnyDeal (ITAD)** が主（円建ての現在価格・過去最安値・価格履歴）、**Steam appdetails (cc=jp)** が補助
- **取得**（API通信）と**サイト生成**（HTML出力）を分離。サイト側はAPIを叩かず、保存済みJSONだけを表示する
- 依存は標準ライブラリのみ（`pip install` 不要）

## 仕組み

```
config/games.json → fetch_data.py → data/*.json → build_site.py → public/*.html
   (監視対象)         (①取得/API)     (保存データ)     (②生成/APIなし)   (公開サイト)
```

## セットアップ

1. ITAD APIキーを取得（無料）: https://isthereanydeal.com/apps/
2. `.env` を作成:
   ```
   cp .env.example .env      # PowerShell: Copy-Item .env.example .env
   ```
   `.env` に `ITAD_API_KEY=あなたのキー` を書く（`.env` は `.gitignore` 済み）

## 使い方

```powershell
# ① データ取得（1日1回だけ叩く想定）
python src/fetch_data.py

# ② サイト生成（何度でも実行可・APIは叩かない）
python src/build_site.py

# 生成物をブラウザで確認
start public/index.html
```

## 監視対象の追加

`config/games.json` にゲームを追加する:

```json
{ "slug": "half-life-2", "title": "Half-Life 2", "steam_appid": 220 }
```

- `slug`: URL・ファイル名に使う識別子（英小文字・ハイフン）
- `title`: ITADのタイトル検索に使う名称
- `steam_appid`: Steam補助取得用（任意）

## ファイル構成

| パス | 役割 |
|---|---|
| `config/games.json` | 監視対象ゲーム一覧（手動編集） |
| `src/itad_client.py` | ITAD APIラッパー |
| `src/steam_client.py` | Steam appdetails 補助 |
| `src/verdict.py` | 買い時判定（純粋関数） |
| `src/sale_history.py` | セール履歴の傾向（回数・直近日を検出する純粋関数、予測はしない） |
| `src/fetch_data.py` | ①取得 → `data/` |
| `src/build_site.py` | ②生成 → `public/` |
| `data/` | 取得済みJSON（`fetch`の出力・`build`の入力） |
| `public/` | 公開する静的サイト（`build`の出力） |
| `assets/style.css` | （任意）CSSを差し替えたい場合に置く。無ければ内蔵CSSを使う |
| `config/steam_sales.json` | Steam大型セールの時期（about.htmlに表示）。取得APIが無いため**手動更新**。Valveは例年、上半期分・下半期分をSteamworks Developer向けに年2回アナウンスするので、発表を見たらここを書き換える |

## 買い時判定

現在価格が過去最安値からどれだけ上振れているかで判定する:

| 条件 | ラベル |
|---|---|
| 現在価格 ≤ 過去最安 | 過去最安更新中 |
| 過去最安 +5%以内 | ほぼ最安 |
| 過去最安 +15%以内 | まあ買い時 |
| それ以上 | 様子見 |

## 自動実行（GitHub Actions）

`.github/workflows/daily.yml` を用意済み。ITADキーをリポジトリの
**Settings → Secrets → Actions** に `ITAD_API_KEY` として登録すると、
毎日「取得 → 生成 → コミット」が自動で走る。
