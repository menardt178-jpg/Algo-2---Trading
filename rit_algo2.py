# -*- coding: utf-8 -*-
"""
RIT ALGO2 - Algorithmic Market Making - OPTIMIZED FOR THE REAL CASE
===================================================================
Tuned to the ALGO2 Case Brief (Rotman, Build 1.00). Pure requests + stdlib,
runs directly in Spyder 6.

CASE FACTS (baked into the config below):
  * one stock: ALGO,  tick size $0.01,  heat = 300 seconds
  * 5,000 shares MAX per order
  * 25,000 shares gross/net position limit
  * MARKET orders (active) cost 1 cent/share commission
  * LIMIT orders (passive) that fill EARN a 1/2 cent/share REBATE
  * FINE of 10 cents/share for EXCEEDING the 25,000 limit  <-- never touch this

WHY THIS BEATS THE TEACHER'S BASE CODE
  The base algo (case solution) rests one bid at LAST-Spread and one ask at
  LAST+Spread and just keeps 2 orders alive. Every student runs the same thing,
  so they all sit at the same stale levels. The edges here:
    1. REBATE-FIRST: quote passively on BOTH sides every cycle so you harvest
       the 1/2c rebate on every fill. Two passive fills = +1c/share BEFORE any
       spread. We NEVER send market orders except the final safety flatten,
       because a market order turns that +1c into -1c.
    2. QUEUE JUMP: rest one tick INSIDE the crowd (still passive, still earns
       the rebate) so your order fills before the students quoting at the top of
       book. This is the "front-run" -- pure priority, no crossing.
    3. INVENTORY SKEW + WIND-DOWN: lean quotes against your position and, in the
       last seconds, only quote the side that REDUCES inventory, so you never
       ride a trend into the 25,000 fine and you finish near flat.
    4. SPEED: a Python REST loop reprices far faster than an Excel F9 / RTD loop.

TARGET: ~$10k in 300s. That needs volume: capture ~a few cents + 1c rebate per
round trip on ~5,000 shares (~$150-250/round trip) about 40-60 times. Achievable
in an active, range-bound market; hard if it trends (see MEAN_REVERT_WEIGHT).
"""

from time import sleep
import requests

# ============================== CONFIG ====================================
API_KEY = "REPLACE_WITH_YOUR_RIT_API_KEY"
HOST = "http://localhost:9999/v1"

TICKER = "ALGO"
TICK_SIZE = 0.01

# ---- SIZE (case: 5,000/order, 25,000 position limit) ----
ORDER_SIZE = 5000              # per layer; case max per order is 5,000
NUM_LAYERS = 2                 # layers per side
MAX_POSITION = 25000           # HARD case limit
POSITION_BUFFER = 2000         # stay this far under the limit -> effective 23,000
                               # (avoids the 10c/share fine on fill-timing races)

# ---- Quoting ----
BASE_HALF_SPREAD = 2           # ticks each side of fair value
VOL_WIDEN = 1.5               # extra half-spread ticks per 1 std of recent vol
VOL_PER_TICK = 0.02            # typical 1-tick move (tune from the Excel data)
MAX_HALF_SPREAD = 8
QUEUE_JUMP = True              # rest one tick inside the crowd (stays passive)

# ---- Mean-reversion edge (buy dips / sell rips) ----
MEAN_REVERT_WEIGHT = 0.25      # 0..1; SET TO 0 IF THE MARKET TRENDS
MEAN_LOOKBACK = 30

# ---- Inventory management / timing (300s heat) ----
INVENTORY_SKEW = 0.5           # 0..1 lean quotes against your position
WIND_DOWN_AT_SEC_LEFT = 30     # last 30s: only quote the inventory-reducing side
FLATTEN_AT_SEC_LEFT = 8        # last 8s: market-flatten any residual (accept 1c)

# ---- Loop / P&L ----
REPRICE_THRESHOLD_TICKS = 1
LOOP_PACING = 0.08
VOL_LOOKBACK = 20
PROFIT_TARGET = 10000
PNL_PRINT_EVERY = 25
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


def place(session, action, price, qty):
    if qty <= 0:
        return
    session.post(HOST + "/orders", params={"ticker": TICKER, "type": "LIMIT",
                 "quantity": min(qty, ORDER_SIZE), "action": action, "price": round(price, 4)})


def market(session, action, qty):
    # only used for the final safety flatten; chunked to the 5,000 per-order max
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
        print("Connected. ALGO2 market maker (limit %d, target $%d). Press STOP to halt."
              % (EFFECTIVE_LIMIT, PROFIT_TARGET))

        try:
            while True:
                loops += 1
                status, tick, tpp = get_case(s)
                if status != "ACTIVE":
                    sleep(0.5)
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

                # ---- final safety flatten (accept the 1c market cost) ----
                if sec_left <= FLATTEN_AT_SEC_LEFT:
                    cancel_all(s)
                    if pos:
                        market(s, "SELL" if pos > 0 else "BUY", pos)
                    sleep(LOOP_PACING)
                    continue

                winding_down = sec_left <= WIND_DOWN_AT_SEC_LEFT

                # ---- dynamic half-spread from recent volatility ----
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

                # ---- sticky orders: hold queue priority unless fair moved ----
                if (last_fair is not None and not winding_down and
                        abs(fair - last_fair) < REPRICE_THRESHOLD_TICKS * TICK_SIZE):
                    sleep(LOOP_PACING)
                    continue
                last_fair = fair

                cancel_all(s)

                # room to the EFFECTIVE limit on each side (never hit the fine)
                room_buy = max(0, EFFECTIVE_LIMIT - pos)
                room_sell = max(0, EFFECTIVE_LIMIT + pos)
                # wind-down: only quote the side that reduces inventory
                if winding_down:
                    if pos > 0:
                        room_buy = 0
                    elif pos < 0:
                        room_sell = 0

                for layer in range(NUM_LAYERS):
                    offset = (half + layer) * TICK_SIZE
                    bid_price = round_tick(fair - offset)
                    ask_price = round_tick(fair + offset)

                    # QUEUE JUMP on the front layer: one tick inside the crowd,
                    # never crossing and never through fair (stays passive).
                    if QUEUE_JUMP and layer == 0:
                        jb = round_tick(best_bid + TICK_SIZE)
                        ja = round_tick(best_ask - TICK_SIZE)
                        if bid_price < jb < fair:
                            bid_price = jb
                        if fair < ja < ask_price:
                            ask_price = ja

                    if bid_price >= ask_price:
                        continue
                    place(s, "BUY", bid_price, min(ORDER_SIZE, room_buy - layer * ORDER_SIZE))
                    place(s, "SELL", ask_price, min(ORDER_SIZE, room_sell - layer * ORDER_SIZE))

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
