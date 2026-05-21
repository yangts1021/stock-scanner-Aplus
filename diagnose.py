"""
診斷單一標的在指定日期是否符合 v7 各條件，輸出 box-grid 表格。

usage:
    python diagnose.py <YYYY-MM-DD> [tw|us] <ticker>
    python diagnose.py 2026-05-21 tw 6526
    python diagnose.py 2026-05-21 us NVDA
"""
import sys, os
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan_vectorized import (
    rolling_pullback,
    MIN_60MA_SLOPE, MIN_20D_MAX_GAIN, PULLBACK_RANGE, MIN_PEAK_TO_TROUGH,
    MAX_MA_SPREAD, MIN_TODAY_CHG, MIN_VOL_RATIO, MIN_BODY_MULT,
)
from today_scan_v7 import fancy_table


def diagnose(ticker, date_str, market='tw'):
    if market == 'tw':
        from twstock import get_conn, get_prices
        code = ticker.split('.')[0]
        suffix = ticker.split('.')[1] if '.' in ticker else None
        con = get_conn()
        if suffix is None:
            row = con.execute(
                "SELECT market, MAX(name) FROM prices WHERE ticker=? GROUP BY ticker",
                (code,)).fetchone()
            if not row:
                print(f"{ticker} 不在 DB"); con.close(); return
            mkt, name = row
            suffix = 'TW' if mkt == 'TW' else 'TWO'
            ticker = f"{code}.{suffix}"
        else:
            row = con.execute(
                "SELECT MAX(name) FROM prices WHERE ticker=?",
                (code,)).fetchone()
            name = row[0] if row else ''
    else:
        from usstock import get_conn, get_prices
        con = get_conn()
        row = con.execute(
            "SELECT MAX(name) FROM prices WHERE ticker=?", (ticker,)).fetchone()
        name = row[0] if row else ''

    df = get_prices(con, ticker)
    con.close()
    if df.empty:
        print(f"{ticker} 無價量資料"); return

    target = pd.Timestamp(date_str)
    if target not in df.index:
        print(f"{ticker}: {date_str} 不在 K 線中（最後一日={df.index[-1].date()}）")
        return

    i = df.index.get_loc(target)
    close = df['Close']; high = df['High']; low = df['Low']
    vol = df['Volume']; opn = df['Open']
    ma5  = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    vol5 = vol.rolling(5).mean()

    # --- 計算每個子條件 ---
    rows = []
    def add(num, label, threshold, actual, ok):
        rows.append({
            '#': num, '條件': label,
            '閾值': threshold, '實際': actual,
            '': '✅' if ok else '❌',
        })

    # C1
    c = close.iloc[i]; m60 = ma60.iloc[i]
    add('1a', 'close > MA60', f'>{m60:.2f}', f'{c:.2f}', c > m60)
    slope60 = ma60.pct_change(20).iloc[i]
    add('1b', 'MA60 20日斜率', f'> 0', f'{slope60*100:+.2f}%', slope60 > MIN_60MA_SLOPE)

    # C2
    g = close.pct_change(20).rolling(60).max().iloc[i]
    add('2', '60日內 20日漲幅', f'≥ {MIN_20D_MAX_GAIN*100:.0f}%', f'{g*100:.2f}%',
        g >= MIN_20D_MAX_GAIN)

    # C3
    pb_s, ptd_s = rolling_pullback(high, low, 60)
    pb = pb_s.iloc[i]; ptd = ptd_s.iloc[i]
    h_arr = high.values; l_arr = low.values
    s = i - 59
    peak_iloc = s + int(np.argmax(h_arr[s:i+1]))
    trough_iloc = peak_iloc + int(np.argmin(l_arr[peak_iloc:i+1]))
    add('3a', '回撤深度 (peak→trough)',
        f'[{PULLBACK_RANGE[0]*100:.0f}%, {PULLBACK_RANGE[1]*100:.0f}%]',
        f'{pb*100:.2f}% ({h_arr[peak_iloc]:.2f}→{l_arr[trough_iloc]:.2f})',
        PULLBACK_RANGE[0] <= pb <= PULLBACK_RANGE[1])
    add('3b', '峰至谷天數',
        f'≥ {MIN_PEAK_TO_TROUGH}',
        f'{ptd} 天 ({df.index[peak_iloc].date()} → {df.index[trough_iloc].date()})',
        ptd >= MIN_PEAK_TO_TROUGH)

    # C4
    mv = [ma5.iloc[i], ma10.iloc[i], ma20.iloc[i]]
    sp = (max(mv) - min(mv)) / min(mv)
    add('4', 'MA spread (5/10/20)',
        f'≤ {MAX_MA_SPREAD*100:.0f}%',
        f'{sp*100:.2f}% (MA5={mv[0]:.2f}, MA10={mv[1]:.2f}, MA20={mv[2]:.2f})',
        sp <= MAX_MA_SPREAD)

    # C5
    chg = (close.iloc[i] - close.iloc[i-1]) / close.iloc[i-1]
    add('5a', '今日漲幅',
        f'≥ {MIN_TODAY_CHG*100:.0f}%', f'{chg*100:+.2f}%', chg >= MIN_TODAY_CHG)
    add('5b', 'close > MA5',
        f'>{ma5.iloc[i]:.2f}', f'{close.iloc[i]:.2f}', close.iloc[i] > ma5.iloc[i])
    add('5c', 'close > MA10',
        f'>{ma10.iloc[i]:.2f}', f'{close.iloc[i]:.2f}', close.iloc[i] > ma10.iloc[i])
    add('5d', 'High ≥ MA20',
        f'≥{ma20.iloc[i]:.2f}', f'{high.iloc[i]:.2f}', high.iloc[i] >= ma20.iloc[i])
    vol5_prev = vol5.iloc[i-1] if not pd.isna(vol5.iloc[i-1]) else 0
    vr = vol.iloc[i] / vol5_prev if vol5_prev > 0 else 0
    add('5e', '量比 (今日/前5日均)',
        f'≥ {MIN_VOL_RATIO}', f'{vr:.2f} ({vol.iloc[i]:,.0f}/{vol5_prev:,.0f})',
        vr >= MIN_VOL_RATIO)
    body = abs(close.iloc[i] - opn.iloc[i])
    body10 = (close.iloc[i-10:i] - opn.iloc[i-10:i]).abs().mean()
    add('5f', 'K 棒實體 / 前10日均',
        f'≥ {MIN_BODY_MULT:.1f}×', f'{body:.2f} vs {body10:.2f} ({body/body10:.2f}×)',
        body >= body10 * MIN_BODY_MULT)

    # 標題與表格
    o0, h0, l0, c0 = opn.iloc[i], high.iloc[i], low.iloc[i], close.iloc[i]
    print(f'\n=== {ticker} {name} @ {date_str} ===')
    print(f'OHLC = {o0:.2f} / {h0:.2f} / {l0:.2f} / {c0:.2f}    '
          f'量 {vol.iloc[i]:,.0f}')
    diag_df = pd.DataFrame(rows)
    print(fancy_table(diag_df))

    n_fail = sum(1 for r in rows if r[''] == '❌')
    if n_fail == 0:
        print(f'\n→ 全 {len(rows)} 條件通過 ✅')
    else:
        fails = [r['#'] for r in rows if r[''] == '❌']
        print(f'\n→ {n_fail} 條件未過: {", ".join(fails)}')


def main():
    if len(sys.argv) < 3:
        print("usage: diagnose.py <YYYY-MM-DD> [tw|us] <ticker>"); sys.exit(1)
    date_str = sys.argv[1]
    if sys.argv[2] in ('tw', 'us'):
        market = sys.argv[2]; ticker = sys.argv[3]
    else:
        market = 'tw'; ticker = sys.argv[2]
    diagnose(ticker, date_str, market=market)


if __name__ == "__main__":
    main()
