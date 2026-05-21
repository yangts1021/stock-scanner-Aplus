"""
回測 v5：隔日開盤進場 + 無停損 + 追 60 天 + 過前高出
- 進場：隔日開盤
- 停損：無
- 停利：過 60 日前高 → 以前高價出場
- 期滿：60 天到期 → 第 60 天收盤出場
"""
import sys, os, time
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twstock import get_conn, get_prices, all_tickers
from scan_vectorized import signals_for_ticker

HOLD_DAYS      = 60
LOOKBACK       = 60
DEDUP_COOLDOWN = 30

OUT_SIGNALS = "/Users/rick/Developer/Aplus/backtest_nostop_signals.csv"
OUT_SUMMARY = "/Users/rick/Developer/Aplus/backtest_nostop_summary.txt"


def simulate_one(df, ticker, name, sidx):
    n = len(df)
    if sidx + 1 >= n: return None

    entry_row = df.iloc[sidx + 1]
    entry_price = float(entry_row['Open'])
    if not np.isfinite(entry_price) or entry_price <= 0: return None
    entry_date = df.index[sidx + 1]

    start = max(0, sidx - (LOOKBACK - 1))
    prev_high = float(df['High'].iloc[start:sidx+1].max())

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
            outcome = "WIN"; exit_idx = k
            exit_price = prev_high
            break

    return_pct = (exit_price - entry_price) / entry_price * 100
    return {
        'signal_date': df.index[sidx].strftime('%Y-%m-%d'),
        'entry_date':  entry_date.strftime('%Y-%m-%d'),
        'ticker': ticker, 'name': name,
        'entry_price': round(entry_price, 2),
        'prev_high': round(prev_high, 2),
        'outcome': outcome,
        'exit_date': forward.index[exit_idx].strftime('%Y-%m-%d'),
        'exit_price': round(exit_price, 2),
        'return_pct': round(return_pct, 2),
        'days_held': exit_idx + 1,
        'max_gain_pct': round(max_gain, 2),
        'max_drawdown_pct': round(max_dd, 2),
    }


def run():
    con = get_conn()
    tk = all_tickers(con)
    print(f"DB {len(tk)} 檔, HOLD_DAYS={HOLD_DAYS}, 無停損")
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
    n = len(df)
    n_win = (df['outcome']=='WIN').sum()
    n_to = (df['outcome']=='TIMEOUT').sum()
    win_rate = n_win/n if n else 0

    # WIN / TIMEOUT 區分
    avg_win = df.loc[df['outcome']=='WIN','return_pct'].mean() if n_win else 0
    avg_to = df.loc[df['outcome']=='TIMEOUT','return_pct'].mean() if n_to else 0
    avg_all = df['return_pct'].mean()

    # TIMEOUT 內部分正負
    to_df = df[df['outcome']=='TIMEOUT']
    to_pos = (to_df['return_pct']>0).sum()
    to_neg = (to_df['return_pct']<=0).sum()
    avg_to_pos = to_df.loc[to_df['return_pct']>0,'return_pct'].mean() if to_pos else 0
    avg_to_neg = to_df.loc[to_df['return_pct']<=0,'return_pct'].mean() if to_neg else 0

    # 全部口徑：正/負
    pos = df[df['return_pct']>0]; neg = df[df['return_pct']<=0]
    avg_pos = pos['return_pct'].mean() if len(pos) else 0
    avg_neg = neg['return_pct'].mean() if len(neg) else 0
    pf = abs(avg_pos/avg_neg) if avg_neg else float('nan')

    # 持有天數
    days_win = df.loc[df['outcome']=='WIN','days_held'].describe()
    days_to = df.loc[df['outcome']=='TIMEOUT','days_held'].describe()

    # 最大回檔
    worst = df.nsmallest(10, 'max_drawdown_pct')[['signal_date','ticker','name','max_drawdown_pct','return_pct','outcome']]

    # 連勝連敗
    cur_w = 0; cur_l = 0; max_w = 0; max_l = 0
    for r in df.sort_values('signal_date')['return_pct']:
        if r > 0: cur_w += 1; cur_l = 0; max_w = max(max_w, cur_w)
        else: cur_l += 1; cur_w = 0; max_l = max(max_l, cur_l)

    lines = []
    lines.append("="*62)
    lines.append(" 回測 v5：隔日開盤 + 無停損 + 追 {} 天 + 過前高出".format(HOLD_DAYS))
    lines.append("="*62)
    lines.append(f"資料期間: {df['signal_date'].min()} ~ {df['signal_date'].max()}")
    lines.append("")
    lines.append("【出場分布】")
    lines.append(f"  WIN  (過前高):       {n_win}  ({win_rate:.1%})")
    lines.append(f"  TIMEOUT (滿{HOLD_DAYS}天):    {n_to}  ({n_to/n:.1%})")
    lines.append(f"    └ 其中正報酬:    {to_pos}  ({to_pos/n_to*100:.1f}%)  平均 {avg_to_pos:+.2f}%")
    lines.append(f"    └ 其中負報酬:    {to_neg}  ({to_neg/n_to*100:.1f}%)  平均 {avg_to_neg:+.2f}%")
    lines.append("")
    lines.append("【報酬】")
    lines.append(f"  平均勝幅(WIN)     : {avg_win:+.2f}%")
    lines.append(f"  TIMEOUT 平均      : {avg_to:+.2f}%")
    lines.append(f"  全部訊號平均      : {avg_all:+.2f}%   ← 期望值")
    lines.append(f"  全部正報酬平均    : {avg_pos:+.2f}%  (n={len(pos)})")
    lines.append(f"  全部負報酬平均    : {avg_neg:+.2f}%  (n={len(neg)})")
    lines.append(f"  正:負 盈虧比      : {pf:.2f}")
    lines.append("")
    lines.append("【持有天數】")
    lines.append(f"  WIN     中位 {days_win.get('50%',float('nan')):.0f}  平均 {days_win.get('mean',0):.1f}")
    lines.append(f"  TIMEOUT 中位 {days_to.get('50%',float('nan')):.0f}  平均 {days_to.get('mean',0):.1f}")
    lines.append("")
    lines.append("【極端最大回檔（每筆內最深的浮動虧損 top 10）】")
    for _, w in worst.iterrows():
        lines.append(f"  {w['signal_date']} {w['ticker']:10s} {w['name'][:10]:10s} "
                     f"最大回檔 {w['max_drawdown_pct']:+.1f}%  結局 {w['return_pct']:+.1f}% ({w['outcome']})")
    lines.append("")
    lines.append(f"【連續紀錄】最大連勝 {max_w}  最大連敗 {max_l}")
    lines.append("="*62)
    return "\n".join(lines)


if __name__ == "__main__":
    run()
