# -*- coding: utf-8 -*-
"""
RIT ALGO2 - Algorithmic Market Making - COMPETITIVE (v4)
========================================================
Built to win fills in a crowded field (20+ students). Pure requests + stdlib,
runs in Spyder 6.

WHY v1/v3 (~8.8k) FADED AGAINST 20 STUDENTS
  They quoted at fair +/- 2 ticks -- BEHIND the touch. With 19 other market
  makers sitting at the top of book, orders behind the touch rarely fill; you
  only get hit on adverse moves. The leaderboard winners did 1-1.7M shares by
  being AT the top of book constantly and harvesting the 0.5c rebate on huge
  passive volume (their spread capture was only ~0.5c).

WHAT v4 DOES DIFFERENTLY
  * TOP-OF-BOOK QUOTING: quotes the most aggressive PASSIVE price -- one tick
    inside the touch (or joins it in a 1-tick book), bounded by fair value so it
    never buys above / sells below fair, and never crosses (stays passive -> earns
    the rebate). This wins the queue against students quoting behind the touch.
  * NON-DESTRUCTIVE TOP-UP (from v3): when calm and a fill happens, it refills
    only the filled side and keeps the resting exit -- no cancel_all churn (the
    bug that made v2 break even).
  * STRONG INVENTORY SKEW: aggressive quoting fills fast, so fair leans harder
    against the position to keep the book balanced.
  * FAST LOOP + rebate-first (never take liquidity except the final flatten).

CASE FACTS: ALGO, $0.01 tick, 300s, 5,000/order, 25,000 limit, 1c commission
(active), 0.5c rebate (passive fills), 10c/share fine over 25,000.

If a path clearly TRENDS, set MEAN_REVERT_WEIGHT=0 and raise INVENTORY_SKEW.
"""

import math
from time import sleep
import requests

# ============================== CONFIG ====================================
API_KEY = "REPLACE_WITH_YOUR_RIT_API_KEY"
HOST = "http://localhost:9999/v1"

TICKER = "ALGO"
TICK_SIZE = 0.01

# ---- SIZE (case: 5,000/order, 25,000 position limit) ----
ORDER_SIZE = 5000
NUM_LAYERS = 2
MAX_POSITION = 25000
POSITION_BUFFER = 2000                 # effective 23,000 -> avoids the 10c fine

# ---- Quoting: top-of-book aggressive (the competitive edge) ----
BASE_HALF_SPREAD = 1                    # 1 = quote at the fair boundary / touch
VOL_WIDEN = 1.5                        # widen only when volatility actually spikes
VOL_PER_TICK = 0.02
MAX_HALF_SPREAD = 6

# ---- Mean-reversion ----
MEAN_REVERT_WEIGHT = 0.25              # SET TO 0 IF A PATH TRENDS
MEAN_LOOKBACK = 30

# ---- Inventory / timing (300s heat) ----
INVENTORY_SKEW = 0.6                    # lean harder; aggressive quoting fills fast
WIND_DOWN_AT_SEC_LEFT = 30
FLATTEN_AT_SEC_LEFT = 8

# ---- Loop ----
REPRICE_THRESHOLD_TICKS = 1
LOOP_PACING = 0.05                      # fast: react to fills/moves before others
VOL_LOOKBACK = 20
PROFIT_TARGET = 10000
PNL_PRINT_EVERY = 40
# ==========================================================================

EFFECTIVE_LIMIT = MAX_POSITION - POSITION_BUFFER


class ApiException(Exception):
    pass


def get_case(session):
    resp = session.get(HOST + "/case")
    if resp.ok:
        c = resp.json()
        return c["status"], c["tick"], c["ticks_per_period"]
    raise ApiException("Cannot read /case (HTTP %s). Check RIT is running and API_KEY is correct." % resp.status_code)


def get_security(session):
    resp = session.get(HOST + "/securities", params={"ticker": TICKER})
    if resp.ok:
        return resp.json()[0]
    raise ApiException("Cannot read /securities for %s (HTTP %s)." % (TICKER, resp.status_code))


def get_resting_orders(session):
    resp = session.get(HOST + "/orders", params={"status": "OPEN"})
    buys, sells = [], []
    if resp.ok:
        for o in resp.json():
            if o.get("ticker") != TICKER:
                continue
            rem = (o.get("quantity", 0) or 0) - (o.get("quantity_filled", 0) or 0)
            if rem <= 0:
                continue
            (buys if o.get("action") == "BUY" else sells).append(rem)
    return buys, sells


def place(session, action, price, qty):
    if qty <= 0:
        return 0
    session.post(HOST + "/orders", params={"ticker": TICKER, "type": "LIMIT",
                 "quantity": min(qty, ORDER_SIZE), "action": action, "price": round(price, 4)})
    return 1


def market(session, action, qty):
    remaining = abs(qty)
    while remaining > 0:
        q = min(5000, remaining)
        session.post(HOST + "/orders", params={"ticker": TICKER, "type": "MARKET",
                     "quantity": q, "action": action})
        remaining -= q


def cancel_all(session):
    session.post(HOST + "/commands/cancel", params={"ticker": TICKER})


def round_tick(price):
    return round(round(price / TICK_SIZE) * TICK_SIZE, 4)


def quote_prices(fair, half, best_bid, best_ask, layer):
    """Most aggressive PASSIVE quote: one tick inside the touch, bounded by fair,
    never crossing the book. Deeper layers step further out."""
    t = TICK_SIZE
    bid = round_tick(best_bid + t)
    ask = round_tick(best_ask - t)
    if bid >= ask:                                  # 1-tick book: join the touch
        bid, ask = round_tick(best_bid), round_tick(best_ask)
    highest_bid = round(math.floor(fair / t - 1e-9) * t, 4)   # largest tick < fair
    lowest_ask = round(math.ceil(fair / t + 1e-9) * t, 4)     # smallest tick > fair
    extra = (half - 1) * t                                    # volatility widening
    bid = min(bid, round_tick(highest_bid - extra))
    ask = max(ask, round_tick(lowest_ask + extra))
    bid = round_tick(bid - layer * t)
    ask = round_tick(ask + layer * t)
    bid = min(bid, round_tick(best_ask - t))        # stay passive: never cross
    ask = max(ask, round_tick(best_bid + t))
    return bid, ask


def pnl_of(sec):
    r = sec.get("realized")
    u = sec.get("unrealized")
    if r is not None or u is not None:
        return (r or 0.0) + (u or 0.0)
    return None


def main():
    mid_hist = []
    last_fair = None
    loops = 0

    with requests.Session() as s:
        s.headers.update({"X-API-Key": API_KEY})
        get_case(s)  # fail fast on bad key/connection
        print("Connected. ALGO2 MM v4 competitive (limit %d, target $%d). Press STOP to halt."
              % (EFFECTIVE_LIMIT, PROFIT_TARGET))

        try:
            while True:
                loops += 1
                status, tick, tpp = get_case(s)
                if status != "ACTIVE":
                    sleep(0.3)
                    continue
                sec_left = tpp - tick

                sec = get_security(s)
                pos = sec["position"]
                best_bid, best_ask = sec["bid"], sec["ask"]
                if not best_bid or not best_ask:
                    sleep(LOOP_PACING)
                    continue

                mid = (best_bid + best_ask) / 2.0
                mid_hist.append(mid)
                if len(mid_hist) > max(VOL_LOOKBACK, MEAN_LOOKBACK):
                    mid_hist.pop(0)

                if loops % PNL_PRINT_EVERY == 0:
                    p = pnl_of(sec)
                    msg = "t-%ds  pos %+d" % (sec_left, pos)
                    if p is not None:
                        msg += "  P&L $%.0f / %d (%.0f%%)" % (p, PROFIT_TARGET, 100.0 * p / PROFIT_TARGET)
                    print(msg)

                # ---- final safety flatten ----
                if sec_left <= FLATTEN_AT_SEC_LEFT:
                    cancel_all(s)
                    if pos:
                        market(s, "SELL" if pos > 0 else "BUY", pos)
                    sleep(LOOP_PACING)
                    continue

                winding_down = sec_left <= WIND_DOWN_AT_SEC_LEFT

                # ---- dynamic half-spread ----
                if len(mid_hist) >= 3:
                    diffs = [abs(mid_hist[i + 1] - mid_hist[i]) for i in range(len(mid_hist) - 1)]
                    vol_ticks = (sum(diffs) / len(diffs)) / TICK_SIZE
                else:
                    vol_ticks = VOL_PER_TICK / TICK_SIZE
                extra = max(0.0, vol_ticks - VOL_PER_TICK / TICK_SIZE)
                half = min(MAX_HALF_SPREAD, max(BASE_HALF_SPREAD, BASE_HALF_SPREAD + VOL_WIDEN * extra))

                # ---- fair value: mean-revert anchor + inventory skew ----
                rolling_mean = sum(mid_hist) / len(mid_hist)
                anchor = mid + MEAN_REVERT_WEIGHT * (rolling_mean - mid)
                inv = max(-1.0, min(1.0, pos / float(EFFECTIVE_LIMIT)))
                fair = anchor - INVENTORY_SKEW * inv * half * TICK_SIZE

                fair_moved = (last_fair is not None and
                              abs(fair - last_fair) >= REPRICE_THRESHOLD_TICKS * TICK_SIZE)

                if last_fair is None or fair_moved or winding_down:
                    # market moved (or late) -> re-quote both sides at the touch
                    cancel_all(s)
                    room_buy = max(0, EFFECTIVE_LIMIT - pos)
                    room_sell = max(0, EFFECTIVE_LIMIT + pos)
                    if winding_down:
                        if pos > 0:
                            room_buy = 0
                        elif pos < 0:
                            room_sell = 0
                    for layer in range(NUM_LAYERS):
                        bid_price, ask_price = quote_prices(fair, half, best_bid, best_ask, layer)
                        if bid_price >= ask_price:
                            continue
                        place(s, "BUY", bid_price, min(ORDER_SIZE, room_buy - layer * ORDER_SIZE))
                        place(s, "SELL", ask_price, min(ORDER_SIZE, room_sell - layer * ORDER_SIZE))
                    last_fair = fair
                else:
                    # calm: top up ONLY the filled side, keep the resting exit
                    buys, sells = get_resting_orders(s)
                    bid_price, ask_price = quote_prices(fair, half, best_bid, best_ask, 0)
                    room_buy = EFFECTIVE_LIMIT - pos - sum(buys)
                    room_sell = EFFECTIVE_LIMIT + pos - sum(sells)
                    if bid_price < ask_price:
                        for _ in range(NUM_LAYERS - len(buys)):
                            q = min(ORDER_SIZE, room_buy)
                            if q <= 0:
                                break
                            place(s, "BUY", bid_price, q)
                            room_buy -= q
                        for _ in range(NUM_LAYERS - len(sells)):
                            q = min(ORDER_SIZE, room_sell)
                            if q <= 0:
                                break
                            place(s, "SELL", ask_price, q)
                            room_sell -= q

                sleep(LOOP_PACING)

        except KeyboardInterrupt:
            print("\nStop requested: cancelling orders and flattening.")
        except requests.exceptions.RequestException as e:
            print("Network/API error: %s" % e)
        finally:
            try:
                cancel_all(s)
                pos = get_security(s)["position"]
                if pos:
                    market(s, "SELL" if pos > 0 else "BUY", pos)
                print("Done: orders cancelled, position flattened.")
            except Exception as e:
                print("Cleanup error: %s" % e)


if __name__ == "__main__":
    main()
