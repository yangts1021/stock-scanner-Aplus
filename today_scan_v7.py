"""
今日訊號掃描（v7 + RR=2.5 規格）
- 用 scan_vectorized 找指定日期的訊號
- 計算前高/前低
- 因為「隔日開盤」尚未發生，以「指定日收盤」作為估算進場價計算 R:R
- 套用 TAIEX 大盤多頭過濾
- 列出 R:R ≥ 2.5 的候選名單供隔日早盤決策
"""
import sys, os, unicodedata
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twstock import get_conn, get_prices, all_tickers, get_index_df
from scan_vectorized import signals_for_ticker

LOOKBACK   = 60

DISPLAY_RENAME = {'reward_pct': 'reward%', 'risk_pct': 'risk%', 'est_rr': 'R:R'}


def _vw(s):
    """字串視覺寬度（CJK 算 2）"""
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
    """把 DataFrame 渲染成 Unicode box-grid（header 置中、資料靠左、每列加分隔）"""
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

def taiex_bull_on(con, date_str):
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
    target_date = sys.argv[1] if len(sys.argv) > 1 else "2026-05-14"
    target = pd.Timestamp(target_date)

    con = get_conn()
    bull, idx_close = taiex_bull_on(con, target_date)
    if bull is None:
        print(f"❌ TAIEX 在 {target_date} 沒有資料")
        con.close()
        return
    print(f"TAIEX {target_date} 收盤 {idx_close:.2f}  大盤多頭? {'是 ✅' if bull else '否 ❌'}")
    print()

    tk = all_tickers(con)
    hits = []
    for _, row in tk.iterrows():
        suffix = "TW" if row["market"]=="TW" else "TWO"
        ticker = f"{row['ticker']}.{suffix}"
        df = get_prices(con, ticker)
        if df.empty or len(df) < 80: continue
        if target not in df.index: continue

        sig = signals_for_ticker(df)
        if not sig.loc[target]: continue

        sidx = df.index.get_loc(target)
        # 前高 / 前低
        start = max(0, sidx - (LOOKBACK - 1))
        high_slice = df['High'].iloc[start:sidx+1]
        prev_high = float(high_slice.max())
        high_iloc = df.index.get_loc(high_slice.idxmax())
        if high_iloc >= sidx:
            pullback_low = float(df.iloc[sidx]['Low'])
        else:
            pullback_low = float(df['Low'].iloc[high_iloc:sidx+1].min())

        est_entry = float(df.iloc[sidx]['Close'])  # 用今日收盤估算
        risk = est_entry - pullback_low
        reward = prev_high - est_entry
        rr = (reward / risk) if risk > 0 else float('inf')

        hits.append({
            'ticker': ticker,
            'name': row['name'],
            'close': round(est_entry, 2),
            'prev_high': round(prev_high, 2),
            'pullback_low': round(pullback_low, 2),
            'reward_pct': round(reward/est_entry*100, 1),
            'risk_pct': round(risk/est_entry*100, 1),
            'est_rr': round(rr, 2) if np.isfinite(rr) else None,
        })
    con.close()

    hits_df = pd.DataFrame(hits)
    if hits_df.empty:
        print(f"{target_date} 無任何 v7 訊號")
        return

    hits_df = hits_df.sort_values('est_rr', ascending=False)
    print(f"=== {target_date} v7 訊號（共 {len(hits_df)} 檔，按 R:R 排序）===")
    print(fancy_table(hits_df))
    print()

    # 嚴格版：R:R ≥ 2.5 + 大盤多頭
    qualified = hits_df[hits_df['est_rr'] >= 2.5]
    print(f"--- v7 + R:R≥2.5 候選 ({len(qualified)} 檔) ---")
    if not bull:
        print("⚠️  大盤非多頭，依策略不應進場（僅供觀察）")
    if qualified.empty:
        print("無 R:R≥2.5 候選")
    else:
        print(fancy_table(qualified))

    hits_df.to_csv(f"/Users/rick/Developer/Aplus/today_scan_{target_date}.csv", index=False)
    print(f"\n→ today_scan_{target_date}.csv")

if __name__ == "__main__":
    main()
