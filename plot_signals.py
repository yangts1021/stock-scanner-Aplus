"""
依日期讀 today_scan_<date>.csv，為每個訊號畫 K 線圖。
也可指定單一 ticker（不需事先掃描）。

usage:
    python plot_signals.py <YYYY-MM-DD> [tw|us]            # 全部訊號
    python plot_signals.py <YYYY-MM-DD> [tw|us] <ticker>   # 指定單檔
"""
import os, sys
import pandas as pd
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.lines import Line2D
import mplfinance as mpf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 中文字型
_available = {f.name for f in fm.fontManager.ttflist}
CJK_FONT = next((f for f in ['Heiti TC', 'PingFang TC', 'Arial Unicode MS',
                              'Hiragino Sans GB', 'Noto Sans CJK TC']
                 if f in _available), None)
if CJK_FONT:
    matplotlib.rcParams['font.sans-serif'] = [CJK_FONT] + matplotlib.rcParams['font.sans-serif']
    matplotlib.rcParams['font.family'] = 'sans-serif'
matplotlib.rcParams['axes.unicode_minus'] = False

LOOKBACK = 60  # 與 scan/backtest 一致
RIGHT_PAD = 20  # 訊號日右側額外留白，讓虛線有空間延伸
MA_CONFIG = [(30, '#ffd54f'), (45, '#4fc3f7'), (60, '#ba68c8')]  # (period, color)


def rr_bucket(rr):
    """依 R:R 回傳子資料夾名稱"""
    if rr is None or not isinstance(rr, (int, float)) or rr != rr:  # NaN/None
        return 'rr_na'
    if rr >= 2.5: return 'rr_2.5+'
    if rr >= 1.5: return 'rr_1.5-2.5'
    if rr >= 1.0: return 'rr_1.0-1.5'
    return 'rr_lt1.0'


def find_segments(df, sidx):
    """回傳 (up_start_iloc, prev_high_iloc, pullback_low_iloc)"""
    start = max(0, sidx - (LOOKBACK - 1))
    high_slice = df['High'].iloc[start:sidx + 1]
    ph_iloc = start + int(np.argmax(high_slice.values))
    # 上漲段起點：前高再往前 LOOKBACK 天的最低 Low
    up_start_a = max(0, ph_iloc - LOOKBACK)
    low_slice = df['Low'].iloc[up_start_a:ph_iloc + 1]
    up_iloc = up_start_a + int(np.argmin(low_slice.values))
    # 回撤段低點：前高到訊號日之間的最低 Low（若前高 == 訊號日，回撤段就退化為當日）
    if ph_iloc >= sidx:
        pb_iloc = sidx
    else:
        pb_slice = df['Low'].iloc[ph_iloc:sidx + 1]
        pb_iloc = ph_iloc + int(np.argmin(pb_slice.values))
    return up_iloc, ph_iloc, pb_iloc


def plot_one(ticker, name, signal_date, market='tw', out_dir='charts'):
    if market == 'tw':
        from twstock import get_conn, get_prices
    else:
        from usstock import get_conn, get_prices
    con = get_conn()
    df = get_prices(con, ticker)
    con.close()
    if df.empty:
        print(f"  {ticker}: 無資料"); return
    target = pd.Timestamp(signal_date)
    if target not in df.index:
        print(f"  {ticker}: {signal_date} 不在 K 線中"); return
    sidx = df.index.get_loc(target)

    up_iloc, ph_iloc, pb_iloc = find_segments(df, sidx)
    # 顯示窗口：上漲段起點往前 5 天、訊號日往後 5 天
    show_start = max(0, up_iloc - 5)
    show_end = min(len(df), sidx + 6)
    plot_df = df.iloc[show_start:show_end].copy()

    prev_high     = float(df.iloc[ph_iloc]['High'])
    pullback_low  = float(df.iloc[pb_iloc]['Low'])
    current_close = float(df.iloc[sidx]['Close'])
    risk_pct   = (current_close - pullback_low) / current_close * 100
    reward_pct = (prev_high - current_close) / current_close * 100
    rr = reward_pct / risk_pct if risk_pct > 0 else float('inf')
    max_dd_pct = (pullback_low - prev_high) / prev_high * 100  # 回撤段最大回撤

    mc = mpf.make_marketcolors(up='#ef5350', down='#26a69a',
                               edge='inherit', wick='inherit', volume='in')
    rc = {
        'font.size': 10, 'axes.unicode_minus': False,
        'figure.facecolor': '#1a1a1a',
        'axes.facecolor':   '#1a1a1a',
        'savefig.facecolor':'#1a1a1a',
        'axes.edgecolor':   '#555555',
        'axes.labelcolor':  '#cccccc',
        'xtick.color':      '#cccccc',
        'ytick.color':      '#cccccc',
        'text.color':       '#dddddd',
        'grid.color':       '#333333',
    }
    if CJK_FONT:
        rc['font.sans-serif'] = [CJK_FONT, 'DejaVu Sans']
        rc['font.family'] = 'sans-serif'
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True, rc=rc)

    # 三均線：在完整 df 上計算後再切到 plot_df 範圍（確保 MA60 起始正確）
    ma_addplots = []
    for period, color in MA_CONFIG:
        ma_series = df['Close'].rolling(period).mean().reindex(plot_df.index)
        if ma_series.notna().any():
            ma_addplots.append(mpf.make_addplot(
                ma_series, color=color, width=1.0, panel=0))

    fig, axes = mpf.plot(
        plot_df, type='candle', style=style,
        volume=True, figsize=(13, 7),
        addplot=ma_addplots,
        title=f"\n{ticker}  {name}    訊號日 {signal_date}",
        returnfig=True,
        warn_too_much_data=10000,
    )
    ax = axes[0]

    # 均線 legend
    ma_handles = [Line2D([0], [0], color=c, lw=1.5, label=f'MA{p}')
                  for p, c in MA_CONFIG]
    ax.legend(handles=ma_handles, loc='upper left', framealpha=0.85, fontsize=9)

    # 把 plot_df 內的日期映射到整數位置（mplfinance 用整數 x 軸）
    pos = {d: i for i, d in enumerate(plot_df.index)}
    up_a, up_b = df.index[up_iloc], df.index[ph_iloc]

    # 右側留白：訊號日之後若無資料，仍給虛線延伸空間
    right_x = len(plot_df) - 1 + RIGHT_PAD
    cur_xlim = ax.get_xlim()
    ax.set_xlim(cur_xlim[0], right_x + 1)
    for extra_ax in axes[1:]:
        extra_ax.set_xlim(cur_xlim[0], right_x + 1)

    # 三條水平虛線：只畫訊號日往右（前高=紅、止損=綠、現價=白）
    sig_x = pos.get(target)
    if sig_x is not None:
        ax.hlines([prev_high, pullback_low, current_close],
                  xmin=sig_x, xmax=right_x,
                  colors=['#ef5350', '#26a69a', '#f5f5f5'],
                  linestyles='--', linewidths=1.0, zorder=4)

    # level + 價位：放在虛線中段（boxed）；reward/risk 放虛線中間區域
    x_label = right_x
    mid_dash_x = (sig_x + right_x) / 2 if sig_x is not None else right_x

    def _boxed(y, text, color, va):
        ax.text(mid_dash_x, y, f' {text} ',
                ha='center', va=va, color=color, fontsize=10,
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#1a1a1a',
                          edgecolor=color, linewidth=0.8, alpha=0.92),
                zorder=5)

    # 目標放虛線上方、止損放虛線下方，現價維持貼齊虛線
    _boxed(prev_high,
           f'目標: {prev_high:.2f} ({reward_pct:+.1f}%)', '#ef5350', 'bottom')
    _boxed(pullback_low,
           f'止損: {pullback_low:.2f} (-{risk_pct:.1f}%)', '#26a69a', 'top')
    _boxed(current_close,
           f'現價: {current_close:.2f}   R:R {rr:.2f}', '#f5f5f5', 'center')

    # 前高 → 回撤低點：向下箭頭 + 最大回撤% 標注
    ph_pos = pos.get(df.index[ph_iloc])
    pb_pos = pos.get(df.index[pb_iloc])
    if ph_pos is not None and pb_pos is not None and pb_pos > ph_pos:
        mid_x = (ph_pos + pb_pos) / 2
        ax.annotate('',
                    xy=(mid_x, pullback_low),
                    xytext=(mid_x, prev_high),
                    arrowprops=dict(arrowstyle='->', color='#ffa726',
                                    lw=1.8, shrinkA=0, shrinkB=0))
        label_y = prev_high + 0.25 * (pullback_low - prev_high)  # 箭頭上方 1/4
        ax.text(mid_x + 1.5, label_y,
                f' {max_dd_pct:.1f}% ',
                ha='left', va='center', color='#ffa726',
                fontsize=11, fontweight='bold',
                bbox=dict(boxstyle='round,pad=0.3',
                          facecolor='#1a1a1a', edgecolor='#ffa726',
                          linewidth=0.8, alpha=0.92))

    # 上漲段起點 / 前高頂點（小標記）
    if up_a in pos:
        ax.scatter(pos[up_a], float(df.iloc[up_iloc]['Low']),
                   marker='^', color='#26a69a', s=60, zorder=5)
    if up_b in pos:
        ax.scatter(pos[up_b], prev_high,
                   marker='v', color='#ef5350', s=60, zorder=5)

    bucket_dir = os.path.join(out_dir, rr_bucket(rr))
    os.makedirs(bucket_dir, exist_ok=True)
    out_path = os.path.join(bucket_dir, f"{ticker}_{signal_date}.png")
    fig.savefig(out_path, dpi=120, bbox_inches='tight')
    plt.close(fig)
    print(f"  → {out_path}")


def _resolve_tw_ticker(code):
    """3673 → 3673.TW / 3673.TWO；若已含 suffix 直接回傳"""
    if '.' in code:
        return code
    from twstock import get_conn
    con = get_conn()
    row = con.execute(
        "SELECT market FROM prices WHERE ticker=? GROUP BY ticker",
        (code,)).fetchone()
    con.close()
    if not row:
        return None
    suffix = 'TW' if row[0] == 'TW' else 'TWO'
    return f"{code}.{suffix}"


def _resolve_name(ticker, market):
    if market == 'tw':
        from twstock import get_conn
        code = ticker.split('.')[0]
        con = get_conn()
        row = con.execute(
            "SELECT MAX(name) FROM prices WHERE ticker=?", (code,)).fetchone()
        con.close()
    else:
        from usstock import get_conn
        con = get_conn()
        row = con.execute(
            "SELECT MAX(name) FROM prices WHERE ticker=?", (ticker,)).fetchone()
        con.close()
    return row[0] if row and row[0] else ''


def main():
    if len(sys.argv) < 2:
        print("usage: plot_signals.py <YYYY-MM-DD> [tw|us] [ticker]")
        sys.exit(1)
    date_str = sys.argv[1]
    market = sys.argv[2] if len(sys.argv) > 2 else 'tw'
    single_ticker = sys.argv[3] if len(sys.argv) > 3 else None

    out_dir = f"/Users/rick/Developer/Aplus/charts/{date_str}"

    if single_ticker:
        ticker = _resolve_tw_ticker(single_ticker) if market == 'tw' else single_ticker
        if ticker is None:
            print(f"{single_ticker} 不在 {market} DB"); sys.exit(1)
        name = _resolve_name(ticker, market)
        print(f"畫 1 檔 → {out_dir}/")
        plot_one(ticker, name, date_str, market=market, out_dir=out_dir)
        return

    prefix = 'today_scan_us_' if market == 'us' else 'today_scan_'
    csv = f"/Users/rick/Developer/Aplus/{prefix}{date_str}.csv"
    if not os.path.exists(csv):
        print(f"找不到 {csv}（請先跑 scan，或加 ticker 參數）"); sys.exit(1)
    hits = pd.read_csv(csv)
    print(f"畫 {len(hits)} 檔 → {out_dir}/")
    for _, row in hits.iterrows():
        plot_one(row['ticker'], str(row['name']), date_str, market=market, out_dir=out_dir)


if __name__ == "__main__":
    main()
