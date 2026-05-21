"""
台股Ｋ棒型態掃描 v2：資料源從 SQLite 讀取（先跑 twstock.py update）
特徵：主升段 → 高檔回檔 → 均線收斂 → 今日放量長紅站回 20MA
"""
import sys, os
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twstock import get_conn, get_prices, all_tickers

# ===== 條件參數 =====
MIN_BARS         = 80
MIN_60MA_SLOPE   = 0.0       # 60MA 斜率為正
MIN_20D_MAX_GAIN = 0.30      # 60日內曾有 20日漲幅 ≥ 30%
PULLBACK_RANGE   = (0.10, 0.40)
MAX_MA_SPREAD    = 0.10      # 5/10/20 MA 收斂
MIN_TODAY_CHG    = 0.03      # 今日漲幅
MIN_VOL_RATIO    = 1.5       # 量比

def analyze(df, name, ticker):
    if len(df) < MIN_BARS: return None
    close = df['Close']; high = df['High']; low = df['Low']
    vol   = df['Volume']; o = df['Open']

    ma5  = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    vol5 = vol.rolling(5).mean()

    t = -1
    c0 = close.iloc[t]; o0 = o.iloc[t]; prev = close.iloc[t-1]

    # 1. 長期多頭
    if pd.isna(ma60.iloc[t]) or c0 <= ma60.iloc[t]: return None
    slope60 = (ma60.iloc[t] - ma60.iloc[t-20]) / ma60.iloc[t-20]
    if slope60 <= MIN_60MA_SLOPE: return None

    # 2. 曾有主升段
    recent60 = close.iloc[-60:]
    max_20d = max((recent60.iloc[j]/recent60.iloc[j-20] - 1)
                  for j in range(20, len(recent60)))
    if max_20d < MIN_20D_MAX_GAIN: return None

    # 3. 自高點回檔
    pb = (high.iloc[-60:].max() - c0) / high.iloc[-60:].max()
    if not (PULLBACK_RANGE[0] <= pb <= PULLBACK_RANGE[1]): return None

    # 4. 均線收斂
    ma_vals = [ma5.iloc[t], ma10.iloc[t], ma20.iloc[t]]
    spread = (max(ma_vals) - min(ma_vals)) / min(ma_vals)
    if spread > MAX_MA_SPREAD: return None

    # 5. 今日啟動
    chg = (c0 - prev) / prev
    if chg < MIN_TODAY_CHG: return None
    if not (c0 > ma5.iloc[t] and c0 > ma10.iloc[t] and high.iloc[t] >= ma20.iloc[t]):
        return None
    if pd.isna(vol5.iloc[t-1]) or vol.iloc[t] < vol5.iloc[t-1] * MIN_VOL_RATIO:
        return None
    body = abs(c0 - o0)
    body10 = (close.iloc[-11:-1] - o.iloc[-11:-1]).abs().mean()
    if body < body10: return None

    return {
        'ticker': ticker, 'name': name,
        'close': round(c0,2), 'chg%': round(chg*100,2),
        'pullback%': round(pb*100,1),
        'max20d_gain%': round(max_20d*100,1),
        'vol_ratio': round(vol.iloc[t]/vol5.iloc[t-1],2),
        'ma_spread%': round(spread*100,2),
        'slope60%': round(slope60*100,2),
    }

def main():
    con = get_conn()
    tk = all_tickers(con)
    print(f"DB 內標的數: {len(tk)} (上市 {sum(tk['market']=='TW')}, 上櫃 {sum(tk['market']=='TWO')})")

    hits = []
    for _, row in tk.iterrows():
        suffix = "TW" if row["market"]=="TW" else "TWO"
        ticker = f"{row['ticker']}.{suffix}"
        df = get_prices(con, ticker)
        if df.empty: continue
        r = analyze(df, row["name"], ticker)
        if r: hits.append(r)

    hits.sort(key=lambda x: -x["chg%"])
    print(f"\n=== 命中 {len(hits)} 檔 ===")
    if hits:
        out = pd.DataFrame(hits)
        print(out.to_string(index=False))
        out.to_csv("/Users/rick/Developer/Aplus/scan_results_v2.csv", index=False)
        print("\n→ scan_results_v2.csv")

    # 驗證三檔診斷
    print("\n=== 重點檔診斷（不論是否命中）===")
    for t in ["3673.TW","4569.TW","2460.TW"]:
        df = get_prices(con, t)
        if df.empty:
            print(f"{t}: DB 無資料"); continue
        last = df.iloc[-1]
        print(f"{t} {df['Name'].iloc[-1]}: {df.index[-1].date()}  "
              f"O={last['Open']:.2f} H={last['High']:.2f} L={last['Low']:.2f} C={last['Close']:.2f} "
              f"V={last['Volume']:,}")
    con.close()

if __name__ == "__main__":
    main()
