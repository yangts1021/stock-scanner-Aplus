"""
台股Ｋ棒型態掃描：
特徵：60MA翻揚 → 主升段 +30% → 高檔回檔 15~35% → 均線收斂 → 今日放量長紅站回 20MA
"""
import urllib.request, ssl, re, sys, time, os, json, pickle
import pandas as pd
import numpy as np
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
CACHE_DIR = "/Users/rick/Developer/Aplus/cache"
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_PKL = os.path.join(CACHE_DIR, "prices.pkl")

def fetch_codes():
    out = []
    for mode, market in [(2,'TW'), (4,'TWO')]:
        url = f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            html = r.read().decode('big5', errors='ignore')
        for code, name in re.findall(r'<td[^>]*>(\d{4})[\s　]+([^<]+)</td>', html):
            # 過濾掉ETF、權證等：只保留代碼開頭 1-9 的普通股
            if code[0] in '123456789':
                out.append((code, name.strip(), market))
    return out

def fetch_prices(codes, market_suffix, batch=80):
    tickers = [f"{c}.{market_suffix}" for c in codes]
    results = {}
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i+batch]
        print(f"  抓 {market_suffix} {i+1}-{i+len(chunk)} / {len(tickers)}", flush=True)
        try:
            df = yf.download(chunk, period="6mo", interval="1d",
                             group_by='ticker', auto_adjust=False, progress=False, threads=True)
        except Exception as e:
            print("  download error:", e); continue
        for t in chunk:
            try:
                if len(chunk) == 1:
                    sub = df
                else:
                    sub = df[t].dropna()
                if sub.empty or len(sub) < 80: continue
                results[t] = sub
            except Exception:
                continue
        time.sleep(0.4)
    return results

def analyze(df, name, ticker):
    """套用嚴格篩選條件，回傳分數與細節"""
    # 處理 yfinance 收盤後 Close 欄位延遲寫入
    df = df.copy()
    last_idx = df.index[-1]
    if pd.isna(df.loc[last_idx, 'Close']) and df.loc[last_idx, 'Volume'] > 0 and len(df) >= 2:
        prev_close = df['Close'].iloc[-2]
        high_today = df.loc[last_idx, 'High']
        if not pd.isna(prev_close):
            up_pct = (high_today / prev_close) - 1
            # 漲停 (含台股 10% 限制；放寬到 9.5% 容忍 round)
            if up_pct >= 0.095:
                df.loc[last_idx, 'Close'] = high_today
    df = df.dropna(subset=['Close'])
    if len(df) < 80: return None
    close = df['Close']
    high = df['High']
    low = df['Low']
    vol = df['Volume']

    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    vol5 = vol.rolling(5).mean()

    today = -1
    c0 = close.iloc[today]
    o0 = df['Open'].iloc[today]
    prev_close = close.iloc[today-1]

    # === 條件 1: 長期多頭：收盤 > 60MA，60MA 斜率 > 0 ===
    if pd.isna(ma60.iloc[today]) or c0 <= ma60.iloc[today]:
        return None
    slope60 = (ma60.iloc[today] - ma60.iloc[today-20]) / ma60.iloc[today-20]
    if slope60 <= 0:
        return None

    # === 條件 2: 過去 60 日內曾有 20 日漲幅 ≥ 30% ===
    recent60 = close.iloc[-60:]
    max_20d_gain = 0
    for j in range(20, len(recent60)):
        gain = (recent60.iloc[j] / recent60.iloc[j-20]) - 1
        if gain > max_20d_gain:
            max_20d_gain = gain
    if max_20d_gain < 0.30:
        return None

    # === 條件 3: 自 60 日高點回檔 15%~35% ===
    high60_max = high.iloc[-60:].max()
    pullback = (high60_max - c0) / high60_max
    if not (0.10 <= pullback <= 0.40):
        return None

    # === 條件 4: 均線收斂：5MA, 10MA, 20MA 三者價差 ≤ 10% (放寬) ===
    ma_vals = [ma5.iloc[today], ma10.iloc[today], ma20.iloc[today]]
    ma_spread = (max(ma_vals) - min(ma_vals)) / min(ma_vals)
    if ma_spread > 0.10:
        return None

    # === 條件 5: 今日啟動 (放寬：MA20 改為高價觸及即可) ===
    today_chg = (c0 - prev_close) / prev_close
    if today_chg < 0.03:
        return None
    if not (c0 > ma5.iloc[today] and c0 > ma10.iloc[today] and high.iloc[today] >= ma20.iloc[today]):
        return None
    if pd.isna(vol5.iloc[today-1]) or vol.iloc[today] < vol5.iloc[today-1] * 1.5:
        return None
    body = abs(c0 - o0)
    body10_avg = (close.iloc[-11:-1] - df['Open'].iloc[-11:-1]).abs().mean()
    if body < body10_avg:
        return None

    return {
        'ticker': ticker, 'name': name,
        'close': round(c0, 2),
        'chg%': round(today_chg * 100, 2),
        'pullback%': round(pullback * 100, 1),
        'max20d_gain%': round(max_20d_gain * 100, 1),
        'vol_ratio': round(vol.iloc[today] / vol5.iloc[today-1], 2),
        'ma_spread%': round(ma_spread * 100, 2),
        'slope60%': round(slope60 * 100, 2),
    }

def main():
    print("1) 抓取上市+上櫃代碼...", flush=True)
    codes = fetch_codes()
    tw = [c for c,n,m in codes if m=='TW']
    two = [c for c,n,m in codes if m=='TWO']
    name_map = {f"{c}.{m}": n for c,n,m in codes}
    print(f"   TWSE: {len(tw)}, TPEX: {len(two)}")

    print("2) 下載日K資料...")
    if os.path.exists(CACHE_PKL):
        with open(CACHE_PKL, "rb") as f:
            all_data = pickle.load(f)
        print(f"   使用快取 ({len(all_data)} 檔)")
    else:
        all_data = {}
        all_data.update(fetch_prices(tw, 'TW'))
        all_data.update(fetch_prices(two, 'TWO'))
        with open(CACHE_PKL, "wb") as f:
            pickle.dump(all_data, f)
        print(f"   成功取得 {len(all_data)} 檔資料")

    print("3) 套用嚴格篩選...")
    hits = []
    for ticker, df in all_data.items():
        result = analyze(df, name_map.get(ticker, '?'), ticker)
        if result:
            hits.append(result)

    hits.sort(key=lambda x: -x['chg%'])
    print(f"\n=== 命中 {len(hits)} 檔 ===")
    if hits:
        df = pd.DataFrame(hits)
        print(df.to_string(index=False))
        df.to_csv("/Users/rick/Developer/Aplus/scan_results.csv", index=False)
        print("\n結果已存到 scan_results.csv")

if __name__ == "__main__":
    main()
