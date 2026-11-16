"""
=====================================================================
LSR BOT — ETHUSD 1H  |  Liquidity Sweep Reversal (Booming Bulls / ICT)
=====================================================================
Backtest (1 saal, real Delta fees): 426 trades, WR 59.4%, PF 1.33,
net +175.6%, MaxDD -22.5%. Sirf ETH 1h + LIMIT entry + London/NY
(7-20 UTC) + EMA200 filter par positive edge mila.

STRATEGY (exactly backtest jaisa):
  Levels : PDH/PDL, CDH/CDL, Asia high/low, EQH/EQL (pivot 3, tol 0.15%)
  Sweep  : wick level ke paar (>= 0.1 ATR), par close wapas andar
  Entry  : LIMIT order wapas level par (maker fee) — 3 bars tak valid
  SL     : sweep extreme +/- 0.3 ATR
  TP     : 1R (fixed)
  Time   : 12 bars (12 ghante) baad exit
  Filter : sirf 07:00-20:00 UTC + close EMA200 ki taraf + max 2 trade/din

IMPORTANT DESIGN (gold bot ki galti se seekha):
  Har run par pichhle run ke baad ke SAARE closed 1h bars replay hote hain,
  isliye GitHub ka schedule late/miss ho jaye to bhi koi bar, koi sweep,
  koi SL/TP nahi chhootta. State commit hota hai, isliye replay idempotent hai.

MODES:
  python algo_bot.py --once                 # ek baar check (paper)
  python algo_bot.py --loop 60              # har 60 sec (VM ke liye)
  python algo_bot.py --selftest             # last 30 din ka replay + stats
  python algo_bot.py --test-telegram        # Telegram check

NOTE: `--live` jaan-boojh kar nahi hai. Is strategy ka edge LIMIT entry par
tika hai; bina proper limit-order management ke real order dena nuksan hai.
Paper mein 4+ hafte proof ke baad hee live sochein.
=====================================================================
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ----------------------------- settings -----------------------------
BASE = "https://api.india.delta.exchange"
HDRS = {"Accept": "application/json", "User-Agent": "lsr-eth-bot/1.0"}

SYMBOL = "ETHUSD"
TF_MIN = 60
RES_MAP = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m",
           60: "1h", 120: "2h", 240: "4h", 360: "6h", 720: "12h",
           1440: "1d"}
UNIT = 0.01                 # ETH per contract
TICK = 0.05
MAKER = 0.0002              # 0.02%
TAKER = 0.0005              # 0.05%
SLIP = 2 * TICK             # 2 ticks slippage (exits)

PIV = 3
TOL_EQ = 0.0015
SWEEP_MIN_ATR = 0.10
SL_ATR = 0.30
RR = 1.0
SESSION = (7, 20)           # UTC hours [7, 20)
TREND_FILTER = True
MAX_TRADES_DAY = 2
MAX_HOLD_BARS = 12
PENDING_BARS = 3

MARGIN_PCT = 25.0
LEVERAGE = 10
START_EQUITY = 100.0

BARS_FETCH = 600            # ~25 din (EMA200 + pool expiry ke liye kaafi)
STATE_FILE = "bot_state.json"
TRADES_FILE = "bot_trades.csv"
CONFIG_FILE = "bot_config.txt"

TRADE_COLS = ["time", "event", "side", "entry_px", "exit_px", "qty",
              "reason", "pnl", "equity_after", "level", "kind"]


# ----------------------------- telegram -----------------------------
def _load_config(path=CONFIG_FILE):
    cfg = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
        except Exception as e:
            print("config read error:", e)
    return cfg


def send_telegram(text):
    cfg = _load_config()
    token = os.getenv("TELEGRAM_BOT_TOKEN") or cfg.get("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID") or cfg.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=15)
        return r.ok
    except Exception as e:
        print("⚠ telegram error:", e)
        return False


# ----------------------------- data -----------------------------
def fetch_candles(bars=BARS_FETCH):
    """Last `bars` CLOSED 1H candles (chal rahi candle hata di jati hai)."""
    now = int(time.time())
    start = now - (bars + 3) * TF_MIN * 60
    # Delta resolution string: 60 -> "1h" (sirf "60m" bhejne par 400 aata hai)
    res = RES_MAP[TF_MIN]
    url = (f"{BASE}/v2/history/candles?resolution={res}"
           f"&symbol={SYMBOL}&start={start}&end={now}")
    r = requests.get(url, headers=HDRS, timeout=30)
    r.raise_for_status()
    j = r.json()
    rows = j.get("result") or []
    if not rows:
        raise RuntimeError("Delta se candles nahi mile")
    df = pd.DataFrame(rows)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = df["time"].astype("int64")
    df = df.drop(columns=["time"]).sort_values("open_time").reset_index(drop=True)
    # sirf poori (closed) candles
    cutoff = now - (now % (TF_MIN * 60))
    df = df[df["open_time"] + TF_MIN * 60 <= cutoff].reset_index(drop=True)
    df = df[df["open_time"] + TF_MIN * 60 <= time.time()].reset_index(drop=True)
    return df


def mark_price():
    r = requests.get(f"{BASE}/v2/tickers/{SYMBOL}", headers=HDRS, timeout=20)
    r.raise_for_status()
    j = r.json()["result"]
    return float(j.get("mark_price") or j.get("close"))


def atr_wilder(h, l, c, length=14):
    n = len(c)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        pc = c[i - 1]
        tr[i] = max(h[i] - l[i], abs(h[i] - pc), abs(l[i] - pc))
    return pd.Series(tr).ewm(alpha=1.0 / length, min_periods=length,
                             adjust=False).mean().to_numpy()


def build_levels(ot, h, l):
    """Backtest ka build_levels — bilkul same (parity test se verify kiya)."""
    n = len(ot)
    day = ot // 86400
    hour = (ot % 86400) // 3600

    s = pd.Series(h)
    dh = s.groupby(day).max()
    dl = pd.Series(l).groupby(day).min()
    pdh_map = dh.shift(1).to_dict()
    pdl_map = dl.shift(1).to_dict()
    pdh = np.array([pdh_map.get(d, np.nan) for d in day])
    pdl = np.array([pdl_map.get(d, np.nan) for d in day])

    cdh = pd.Series(h).groupby(day).cummax().shift(1).to_numpy()
    cdl = pd.Series(l).groupby(day).cummin().shift(1).to_numpy()

    asia_mask = hour < 8
    ah = pd.Series(np.where(asia_mask, h, np.nan))
    al = pd.Series(np.where(asia_mask, l, np.nan))
    ah_map = ah.groupby(day).max().to_dict()
    al_map = al.groupby(day).min().to_dict()
    asia_h = np.array([ah_map.get(d, np.nan) if hour[i] >= 8 else np.nan
                       for i, d in enumerate(day)])
    asia_l = np.array([al_map.get(d, np.nan) if hour[i] >= 8 else np.nan
                       for i, d in enumerate(day)])

    ph = np.zeros(n, bool)
    pl = np.zeros(n, bool)
    for j in range(PIV, n - PIV):
        win_h = h[j - PIV:j + PIV + 1]
        win_l = l[j - PIV:j + PIV + 1]
        if h[j] >= win_h.max() and (win_h == h[j]).sum() == 1:
            ph[j] = True
        if l[j] <= win_l.min() and (win_l == l[j]).sum() == 1:
            pl[j] = True

    return dict(pdh=pdh, pdl=pdl, cdh=cdh, cdl=cdl,
                asia_h=asia_h, asia_l=asia_l, ph=ph, pl=pl,
                day=day, hour=hour)


def build_pool(ot, h, l):
    """EQH/EQL pool — har bar par kaun-kaun se level active hain (same as backtest)."""
    n = len(ot)
    L = build_levels(ot, h, l)
    pool = []
    last_ph = np.nan
    last_pl = np.nan
    per_bar = [None] * n
    for i in range(n):
        if i > 0:
            j = i - PIV - 1
            if j >= PIV:
                if L["ph"][j]:
                    if not np.isnan(last_ph) and abs(h[j] - last_ph) <= TOL_EQ * h[j]:
                        pool.append(dict(price=max(h[j], last_ph), kind="EQH",
                                         exp=i + 200))
                    last_ph = h[j]
                if L["pl"][j]:
                    if not np.isnan(last_pl) and abs(l[j] - last_pl) <= TOL_EQ * l[j]:
                        pool.append(dict(price=min(l[j], last_pl), kind="EQL",
                                         exp=i + 200))
                    last_pl = l[j]
        pool = [p for p in pool if p["exp"] > i]
        per_bar[i] = [(p["price"], p["kind"]) for p in pool]
    return L, per_bar


# ----------------------------- state -----------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    else:
        st = {}
    st.setdefault("equity", START_EQUITY)
    st.setdefault("pos", None)
    st.setdefault("pending", None)
    st.setdefault("last_bar", 0)
    st.setdefault("trades", 0)
    st.setdefault("day_counts", {})
    st.setdefault("held", [])
    return st


def save_state(st):
    st["day_counts"] = {k: v for k, v in st.get("day_counts", {}).items()}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)


def log_trade(rec):
    row = {c: rec.get(c, "") for c in TRADE_COLS}
    df = pd.DataFrame([row])
    header = not os.path.exists(TRADES_FILE)
    df.to_csv(TRADES_FILE, mode="a", header=header, index=False)


# ----------------------------- core machine -----------------------------
def _size(equity, price):
    notional = equity * (MARGIN_PCT / 100.0) * LEVERAGE
    qty = float(max(1, int(notional / (price * UNIT))))
    return min(qty, max(1.0, float(int((equity * LEVERAGE) / (price * UNIT)))))


def _close(st, fill, reason, fee_rate, bar_ts, ctx):
    """Position band karo — backtest ke close_pos jaisa."""
    P = st["pos"]
    d = 1 if P["side"] == "LONG" else -1
    gross = d * (fill - P["entry_px"]) * UNIT * P["qty"]
    fee = abs(fill * UNIT * P["qty"]) * fee_rate
    pnl = gross - P["entry_fee"] - fee
    st["equity"] += pnl
    st["trades"] += 1
    risk = abs(P["entry_px"] - P["sl"])
    r = d * (fill - P["entry_px"]) / risk if risk > 0 else None
    ts = datetime.fromtimestamp(bar_ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log_trade(dict(time=ts, event="EXIT", side=P["side"],
                   entry_px=round(P["entry_px"], 4), exit_px=round(fill, 4),
                   qty=P["qty"], reason=reason, pnl=round(pnl, 4),
                   equity_after=round(st["equity"], 2),
                   level=P.get("lvl"), kind=P.get("kind")))
    print(f"  ✅ EXIT {P['side']} @ {fill:.2f} ({reason}) "
          f"pnl=${pnl:+.2f} R={r if r is None else round(r, 2)} "
          f"| equity ${st['equity']:.2f}")
    send_telegram(
        f"🔔 <b>LSR ETH — EXIT</b>\n"
        f"Side: {P['side']}\nEntry: {P['entry_px']:.2f}\n"
        f"Exit: {fill:.2f} ({reason})\n"
        f"P&L: <b>${pnl:+.2f}</b>\n"
        f"R: {r if r is None else round(r, 2)}\n"
        f"Equity: ${st['equity']:.2f}")
    st["pos"] = None


def process_bar(i, ctx, st, verbose=True):
    o, h, l, c = ctx["o"], ctx["h"], ctx["l"], ctx["c"]
    ot, atr, ema = ctx["ot"], ctx["atr"], ctx["ema"]
    L, pool = ctx["L"], ctx["pool_by_bar"][i]

    oi, hi, li, ci = o[i], h[i], l[i], c[i]
    bar_ts = int(ot[i])
    hour = int((bar_ts % 86400) // 3600)
    day_key = str(int(bar_ts // 86400))

    # ---------- 1) pending limit order ----------
    pend = st.get("pending")
    if pend and not st.get("pos"):
        bars_since = (bar_ts - pend["bar_ts"]) // (TF_MIN * 60)
        if bars_since > PENDING_BARS:
            if verbose:
                print(f"  ⏳ pending expire ({pend['kind']} {pend['px']:.2f})")
            st["pending"] = None
        else:
            hit = (pend["dir"] == 1 and li <= pend["px"]) or \
                  (pend["dir"] == -1 and hi >= pend["px"])
            if hit:
                fill = pend["px"]
                qty = _size(st["equity"], fill)
                if qty >= 1:
                    side = "LONG" if pend["dir"] == 1 else "SHORT"
                    st["pos"] = dict(
                        side=side, entry_px=fill, qty=qty,
                        sl=round(pend["sl"], 4), tp=round(pend["tp"], 4),
                        entry_fee=abs(fill * UNIT * qty) * MAKER,
                        entry_ts=bar_ts, entry_bar_ts=bar_ts,
                        lvl=round(pend["px"], 4), kind=pend["kind"],
                        day=pend.get("day"))
                    st["day_counts"][day_key] = st["day_counts"].get(day_key, 0) + 1
                    st["held"].append(f"{day_key}|{pend['kind']}|{round(pend['px'], 2)}")
                    st["pending"] = None
                    print(f"  🎯 FILL {side} {int(qty)} @ {fill:.2f} "
                          f"(SL {pend['sl']:.2f} TP {pend['tp']:.2f}) "
                          f"[{pend['kind']} sweep]")
                    ts = datetime.fromtimestamp(bar_ts, timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC")
                    log_trade(dict(time=ts, event="ENTRY", side=side,
                                   entry_px=round(fill, 4), exit_px="",
                                   qty=qty, reason="LSR-LIMIT", pnl="",
                                   equity_after=round(st["equity"], 2),
                                   level=round(pend["px"], 4), kind=pend["kind"]))
                    send_telegram(
                        f"🎯 <b>LSR ETH — ENTRY</b>\n"
                        f"Side: {side}\nEntry: {fill:.2f} (limit)\n"
                        f"SL: {pend['sl']:.2f}  TP: {pend['tp']:.2f}\n"
                        f"Level: {pend['kind']} @ {pend['px']:.2f}\n"
                        f"Qty: {int(qty)}\nEquity: ${st['equity']:.2f}")

                    # same bar exit (conservative: SL pehle)
                    P = st["pos"]
                    d = 1 if P["side"] == "LONG" else -1
                    if (d == 1 and li <= P["sl"]) or (d == -1 and hi >= P["sl"]):
                        _close(st, P["sl"] - d * SLIP, "SL", TAKER, bar_ts, ctx)
                    elif (d == 1 and hi >= P["tp"]) or (d == -1 and li <= P["tp"]):
                        _close(st, P["tp"] - d * SLIP, "TP", MAKER, bar_ts, ctx)

    # ---------- 2) open position ----------
    P = st.get("pos")
    if P:
        d = 1 if P["side"] == "LONG" else -1
        if (d == 1 and li <= P["sl"]) or (d == -1 and hi >= P["sl"]):
            _close(st, P["sl"] - d * SLIP, "SL", TAKER, bar_ts, ctx)
        elif (d == 1 and hi >= P["tp"]) or (d == -1 and li <= P["tp"]):
            _close(st, P["tp"] - d * SLIP, "TP", MAKER, bar_ts, ctx)
        elif (bar_ts - P["entry_bar_ts"]) // (TF_MIN * 60) >= MAX_HOLD_BARS:
            _close(st, ci - d * SLIP, "TIME", TAKER, bar_ts, ctx)

    # ---------- 3) naya signal ----------
    if st.get("pos") is None and st.get("pending") is None:
        if i > PIV * 2 and not np.isnan(atr[i]) and atr[i] > 0:
            if SESSION[0] <= hour < SESSION[1]:
                cand = []
                for name, arr in (("PDH", L["pdh"]), ("CDH", L["cdh"]),
                                  ("ASIA_H", L["asia_h"])):
                    v = arr[i]
                    if not np.isnan(v):
                        cand.append((float(v), name))
                for name, arr in (("PDL", L["pdl"]), ("CDL", L["cdl"]),
                                  ("ASIA_L", L["asia_l"])):
                    v = arr[i]
                    if not np.isnan(v):
                        cand.append((float(v), name))
                cand.extend(pool)

                best = None
                for lvl, kind in cand:
                    dist = SWEEP_MIN_ATR * atr[i]
                    if TREND_FILTER and not np.isnan(ema[i]):
                        allow_long = ci > ema[i]
                        allow_short = ci < ema[i]
                    else:
                        allow_long = allow_short = True

                    # SHORT: high paar, close andar (buy-side liquidity sweep)
                    if allow_short and hi > lvl and ci < lvl and \
                            hi - lvl >= dist and ci < oi:
                        key = f"{day_key}|{kind}|{round(lvl, 2)}"
                        if key in st["held"] or \
                                st["day_counts"].get(day_key, 0) >= MAX_TRADES_DAY:
                            continue
                        score = (hi - lvl) / atr[i]
                        if best is None or score > best["score"]:
                            best = dict(dir=-1, lvl=lvl, kind=kind, score=score,
                                        sl=hi + SL_ATR * atr[i], day=day_key)

                    # LONG: low paar, close andar (sell-side liquidity sweep)
                    if allow_long and li < lvl and ci > lvl and \
                            lvl - li >= dist and ci > oi:
                        key = f"{day_key}|{kind}|{round(lvl, 2)}"
                        if key in st["held"] or \
                                st["day_counts"].get(day_key, 0) >= MAX_TRADES_DAY:
                            continue
                        score = (lvl - li) / atr[i]
                        if best is None or score > best["score"]:
                            best = dict(dir=1, lvl=lvl, kind=kind, score=score,
                                        sl=li - SL_ATR * atr[i], day=day_key)

                if best:
                    risk = abs(best["lvl"] - best["sl"])
                    if risk > 0:
                        tp = best["lvl"] + best["dir"] * RR * risk
                        st["pending"] = dict(
                            dir=best["dir"], px=round(best["lvl"], 4),
                            sl=round(best["sl"], 4), tp=round(tp, 4),
                            bar_ts=bar_ts, day=day_key, kind=best["kind"])
                        print(f"  📌 SIGNAL {best['kind']} {best['lvl']:.2f} "
                              f"-> limit {'BUY' if best['dir'] == 1 else 'SELL'} "
                              f"SL {best['sl']:.2f} TP {tp:.2f}")
                        send_telegram(
                            f"📌 <b>LSR ETH — LIMIT ORDER</b>\n"
                            f"{best['kind']} sweep @ {best['lvl']:.2f}\n"
                            f"{'BUY' if best['dir'] == 1 else 'SELL'} limit\n"
                            f"SL: {best['sl']:.2f}\nTP: {tp:.2f}\n"
                            f"(3 ghante tak valid)")


def build_ctx(df):
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    ot = df["open_time"].to_numpy("int64")
    atr = atr_wilder(h, l, c, 14)
    ema = pd.Series(c).ewm(span=200, adjust=False).mean().to_numpy()
    L, pool_by_bar = build_pool(ot, h, l)
    return dict(o=o, h=h, l=l, c=c, ot=ot, atr=atr, ema=ema,
                L=L, pool_by_bar=pool_by_bar, df=df)


# ----------------------------- run modes -----------------------------
def prune(st, now_ts):
    """Purane din ke counters hata do (state chhota rahe)."""
    keep = {str(int(now_ts // 86400)), str(int(now_ts // 86400) - 1)}
    st["day_counts"] = {k: v for k, v in st.get("day_counts", {}).items() if k in keep}
    st["held"] = [x for x in st.get("held", []) if x.split("|")[0] in keep]


def run_once(notify=False, heartbeat=False, verbose=True):
    st = load_state()
    df = fetch_candles()
    ctx = build_ctx(df)
    n = len(df)
    last_ot = int(df["open_time"].iloc[-1])
    px = mark_price()

    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}] {SYMBOL} 1H | "
          f"price={px} | equity=${st['equity']:.2f} | "
          f"pos={st['pos']['side'] if st['pos'] else 'FLAT'} | "
          f"candles={n}")

    # replay: pichhle run ke baad ke saare closed bars
    start_i = 0
    if st.get("last_bar"):
        newer = np.where(ctx["ot"] > int(st["last_bar"]))[0]
        start_i = int(newer[0]) if len(newer) else n
        if start_i == n and verbose:
            print("  (koi naya closed bar nahi — state waisa hi hai)")
    else:
        # pehli baar: sirf aakhri bar process karo (history replay nahi)
        start_i = max(0, n - 1)

    for i in range(start_i, n):
        process_bar(i, ctx, st, verbose=verbose)

    st["last_bar"] = last_ot
    st["last_price"] = round(px, 3)
    st["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    st["mode"] = "PAPER"
    prune(st, last_ot)

    # heartbeat: din mein ek baar status
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if heartbeat and st.get("last_heartbeat") != today:
        st["last_heartbeat"] = today
        P = st["pos"]
        pend = st["pending"]
        pos_txt = (f"{P['side']} {int(P['qty'])} @ {P['entry_px']:.2f} "
                   f"(SL {P['sl']:.2f} / TP {P['tp']:.2f})") if P else "FLAT"
        pend_txt = (f"{pend['kind']} limit {pend['px']:.2f}") if pend else "—"
        send_telegram(
            f"📊 <b>LSR ETH heartbeat</b> {today}\n"
            f"Price: {px:.2f}\nEquity: ${st['equity']:.2f}\n"
            f"Position: {pos_txt}\n"
            f"Pending: {pend_txt}\n"
            f"Trades: {st['trades']}")

    save_state(st)
    print(f"  state saved | equity=${st['equity']:.2f} | trades={st['trades']} | "
          f"pos={st['pos']['side'] if st['pos'] else 'FLAT'} | "
          f"pending={'yes' if st['pending'] else 'no'}")
    return st


def selftest(days=30):
    """Live code ko history par replay karke stats — backtest se compare karne ke liye."""
    st = dict(equity=START_EQUITY, pos=None, pending=None, last_bar=0,
              trades=0, day_counts={}, held=[])
    df = fetch_candles(bars=min(BARS_FETCH, days * 24 + 260))
    ctx = build_ctx(df)
    n = len(df)
    start = max(260, n - days * 24)
    for i in range(start, n):
        process_bar(i, ctx, st, verbose=False)
    if st["pos"]:
        P = st["pos"]
        d = 1 if P["side"] == "LONG" else -1
        _close(st, ctx["c"][-1] - d * SLIP, "END", TAKER, ctx["ot"][-1], ctx)

    tr = pd.read_csv(TRADES_FILE)
    ex = tr[tr.event == "EXIT"]
    if len(ex) == 0:
        print("Selftest: koi trade nahi bana.")
        return
    pnl = ex["pnl"].astype(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else 999
    print(f"\nSELFTEST ({days} din, last {n - start} bars)")
    print(f"  trades={len(ex)}  WR={len(wins) / len(ex) * 100:.1f}%  PF={pf:.2f}")
    print(f"  net=${pnl.sum():+.2f}  equity=${st['equity']:.2f}  "
          f"best=${pnl.max():+.2f}  worst=${pnl.min():+.2f}")
    print(f"  exits: " + ", ".join(f"{k}={v}" for k, v in
                                   ex["reason"].value_counts().items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0, metavar="SEC")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--heartbeat", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()

    print(f"=== LSR ETH BOT | {SYMBOL} {TF_MIN}m | mode=PAPER 📝 ===")
    cfg = _load_config()
    tok = os.getenv("TELEGRAM_BOT_TOKEN") or cfg.get("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID") or cfg.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        print("Telegram alerts: ON ✅")
    else:
        print("Telegram alerts: OFF ❌ — token/chat id set nahi hai")

    if a.test_telegram:
        ok = send_telegram("✅ <b>LSR ETH bot</b> — Telegram test working!")
        print("Test message bheja" if ok else "Test message FAIL (token/chat check karo)")
        return

    if a.selftest:
        selftest(a.days)
        return

    if a.loop:
        print(f"Loop mode: har {a.loop} sec check (Ctrl+C se band)")
        while True:
            try:
                run_once(notify=a.notify, heartbeat=a.heartbeat)
            except Exception as e:
                print("⚠ run error:", e)
            time.sleep(a.loop)

    run_once(notify=a.notify, heartbeat=a.heartbeat)


if __name__ == "__main__":
    main()
