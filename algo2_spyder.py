# -*- coding: utf-8 -*-
"""
RIT ALGO2 - Algorithmic Market Making - OPTIMIZED (single-file, Spyder-ready)
============================================================================

How to use in Spyder 6 (no terminal / no command-line arguments needed):

  STEP 1 - ANALYZE YOUR DATA
     * Set   MODE = "ANALYZE"
     * Set   EXCEL_PATH = r"C:\\Users\\tmenar2\\Downloads\\algo 2 market data (1).xlsx"
     * Press the green Run button. It prints a RECOMMENDED CONFIG block.

  STEP 2 - PASTE THE NUMBERS
     * Copy the printed values into the CONFIG section below
       (TICK_SIZE, BASE_HALF_SPREAD, VOL_PER_TICK, INVENTORY_SKEW).

  STEP 3 - GO LIVE
     * Set   MODE = "TRADE"
     * Set   API_KEY  (RIT client -> Settings -> API key)
     * Confirm TICKER / ORDER_SIZE / MAX_POSITION against your Case Brief PDF.
     * Make sure the RIT client is running and the case is ACTIVE, then Run.
     * Press Spyder's red STOP button to halt - it will cancel & flatten.

Requires:  requests  (for TRADE mode) and pandas/numpy/openpyxl (for ANALYZE).
Anaconda already ships these; if not:  pip install requests pandas numpy openpyxl

NOTE: In a classroom RIT sim, "front-running" = posting smarter/faster quotes
than classmates running the default code. Legitimate competition inside a game.
"""

import time
from collections import deque

# ===========================================================================
#                    >>>>>  MODE + CONFIG (edit here)  <<<<<
# ===========================================================================

MODE = "ANALYZE"          # "ANALYZE" to crunch the Excel, "TRADE" to run the bot

# ---- ANALYZE mode ----
EXCEL_PATH = r"C:\Users\tmenar2\Downloads\algo 2 market data (1).xlsx"

# ---- TRADE mode: connection ----
API_KEY = "REPLACE_WITH_YOUR_RIT_API_KEY"
HOST = "http://localhost:9999/v1"

# ---- Instrument (CONFIRM against the Case Brief PDF) ----
TICKER = "ALGO"
TICK_SIZE = 0.01              # smallest price increment (ANALYZE infers this)

# ---- Sizing & risk ----
ORDER_SIZE = 100             # shares per order layer
NUM_LAYERS = 2              # ladder depth on each side
MAX_POSITION = 5000          # gross position limit from the case brief
LAYER_STEP_TICKS = 1         # ticks between ladder layers

# ---- Quoting ----
BASE_HALF_SPREAD = 2         # ticks each side of fair value (from ANALYZE)
VOL_PER_TICK = 0.02          # measured 1-tick move (from ANALYZE)
VOL_WIDEN = 1.5              # extra half-spread ticks per 1 std of recent vol
MAX_HALF_SPREAD = 8          # cap so we always quote something
QUEUE_JUMP = True            # post one tick inside the crowd to win priority

# ---- Inventory management ----
INVENTORY_SKEW = 0.5         # 0..1; how hard to lean quotes against your position
FLATTEN_AT_TICKS_LEFT = 8    # start unwinding when this many ticks remain

# ---- Order maintenance ----
REPRICE_THRESHOLD_TICKS = 1  # only cancel/repost if fair value moved > this
LOOP_PACING = 0.10           # seconds between iterations (respect rate limits)
VOL_LOOKBACK = 20            # mid observations used for the vol estimate

# ===========================================================================
#                    >>>>>  END OF EDITABLE CONFIG  <<<<<
# ===========================================================================


# ---------------------------------------------------------------------------
#                            ANALYZE MODE
# ---------------------------------------------------------------------------
def run_analyze(path):
    import numpy as np
    import pandas as pd

    COLUMN_HINTS = {
        "time":   ["time", "tick", "timestamp", "period", "t"],
        "bid":    ["bid", "best_bid", "bidprice"],
        "ask":    ["ask", "best_ask", "askprice", "offer"],
        "last":   ["last", "price", "close", "trade", "mid", "vwap"],
        "volume": ["volume", "vol", "size", "qty", "quantity"],
    }

    def find_col(cols, hints):
        lower = {c.lower().strip(): c for c in cols}
        for h in hints:
            if h in lower:
                return lower[h]
        for h in hints:
            for lc, orig in lower.items():
                if h in lc:
                    return orig
        return None

    def infer_tick_size(prices):
        p = np.round(np.asarray(prices, dtype=float), 6)
        p = p[~np.isnan(p)]
        diffs = np.diff(np.unique(p))
        diffs = diffs[diffs > 1e-9]
        if len(diffs) == 0:
            return 0.01
        small = diffs[diffs <= np.percentile(diffs, 25)]
        return float(np.round(np.median(small if len(small) else diffs), 4))

    def ar1(series):
        x = np.asarray(series, dtype=float)
        x = x[~np.isnan(x)]
        if len(x) < 10:
            return np.nan, np.nan
        b, _ = np.polyfit(x[:-1], x[1:], 1)
        hl = np.log(2) / (-np.log(b)) if 0 < b < 1 else np.inf
        return float(b), float(hl)

    print("Loading %s ..." % path)
    xls = pd.ExcelFile(path)
    best = None
    for sh in xls.sheet_names:
        df = xls.parse(sh)
        score = df.select_dtypes("number").shape[1] * len(df)
        if best is None or score > best[0]:
            best = (score, sh, df)
    _, sheet, df = best
    print("Using sheet: %r  shape=%s\n" % (sheet, df.shape))

    cols = list(df.columns)
    c = {k: find_col(cols, h) for k, h in COLUMN_HINTS.items()}
    print("Detected columns:")
    for k, v in c.items():
        print("   %-8s -> %s" % (k, v))
    print()

    if c["bid"] and c["ask"]:
        bid = pd.to_numeric(df[c["bid"]], errors="coerce")
        ask = pd.to_numeric(df[c["ask"]], errors="coerce")
        mid = (bid + ask) / 2.0
        spread = (ask - bid)
        tick_src = np.concatenate([bid.dropna().values, ask.dropna().values])
    elif c["last"]:
        mid = pd.to_numeric(df[c["last"]], errors="coerce")
        spread = None
        tick_src = mid.dropna().values
    else:
        num = df.select_dtypes("number")
        mid = num.iloc[:, 0]
        spread = None
        tick_src = mid.dropna().values
        print("!! Could not detect bid/ask/last; using first numeric column.")

    mid = mid.dropna().reset_index(drop=True)
    prices = mid.values
    rets = np.diff(prices)
    tick = infer_tick_size(tick_src)

    print("=" * 60)
    print("MARKET STATISTICS")
    print("=" * 60)
    print("Observations ............ %d" % len(prices))
    print("Price range ............. %.3f - %.3f" % (np.nanmin(prices), np.nanmax(prices)))
    print("Mean price .............. %.3f" % np.nanmean(prices))
    print("Inferred tick size ...... %.4f" % tick)

    per_tick_vol = float(np.nanstd(rets))
    print("Per-tick vol (std dMid).. %.4f  (%.2f ticks)" % (per_tick_vol, per_tick_vol / tick))

    if spread is not None:
        sp = spread.dropna()
        print("Spread mean/med/90%% ..... %.4f / %.4f / %.4f  (%.1f ticks median)"
              % (sp.mean(), sp.median(), sp.quantile(.9), sp.median() / tick))

    b, hl = ar1(prices)
    if np.isfinite(hl):
        print("AR(1) coef on price ..... %.4f   half-life = %.1f ticks" % (b, hl))
    else:
        print("AR(1) coef on price ..... %.4f   (no mean reversion / trending)" % b)

    if len(rets) > 2:
        ac1 = float(np.corrcoef(rets[:-1], rets[1:])[0, 1])
        tag = "momentum" if ac1 > 0.05 else "reversal" if ac1 < -0.05 else "noise"
        print("Return autocorr lag1 .... %+.4f  (%s)" % (ac1, tag))

    if c["volume"]:
        v = pd.to_numeric(df[c["volume"]], errors="coerce").dropna()
        print("Volume mean/median ...... %.0f / %.0f" % (v.mean(), v.median()))

    reverting = np.isfinite(hl) and hl < 40
    vol_ticks = per_tick_vol / tick if tick else 1.0
    base_hs = max(1, int(round(vol_ticks * (0.6 if reverting else 1.2))))
    skew = 0.35 if reverting else 0.7

    print()
    print("=" * 60)
    print("RECOMMENDED CONFIG  (paste into the CONFIG section above)")
    print("=" * 60)
    print("    TICK_SIZE            = %.4f" % tick)
    print("    BASE_HALF_SPREAD     = %d" % base_hs)
    print("    VOL_PER_TICK         = %.4f" % per_tick_vol)
    print("    INVENTORY_SKEW       = %.2f" % skew)
    regime = ("MEAN-REVERTING (great for MM, can lean on inventory)"
              if reverting else "TRENDING/RANDOM-WALK (keep inventory tight)")
    print("    # Market is %s" % regime)
    print("=" * 60)


# ---------------------------------------------------------------------------
#                             TRADE MODE
# ---------------------------------------------------------------------------
def run_trade():
    import requests

    session = requests.Session()
    session.headers.update({"X-API-Key": API_KEY})

    def get(path, **params):
        r = session.get(HOST + path, params=params, timeout=5)
        r.raise_for_status()
        return r.json()

    def get_case():
        j = get("/case")
        return j["status"], j["tick"], j["ticks_per_period"]

    def get_security():
        return get("/securities", ticker=TICKER)[0]

    def place(action, price, qty):
        session.post(HOST + "/orders", params={
            "ticker": TICKER, "type": "LIMIT", "quantity": qty,
            "action": action, "price": round(price, 4)}, timeout=5)

    def market(action, qty):
        session.post(HOST + "/orders", params={
            "ticker": TICKER, "type": "MARKET",
            "quantity": qty, "action": action}, timeout=5)

    def cancel_all():
        session.post(HOST + "/commands/cancel", params={"ticker": TICKER}, timeout=5)

    def round_tick(price):
        return round(round(price / TICK_SIZE) * TICK_SIZE, 4)

    def flatten(position):
        if position == 0:
            return
        cancel_all()
        market("SELL" if position > 0 else "BUY", abs(position))

    mid_hist = deque(maxlen=VOL_LOOKBACK)
    last_fair = [None]

    def recent_vol_ticks():
        if len(mid_hist) < 3:
            return VOL_PER_TICK / TICK_SIZE
        arr = list(mid_hist)
        diffs = [abs(arr[i + 1] - arr[i]) for i in range(len(arr) - 1)]
        return (sum(diffs) / len(diffs)) / TICK_SIZE

    def dynamic_half_spread():
        extra = max(0.0, recent_vol_ticks() - VOL_PER_TICK / TICK_SIZE)
        hs = BASE_HALF_SPREAD + VOL_WIDEN * extra
        return min(MAX_HALF_SPREAD, max(BASE_HALF_SPREAD, hs))

    print("Market maker started on %s. Press Spyder's STOP button to halt." % TICKER)
    try:
        while True:
            try:
                status, tick, tpp = get_case()
                if status != "ACTIVE":
                    time.sleep(0.5)
                    continue

                sec = get_security()
                pos = sec["position"]
                best_bid, best_ask = sec["bid"], sec["ask"]
                if not best_bid or not best_ask:
                    time.sleep(LOOP_PACING)
                    continue

                mid = (best_bid + best_ask) / 2.0
                mid_hist.append(mid)

                # end-of-period risk unwind
                if tpp - tick <= FLATTEN_AT_TICKS_LEFT:
                    flatten(pos)
                    time.sleep(LOOP_PACING)
                    continue

                # fair value with inventory skew
                inv = max(-1.0, min(1.0, pos / float(MAX_POSITION)))
                half = dynamic_half_spread()
                fair = mid - INVENTORY_SKEW * inv * half * TICK_SIZE

                # sticky orders: only reprice on meaningful moves
                if (last_fair[0] is not None and
                        abs(fair - last_fair[0]) < REPRICE_THRESHOLD_TICKS * TICK_SIZE):
                    time.sleep(LOOP_PACING)
                    continue
                last_fair[0] = fair

                cancel_all()
                can_buy = pos < MAX_POSITION
                can_sell = pos > -MAX_POSITION

                for layer in range(NUM_LAYERS):
                    offset = (half + layer * LAYER_STEP_TICKS) * TICK_SIZE
                    bid_price = round_tick(fair - offset)
                    ask_price = round_tick(fair + offset)

                    # queue jump on the front layer (never cross, never through fair)
                    if QUEUE_JUMP and layer == 0:
                        jb = round_tick(best_bid + TICK_SIZE)
                        ja = round_tick(best_ask - TICK_SIZE)
                        if bid_price < jb < fair:
                            bid_price = jb
                        if fair < ja < ask_price:
                            ask_price = ja

                    if bid_price >= ask_price:
                        continue
                    if can_buy:
                        place("BUY", bid_price, ORDER_SIZE)
                    if can_sell:
                        place("SELL", ask_price, ORDER_SIZE)

                time.sleep(LOOP_PACING)

            except requests.exceptions.RequestException as e:
                print("API error: %s; retrying..." % e)
                time.sleep(1.0)

    except KeyboardInterrupt:
        print("\nStop requested: cancelling orders and flattening.")
        try:
            cancel_all()
            flatten(get_security()["position"])
        except Exception as e:
            print("Cleanup error: %s" % e)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if MODE.upper() == "ANALYZE":
        run_analyze(EXCEL_PATH)
    elif MODE.upper() == "TRADE":
        run_trade()
    else:
        print('MODE must be "ANALYZE" or "TRADE" (currently: %r)' % MODE)
