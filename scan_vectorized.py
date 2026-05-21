"""
向量化篩選器：對單一 ticker 的完整時間序列一次算完全部訊號日。
邏輯與 scan_v2.py analyze() 完全對齊；最後一天訊號集合必須一致。
"""
import sys, os
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twstock import get_conn, get_prices, all_tickers

# 條件參數（與 scan_v2.py 同步）
MIN_BARS              = 80
MIN_60MA_SLOPE        = 0.0
MIN_20D_MAX_GAIN      = 0.30
PULLBACK_RANGE        = (0.10, 0.30)
MIN_PEAK_TO_TROUGH    = 1     # 峰到谷至少 1 個交易日，過濾同日峰谷假回檔
MAX_MA_SPREAD         = 0.10
MIN_TODAY_CHG         = 0.03
MIN_VOL_RATIO         = 1.5
MIN_BODY_MULT         = 1.0


def rolling_pullback(high, low, window=60):
    """每日同時回傳：
    - pullback: (60日 high.max - 該高點之後的 low.min) / 60日 high.max（峰→谷深度）
    - days_to_trough: 谷底 iloc - 峰 iloc（修正歷時的交易日數）
    """
    n = len(high)
    h = high.values
    l = low.values
    pb = np.full(n, np.nan)
    days = np.full(n, -1, dtype=int)
    for t in range(window - 1, n):
        s = t - window + 1
        peak_offset = int(np.argmax(h[s:t + 1]))
        peak_iloc = s + peak_offset
        peak_high = h[peak_iloc]
        trough_offset = int(np.argmin(l[peak_iloc:t + 1]))
        trough_iloc = peak_iloc + trough_offset
        trough_low = l[trough_iloc]
        if peak_high > 0:
            pb[t] = (peak_high - trough_low) / peak_high
            days[t] = trough_iloc - peak_iloc
    return (pd.Series(pb, index=high.index),
            pd.Series(days, index=high.index))


def signals_for_ticker(df):
    """回傳 Boolean Series (index=Date)。True = 該日為訊號日。"""
    if len(df) < MIN_BARS:
        return pd.Series(False, index=df.index)

    close = df['Close']; high = df['High']; low = df['Low']
    vol = df['Volume']; opn = df['Open']

    ma5  = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    vol5 = vol.rolling(5).mean()

    # 條件 1：長期多頭
    slope60 = ma60.pct_change(20)
    c1 = (close > ma60) & (slope60 > MIN_60MA_SLOPE)

    # 條件 2：60 日內曾有 20 日漲幅 ≥ 30%
    pct20 = close.pct_change(20)
    c2 = pct20.rolling(60).max() >= MIN_20D_MAX_GAIN

    # 條件 3：自 60 日高點之最大回撤深度（peak → trough，不是 peak → close）
    # 並要求修正歷時 ≥ MIN_PEAK_TO_TROUGH 個交易日，過濾單日震盪假回檔
    pullback, ptd = rolling_pullback(high, low, window=60)
    c3 = (pullback.between(PULLBACK_RANGE[0], PULLBACK_RANGE[1])
          & (ptd >= MIN_PEAK_TO_TROUGH))

    # 條件 4：5/10/20MA 收斂
    ma_df = pd.concat([ma5, ma10, ma20], axis=1)
    ma_min = ma_df.min(axis=1)
    spread = (ma_df.max(axis=1) - ma_min) / ma_min
    c4 = spread <= MAX_MA_SPREAD

    # 條件 5：今日啟動
    chg = close.pct_change()
    body = (close - opn).abs()
    body10 = body.shift(1).rolling(10).mean()
    vol5_prev = vol5.shift(1)
    c5 = (
        (chg >= MIN_TODAY_CHG)
        & (close > ma5)
        & (close > ma10)
        & (high >= ma20)
        & (vol >= vol5_prev * MIN_VOL_RATIO)
        & (body >= body10 * MIN_BODY_MULT)
    )

    sig = c1 & c2 & c3 & c4 & c5
    return sig.fillna(False)


def signals_with_metrics(df):
    """除了訊號 mask，還回傳同期的指標值供回測使用。"""
    sig = signals_for_ticker(df)
    return sig


def verify_against_v2(target_date="2026-05-13"):
    """跑全市場，比對 scan_v2 在 target_date 的命中清單。"""
    from scan_v2 import analyze

    con = get_conn()
    tk = all_tickers(con)

    vec_hits = set()
    v2_hits = set()

    for _, row in tk.iterrows():
        suffix = "TW" if row["market"] == "TW" else "TWO"
        ticker = f"{row['ticker']}.{suffix}"
        df = get_prices(con, ticker)
        if df.empty:
            continue

        sig = signals_for_ticker(df)
        target_ts = pd.Timestamp(target_date)
        if target_ts in sig.index and sig.loc[target_ts]:
            vec_hits.add(ticker)

        # scan_v2 用 iloc[-1] 當「今天」，所以截斷到 target_date 為止
        df_trunc = df.loc[:target_ts]
        if len(df_trunc) == 0 or df_trunc.index[-1] != target_ts:
            continue
        r = analyze(df_trunc, row["name"], ticker)
        if r:
            v2_hits.add(ticker)

    con.close()

    print(f"=== 一致性驗證 @ {target_date} ===")
    print(f"vectorized 命中 ({len(vec_hits)}): {sorted(vec_hits)}")
    print(f"scan_v2 命中 ({len(v2_hits)}): {sorted(v2_hits)}")
    only_vec = vec_hits - v2_hits
    only_v2 = v2_hits - vec_hits
    if only_vec:
        print(f"⚠️ 只在 vectorized 命中: {sorted(only_vec)}")
    if only_v2:
        print(f"⚠️ 只在 scan_v2 命中: {sorted(only_v2)}")
    if not only_vec and not only_v2:
        print(f"✅ 完全一致")
    return vec_hits == v2_hits


if __name__ == "__main__":
    verify_against_v2()
