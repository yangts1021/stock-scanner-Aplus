"""
回測 v3：等回檔進場 + R:R 過濾
- 訊號日的 (high+low)/2 為中點
- 訊號日後 PULLBACK_WAIT 天內等價格觸及中點再進場
- 停損 = 60 日高之後到訊號日的最低低點（前低）
- 停利 = 60 日高（前高）
- 進場前 R:R = (前高-進場價)/(進場價-前低) >= MIN_RR
- 持有最多 HOLD_DAYS 天
"""
import sys, os, time
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twstock import get_conn, get_prices, all_tickers
from scan_vectorized import signals_for_ticker

PULLBACK_WAIT  = 3
HOLD_DAYS      = 20
MIN_RR         = 1.5
LOOKBACK       = 60
DEDUP_COOLDOWN = 30

OUT_SIGNALS = "/Users/rick/Developer/Aplus/backtest_pullback_signals.csv"
OUT_SUMMARY = "/Users/rick/Developer/Aplus/backtest_pullback_summary.txt"


def simulate_one(df, ticker, name, sidx):
    n = len(df)
    if sidx + 1 >= n: return None

    sig_bar = df.iloc[sidx]
    midpoint = float((sig_bar['High'] + sig_bar['Low']) / 2)

    # 前高 = 60 日 high
    start = max(0, sidx - (LOOKBACK - 1))
    high_slice = df['High'].iloc[start:sidx+1]
    prev_high = float(high_slice.max())
    high_iloc_idx = high_slice.idxmax()
    high_iloc = df.index.get_loc(high_iloc_idx)

    # 前低 = 60 日高出現後到訊號日之間的最低 low
    if high_iloc >= sidx:
        pullback_low = float(sig_bar['Low'])
    else:
        pullback_low = float(df['Low'].iloc[high_iloc:sidx+1].min())

    # 等回檔
    entry_idx = None; entry_price = None
    for k in range(1, PULLBACK_WAIT + 1):
        if sidx + k >= n: break
        bar = df.iloc[sidx + k]
        bar_open = float(bar['Open']); bar_low = float(bar['Low'])
        if bar_open <= midpoint:
            entry_idx = sidx + k; entry_price = bar_open; break
        if bar_low <= midpoint:
            entry_idx = sidx + k; entry_price = midpoint; break

    base = {
        'signal_date': df.index[sidx].strftime('%Y-%m-%d'),
        'ticker': ticker, 'name': name,
        'midpoint': round(midpoint, 2),
        'prev_high': round(prev_high, 2),
        'pullback_low': round(pullback_low, 2),
    }

    if entry_idx is None:
        return {**base, 'outcome': 'NO_PULLBACK',
                'entry_date': '', 'entry_price': None,
                'rr_ratio': None, 'exit_date': '', 'exit_price': None,
                'return_pct': 0.0, 'days_held': 0}

    # R:R 過濾
    risk = entry_price - pullback_low
    reward = prev_high - entry_price
    rr = (reward / risk) if risk > 0 else float('inf')

    base['entry_date'] = df.index[entry_idx].strftime('%Y-%m-%d')
    base['entry_price'] = round(entry_price, 2)
    base['rr_ratio'] = round(rr, 2) if np.isfinite(rr) else None

    if risk <= 0 or rr < MIN_RR:
        return {**base, 'outcome': 'POOR_RR',
                'exit_date': '', 'exit_price': None,
                'return_pct': 0.0, 'days_held': 0}

    # 模擬持有：從 entry 隔日開始追蹤（保守，避免使用同日盤中已發生的高低點）
    forward = df.iloc[entry_idx + 1 : entry_idx + 1 + HOLD_DAYS]
    outcome = 'TIMEOUT'
    exit_idx = len(forward) - 1 if len(forward) else 0
    exit_price = float(forward.iloc[-1]['Close']) if len(forward) else entry_price
    for k in range(len(forward)):
        h = float(forward.iloc[k]['High'])
        c = float(forward.iloc[k]['Close'])
        if h > prev_high:
            outcome = 'WIN'; exit_idx = k; exit_price = prev_high; break
        if c <= pullback_low:
            outcome = 'LOSS'; exit_idx = k; exit_price = c; break

    return_pct = (exit_price - entry_price) / entry_price * 100
    return {**base, 'outcome': outcome,
            'exit_date': forward.index[exit_idx].strftime('%Y-%m-%d') if len(forward) else '',
            'exit_price': round(exit_price, 2),
            'return_pct': round(return_pct, 2),
            'days_held': exit_idx + 1}


def run():
    con = get_conn()
    tk = all_tickers(con)
    print(f"DB: {len(tk)} 檔，PULLBACK_WAIT={PULLBACK_WAIT}, MIN_RR={MIN_RR}, HOLD_DAYS={HOLD_DAYS}")

    records = []
    t0 = time.time()
    for i, row in tk.iterrows():
        if (i + 1) % 300 == 0:
            print(f"  {i+1}/{len(tk)}  {time.time()-t0:.1f}s  訊號={len(records)}")
        suffix = "TW" if row["market"] == "TW" else "TWO"
        ticker = f"{row['ticker']}.{suffix}"
        df = get_prices(con, ticker)
        if df.empty or len(df) < 80: continue
        sig = signals_for_ticker(df)
        if not sig.any(): continue

        date_to_idx = {d: idx for idx, d in enumerate(df.index)}
        raw = list(sig.index[sig.values])
        # 去重
        kept = []; last = -10**9
        for sd in raw:
            ci = date_to_idx[sd]
            if ci - last >= DEDUP_COOLDOWN:
                kept.append(sd); last = ci

        for sd in kept:
            r = simulate_one(df, ticker, row["name"], date_to_idx[sd])
            if r is not None: records.append(r)

    con.close()
    print(f"\n總訊號 {len(records)}  耗時 {time.time()-t0:.1f}s")
    if not records: return

    df_rec = pd.DataFrame(records)
    df_rec.to_csv(OUT_SIGNALS, index=False)
    summary = summarize(df_rec)
    with open(OUT_SUMMARY, "w") as f: f.write(summary)
    print()
    print(summary)


def summarize(df):
    n_total = len(df)
    n_no_pb = (df['outcome']=='NO_PULLBACK').sum()
    n_poor_rr = (df['outcome']=='POOR_RR').sum()
    n_trades = n_total - n_no_pb - n_poor_rr   # 實際進場數
    n_win = (df['outcome']=='WIN').sum()
    n_loss = (df['outcome']=='LOSS').sum()
    n_to = (df['outcome']=='TIMEOUT').sum()

    traded = df[df['outcome'].isin(['WIN','LOSS','TIMEOUT'])]
    win_rate = n_win / n_trades if n_trades else 0
    loss_rate = n_loss / n_trades if n_trades else 0
    avg_win = traded.loc[traded['outcome']=='WIN', 'return_pct'].mean() if n_win else 0
    avg_loss = traded.loc[traded['outcome']=='LOSS', 'return_pct'].mean() if n_loss else 0
    avg_to = traded.loc[traded['outcome']=='TIMEOUT', 'return_pct'].mean() if n_to else 0
    avg_all = traded['return_pct'].mean() if n_trades else 0
    pf = abs(avg_win/avg_loss) if (n_loss and avg_loss != 0) else float('nan')
    ev = (win_rate*avg_win + (n_loss/n_trades)*avg_loss + (n_to/n_trades)*avg_to) if n_trades else 0

    # 訊號漏斗
    rr_vals = df.loc[df['rr_ratio'].notna(), 'rr_ratio']

    lines = []
    lines.append("="*60)
    lines.append(" 回測 v3：等回檔 + R:R≥{} 過濾".format(MIN_RR))
    lines.append("="*60)
    lines.append(f"資料期間: {df['signal_date'].min()} ~ {df['signal_date'].max()}")
    lines.append("")
    lines.append("【訊號漏斗】")
    lines.append(f"  原始訊號:     {n_total}")
    lines.append(f"  未回檔(skip): {n_no_pb}  ({n_no_pb/n_total*100:.1f}%)")
    lines.append(f"  R:R 不足:    {n_poor_rr}  ({n_poor_rr/n_total*100:.1f}%)")
    lines.append(f"  ▸ 實際進場:  {n_trades}  ({n_trades/n_total*100:.1f}%)")
    if len(rr_vals):
        lines.append(f"  R:R 分布: 中位 {rr_vals.median():.2f}, 平均 {rr_vals.mean():.2f}, 最大 {rr_vals.max():.2f}")
    lines.append("")
    lines.append("【進場後表現】")
    if n_trades:
        lines.append(f"  WIN  (過前高): {n_win}  ({win_rate:.1%})")
        lines.append(f"  LOSS (破前低): {n_loss}  ({loss_rate:.1%})")
        lines.append(f"  TIMEOUT:      {n_to}  ({n_to/n_trades:.1%})")
        lines.append("")
        lines.append(f"  平均勝幅: {avg_win:+.2f}%   平均敗幅: {avg_loss:+.2f}%   TIMEOUT 平均: {avg_to:+.2f}%")
        lines.append(f"  進場平均報酬: {avg_all:+.2f}%   盈虧比: {pf:.2f}   期望值: {ev:+.2f}%")
    lines.append("")
    if n_total:
        ev_inc_skipped = (avg_all * n_trades) / n_total
        lines.append(f"【含 skip 全口徑期望值】每個原始訊號平均報酬: {ev_inc_skipped:+.2f}%")
    lines.append("="*60)
    return "\n".join(lines)


if __name__ == "__main__":
    run()
