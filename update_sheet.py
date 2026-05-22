"""
把當日 scan 結果 append 進 Google Sheet「Aplus掃描」。

使用 service account 認證（一次性設定，腳本之後完全自動）：

1. 到 https://console.cloud.google.com/ 建專案（或用既有）
2. 「APIs & Services > Library」搜 "Google Sheets API"，按 Enable
3. 「APIs & Services > Credentials > Create Credentials > Service Account」
4. 建立後進入該 Service Account → "Keys" → "Add Key" → JSON → 下載
5. 把 JSON 存到 ~/.config/aplus/service_account.json
6. 開啟 Google Sheet「Aplus掃描」→ Share → 加入 service account 的 email
   （JSON 內 "client_email" 那個地址）→ 給 Editor 權限

之後跑：
    python update_sheet.py                  # 預設今日
    python update_sheet.py 2026-05-22       # 指定日期

特色：
- 自動跳過已存在的日期（避免重複 append）
- 自動加 date + bucket 兩欄
- 依 R:R 排序
"""
import sys, os
from datetime import date
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plot_signals import rr_bucket

CRED_PATH = os.path.expanduser('~/.config/aplus/service_account.json')
SHEET_ID  = '1CW9J3m71fOyfaAhX5hgT0mfoELFRkVmYEROnIaf2m08'  # Aplus掃描

DISPLAY_COLS = ['date', 'ticker', 'name', 'close', 'prev_high', 'pullback_low',
                'reward%', 'risk%', 'R:R', 'bucket']
RENAME_MAP   = {'est_rr': 'R:R', 'reward_pct': 'reward%', 'risk_pct': 'risk%'}


def load_today_df(date_str, market='tw'):
    prefix = 'today_scan_us_' if market == 'us' else 'today_scan_'
    csv_path = f'/Users/rick/Developer/Aplus/{prefix}{date_str}.csv'
    if not os.path.exists(csv_path):
        print(f'❌ 找不到 {csv_path}（請先跑 scan）'); sys.exit(1)
    df = pd.read_csv(csv_path)
    df.insert(0, 'date', date_str)
    df['bucket'] = df['est_rr'].apply(rr_bucket)
    df = df.sort_values('est_rr', ascending=False).reset_index(drop=True)
    df = df.rename(columns=RENAME_MAP)
    return df[DISPLAY_COLS]


def get_sheet():
    try:
        import gspread
    except ImportError:
        print('❌ 缺套件：pip install gspread')
        sys.exit(1)
    if not os.path.exists(CRED_PATH):
        print(f'❌ 找不到 service account JSON：{CRED_PATH}')
        print('   設定步驟見本檔 docstring')
        sys.exit(1)
    client = gspread.service_account(filename=CRED_PATH)
    return client.open_by_key(SHEET_ID).sheet1


def append_today(date_str, market='tw'):
    df = load_today_df(date_str, market=market)
    sheet = get_sheet()

    # 判重：抓現有 date 欄
    existing_dates = set(sheet.col_values(1)[1:])   # 跳過 header
    if date_str in existing_dates:
        print(f'⚠️  {date_str} 已存在於 Sheet，跳過 append')
        return

    rows = df.astype(object).values.tolist()
    sheet.append_rows(rows, value_input_option='USER_ENTERED')
    print(f'✅ 已 append {len(rows)} 列 (date={date_str})')


if __name__ == '__main__':
    args = sys.argv[1:]
    market = 'tw'
    if args and args[-1] in ('tw', 'us'):
        market = args.pop()
    date_str = args[0] if args else date.today().strftime('%Y-%m-%d')
    append_today(date_str, market=market)
