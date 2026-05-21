"""
回測引擎：對歷史每個交易日跑篩選 → 隔日開盤進場 → 20 日追蹤 → 過前高 / -8% 停損。
產出 backtest_signals.csv 與 backtest_summary.txt。
"""
import sys, os, time
import pandas as pd
import numpy as np
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twstock import get_conn, get_prices, all_tickers
from scan_vectorized import signals_for_ticker

# === 回測參數 ===
HOLD_DAYS      = 20      # 追蹤期間
STOP_LOSS_PCT  = 0.08    # 進場價 -8% 停損
PREV_HIGH_LOOKBACK = 60  # 前高 = 訊號日當下的 60 日 high（與條件 3 一致）
DEDUP_COOLDOWN = 30      # 同檔訊號冷卻日（避免重複訊號雜訊）

OUT_SIGNALS = "/Users/rick/Developer/Aplus/backtest_signals.csv"
OUT_SUMMARY = "/Users/rick/Developer/Aplus/backtest_summary.txt"


def simulate_one(df_full, ticker, name, signal_idx):
    """signal_idx = 訊號日在 df_full 中的 iloc 位置；回傳 trade record dict 或 None。"""
    n = len(df_full)
    if signal_idx + 1 >= n:
        return None  # 無法進場（DB 最後一天訊號）

    entry_row = df_full.iloc[signal_idx + 1]
    entry_price = float(entry_row['Open'])
    if not np.isfinite(entry_price) or entry_price <= 0:
        return None
    entry_date = df_full.index[signal_idx + 1]

    # 前高（與條件 3 同步：60 日 high 含訊號日）
    start = max(0, signal_idx - (PREV_HIGH_LOOKBACK - 1))
    prev_high = float(df_full['High'].iloc[start:signal_idx + 1].max())

    # 未來 HOLD_DAYS 個交易日
    forward = df_full.iloc[signal_idx + 1:signal_idx + 1 + HOLD_DAYS]
    if len(forward) == 0:
        return None

    stop_price = entry_price * (1 - STOP_LOSS_PCT)
    outcome = "TIMEOUT"
    exit_idx = len(forward) - 1
    exit_price = float(forward.iloc[-1]['Close'])
    max_gain_pct = -float("inf")
    max_drawdown_pct = float("inf")

    for k in range(len(forward)):
        h = float(forward.iloc[k]['High'])
        c = float(forward.iloc[k]['Close'])
        max_gain_pct     = max(max_gain_pct,     (h - entry_price) / entry_price * 100)
        max_drawdown_pct = min(max_drawdown_pct, (c - entry_price) / entry_price * 100)

        if h > prev_high:
            outcome = "WIN"
            exit_idx = k
            exit_price = prev_high  # 保守：以剛過前高的價位估
            break
        if c <= stop_price:
            outcome = "LOSS"
            exit_idx = k
            exit_price = c
            break

    return_pct = (exit_price - entry_price) / entry_price * 100

    return {
        'signal_date': df_full.index[signal_idx].strftime('%Y-%m-%d'),
        'entry_date':  entry_date.strftime('%Y-%m-%d'),
        'ticker': ticker,
        'name': name,
        'entry_price': round(entry_price, 2),
        'prev_high': round(prev_high, 2),
        'outcome': outcome,
        'exit_date': forward.index[exit_idx].strftime('%Y-%m-%d'),
        'exit_price': round(exit_price, 2),
        'return_pct': round(return_pct, 2),
        'days_held': exit_idx + 1,
        'max_gain_pct': round(max_gain_pct, 2),
        'max_drawdown_pct': round(max_drawdown_pct, 2),
    }


def run_backtest(min_date=None, max_date=None):
    con = get_conn()
    tk = all_tickers(con)
    print(f"DB 內標的: {len(tk)} (上市 {sum(tk['market']=='TW')}, 上櫃 {sum(tk['market']=='TWO')})")

    records = []
    t0 = time.time()
    for i, row in tk.iterrows():
        if (i + 1) % 200 == 0:
            print(f"  進度 {i+1}/{len(tk)}  耗時 {time.time()-t0:.1f}s  累積訊號 {len(records)}")

        suffix = "TW" if row["market"] == "TW" else "TWO"
        ticker = f"{row['ticker']}.{suffix}"
        df = get_prices(con, ticker)
        if df.empty or len(df) < 80:
            continue

        sig = signals_for_ticker(df)
        if not sig.any():
            continue

        # 將訊號日轉成 iloc 位置
        date_to_idx = {d: i for i, d in enumerate(df.index)}
        raw_signal_dates = list(sig.index[sig.values])

        # 去重：同檔 DEDUP_COOLDOWN 天內只計第一筆
        signal_dates = []
        last_kept_idx = -10**9
        for sd in raw_signal_dates:
            cur_idx = date_to_idx[sd]
            if cur_idx - last_kept_idx >= DEDUP_COOLDOWN:
                signal_dates.append(sd)
                last_kept_idx = cur_idx

        for sd in signal_dates:
            if min_date and sd < pd.Timestamp(min_date): continue
            if max_date and sd > pd.Timestamp(max_date): continue
            r = simulate_one(df, ticker, row["name"], date_to_idx[sd])
            if r is not None:
                records.append(r)

    con.close()

    print(f"\n=== 回測完成 ===")
    print(f"總訊號數: {len(records)}  (耗時 {time.time()-t0:.1f}s)")

    if not records:
        print("沒有任何訊號可分析")
        return

    df_rec = pd.DataFrame(records)
    df_rec.to_csv(OUT_SIGNALS, index=False)
    print(f"明細寫入: {OUT_SIGNALS}")

    summary = summarize(df_rec)
    with open(OUT_SUMMARY, "w") as f:
        f.write(summary)
    print(f"摘要寫入: {OUT_SUMMARY}")
    print()
    print(summary)


def summarize(df):
    n = len(df)
    n_win = (df['outcome']=='WIN').sum()
    n_loss = (df['outcome']=='LOSS').sum()
    n_to = (df['outcome']=='TIMEOUT').sum()
    win_rate = n_win / n if n else 0
    loss_rate = n_loss / n if n else 0

    avg_win = df.loc[df['outcome']=='WIN', 'return_pct'].mean() if n_win else 0
    avg_loss = df.loc[df['outcome']=='LOSS', 'return_pct'].mean() if n_loss else 0
    avg_to = df.loc[df['outcome']=='TIMEOUT', 'return_pct'].mean() if n_to else 0
    avg_all = df['return_pct'].mean()

    profit_factor = abs(avg_win / avg_loss) if (n_loss and avg_loss != 0) else float('nan')
    expected = (win_rate*avg_win + (n_loss/n)*avg_loss + (n_to/n)*avg_to) if n else 0

    # 持有天數分布
    days_win = df.loc[df['outcome']=='WIN', 'days_held'].describe()
    days_loss = df.loc[df['outcome']=='LOSS', 'days_held'].describe()

    # 最大連敗
    streak = 0; max_loss_streak = 0; max_win_streak = 0; cur_w = 0
    for o in df.sort_values('signal_date')['outcome']:
        if o == 'LOSS':
            streak += 1; cur_w = 0
            max_loss_streak = max(max_loss_streak, streak)
        elif o == 'WIN':
            cur_w += 1; streak = 0
            max_win_streak = max(max_win_streak, cur_w)
        else:
            streak = 0; cur_w = 0

    # 月份分布
    df['month'] = df['signal_date'].str[:7]
    by_month = df.groupby('month').size().to_dict()

    # 重複命中標的
    by_ticker = df.groupby('ticker').size().sort_values(ascending=False).head(10)

    # 訊號集中度
    by_day = df.groupby('signal_date').size().sort_values(ascending=False).head(5)

    lines = []
    lines.append("="*60)
    lines.append(" 台股「主升回檔再啟動」5 條件回測結果")
    lines.append("="*60)
    lines.append(f"資料期間: {df['signal_date'].min()} ~ {df['signal_date'].max()}")
    lines.append(f"進場規則: 隔日開盤買入")
    lines.append(f"出場規則: 過 60 日前高 = WIN / 收盤 -{int(STOP_LOSS_PCT*100)}% = LOSS / 滿 {HOLD_DAYS} 天 = TIMEOUT")
    lines.append("")
    lines.append("【整體統計】")
    lines.append(f"  總訊號數          : {n}")
    lines.append(f"  WIN (過前高)      : {n_win}  ({win_rate:.1%})")
    lines.append(f"  LOSS (停損)       : {n_loss}  ({loss_rate:.1%})")
    lines.append(f"  TIMEOUT (滿 20 天): {n_to}  ({n_to/n:.1%})")
    lines.append("")
    lines.append("【報酬指標】")
    lines.append(f"  平均勝幅          : {avg_win:+.2f}%")
    lines.append(f"  平均敗幅          : {avg_loss:+.2f}%")
    lines.append(f"  TIMEOUT 平均報酬  : {avg_to:+.2f}%")
    lines.append(f"  全部訊號平均報酬  : {avg_all:+.2f}%")
    lines.append(f"  盈虧比 (|勝/敗|)   : {profit_factor:.2f}")
    lines.append(f"  每筆期望值        : {expected:+.2f}%")
    lines.append("")
    lines.append("【持有天數】")
    lines.append(f"  WIN  天數中位數: {days_win.get('50%', float('nan')):.0f}  (平均 {days_win.get('mean', 0):.1f})")
    lines.append(f"  LOSS 天數中位數: {days_loss.get('50%', float('nan')):.0f}  (平均 {days_loss.get('mean', 0):.1f})")
    lines.append("")
    lines.append("【連續紀錄】")
    lines.append(f"  最大連勝: {max_win_streak}  最大連敗: {max_loss_streak}")
    lines.append("")
    lines.append("【月份訊號分布】")
    for m, c in sorted(by_month.items()):
        lines.append(f"  {m}: {c}")
    lines.append("")
    lines.append("【重複命中前 10】")
    for t, c in by_ticker.items():
        lines.append(f"  {t}: {c} 次")
    lines.append("")
    lines.append("【訊號最多的 5 天】")
    for d, c in by_day.items():
        lines.append(f"  {d}: {c} 檔")
    lines.append("="*60)
    return "\n".join(lines)


if __name__ == "__main__":
    min_d = sys.argv[1] if len(sys.argv) > 1 else None
    max_d = sys.argv[2] if len(sys.argv) > 2 else None
    run_backtest(min_d, max_d)
