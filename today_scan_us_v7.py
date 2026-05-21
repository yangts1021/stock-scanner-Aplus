"""
美股 v7 掃描（根據 1000 檔 × 2yr 回測調校）
- V7 形態：scan_vectorized.signals_for_ticker
- 大盤過濾：^IXIC 收盤 > 60MA 且 60MA 20 日斜率 > 0
- 候選資格（依回測 EV 排序）：
    Tier A: R:R ∈ [1.0, 1.5) + 停損 ≥ 5%  → 回測 EV +3.95%（n=49, 勝率 42.9%）
    Tier B: R:R ≥ 2.0          + 停損 ≥ 5%  → 回測 EV +1.16%（n=19, 勝率 21.1%）
    死亡帶: R:R ∈ [1.5, 2.0)               → 回測 EV -0.28%，**不進場**
"""
import sys, os, unicodedata
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usstock import get_conn, get_prices, all_tickers, get_index_df
from scan_vectorized import signals_for_ticker

LOOKBACK     = 60
MIN_RISK_PCT = 5.0
RR_TIER_A    = (1.0, 1.5)   # 主力區間（最佳 EV）
RR_TIER_B    = 2.0          # 次要：R:R ≥ 此值（避開死亡帶 1.5~2.0）

DISPLAY_RENAME = {'reward_pct': 'reward%', 'risk_pct': 'risk%', 'est_rr': 'R:R'}


def _vw(s):
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in str(s))


def _pad(s, w, align='left'):
    s = str(s)
    extra = w - _vw(s)
    if extra <= 0:
        return s
    if align == 'center':
        l = extra // 2
        return ' ' * l + s + ' ' * (extra - l)
    return s + ' ' * extra


def fancy_table(df):
    cols = [DISPLAY_RENAME.get(c, c) for c in df.columns]
    rows = [['' if pd.isna(v) else str(v) for v in row]
            for row in df.itertuples(index=False, name=None)]
    widths = []
    for i, h in enumerate(cols):
        w = _vw(h)
        for r in rows:
            w = max(w, _vw(r[i]))
        widths.append(w)
    def hline(l, m, r):
        return l + m.join('─' * (w + 2) for w in widths) + r
    def cell_row(cells, align):
        return '│ ' + ' │ '.join(_pad(c, widths[i], align) for i, c in enumerate(cells)) + ' │'
    out = [hline('┌', '┬', '┐'), cell_row(cols, 'center')]
    for r in rows:
        out.append(hline('├', '┼', '┤'))
        out.append(cell_row(r, 'left'))
    out.append(hline('└', '┴', '┘'))
    return '\n'.join(out)

def ndx_bull_on(con, date_str):
    idx = get_index_df(con)
    close = idx['Close'].dropna()
    ma60 = close.rolling(60).mean()
    slope60 = ma60.pct_change(20)
    bull = ((close > ma60) & (slope60 > 0)).fillna(False)
    target = pd.Timestamp(date_str)
    if target not in bull.index:
        return None, None
    return bool(bull.loc[target]), float(close.loc[target])

def main():
    target_date = sys.argv[1] if len(sys.argv) > 1 else None
    if not target_date:
        from datetime import date
        target_date = date.today().strftime("%Y-%m-%d")
    target = pd.Timestamp(target_date)

    con = get_conn()
    bull, idx_close = ndx_bull_on(con, target_date)
    if bull is None:
        print(f"❌ ^IXIC 在 {target_date} 沒有資料")
        con.close()
        return
    print(f"^IXIC {target_date} 收盤 {idx_close:.2f}  大盤多頭? {'是 ✅' if bull else '否 ❌'}")
    print()

    tk = all_tickers(con)
    hits = []
    for _, row in tk.iterrows():
        ticker = row["ticker"]
        df = get_prices(con, ticker)
        if df.empty or len(df) < 80: continue
        if target not in df.index: continue

        sig = signals_for_ticker(df)
        if not sig.loc[target]: continue

        sidx = df.index.get_loc(target)
        start = max(0, sidx - (LOOKBACK - 1))
        high_slice = df['High'].iloc[start:sidx + 1]
        prev_high = float(high_slice.max())
        high_iloc = df.index.get_loc(high_slice.idxmax())
        if high_iloc >= sidx:
            pullback_low = float(df.iloc[sidx]['Low'])
        else:
            pullback_low = float(df['Low'].iloc[high_iloc:sidx + 1].min())

        est_entry = float(df.iloc[sidx]['Close'])
        risk = est_entry - pullback_low
        reward = prev_high - est_entry
        rr = (reward / risk) if risk > 0 else float('inf')

        hits.append({
            'ticker': ticker,
            'name': row['name'][:40],
            'close': round(est_entry, 2),
            'prev_high': round(prev_high, 2),
            'pullback_low': round(pullback_low, 2),
            'reward_pct': round(reward / est_entry * 100, 1),
            'risk_pct': round(risk / est_entry * 100, 1),
            'est_rr': round(rr, 2) if np.isfinite(rr) else None,
        })
    con.close()

    hits_df = pd.DataFrame(hits)
    if hits_df.empty:
        print(f"{target_date} 無任何 v7 訊號")
        return

    # 分桶標記
    def bucket(r, rp):
        if rp < MIN_RISK_PCT:    return "X_tight_stop"
        if RR_TIER_A[0] <= r < RR_TIER_A[1]: return "A"
        if RR_TIER_A[1] <= r < RR_TIER_B:    return "X_death_zone"
        if r >= RR_TIER_B:                   return "B"
        return "X_low_rr"
    hits_df['tier'] = hits_df.apply(lambda x: bucket(x['est_rr'] or 0, x['risk_pct']), axis=1)
    hits_df = hits_df.sort_values(['tier', 'est_rr'], ascending=[True, True])

    print(f"=== {target_date} v7 全部訊號（共 {len(hits_df)} 檔，含 tier 標記）===")
    print(fancy_table(hits_df))
    print()

    tier_a = hits_df[hits_df['tier'] == 'A']
    tier_b = hits_df[hits_df['tier'] == 'B']

    if not bull:
        print("⚠️  大盤非多頭，依策略不應進場（僅供觀察）")
    print(f"--- Tier A: R:R ∈ [{RR_TIER_A[0]}, {RR_TIER_A[1]}) + 停損≥{MIN_RISK_PCT}% ({len(tier_a)} 檔) [回測 EV +3.95%] ---")
    if tier_a.empty:
        print("(無)")
    else:
        print(fancy_table(tier_a))
    print()
    print(f"--- Tier B: R:R ≥ {RR_TIER_B} + 停損≥{MIN_RISK_PCT}% ({len(tier_b)} 檔) [回測 EV +1.16%] ---")
    if tier_b.empty:
        print("(無)")
    else:
        print(fancy_table(tier_b))

    out = f"/Users/rick/Developer/Aplus/today_scan_us_{target_date}.csv"
    hits_df.to_csv(out, index=False)
    print(f"\n→ {out}")

if __name__ == "__main__":
    main()
