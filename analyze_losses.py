"""
分析方案 C：對所有 LOSS 訊號，看看放棄停損改延長持有，多少最終會過前高？
- 從訊號日後追蹤 EXTENDED_WINDOW 個交易日（不停損，純觀察）
- 統計：
    a) 在 21~EXTENDED_WINDOW 天內過前高的比例（停損後才過 = 被洗）
    b) 過前高的「總體比例」變化
    c) 若改用「不停損、N 天到期」的策略，會變什麼樣
"""
import sys, os
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twstock import get_conn, get_prices

EXTENDED_WINDOW = 60  # 延長至 60 個交易日

def main():
    signals = pd.read_csv("/Users/rick/Developer/Aplus/backtest_signals.csv")
    losses = signals[signals['outcome']=='LOSS'].copy()
    print(f"原回測：總訊號 {len(signals)}，其中 LOSS = {len(losses)}（{len(losses)/len(signals)*100:.1f}%）")
    print(f"延長觀察視窗：{EXTENDED_WINDOW} 個交易日")
    print()

    con = get_conn()
    cache = {}
    def get_df(ticker):
        if ticker not in cache:
            cache[ticker] = get_prices(con, ticker)
        return cache[ticker]

    saved = []        # 被殺但其實會過前高的訊號
    truly_dead = []   # 被殺且 60 天內也沒過前高
    new_returns = []  # 不停損的話會拿到的報酬（過前高就出 / 60 天到期）

    for _, r in losses.iterrows():
        df = get_df(r['ticker'])
        if df.empty: continue
        sig_ts = pd.Timestamp(r['signal_date'])
        if sig_ts not in df.index: continue
        sig_idx = df.index.get_loc(sig_ts)
        entry_idx = sig_idx + 1
        if entry_idx >= len(df): continue
        entry_price = df.iloc[entry_idx]['Open']
        prev_high = r['prev_high']

        forward = df.iloc[entry_idx : entry_idx + EXTENDED_WINDOW]
        break_idx = None
        for k in range(len(forward)):
            if forward.iloc[k]['High'] > prev_high:
                break_idx = k; break

        if break_idx is not None:
            saved.append({
                'ticker': r['ticker'], 'name': r['name'],
                'signal_date': r['signal_date'],
                'stopped_day': r['days_held'],
                'broke_through_day': break_idx + 1,
                'stop_return_pct': r['return_pct'],
                'breakthrough_return_pct': round((prev_high - entry_price)/entry_price*100, 2),
            })
            new_returns.append((prev_high - entry_price)/entry_price*100)
        else:
            truly_dead.append(r['ticker'])
            # 60 天到期，用第 60 天收盤算
            if len(forward) > 0:
                exit_price = forward.iloc[-1]['Close']
                new_returns.append((exit_price - entry_price)/entry_price*100)
            else:
                new_returns.append(r['return_pct'])

    n_loss = len(losses)
    n_saved = len(saved)
    n_dead = len(truly_dead)

    print("="*60)
    print(" 方案 C 分析：移除 -8% 停損，延長至 60 天觀察")
    print("="*60)
    print()
    print(f"原 LOSS 訊號 {n_loss} 筆，延長持有後：")
    print(f"  ▸ 有 {n_saved} 筆（{n_saved/n_loss*100:.1f}%）最終會過前高 = 「被停損誤殺」")
    print(f"  ▸ 有 {n_dead} 筆（{n_dead/n_loss*100:.1f}%）60 天內都沒過前高 = 「真死」")
    print()
    if saved:
        saved_df = pd.DataFrame(saved)
        print(f"【誤殺訊號分析】({n_saved} 筆)")
        print(f"  停損出場時平均報酬: {saved_df['stop_return_pct'].mean():+.2f}%")
        print(f"  若延長持有平均報酬: {saved_df['breakthrough_return_pct'].mean():+.2f}%")
        print(f"  平均過前高所需天數: {saved_df['broke_through_day'].mean():.1f} 天")
        print(f"  停損日到突破日中位距離: {(saved_df['broke_through_day']-saved_df['stopped_day']).median():.0f} 天")
        print()
        # 哪些是「停損後 1~5 天內就突破」（最冤）
        within_5 = saved_df[saved_df['broke_through_day'] - saved_df['stopped_day'] <= 5]
        print(f"  其中 {len(within_5)} 筆（{len(within_5)/n_saved*100:.1f}%）在停損後 5 天內就突破前高")
        print(f"  其中 {(saved_df['broke_through_day'] <= 20).sum()} 筆若原本 20 天視窗內就會突破（被停損提前殺出）")

    # 假設不停損版本，整體期望值會變多少？
    print()
    print("【假設改為「無停損、追 60 天、過前高即出」整套策略期望值】")
    # 重新整體計算：把原 WIN + TIMEOUT + 新的 LOSS 替換結果合併
    win_df = signals[signals['outcome']=='WIN']
    timeout_df = signals[signals['outcome']=='TIMEOUT']

    # 新版 = 原 WIN 報酬 + 原 TIMEOUT 報酬 + 新 LOSS 報酬
    all_new = list(win_df['return_pct']) + list(timeout_df['return_pct']) + new_returns
    n_total = len(all_new)

    # 把新版的 outcome 分類（簡化版）
    new_wins = sum(r > 0 for r in all_new)
    new_losses_50_pct = sum(r < 0 for r in all_new)
    avg = np.mean(all_new)
    avg_win = np.mean([r for r in all_new if r > 0])
    avg_loss = np.mean([r for r in all_new if r < 0]) if new_losses_50_pct else 0
    print(f"  新版總訊號 {n_total}")
    print(f"  正報酬 {new_wins} ({new_wins/n_total*100:.1f}%)  負報酬 {new_losses_50_pct} ({new_losses_50_pct/n_total*100:.1f}%)")
    print(f"  平均報酬 {avg:+.2f}%   平均賺 {avg_win:+.2f}%   平均賠 {avg_loss:+.2f}%")
    print(f"  盈虧比 {abs(avg_win/avg_loss) if avg_loss else float('nan'):.2f}")
    print()
    print(f"  對照原版（-8% 停損 + 20 天追蹤）：期望值 -1.26%")

    con.close()

if __name__ == "__main__":
    main()
