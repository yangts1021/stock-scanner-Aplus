"""
美股每日資料：yfinance → SQLite (usstock.db)。
- init/update：增量抓取
- query：給定 ticker 回傳 DataFrame
Universe：Nasdaq-listed (api.nasdaq.com/api/screener)
"""
import sqlite3, urllib.request, ssl, json, time, os, sys
from datetime import datetime, timedelta, date
import pandas as pd
import yfinance as yf

DB_PATH        = "/Users/rick/Developer/Aplus/usstock.db"
NDX_PKL        = "/Users/rick/Developer/Aplus/nasdaq_idx.pkl"  # legacy, only used for one-time bootstrap
NDX_SYMBOL     = "^IXIC"
UNIVERSE_CACHE = "/Users/rick/Developer/Aplus/cache/us_universe.json"

ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# ---------- DB ----------
def get_conn():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS prices (
        ticker TEXT, date TEXT, name TEXT,
        open REAL, high REAL, low REAL, close REAL, volume INTEGER,
        PRIMARY KEY (ticker, date)
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ticker ON prices(ticker)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_date ON prices(date)")
    con.execute("""CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS index_daily (
        symbol TEXT, date TEXT,
        open REAL, high REAL, low REAL, close REAL, volume INTEGER,
        PRIMARY KEY (symbol, date)
    )""")
    return con

def last_date_in_db(con):
    row = con.execute("SELECT MAX(date) FROM prices").fetchone()
    return row[0] if row and row[0] else None


def get_index_df(con, symbol=NDX_SYMBOL):
    """讀指數日線，回傳與原 pkl 相容的 DataFrame"""
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

# ---------- Universe ----------
def _parse_mc(s):
    try:
        return float((s or "").replace(",", "").replace("$", "").strip() or 0)
    except ValueError:
        return 0.0

def fetch_nasdaq_universe(use_cache=True, max_age_days=7):
    """回傳 list[{symbol, name, market_cap}]，依市值降序排列"""
    if use_cache and os.path.exists(UNIVERSE_CACHE):
        age = (time.time() - os.path.getmtime(UNIVERSE_CACHE)) / 86400
        if age < max_age_days:
            with open(UNIVERSE_CACHE) as f:
                return json.load(f)
    print("  抓取 Nasdaq universe...")
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&exchange=NASDAQ"
    req = urllib.request.Request(url, headers=H)
    data = json.loads(urllib.request.urlopen(req, context=ctx, timeout=60).read())
    rows = data.get("data", {}).get("table", {}).get("rows", [])
    universe = []
    for r in rows:
        sym = (r.get("symbol") or "").strip()
        if not sym or "/" in sym or "^" in sym:  # 跳過 preferred / warrants
            continue
        mc = _parse_mc(r.get("marketCap"))
        if mc <= 0:
            continue  # 無市值資料的標的（多半是無交易/剛上市）跳過
        universe.append({
            "symbol": sym,
            "name": r.get("name", "").strip(),
            "market_cap": mc,
        })
    universe.sort(key=lambda x: x["market_cap"], reverse=True)
    os.makedirs(os.path.dirname(UNIVERSE_CACHE), exist_ok=True)
    with open(UNIVERSE_CACHE, "w") as f:
        json.dump(universe, f)
    print(f"  +{len(universe)} 檔（依市值降序） → {UNIVERSE_CACHE}")
    return universe

# ---------- Fetchers ----------
def fetch_batch(tickers, start=None, end=None, period=None, retries=2):
    """yfinance 批次下載。回傳 dict {ticker: DataFrame[Open,High,Low,Close,Adj Close,Volume]}"""
    kwargs = dict(progress=False, auto_adjust=False, group_by="ticker", threads=True)
    if period:
        kwargs["period"] = period
    else:
        kwargs["start"] = start
        if end:
            kwargs["end"] = end
    last_err = None
    for i in range(retries + 1):
        try:
            df = yf.download(tickers, **kwargs)
            last_err = None
            break
        except Exception as e:
            last_err = e
            if i < retries:
                time.sleep(3 + i * 3)
    if last_err is not None:
        raise last_err

    result = {}
    if df is None or df.empty:
        return result
    if isinstance(df.columns, pd.MultiIndex):
        top = set(df.columns.get_level_values(0))
        for tk in tickers:
            if tk in top:
                sub = df[tk].dropna(how="all")
                if not sub.empty:
                    result[tk] = sub
    else:
        if len(tickers) == 1 and not df.empty:
            result[tickers[0]] = df.dropna(how="all")
    return result

def upsert(con, ticker, name, df):
    rows = []
    for d, row in df.iterrows():
        c = row.get("Close")
        if pd.isna(c):
            continue
        rows.append((
            ticker, d.strftime("%Y-%m-%d"), name,
            None if pd.isna(row.get("Open"))  else float(row["Open"]),
            None if pd.isna(row.get("High"))  else float(row["High"]),
            None if pd.isna(row.get("Low"))   else float(row["Low"]),
            float(c),
            0 if pd.isna(row.get("Volume")) else int(row["Volume"]),
        ))
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO prices(ticker,date,name,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?,?)",
            rows
        )
    return len(rows)

# ---------- Init / Update ----------
def _run_batches(tickers, names, fetch_kwargs, batch_size=100, sleep_between=0.5):
    con = get_conn()
    total = 0
    n_batches = (len(tickers) + batch_size - 1) // batch_size
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        b_idx = i // batch_size + 1
        try:
            data = fetch_batch(batch, **fetch_kwargs)
            added = 0
            for tk, df in data.items():
                added += upsert(con, tk, names.get(tk, ""), df)
            con.commit()
            total += added
            print(f"  [{b_idx}/{n_batches}] +{added} 筆 ({len(data)}/{len(batch)} 有資料)")
        except Exception as e:
            print(f"  [{b_idx}/{n_batches}] ❌ {e}")
        if b_idx < n_batches:
            time.sleep(sleep_between)
    con.close()
    print(f"  總計 +{total} 筆")
    return total

def init_universe(period="6mo", batch_size=100, limit=None):
    universe = fetch_nasdaq_universe()
    if limit:
        universe = universe[:limit]
    tickers = [u["symbol"] for u in universe]
    names = {u["symbol"]: u["name"] for u in universe}
    print(f"初始化 {len(tickers)} 檔 (period={period})...")
    return _run_batches(tickers, names, {"period": period}, batch_size=batch_size)

def update_universe(batch_size=100):
    con = get_conn()
    last = last_date_in_db(con)
    if last is None:
        con.close()
        print("DB 為空，請先執行 init")
        return 0
    # 只更新 DB 內已存在的 ticker（不會偷偷擴張 universe）
    existing = pd.read_sql_query(
        "SELECT ticker, MAX(name) as name FROM prices GROUP BY ticker", con)
    con.close()
    tickers = existing["ticker"].tolist()
    names = dict(zip(existing["ticker"], existing["name"]))
    # 抓最後一日當天起，覆蓋當日修正 + 抓新日
    start = last
    print(f"增量更新 {len(tickers)} 檔 (start={start})...")
    return _run_batches(tickers, names, {"start": start}, batch_size=batch_size)

def update_nasdaq_idx():
    """增量更新 ^IXIC (Nasdaq Composite) 到 SQLite index_daily。
    第一次跑時若 db 為空且 nasdaq_idx.pkl 存在，自動把 pkl 灌入作為起點。"""
    con = get_conn()
    last = last_index_date(con, NDX_SYMBOL)

    if last is None and os.path.exists(NDX_PKL):
        legacy = pd.read_pickle(NDX_PKL)
        n = upsert_index(con, NDX_SYMBOL, legacy)
        con.commit()
        print(f"  從 nasdaq_idx.pkl bootstrap {n} 天")
        last = last_index_date(con, NDX_SYMBOL)

    if last:
        start = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start = "2024-04-01"

    today_str = date.today().strftime("%Y-%m-%d")
    if start > today_str:
        print(f"  ^IXIC 已最新 ({last})")
        con.close()
        return 0
    new = yf.download("^IXIC", start=start, multi_level_index=False,
                      progress=False, auto_adjust=False)
    if new.empty:
        print(f"  ^IXIC 無新資料")
        con.close()
        return 0
    added = upsert_index(con, NDX_SYMBOL, new)
    con.commit()
    new_last = last_index_date(con, NDX_SYMBOL)
    print(f"  ^IXIC +{added} 天 (最新: {new_last})")
    con.close()
    return added

# ---------- Query ----------
def get_prices(con, ticker):
    df = pd.read_sql_query(
        "SELECT date,open,high,low,close,volume FROM prices WHERE ticker=? ORDER BY date",
        con, params=(ticker,)
    )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    df.index.name = "Date"
    df.columns = ["Open", "High", "Low", "Close", "Volume"]
    return df

def all_tickers(con):
    return pd.read_sql_query(
        "SELECT ticker, MAX(name) as name FROM prices GROUP BY ticker", con)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "update"
    if cmd == "init":
        period = sys.argv[2] if len(sys.argv) > 2 else "6mo"
        limit  = int(sys.argv[3]) if len(sys.argv) > 3 else 500
        init_universe(period=period, limit=limit)
        print("更新 ^IXIC...")
        update_nasdaq_idx()
    elif cmd == "update":
        update_universe()
        print("更新 ^IXIC...")
        update_nasdaq_idx()
    elif cmd == "stats":
        con = get_conn()
        last = last_date_in_db(con)
        cnt = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        tk_cnt = con.execute("SELECT COUNT(DISTINCT ticker) FROM prices").fetchone()[0]
        print(f"最後日期: {last}")
        print(f"ticker 數: {tk_cnt}")
        print(f"總 row 數: {cnt}")
        con.close()
    elif cmd == "show":
        con = get_conn()
        df = get_prices(con, sys.argv[2])
        print(df.tail(10))
        con.close()
    elif cmd == "universe":
        u = fetch_nasdaq_universe(use_cache=False)
        print(f"{len(u)} tickers")
        print("first 10:", [x["symbol"] for x in u[:10]])
    else:
        print(f"unknown cmd: {cmd}")
        print("usage: usstock.py [init [period] [limit] | update | stats | show <ticker> | universe]")
