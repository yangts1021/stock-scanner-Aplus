"""
回測 v6：隔日開盤進場 + R:R≥1.0 過濾 + 前低停損 + 過前高停利
- 進場：隔日開盤
- 停損：前低（60 日高之後到訊號日之間的最低 low）
- 停利：前高（60 日 high）
- R:R = (前高 - 進場價) / (進場價 - 前低)，< 1.0 不進場
- 持有：最多 HOLD_DAYS 天
"""
import sys, os, time
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twstock import get_conn, get_prices, all_tickers
from scan_vectorized import signals_for_ticker

HOLD_DAYS      = 20
MIN_RR         = 1.0
LOOKBACK       = 60
DEDUP_COOLDOWN = 30

OUT_SIGNALS = "/Users/rick/Developer/Aplus/backtest_rr_signals.csv"
OUT_SUMMARY = "/Users/rick/Developer/Aplus/backtest_rr_summary.txt"


def simulate_one(df, ticker, name, sidx):
    n = len(df)
    if sidx + 1 >= n: return None

    entry_row = df.iloc[sidx + 1]
    entry_price = float(entry_row['Open'])
    if not np.isfinite(entry_price) or entry_price <= 0: return None
    entry_date = df.index[sidx + 1]

    # 前高 / 前低
    start = max(0, sidx - (LOOKBACK - 1))
    high_slice = df['High'].iloc[start:sidx+1]
    prev_high = float(high_slice.max())
    high_iloc = df.index.get_loc(high_slice.idxmax())
    if high_iloc >= sidx:
        pullback_low = float(df.iloc[sidx]['Low'])
    else:
        pullback_low = float(df['Low'].iloc[high_iloc:sidx+1].min())

    # R:R
    risk = entry_price - pullback_low
    reward = prev_high - entry_price
    rr = (reward / risk) if risk > 0 else float('inf')

    base = {
        'signal_date': df.index[sidx].strftime('%Y-%m-%d'),
        'entry_date':  entry_date.strftime('%Y-%m-%d'),
        'ticker': ticker, 'name': name,
        'entry_price': round(entry_price, 2),
        'prev_high': round(prev_high, 2),
        'pullback_low': round(pullback_low, 2),
        'rr_ratio': round(rr, 2) if np.isfinite(rr) else None,
    }

    if risk <= 0 or rr < MIN_RR:
        return {**base, 'outcome': 'POOR_RR',
                'exit_date': '', 'exit_price': None,
                'return_pct': 0.0, 'days_held': 0,
                'max_gain_pct': 0.0, 'max_drawdown_pct': 0.0}

    forward = df.iloc[sidx+1 : sidx+1+HOLD_DAYS]
    if len(forward) == 0: return None

    outcome = "TIMEOUT"
    exit_idx = len(forward) - 1
    exit_price = float(forward.iloc[-1]['Close'])
    max_gain = -float('inf'); max_dd = float('inf')

    for k in range(len(forward)):
        h = float(forward.iloc[k]['High'])
        c = float(forward.iloc[k]['Close'])
        max_gain = max(max_gain, (h - entry_price)/entry_price*100)
        max_dd   = min(max_dd,   (c - entry_price)/entry_price*100)
        if h > prev_high:
            outcome = "WIN"; exit_idx = k; exit_price = prev_high; break
        if c <= pullback_low:
            outcome = "LOSS"; exit_idx = k; exit_price = c; break

    return_pct = (exit_price - entry_price) / entry_price * 100
    return {**base, 'outcome': outcome,
            'exit_date': forward.index[exit_idx].strftime('%Y-%m-%d'),
            'exit_price': round(exit_price, 2),
            'return_pct': round(return_pct, 2),
            'days_held': exit_idx + 1,
            'max_gain_pct': round(max_gain, 2),
            'max_drawdown_pct': round(max_dd, 2)}


def run():
    con = get_conn()
    tk = all_tickers(con)
    print(f"DB {len(tk)} 檔, MIN_RR={MIN_RR}, HOLD={HOLD_DAYS}")
    records = []
    t0 = time.time()
    for i, row in tk.iterrows():
        if (i+1) % 300 == 0:
            print(f"  {i+1}/{len(tk)} {time.time()-t0:.1f}s 訊號={len(records)}")
        suffix = "TW" if row["market"]=="TW" else "TWO"
        ticker = f"{row['ticker']}.{suffix}"
        df = get_prices(con, ticker)
        if df.empty or len(df) < 80: continue
        sig = signals_for_ticker(df)
        if not sig.any(): continue
        date_to_idx = {d:idx for idx,d in enumerate(df.index)}
        raw = list(sig.index[sig.values])
        kept=[]; last=-10**9
        for sd in raw:
            ci = date_to_idx[sd]
            if ci - last >= DEDUP_COOLDOWN:
                kept.append(sd); last = ci
        for sd in kept:
            r = simulate_one(df, ticker, row["name"], date_to_idx[sd])
            if r is not None: records.append(r)
    con.close()
    if not records: print("無訊號"); return
    df_rec = pd.DataFrame(records)
    df_rec.to_csv(OUT_SIGNALS, index=False)
    summary = summarize(df_rec)
    with open(OUT_SUMMARY, "w") as f: f.write(summary)
    print(); print(summary)


def summarize(df):
    n_total = len(df)
    n_poor = (df['outcome']=='POOR_RR').sum()
    traded = df[df['outcome'].isin(['WIN','LOSS','TIMEOUT'])]
    n_trades = len(traded)
    n_win = (traded['outcome']=='WIN').sum()
    n_loss = (traded['outcome']=='LOSS').sum()
    n_to = (traded['outcome']=='TIMEOUT').sum()

    win_rate = n_win/n_trades if n_trades else 0
    avg_win = traded.loc[traded['outcome']=='WIN','return_pct'].mean() if n_win else 0
    avg_loss = traded.loc[traded['outcome']=='LOSS','return_pct'].mean() if n_loss else 0
    avg_to = traded.loc[traded['outcome']=='TIMEOUT','return_pct'].mean() if n_to else 0
    avg_all = traded['return_pct'].mean() if n_trades else 0
    pf = abs(avg_win/avg_loss) if (n_loss and avg_loss != 0) else float('nan')
    ev = avg_all

    # 整體口徑（含 POOR_RR 視為 0%）
    ev_full = (avg_all * n_trades) / n_total if n_total else 0

    rr_vals = traded['rr_ratio'].dropna()

    lines = []
    lines.append("="*62)
    lines.append(" 回測 v6：隔日開盤 + R:R≥{} + 前低停損 + 過前高停利 + {}天".format(MIN_RR, HOLD_DAYS))
    lines.append("="*62)
    lines.append(f"資料期間: {df['signal_date'].min()} ~ {df['signal_date'].max()}")
    lines.append("")
    lines.append("【漏斗】")
    lines.append(f"  原始訊號:  {n_total}")
    lines.append(f"  R:R<{MIN_RR} skip: {n_poor}  ({n_poor/n_total*100:.1f}%)")
    lines.append(f"  ▸ 進場:     {n_trades}  ({n_trades/n_total*100:.1f}%)")
    if len(rr_vals):
        lines.append(f"  進場 R:R 分布: 中位 {rr_vals.median():.2f}, 平均 {rr_vals.mean():.2f}, 最大 {rr_vals.max():.2f}")
    lines.append("")
    lines.append("【進場後表現】")
    if n_trades:
        lines.append(f"  WIN  (過前高): {n_win}  ({win_rate:.1%})")
        lines.append(f"  LOSS (破前低): {n_loss}  ({n_loss/n_trades:.1%})")
        lines.append(f"  TIMEOUT:      {n_to}  ({n_to/n_trades:.1%})")
        lines.append("")
        lines.append(f"  平均勝幅: {avg_win:+.2f}%   平均敗幅: {avg_loss:+.2f}%   TIMEOUT 平均: {avg_to:+.2f}%")
        lines.append(f"  盈虧比 |勝/敗|: {pf:.2f}")
        lines.append(f"  進場期望值: {ev:+.2f}%   全口徑期望值: {ev_full:+.2f}%")
    lines.append("="*62)
    return "\n".join(lines)


if __name__ == "__main__":
    run()
