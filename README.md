# stock-scanner-Aplus

台股 / 美股 K 線型態掃描器（v7 strategy）

從 60 日新高回檔 10~30% 後出現「放量站回均線」的進場訊號，自動算 R:R、畫線標注、按 R:R 分桶輸出 K 線圖；同一套形態邏輯可同時跑掃描與回測，保證口徑一致。

## 環境需求

- Python 3.8+
- 套件：`pandas` `numpy` `yfinance` `matplotlib` `mplfinance`

```bash
pip install pandas numpy yfinance matplotlib mplfinance
```

## 快速開始

### 1. 初始化（首次執行）

**台股**：`twstock.py` 沒有獨立 `init`；首次 `update` 時若 DB 為空，會自動從 6 個月前開始抓。

```bash
python twstock.py update           # 自動：上市 + 上櫃 + TAIEX + 美股 + ^IXIC
```

**美股**：需要先 `init` 來建立股池（Nasdaq market cap top N，預設 500 檔）。

```bash
python usstock.py init             # 預設 period=6mo, limit=500
python usstock.py init 1y 1000     # 自訂 1 年歷史、Nasdaq 前 1000 檔
python usstock.py init 6mo 500     # 等同預設
```

第一次跑 `usstock.py init` 會抓 Nasdaq screener 取得股池清單 → 從 yfinance 批次下載歷史 K 線 → 寫入 `usstock.db`。

### 2. 每日增量更新

| 用途 | 指令 | 涵蓋範圍 |
|---|---|---|
| **一鍵全更新（推薦）** | `python twstock.py update` | 上市 + 上櫃 + TAIEX + 美股 + ^IXIC |
| 只更新美股 | `python usstock.py update` | 美股股池 + ^IXIC |
| 查台股 DB 統計 | `python twstock.py stats` | 顯示最後日期、檔數、總列數 |
| 查美股 DB 統計 | `python usstock.py stats` | 同上 |
| 查單檔近 10 筆 | `python twstock.py show 3673.TW` | 顯示 OHLCV |
|  | `python usstock.py show NVDA` | 顯示 OHLCV |

> 提示：`twstock.py update` 內建會自動 import `usstock` 一起跑，所以日常只需執行這一個指令即可。

### 3. 掃描

```bash
python today_scan_v7.py 2026-05-21          # 台股，指定訊號日
python today_scan_us_v7.py 2026-05-21       # 美股，指定訊號日
```

把指定日期當訊號日，輸出表格 + CSV，**忽略該日之後的價格**（rolling/shift 皆向後不偷看未來）。

### 4. 畫 K 線圖

```bash
python plot_signals.py 2026-05-21 tw        # 全部訊號，按 R:R 分桶
python plot_signals.py 2026-05-21 us        # 美股版本
python plot_signals.py 2026-05-21 tw 4919   # 指定單檔（不需事先掃描）
```

PNG 自動依 R:R 進不同子資料夾：`charts/<date>/rr_2.5+/`、`rr_1.5-2.5/`、`rr_1.0-1.5/`、`rr_lt1.0/`。

### 5. 回測

```bash
python backtest_v7.py        # 台股
python backtest_us_v7.py     # 美股
```

20 天 walk-forward；R:R≥1.0 才進場、隔日開盤、前低停損、過前高停利。

## v7 策略五條件（`scan_vectorized.py`）

| # | 條件 | 規則 |
|---|---|---|
| 1 | 長期多頭 | `close > MA60` 且 MA60 20 日斜率 > 0 |
| 2 | 主升段曾啟動 | 過去 60 日內任一日的 20 日漲幅 ≥ 30% |
| 3 | 高檔回檔 | 自 60 日高點之最大回撤（peak → trough）∈ [10%, 30%]；峰至谷至少 1 個交易日 |
| 4 | 均線收斂 | 5/10/20 MA 最大價差 ≤ 10% |
| 5 | 今日放量啟動 | 漲幅 ≥ 3%；close > MA5 且 > MA10；High ≥ MA20；量 ≥ 前 5 日均量 × 1.5；K 棒實體 ≥ 前 10 日均實體 |

R:R 計算：
```
reward = (prev_high − close) / close
risk   = (close − pullback_low) / close
R:R    = reward / risk
```

`prev_high` = 60 日 High 最大值；`pullback_low` = 該高點之後到訊號日的 Low 最小值。

## 資料

- **台股**：證交所 / 櫃買中心官方 API（無需 API key）
- **美股**：yfinance（Nasdaq screener top 1000 by market cap）
- **大盤指數**：^TWII / ^IXIC（yfinance）
- **存放**：本地 SQLite — `twstock.db` / `usstock.db`，含 `index_daily` 表

## 主要檔案

```
twstock.py / usstock.py          資料抓取 + SQLite I/O
scan_vectorized.py               v7 形態判定（被 scan/backtest 共用）
today_scan_v7.py / *_us_v7.py    當日掃描，輸出 R:R 分桶結果
backtest_v7.py   / *_us_v7.py    20 天 walk-forward 回測
plot_signals.py                  暗色系 K 線圖（目標/止損/回撤標注）
scan_v2.py / scan.py             scan_vectorized 的舊版（純量 for-loop）
SCANNERS.md                      三版本掃描器規格與差異
BACKTEST_RESULTS.md              回測歷史結果
```

## K 線圖標注

每張 PNG 含：
- MA30（黃）/ MA45（藍）/ MA60（紫）
- 目標（紅虛線）/ 現價 R:R（白框）/ 止損（綠虛線）
- 峰 → 谷 向下橘色箭頭 + 最大回撤% 標籤
- 自動依 R:R 分桶：`rr_2.5+` / `rr_1.5-2.5` / `rr_1.0-1.5` / `rr_lt1.0`

## License

MIT
