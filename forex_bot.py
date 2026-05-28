#!/usr/bin/env python3
"""
FOREX SIGNAL BOT v2 - Mac Edition (WebTerminal + File Bridge)
==============================================================
Works on Apple Silicon Mac without the MT5 Python package.

Two modes:
  MODE 1: SIGNAL SCANNER — detects setups using IC Markets price data
  MODE 2: FILE BRIDGE    — writes signals to a file that MT5 reads via EA

Pairs: EURUSD, GBPUSD, XAUUSD, USDJPY, EURJPY
Sessions: London (07-16), New York (13-22), Asian (00-09)

Commands:
  python3 forex_bot.py scan     -- scan and print signals
  python3 forex_bot.py trade    -- scan + write to MT5 bridge file
  python3 forex_bot.py status   -- show open positions + P&L
  python3 forex_bot.py analyse  -- performance by strategy/session
"""

import os, sys, time, json, sqlite3, datetime, traceback, math, requests, random

DB_PATH       = os.path.expanduser("~/Desktop/forex_bot.db")
LOG_FILE      = os.path.expanduser("~/Desktop/forex_bot.log")
BRIDGE_FILE   = os.path.expanduser("~/Desktop/mt5_signals.json")  # MT5 EA reads this
DRY_RUN       = os.environ.get("DRY_RUN", "true").lower() != "false"

# Risk settings — $300 starting balance
ACCOUNT_BALANCE = 300.0
RISK_PER_TRADE  = 0.02       # 2% per trade = $6 risk on $300 (standard for small accounts)
MAX_OPEN_TRADES = 4          # max 4 positions on $300 (never >50% deployed)
MAX_PER_PAIR    = 1          # 1 position per pair max on small account
MIN_RR          = 1.5        # minimum 1.5:1 reward:risk

# Session schedule (UTC hours)
# FIX: Tightened session windows — cut last 60 mins of each session
# (liquidity drops, spreads widen, reversals more common near session close)
SESSIONS = {
    "london":    {"start": 7,  "end": 15, "pairs": ["EURUSD","GBPUSD","XAUUSD","EURJPY"]},
    "new_york":  {"start": 14, "end": 21, "pairs": ["EURUSD","GBPUSD","XAUUSD","USDJPY"]},
    "overlap":   {"start": 13, "end": 16, "pairs": ["EURUSD","GBPUSD","XAUUSD","USDJPY","EURJPY"]},
    "asian":     {"start": 1,  "end": 8,  "pairs": ["USDJPY","XAUUSD"]},
    "dead_zone": {"start": 21, "end": 1,  "pairs": []},
}

PAIR_CFG = {
    "EURUSD": {"pip": 0.0001, "spread_max": 1.5,  "digits": 5},
    "GBPUSD": {"pip": 0.0001, "spread_max": 2.0,  "digits": 5},
    "XAUUSD": {"pip": 0.01,   "spread_max": 35.0, "digits": 2},
    "USDJPY": {"pip": 0.01,   "spread_max": 1.5,  "digits": 3},
    "EURJPY": {"pip": 0.01,   "spread_max": 2.5,  "digits": 3},
}

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
def log(msg, level="INFO"):
    ts   = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ─────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────
def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opened_at TEXT, symbol TEXT, strategy TEXT, session TEXT,
        direction TEXT, entry_price REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
        lot_size REAL, risk_usd REAL, ticket INTEGER,
        status TEXT DEFAULT 'open', closed_at TEXT, close_price REAL,
        outcome TEXT, pnl_usd REAL, pnl_pips REAL, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        detected_at TEXT, symbol TEXT, strategy TEXT, session TEXT,
        direction TEXT, entry_price REAL, sl REAL, tp1 REAL,
        confidence REAL, acted INTEGER DEFAULT 0, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS performance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evaluated_at TEXT, strategy TEXT, session TEXT,
        trades_total INTEGER, trades_won INTEGER, win_rate REAL,
        total_pnl REAL, recommendation TEXT
    );
    """)
    conn.commit()

# ─────────────────────────────────────────
# SESSION
# ─────────────────────────────────────────
def get_session():
    h = datetime.datetime.now(datetime.timezone.utc).hour
    if 13 <= h < 16: return "overlap"
    if 7  <= h < 16: return "london"
    if 16 <= h < 22: return "new_york"
    if 0  <= h < 9:  return "asian"
    return "dead_zone"

def get_pairs():
    return SESSIONS[get_session()]["pairs"]

# ─────────────────────────────────────────
# PRICE DATA — Yahoo Finance (free, no auth)
# ─────────────────────────────────────────
YAHOO_SYMBOLS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X", "EURJPY": "EURJPY=X",
    "XAUUSD": "GC=F",  # Gold futures (Yahoo has no spot XAUUSD — GC=F tracks closely, ~$20-30 premium; avoid entries near futures roll dates)
}

INTERVAL_MAP = {
    "M5":  ("5m",  "1d"),
    "M15": ("15m", "5d"),
    "H1":  ("1h",  "1mo"),
    "H4":  ("4h",  "3mo"),
}

def get_candles(symbol, timeframe="M15", count=100):
    """Fetch OHLCV candles from Yahoo Finance."""
    ysym     = YAHOO_SYMBOLS.get(symbol)
    if not ysym:
        return None
    interval, period = INTERVAL_MAP.get(timeframe, ("15m","5d"))
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}"
           f"?interval={interval}&range={period}&includePrePost=false")
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        time.sleep(random.uniform(0.3, 0.8))  # polite delay
        r = requests.get(url, headers=headers, timeout=15)
        d = r.json()
        result = d["chart"]["result"][0]
        ts     = result["timestamp"]
        ohlcv  = result["indicators"]["quote"][0]
        candles = []
        for i in range(len(ts)):
            o = ohlcv["open"][i]
            h = ohlcv["high"][i]
            l = ohlcv["low"][i]
            c = ohlcv["close"][i]
            v = ohlcv.get("volume", [0]*len(ts))[i]
            if None in (o, h, l, c):
                continue
            candles.append({"time": ts[i], "open": o, "high": h, "low": l, "close": c, "volume": v or 0})
        return candles[-count:] if len(candles) >= 10 else None
    except Exception as e:
        log(f"Price fetch failed for {symbol} {timeframe}: {e}", "WARN")
        return None

def get_current_price(symbol):
    """Get latest price via Yahoo."""
    candles = get_candles(symbol, "M5", 5)
    if candles:
        return candles[-1]["close"]
    return None

# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def ema(prices, period):
    if len(prices) < period: return None
    k = 2.0 / (period + 1)
    e = prices[0]
    for p in prices[1:]: e = p * k + e * (1 - k)
    return e

def rsi(closes, period=14):
    if len(closes) < period + 1: return None
    g, l = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        g.append(max(d, 0)); l.append(max(-d, 0))
    ag = sum(g[-period:]) / period
    al = sum(l[-period:]) / period
    return 100 if al == 0 else 100 - (100 / (1 + ag / al))

def atr(highs, lows, closes, period=14):
    if len(highs) < period + 1: return None
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(highs))]
    return sum(trs[-period:]) / period

def bollinger(closes, period=20, std=2.0):
    if len(closes) < period: return None, None, None
    r = closes[-period:]; m = sum(r) / period
    s = math.sqrt(sum((x-m)**2 for x in r) / period)
    return m + std*s, m, m - std*s

# ─────────────────────────────────────────
# COMPOUNDING ENGINE
# ─────────────────────────────────────────
# Milestones: each tier unlocks higher lot caps and tracks daily P&L target
COMPOUND_TIERS = [
    {"min_balance":    0, "max_balance":   499, "risk_pct": 0.02, "lot_cap": 0.05, "daily_target":   9, "label": "Tier 1 — Micro"},
    {"min_balance":  500, "max_balance":   999, "risk_pct": 0.02, "lot_cap": 0.10, "daily_target":  20, "label": "Tier 2 — Building"},
    {"min_balance": 1000, "max_balance":  1999, "risk_pct": 0.02, "lot_cap": 0.20, "daily_target":  40, "label": "Tier 3 — Growing"},
    {"min_balance": 2000, "max_balance":  4999, "risk_pct": 0.02, "lot_cap": 0.50, "daily_target":  80, "label": "Tier 4 — Scaling"},
    {"min_balance": 5000, "max_balance":  9999, "risk_pct": 0.015,"lot_cap": 1.00, "daily_target": 150, "label": "Tier 5 — Pro"},
    {"min_balance":10000, "max_balance": 99999, "risk_pct": 0.01, "lot_cap": 5.00, "daily_target": 300, "label": "Tier 6 — Full Size"},
]

def get_tier(balance):
    for t in COMPOUND_TIERS:
        if t["min_balance"] <= balance < t["max_balance"]:
            return t
    return COMPOUND_TIERS[-1]

def get_current_balance(conn):
    """Calculate real-time balance from starting capital + all closed P&L."""
    closed_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd),0) FROM trades WHERE status='closed' AND pnl_usd IS NOT NULL"
    ).fetchone()[0]
    return ACCOUNT_BALANCE + closed_pnl

def get_daily_pnl(conn):
    """P&L for today only."""
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    return conn.execute(
        "SELECT COALESCE(SUM(pnl_usd),0) FROM trades WHERE status='closed' AND closed_at LIKE ?",
        (f"{today}%",)
    ).fetchone()[0]

def compound_status(conn):
    """Print full compounding dashboard."""
    balance      = get_current_balance(conn)
    tier         = get_tier(balance)
    daily_pnl    = get_daily_pnl(conn)
    total_pnl    = balance - ACCOUNT_BALANCE
    daily_target = tier["daily_target"]
    progress     = (daily_pnl / daily_target * 100) if daily_target > 0 else 0
    next_tier    = next((t for t in COMPOUND_TIERS if t["min_balance"] > balance), None)

    print("\n" + "="*60)
    print("💰 COMPOUNDING DASHBOARD")
    print("="*60)
    print(f"  Starting capital:  ${ACCOUNT_BALANCE:.2f}")
    print(f"  Current balance:   ${balance:.2f}  ({total_pnl:+.2f} total P&L)")
    print(f"  Current tier:      {tier['label']}")
    print(f"  Risk per trade:    {tier['risk_pct']*100:.1f}% = ${balance*tier['risk_pct']:.2f}")
    print(f"  Max lot size:      {tier['lot_cap']} lots")
    print(f"  Daily target:      ${daily_target:.0f}/day")
    print(f"  Today's P&L:       ${daily_pnl:+.2f}  ({progress:.0f}% of target)")

    # Progress bar
    filled = int(progress / 5)
    bar    = "█" * min(filled, 20) + "░" * max(0, 20 - filled)
    print(f"  Progress:          [{bar}] {progress:.0f}%")

    if next_tier:
        needed = next_tier["min_balance"] - balance
        print(f"\n  Next tier ({next_tier['label']}):")
        print(f"    Need ${needed:.2f} more → unlocks ${next_tier['daily_target']}/day target")
        print(f"    Max lots → {next_tier['lot_cap']}")

    # Compounding projection
    print(f"\n  📈 Compounding projection (at current WR):")
    closed = conn.execute("SELECT * FROM trades WHERE status='closed'").fetchall()
    if len(closed) >= 5:
        wins   = [t for t in closed if t["outcome"]=="WIN"]
        wr     = len(wins)/len(closed)
        avg_win  = sum(t["pnl_usd"] for t in wins)/max(len(wins),1)
        avg_loss = abs(sum(t["pnl_usd"] for t in closed if t["outcome"]=="LOSS")/max(len(closed)-len(wins),1))
        daily_ev = 3 * (wr * avg_win - (1-wr) * avg_loss)  # assume 3 trades/day
        proj_bal = balance
        for label, days in [("1 week",7),("1 month",30),("3 months",90)]:
            proj_bal_end = balance * ((1 + daily_ev/balance) ** days) if balance > 0 else balance
            print(f"    {label:10}: ${proj_bal_end:.0f}  (+{(proj_bal_end-balance)/balance*100:.0f}%)")
    else:
        print(f"    (need 5+ closed trades for projection)")

    print("="*60)

def lot_size(symbol, entry, sl, balance):
    """
    FIX: Hard cap at tier lot_cap AND enforce minimum pip distance.
    On $300 (Tier 1) max is 0.05 lots regardless of calculation.
    Also require SL to be at least 5 pips — prevents tiny SLs creating oversized lots.
    """
    tier    = get_tier(balance)
    pip     = PAIR_CFG.get(symbol, {}).get("pip", 0.0001)
    sl_pips = abs(entry - sl) / pip

    # FIX: Minimum SL distance — reject signals with SL < 5 pips (too tight, noise)
    if sl_pips < 5:
        sl_pips = 10  # use a safe default of 10 pips if signal SL is too tight

    risk    = balance * tier["risk_pct"]  # e.g. 2% of $300 = $6
    pv      = 10.0   # $10 per pip per standard lot
    lots    = risk / (sl_pips * pv)

    # Hard cap at tier maximum — this is the critical fix
    lots = min(lots, tier["lot_cap"])
    lots = max(0.01, round(lots, 2))
    return lots

def validate_signal(sig):
    """
    FIX: Reject signals with SL too tight (< 5 pips) or RR too low.
    Tight SLs are noise — they get hit by normal spread fluctuation.
    """
    if sig is None: return None
    pip     = PAIR_CFG.get(sig["symbol"], {}).get("pip", 0.0001)
    sl_pips = abs(sig["entry"] - sig["sl"]) / pip
    tp1_pips= abs(sig["tp1"] - sig["entry"]) / pip
    # Minimum 5 pip SL, minimum 8 pip TP1
    if sl_pips < 5:   return None
    if tp1_pips < 8:  return None
    if sig["rr"] < MIN_RR: return None
    return sig

# ─────────────────────────────────────────
# STRATEGIES
# ─────────────────────────────────────────
def s1_breakout(symbol, session, c15, c1h):
    if not c15 or len(c15) < 50: return None
    H = [c["high"] for c in c15]; L = [c["low"] for c in c15]; C = [c["close"] for c in c15]
    a = atr(H, L, C, 14)
    if not a: return None
    rh = max(H[-4:]); rl = min(L[-4:]); rs = rh - rl
    if rs < a * 0.3: return None
    cp = C[-1]
    pip = PAIR_CFG.get(symbol, {}).get("pip", 0.0001)
    if   cp > rh + a * 0.1: d="BUY";  sl=rl-a*0.3; tp1=cp+rs;    tp2=cp+rs*2;  tp3=cp+rs*3
    elif cp < rl - a * 0.1: d="SELL"; sl=rh+a*0.3; tp1=cp-rs;    tp2=cp-rs*2;  tp3=cp-rs*3
    else: return None
    risk=abs(cp-sl); rw=abs(tp1-cp)
    if risk <= 0 or rw/risk < MIN_RR: return None
    return {"strategy":"S1_breakout","symbol":symbol,"session":session,"direction":d,
            "entry":cp,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"atr":a,"rr":rw/risk,
            "confidence":0.73,"notes":f"Range {rs/pip:.0f}p | ATR {a/pip:.0f}p"}

def s2_mean_rev(symbol, session, c15, c1h):
    if not c15 or len(c15) < 50: return None
    H=[c["high"] for c in c15]; L=[c["low"] for c in c15]; C=[c["close"] for c in c15]
    r=rsi(C,14); bbu,bbm,bbl=bollinger(C,20,2.0); a=atr(H,L,C,14)
    if not all([r, bbu, a]): return None
    cp=C[-1]; conf=0.0; d=None
    if   r < 25 and cp <= bbl * 1.001: d="BUY";  conf=0.70+(0.05 if r<20 else 0)
    elif r > 75 and cp >= bbu * 0.999: d="SELL"; conf=0.70+(0.05 if r>80 else 0)
    if not d: return None
    if d=="BUY":  sl=bbl-a*0.5; tp1=bbm; tp2=bbu; tp3=bbu+a*0.5
    else:         sl=bbu+a*0.5; tp1=bbm; tp2=bbl; tp3=bbl-a*0.5
    risk=abs(cp-sl); rw=abs(tp1-cp)
    if risk <= 0 or rw/risk < MIN_RR: return None
    return {"strategy":"S2_mean_rev","symbol":symbol,"session":session,"direction":d,
            "entry":cp,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"atr":a,"rr":rw/risk,
            "confidence":conf,"notes":f"RSI {r:.0f} | BB {(cp-bbl)/(bbu-bbl)*100:.0f}%"}

def s3_gold(symbol, session, c15, c1h):
    if symbol != "XAUUSD" or not c1h or len(c1h) < 50: return None
    H=[c["high"] for c in c1h]; L=[c["low"] for c in c1h]; C=[c["close"] for c in c1h]
    e20=ema(C,20); e50=ema(C,50); a=atr(H,L,C,14)
    if not all([e20,e50,a]): return None
    cp=C[-1]; pp=C[-2]
    if   e20>e50 and cp>e20 and pp<=e20: d="BUY"
    elif e20<e50 and cp<e20 and pp>=e20: d="SELL"
    else: return None
    if d=="BUY":  sl=cp-a*1.5; tp1=cp+a; tp2=cp+a*2; tp3=cp+a*3.5
    else:         sl=cp+a*1.5; tp1=cp-a; tp2=cp-a*2; tp3=cp-a*3.5
    risk=abs(cp-sl); rw=abs(tp1-cp)
    if risk<=0 or rw/risk<MIN_RR: return None
    return {"strategy":"S3_gold","symbol":symbol,"session":session,"direction":d,
            "entry":cp,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"atr":a,"rr":rw/risk,
            "confidence":0.70,"notes":f"EMA20:{e20:.1f} EMA50:{e50:.1f} ATR:{a:.1f}"}

def s4_asian(symbol, session, c15, c1h):
    if session != "asian" or symbol not in ("USDJPY","XAUUSD"): return None
    if not c1h or len(c1h) < 20: return None
    H=[c["high"] for c in c1h]; L=[c["low"] for c in c1h]; C=[c["close"] for c in c1h]
    a=atr(H,L,C,14); cp=C[-1]
    if not a: return None
    rh=max(H[-6:]); rl=min(L[-6:]); rm=(rh+rl)/2; rs=rh-rl
    if rs < a*0.2: return None
    if   cp >= rh*0.9998: d="SELL"; sl=rh+a*0.3; tp1=rm; tp2=rl; tp3=rl-rs*0.5
    elif cp <= rl*1.0002: d="BUY";  sl=rl-a*0.3; tp1=rm; tp2=rh; tp3=rh+rs*0.5
    else: return None
    risk=abs(cp-sl); rw=abs(tp1-cp)
    if risk<=0 or rw/risk<MIN_RR: return None
    pip=PAIR_CFG.get(symbol,{}).get("pip",0.0001)
    return {"strategy":"S4_asian","symbol":symbol,"session":session,"direction":d,
            "entry":cp,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"atr":a,"rr":rw/risk,
            "confidence":0.67,"notes":f"Range {rs/pip:.0f}p | Mid {rm:.3f}"}

def s5_momentum(symbol, session, c15, c1h):
    if session not in ("london","new_york","overlap"): return None
    if not c1h or len(c1h)<30 or not c15 or len(c15)<30: return None
    H1H=[c["high"] for c in c1h]; H1L=[c["low"] for c in c1h]
    H1C=[c["close"] for c in c1h]; H1O=[c["open"] for c in c1h]
    M15H=[c["high"] for c in c15]; M15L=[c["low"] for c in c15]
    M15C=[c["close"] for c in c15]
    last=c1h[-2]; body=abs(last["close"]-last["open"]); rng=last["high"]-last["low"]
    if rng==0 or body/rng<0.65: return None
    bullish=last["close"]>last["open"]
    a=atr(H1H,H1L,H1C,14); e21=ema(M15C,21)
    if not a or not e21: return None
    cp=M15C[-1]
    if abs(cp-e21) > a*0.3: return None
    d="BUY" if bullish else "SELL"
    if d=="BUY":  sl=min(M15L[-3:])-a*0.2; tp1=cp+a;  tp2=cp+a*2; tp3=cp+a*3
    else:         sl=max(M15H[-3:])+a*0.2; tp1=cp-a;  tp2=cp-a*2; tp3=cp-a*3
    risk=abs(cp-sl); rw=abs(tp1-cp)
    if risk<=0 or rw/risk<MIN_RR: return None
    pip=PAIR_CFG.get(symbol,{}).get("pip",0.0001)
    return {"strategy":"S5_momentum","symbol":symbol,"session":session,"direction":d,
            "entry":cp,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"atr":a,"rr":rw/risk,
            "confidence":0.73,"notes":f"H1 body {body/rng*100:.0f}% | EMA dist {abs(cp-e21)/pip:.1f}p"}

# ─────────────────────────────────────────
# SCANNING
# ─────────────────────────────────────────
def scan(conn, balance):
    session = get_session()
    pairs   = get_pairs()
    if not pairs:
        log("Dead zone (22:00-00:00 UTC) — paused"); return []
    log(f"Session: {session.upper()} | Pairs: {', '.join(pairs)}")
    signals = []
    for symbol in pairs:
        oc = conn.execute("SELECT COUNT(*) FROM trades WHERE symbol=? AND status IN ('open','dry_run')",(symbol,)).fetchone()[0]
        if oc >= MAX_PER_PAIR: continue
        tc = conn.execute("SELECT COUNT(*) FROM trades WHERE status IN ('open','dry_run')").fetchone()[0]
        if tc >= MAX_OPEN_TRADES: break
        log(f"  Fetching {symbol}...")
        c15 = get_candles(symbol, "M15", 100)
        c1h = get_candles(symbol, "H1",  100)
        for sig in [validate_signal(s1_breakout(symbol,session,c15,c1h)),
                    validate_signal(s2_mean_rev(symbol,session,c15,c1h)),
                    validate_signal(s3_gold(symbol,session,c15,c1h)),
                    validate_signal(s4_asian(symbol,session,c15,c1h)),
                    validate_signal(s5_momentum(symbol,session,c15,c1h))]:
            if sig is None: continue
            # Skip if same symbol+direction already signalled in the last 4 hours
            cutoff = (datetime.datetime.now(datetime.timezone.utc) -
                      datetime.timedelta(hours=4)).isoformat()
            recent = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE symbol=? AND direction=? AND detected_at>?",
                (sig["symbol"], sig["direction"], cutoff)
            ).fetchone()[0]
            if recent > 0:
                log(f"  Skipping duplicate {sig['symbol']} {sig['direction']} ({sig['strategy']}) — already signalled in last 4h")
                continue
            conn.execute("""INSERT INTO signals
                (detected_at,symbol,strategy,session,direction,entry_price,sl,tp1,confidence,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 sig["symbol"],sig["strategy"],sig["session"],sig["direction"],
                 sig["entry"],sig["sl"],sig["tp1"],sig["confidence"],sig["notes"]))
            conn.commit()
            signals.append(sig)
    signals.sort(key=lambda s: -s["confidence"])
    return signals

# ─────────────────────────────────────────
# TRADE LOGGING + BRIDGE FILE
# ─────────────────────────────────────────
def place_trade(conn, sig, balance):
    lots     = lot_size(sig["symbol"], sig["entry"], sig["sl"], balance)
    risk_usd = balance * RISK_PER_TRADE
    pip      = PAIR_CFG.get(sig["symbol"],{}).get("pip", 0.0001)
    sl_pips  = abs(sig["entry"]-sig["sl"]) / pip
    tp1_pips = abs(sig["tp1"]-sig["entry"]) / pip

    tag = "[DRY RUN]" if DRY_RUN else "[LIVE]"
    log(f"{tag} {sig['strategy']} | {sig['symbol']} {sig['direction']}")
    log(f"  Entry:{sig['entry']:.5f} | SL:{sig['sl']:.5f} ({sl_pips:.0f}p) | "
        f"TP1:{sig['tp1']:.5f} ({tp1_pips:.0f}p) | TP2:{sig['tp2']:.5f} | TP3:{sig['tp3']:.5f}")
    log(f"  Lots:{lots} | Risk:${risk_usd:.2f} | RR:{sig['rr']:.1f}:1 | Conf:{sig['confidence']*100:.0f}%")

    # Write to bridge file for MT5 EA to pick up
    bridge_signals = []
    try:
        if os.path.exists(BRIDGE_FILE):
            with open(BRIDGE_FILE) as f:
                bridge_signals = json.load(f)
    except Exception:
        pass

    bridge_signals.append({
        "id":        f"{sig['strategy']}_{sig['symbol']}_{int(time.time())}",
        "symbol":    sig["symbol"],
        "strategy":  sig["strategy"],
        "direction": sig["direction"],
        "entry":     sig["entry"],
        "sl":        sig["sl"],
        "tp1":       sig["tp1"],
        "tp2":       sig["tp2"],
        "tp3":       sig["tp3"],
        "lots":      lots,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status":    "pending"
    })
    with open(BRIDGE_FILE, "w") as f:
        json.dump(bridge_signals, f, indent=2)
    log(f"  Signal written to bridge file: {BRIDGE_FILE}")

    # Log to DB
    conn.execute("""INSERT INTO trades
        (opened_at,symbol,strategy,session,direction,entry_price,sl,tp1,tp2,tp3,
         lot_size,risk_usd,status,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(),
         sig["symbol"],sig["strategy"],sig["session"],sig["direction"],
         sig["entry"],sig["sl"],sig["tp1"],sig["tp2"],sig["tp3"],
         lots,risk_usd,"dry_run" if DRY_RUN else "open",sig["notes"]))
    conn.commit()
    return True

# ─────────────────────────────────────────
# STATUS + ANALYSIS
# ─────────────────────────────────────────
def show_status(conn):
    print("\n" + "="*65)
    print("FOREX BOT STATUS")
    print("="*65)
    now = datetime.datetime.now(datetime.timezone.utc)
    print(f"Time: {now.strftime('%H:%M UTC')} | Session: {get_session().upper()}")
    print(f"Active pairs: {', '.join(get_pairs()) or 'None (dead zone)'}")

    open_t = conn.execute(
        "SELECT * FROM trades WHERE status IN ('open','dry_run') ORDER BY opened_at DESC"
    ).fetchall()
    print(f"\nOpen positions: {len(open_t)}")
    for t in open_t:
        pip = PAIR_CFG.get(t["symbol"],{}).get("pip",0.0001)
        sl_p  = abs(t["entry_price"]-t["sl"])/pip
        tp1_p = abs(t["tp1"]-t["entry_price"])/pip
        print(f"  {t['direction']:4} {t['symbol']:8} {t['strategy']:15} "
              f"@ {t['entry_price']:.5f} | SL:{sl_p:.0f}p TP1:{tp1_p:.0f}p "
              f"lots:{t['lot_size']} [{t['status']}]")

    closed = conn.execute(
        "SELECT * FROM trades WHERE status='closed' ORDER BY closed_at DESC"
    ).fetchall()
    if closed:
        wins  = [t for t in closed if t["outcome"]=="WIN"]
        total = sum(t["pnl_usd"] for t in closed if t["pnl_usd"])
        wr    = len(wins)/len(closed)*100
        print(f"\nClosed: {len(closed)} | WR: {wr:.1f}% | P&L: ${total:+.2f}")
        for t in closed[:15]:
            r = "WIN " if t["outcome"]=="WIN" else "LOSS"
            pips = t["pnl_pips"] or 0
            print(f"  {r} {t['direction']:4} {t['symbol']:8} "
                  f"${t['pnl_usd']:+.2f} ({pips:+.1f}p) [{t['strategy']}]")
    else:
        print("\nNo closed trades yet — running in dry run mode.")
    print("="*65)

def analyse(conn):
    print("\n" + "="*65)
    print("PERFORMANCE ANALYSIS")
    print("="*65)
    closed = conn.execute("SELECT * FROM trades WHERE status='closed'").fetchall()
    if not closed:
        print("No closed trades yet."); return
    wins  = [t for t in closed if t["outcome"]=="WIN"]
    total = sum(t["pnl_usd"] for t in closed if t["pnl_usd"])
    print(f"\nOverall: {len(closed)} trades | {len(wins)/len(closed)*100:.1f}% WR | ${total:+.2f} P&L\n")
    print("By Strategy:")
    for strat in sorted(set(t["strategy"] for t in closed)):
        ts = [t for t in closed if t["strategy"]==strat]
        w  = [t for t in ts if t["outcome"]=="WIN"]
        pnl= sum(t["pnl_usd"] for t in ts if t["pnl_usd"])
        wr = len(w)/len(ts)*100
        mark = "✅" if wr>70 else ("⚠️" if wr>55 else "❌")
        print(f"  {mark} {strat:18} {len(ts):3} trades | {wr:.0f}% WR | ${pnl:+.2f}")
    print("\nBy Session:")
    for sess in sorted(set(t["session"] for t in closed)):
        ts = [t for t in closed if t["session"]==sess]
        w  = [t for t in ts if t["outcome"]=="WIN"]
        pnl= sum(t["pnl_usd"] for t in ts if t["pnl_usd"])
        wr = len(w)/len(ts)*100 if ts else 0
        print(f"  {sess:12} {len(ts):3} trades | {wr:.0f}% WR | ${pnl:+.2f}")
    print("="*65)

# ─────────────────────────────────────────
# CHECK OPEN TRADES
# ─────────────────────────────────────────
def check_open_trades(conn):
    """Fetch current prices and close any open trade that has hit SL or TP1.
    Also prints floating P&L for positions still running."""
    open_trades = conn.execute(
        "SELECT * FROM trades WHERE status IN ('open','dry_run') ORDER BY opened_at"
    ).fetchall()

    if not open_trades:
        log("No open positions to check.")
        return

    log(f"Checking {len(open_trades)} open position(s)...")
    closed_count = 0

    for t in open_trades:
        price = get_current_price(t["symbol"])
        if price is None:
            log(f"  Could not fetch price for {t['symbol']} — skipping", "WARN")
            continue

        pip       = PAIR_CFG.get(t["symbol"], {}).get("pip", 0.0001)
        direction = t["direction"]
        entry     = t["entry_price"]
        sl        = t["sl"]
        tp1       = t["tp1"]
        outcome   = None
        close_px  = None

        if direction == "BUY":
            if price <= sl:   outcome = "LOSS"; close_px = sl
            elif price >= tp1: outcome = "WIN";  close_px = tp1
        else:  # SELL
            if price >= sl:   outcome = "LOSS"; close_px = sl
            elif price <= tp1: outcome = "WIN";  close_px = tp1

        if outcome:
            pnl_pips = (close_px - entry) / pip * (1 if direction == "BUY" else -1)
            pnl_usd  = pnl_pips * 10.0 * (t["lot_size"] or 0.01)
            now      = datetime.datetime.now(datetime.timezone.utc).isoformat()
            conn.execute(
                """UPDATE trades SET status='closed', closed_at=?, close_price=?,
                   outcome=?, pnl_usd=?, pnl_pips=? WHERE id=?""",
                (now, close_px, outcome, round(pnl_usd, 2), round(pnl_pips, 1), t["id"])
            )
            conn.commit()
            log(f"  CLOSED {t['symbol']:6} {direction:4} [{t['strategy']}] → {outcome} | "
                f"Entry:{entry:.5f} → {close_px:.5f} | "
                f"P&L: ${pnl_usd:+.2f} ({pnl_pips:+.1f}p)")
            closed_count += 1
        else:
            # Show floating P&L
            pnl_pips = (price - entry) / pip * (1 if direction == "BUY" else -1)
            pnl_usd  = pnl_pips * 10.0 * (t["lot_size"] or 0.01)
            sl_dist  = abs(price - sl) / pip
            tp_dist  = abs(tp1 - price) / pip
            log(f"  OPEN  {t['symbol']:6} {direction:4} [{t['strategy']}] | "
                f"Current:{price:.5f} | Float: ${pnl_usd:+.2f} ({pnl_pips:+.1f}p) | "
                f"SL {sl_dist:.0f}p away | TP1 {tp_dist:.0f}p away")

    if closed_count > 0:
        log(f"\n{closed_count} position(s) closed this check.")
    else:
        log("All positions still open — no SL/TP hit.")

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    cmd  = sys.argv[1] if len(sys.argv)>1 else "scan"
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    if cmd == "status":
        show_status(conn)
        compound_status(conn)
        conn.close(); return
    if cmd == "analyse":  analyse(conn);            conn.close(); return
    if cmd == "compound": compound_status(conn);    conn.close(); return
    if cmd == "check":    check_open_trades(conn);  conn.close(); return

    global DRY_RUN
    # Always use live compounded balance, not static ACCOUNT_BALANCE
    balance = get_current_balance(conn)
    tier    = get_tier(balance)
    if cmd == "trade":
        log(f"{'DRY RUN' if DRY_RUN else 'LIVE'} | Balance: ${balance:.2f} | "
            f"Tier: {tier['label']} | Risk: ${balance*tier['risk_pct']:.2f}/trade | "
            f"Max lots: {tier['lot_cap']}")
    else:
        DRY_RUN = True
        log("SCAN MODE")

    try:
        log("─"*50)
        signals = scan(conn, balance)
        log(f"\nSignals found: {len(signals)}")

        if not signals:
            log("No signals this scan — market conditions not met")
        else:
            placed = 0
            for sig in signals:
                pip   = PAIR_CFG.get(sig["symbol"],{}).get("pip",0.0001)
                sl_p  = abs(sig["entry"]-sig["sl"])/pip
                tp1_p = abs(sig["tp1"]-sig["entry"])/pip
                log(f"\n  ▶ {sig['strategy']:15} {sig['symbol']:8} {sig['direction']:4} "
                    f"@ {sig['entry']:.5f}")
                log(f"    SL:{sl_p:.0f}p | TP1:{tp1_p:.0f}p | TP2:{abs(sig['tp2']-sig['entry'])/pip:.0f}p | "
                    f"TP3:{abs(sig['tp3']-sig['entry'])/pip:.0f}p | RR:{sig['rr']:.1f}:1 | "
                    f"Conf:{sig['confidence']*100:.0f}%")
                log(f"    {sig['notes']}")
                if cmd == "trade":
                    if place_trade(conn, sig, balance): placed += 1
            if cmd == "trade":
                log(f"\nPlaced {placed} trade(s) in bridge file")

        show_status(conn)

    except Exception as e:
        log(f"FAILED: {e}", "ERROR")
        log(traceback.format_exc(), "ERROR")
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
