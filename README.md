# EV Group Hiring Site — hiring.ev-hk.com

招聘網站 + 每週自動更新流量數字。改咗呢個 repo 嘅嘢，一兩分鐘內自動上線，唔使上載去任何地方。

## 個 repo 點運作

```
index.html                        ← 成個網站（一頁過）
stats.json                        ← 三個 hero 數字（views／片數／followers），網頁 load 時讀
scripts/update_stats.py           ← 拉數 script（Apify → IG/FB）
scripts/stats_config.json         ← 邊啲帳號、點 filter「我哋整嘅片」
.github/workflows/update-stats.yml ← 每逢星期一 03:00（香港時間）自動跑
CNAME                             ← custom domain 設定，唔好刪
```

## 想改網站文案

1. 喺 GitHub 開 `index.html` → 撳右上角鉛筆（Edit）
2. 改完撳 **Commit changes**
3. 等一兩分鐘，hiring.ev-hk.com 自動更新（進度喺 Actions tab 嘅 pages-build-deployment）

## 數字自動更新

- 逢星期一 03:00（HKT）自動跑：拉晒 config 入面全部帳號 → 計三個總數 → 寫入 `stats.json` → 網站即刻用新數。
- 想即刻手動跑一次：**Actions tab → Update stats → Run workflow**。
- **跑失敗 = 唔會更新數字**（網站繼續顯示上一次嘅好數），GitHub 會 email 通知 repo owner。常見原因：某帳號拉返嚟條數低過 `min_expected`（filter 飄咗／scrape 壞咗），入去 Actions log 睇邊個帳號報錯。

## 加新 client／filter 飄咗

改 `scripts/stats_config.json`（唔使掂 script）：

- 加一個 object 落 `accounts`：`label`、`handle`、`filter`、`min_expected`
- filter 三款：`caption_contains`（caption 有某簽名先算）、`any_video`（成個帳號啲片都係我哋嘅）、`video_not_sponsor`（剔走 `sponsor_brands` 嘅 brand ad）
- 有 cross-post 去 Facebook 嘅帳號要加 `facebook_page`（IG 單邊會低報，兩邊加總先係 client 見到嘅數）
- 唔想某帳號啲 followers 計入總數：加 `"count_followers": false`

## Secrets

- `APIFY_TOKEN`（Settings → Secrets and variables → Actions）— 拉 IG/FB 數用。冇佢 workflow 必死。

## 數字口徑

- **Views** = IG `videoPlayCount`（IG 公開顯示嘅「Views」；唔用舊制 `videoViewCount`）＋ cross-post 帳號嘅 FB plays；2026 年內 post、經 filter 認定係我哋出品嘅片；累計終身數。
- **顯示值捨入**：views 落捨到十萬位、followers 到千位、片數到十位，配「+」號（`stats.json` 入面 `*_display` 係顯示值，齋數係實數）。
