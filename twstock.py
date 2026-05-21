"""
台股每日資料：證交所 / 櫃買中心官方 API → SQLite。
- init/update：增量抓取
- query：給定 ticker 回傳 DataFrame
"""
import sqlite3, urllib.request, ssl, json, time, os, sys
from datetime import datetime, timedelta, date
import pandas as pd

DB_PATH = "/Users/rick/Developer/Aplus/twstock.db"
TAIEX_PKL = "/Users/rick/Developer/Aplus/taiex.pkl"  # legacy, only used for one-time bootstrap
TAIEX_SYMBOL = "^TWII"
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
H = {"User-Agent":"Mozilla/5.0","Accept":"application/json"}

# ---------- DB ----------
def get_conn():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS prices (
        ticker TEXT, date TEXT, market TEXT, name TEXT,
        open REAL, high REAL, low REAL, close REAL, volume INTEGER,
        PRIMARY KEY (ticker, date)
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ticker ON prices(ticker)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_date ON prices(date)")
    con.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS index_daily (
        symbol TEXT, date TEXT,
        open REAL, high REAL, low REAL, close REAL, volume INTEGER,
        PRIMARY KEY (symbol, date)
    )""")
    return con


def get_index_df(con, symbol=TAIEX_SYMBOL):
    """讀指數日線，回傳 DataFrame 與原 pkl 相容（DatetimeIndex + Open/High/Low/Close/Volume）"""
    df = pd.read_sql_query(
        "SELECT date, open, high, low, close, volume FROM index_daily "
        "WHERE symbol=? ORDER BY date",
        con, params=(symbol,))
    if df.empty:
        return df
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date')
    df.index.name = 'Date'
    df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
    return df


def upsert_index(con, symbol, df):
    """df: DataFrame with DatetimeIndex + Open/High/Low/Close/Volume"""
    rows = []
    for d, row in df.iterrows():
        c = row.get('Close')
        if pd.isna(c):
            continue
        rows.append((
            symbol, d.strftime('%Y-%m-%d'),
            None if pd.isna(row.get('Open')) else float(row['Open']),
            None if pd.isna(row.get('High')) else float(row['High']),
            None if pd.isna(row.get('Low'))  else float(row['Low']),
            float(c),
            0 if pd.isna(row.get('Volume')) else int(row['Volume']),
        ))
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO index_daily(symbol,date,open,high,low,close,volume) "
            "VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def last_index_date(con, symbol):
    row = con.execute("SELECT MAX(date) FROM index_daily WHERE symbol=?", (symbol,)).fetchone()
    return row[0] if row and row[0] else None

def last_date_in_db(con, market):
    row = con.execute("SELECT MAX(date) FROM prices WHERE market=?", (market,)).fetchone()
    return row[0] if row and row[0] else None

def mark_fetched(con, market, d):
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
                (f"fetched:{market}:{d}", "1"))

def already_fetched(con, market, d):
    row = con.execute("SELECT value FROM meta WHERE key=?",
                      (f"fetched:{market}:{d}",)).fetchone()
    return bool(row)

# ---------- Fetchers ----------
def _http_json(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=H)
            return json.loads(urllib.request.urlopen(req, context=ctx, timeout=30).read())
        except Exception as e:
            if i == retries-1:
                raise
            time.sleep(2 + i*2)

def fetch_twse_day(yyyymmdd):
    """回傳 list of dict; 若該日無交易回 []"""
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={yyyymmdd}&type=ALL&response=json"
    data = _http_json(url)
    if data.get("stat","").upper() != "OK":
        return []
    out = []
    for table in data.get("tables", []):
        fields = table.get("fields", [])
        if "證券代號" in fields and "收盤價" in fields:
            f = {k: fields.index(k) for k in
                 ["證券代號","證券名稱","開盤價","最高價","最低價","收盤價","成交股數"]}
            for r in table.get("data", []):
                try:
                    code = r[f["證券代號"]].strip()
                    if not (code and code[0] in "123456789" and len(code) == 4):
                        continue
                    o = float(r[f["開盤價"]].replace(",",""))
                    h = float(r[f["最高價"]].replace(",",""))
                    l = float(r[f["最低價"]].replace(",",""))
                    c = float(r[f["收盤價"]].replace(",",""))
                    v = int(r[f["成交股數"]].replace(",",""))
                    out.append({
                        "ticker": code, "name": r[f["證券名稱"]].strip(),
                        "open": o, "high": h, "low": l, "close": c, "volume": v
                    })
                except (ValueError, IndexError):
                    continue
            break
    return out

def fetch_tpex_day(yyyymmdd):
    """yyyymmdd -> dailyQuotes; 自動轉日期格式"""
    dt = datetime.strptime(yyyymmdd, "%Y%m%d")
    iso = f"{dt.year}/{dt.month:02d}/{dt.day:02d}"
    url = f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={iso}&type=Daily&id=&response=json"
    data = _http_json(url)
    if data.get("stat","").upper() != "OK":
        return []
    out = []
    for table in data.get("tables", []):
        if "上櫃股票行情" not in table.get("title",""):
            continue
        fields = table.get("fields", [])
        try:
            f = {k: fields.index(k) for k in ["代號","名稱","開盤","最高","最低","收盤","成交股數"]}
        except ValueError:
            continue
        for r in table.get("data", []):
            try:
                code = str(r[f["代號"]]).strip()
                if not (code and code[0] in "123456789" and len(code) == 4):
                    continue
                # TPEX 用 -- 代表無交易
                vals = {}
                for k in ["開盤","最高","最低","收盤"]:
                    s = str(r[f[k]]).replace(",","").strip()
                    vals[k] = float(s) if s and s != "--" else None
                if None in vals.values():
                    continue
                v_s = str(r[f["成交股數"]]).replace(",","").strip()
                v = int(v_s) if v_s and v_s != "--" else 0
                out.append({
                    "ticker": code, "name": str(r[f["名稱"]]).strip(),
                    "open": vals["開盤"], "high": vals["最高"],
                    "low": vals["最低"], "close": vals["收盤"], "volume": v
                })
            except (ValueError, IndexError):
                continue
        break
    return out

# ---------- Update ----------
def upsert(con, market, iso_date, rows):
    con.executemany(
        "INSERT OR REPLACE INTO prices (ticker,date,market,name,open,high,low,close,volume) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(r["ticker"], iso_date, market, r["name"],
          r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]
    )

def trading_days_in_range(start_date, end_date):
    """產生 start ~ end (含) 的非週末日期"""
    d = start_date
    while d <= end_date:
        if d.weekday() < 5:  # 0~4 = 週一到週五
            yield d
        d += timedelta(days=1)

def update_market(con, market, start_date=None, end_date=None, verbose=True):
    """市場代號 TW (上市) / TWO (上櫃)"""
    fetcher = fetch_twse_day if market == "TW" else fetch_tpex_day
    today = date.today()
    if end_date is None: end_date = today
    if start_date is None:
        last = last_date_in_db(con, market)
        if last:
            start_date = datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=1)
        else:
            start_date = end_date - timedelta(days=180)  # 預設 6 個月

    if start_date > end_date:
        if verbose: print(f"  [{market}] 已是最新（最後 {last}）")
        return 0

    if verbose: print(f"  [{market}] 抓 {start_date} ~ {end_date}")
    inserted_days = 0
    for d in trading_days_in_range(start_date, end_date):
        ymd = d.strftime("%Y%m%d"); iso = d.strftime("%Y-%m-%d")
        if already_fetched(con, market, iso):
            continue
        try:
            rows = fetcher(ymd)
        except Exception as e:
            if verbose: print(f"    {iso} 失敗：{e}")
            time.sleep(3); continue
        if rows:
            upsert(con, market, iso, rows)
            inserted_days += 1
            if verbose: print(f"    {iso} +{len(rows)} 檔")
            mark_fetched(con, market, iso)
        else:
            # 「最近 5 天」可能是盤後資料還沒釋出，不標記讓下次重試
            days_ago = (today - d).days
            if days_ago > 5:
                if verbose: print(f"    {iso} 非交易日（已標記）")
                mark_fetched(con, market, iso)
            else:
                if verbose: print(f"    {iso} 暫無資料（{days_ago} 天前，留待下次重試）")
        con.commit()
        time.sleep(1.0)  # 善待 API
    return inserted_days

# ---------- Query ----------
def get_prices(con, ticker_with_suffix):
    """ticker_with_suffix: '3673.TW' / '4952.TWO'"""
    if "." not in ticker_with_suffix: return pd.DataFrame()
    code, suffix = ticker_with_suffix.split(".")
    market = "TW" if suffix == "TW" else "TWO"
    df = pd.read_sql_query(
        "SELECT date, open, high, low, close, volume, name FROM prices "
        "WHERE ticker=? AND market=? ORDER BY date",
        con, params=(code, market))
    if df.empty: return df
    df['Date'] = pd.to_datetime(df['date'])
    df = df.set_index('Date')
    df.columns = ['date','Open','High','Low','Close','Volume','Name']
    return df

def all_tickers(con):
    """回傳 DataFrame[ticker, market, name]"""
    return pd.read_sql_query(
        "SELECT ticker, market, MAX(name) as name FROM prices GROUP BY ticker, market", con)


def update_taiex():
    """增量更新 TAIEX (^TWII) 到 SQLite index_daily。
    第一次跑時若 db 為空且 taiex.pkl 存在，自動把 pkl 灌入作為起點。"""
    import yfinance as yf
    con = get_conn()
    last = last_index_date(con, TAIEX_SYMBOL)

    if last is None and os.path.exists(TAIEX_PKL):
        legacy = pd.read_pickle(TAIEX_PKL)
        n = upsert_index(con, TAIEX_SYMBOL, legacy)
        con.commit()
        print(f"  從 taiex.pkl bootstrap {n} 天")
        last = last_index_date(con, TAIEX_SYMBOL)

    if last:
        start = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start = "2024-04-01"

    today_str = date.today().strftime("%Y-%m-%d")
    if start > today_str:
        print(f"  已最新 ({last})")
        con.close()
        return 0

    new = yf.download("^TWII", start=start, multi_level_index=False,
                      progress=False, auto_adjust=False)
    if new.empty:
        print(f"  無新資料 (last: {last})")
        con.close()
        return 0

    added = upsert_index(con, TAIEX_SYMBOL, new)
    con.commit()
    new_last = last_index_date(con, TAIEX_SYMBOL)
    print(f"  +{added} 天 (最新: {new_last})")
    con.close()
    return added


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "update"
    con = get_conn()
    if cmd == "update":
        print("更新上市...")
        n1 = update_market(con, "TW")
        print(f"  +{n1} 天")
        print("更新上櫃...")
        n2 = update_market(con, "TWO")
        print(f"  +{n2} 天")
        print("更新 TAIEX...")
        update_taiex()
        # 美股 + Nasdaq 指數
        try:
            import usstock
            print("更新美股...")
            usstock.update_universe()
            print("更新 ^IXIC...")
            usstock.update_nasdaq_idx()
        except Exception as e:
            print(f"⚠️  美股更新失敗: {e}")
    elif cmd == "stats":
        last_tw = last_date_in_db(con, "TW")
        last_two = last_date_in_db(con, "TWO")
        cnt = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        tickers_tw = con.execute("SELECT COUNT(DISTINCT ticker) FROM prices WHERE market='TW'").fetchone()[0]
        tickers_two = con.execute("SELECT COUNT(DISTINCT ticker) FROM prices WHERE market='TWO'").fetchone()[0]
        print(f"上市最後日期: {last_tw}  ({tickers_tw} 檔)")
        print(f"上櫃最後日期: {last_two}  ({tickers_two} 檔)")
        print(f"總 row 數: {cnt}")
    elif cmd == "show":
        ticker = sys.argv[2]
        df = get_prices(con, ticker)
        print(df.tail(10))
    con.close()
