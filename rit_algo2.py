# -*- coding: utf-8 -*-
"""
RIT ALGO2 - Algorithmic Market Making - OPTIMIZED (v3)
======================================================
Reverts to the v1 economics that scored ~8.8k, and fixes the "stops trading
after ~100s" idle bug WITHOUT destroying spread capture. Pure requests + stdlib,
runs in Spyder 6.

WHAT WENT WRONG IN v2 (broke even):
  v2 reposted on every fill with cancel_all, which cancelled the resting exit
  order (the one about to catch the rebound and lock in the spread) and
  re-centered both quotes on the dipped price. Result: buy low, sell not-high =
  spread thrown away, only the thin rebate left. Tighter quoting made it worse.

THE CORRECT FIX (v3):
  * Keep v1's wider quoting so each round trip captures real spread + rebate.
  * When the price MOVES past a threshold: reprice both sides (v1 behaviour).
  * When the market is CALM (price frozen) and a fill happened: DON'T cancel the
    resting exit. Only TOP UP the missing side with a fresh order. This keeps you
    trading through the calm second half while preserving the resting order that
    captures the spread on the other side.

CASE FACTS: ALGO, $0.01 tick, 300s, 5,000/order, 25,000 limit, 1c commission
(active), 0.5c rebate (passive fills), 10c/share fine over 25,000.
"""

from time import sleep, monotonic
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

# ---- Quoting (v1 values that made ~8.8k) ----
BASE_HALF_SPREAD = 2                    # ticks each side -> real spread capture
VOL_WIDEN = 1.5
VOL_PER_TICK = 0.02
MAX_HALF_SPREAD = 8
QUEUE_JUMP = True

# ---- Mean-reversion (v1 value) ----
MEAN_REVERT_WEIGHT = 0.25              # SET TO 0 IF A PATH TRENDS
MEAN_LOOKBACK = 30

# ---- Inventory / timing (300s heat) ----
INVENTORY_SKEW = 0.5
WIND_DOWN_AT_SEC_LEFT = 30
FLATTEN_AT_SEC_LEFT = 8

# ---- Loop ----
REPRICE_THRESHOLD_TICKS = 1            # reprice both sides when fair moves this
LOOP_PACING = 0.08
VOL_LOOKBACK = 20
PROFIT_TARGET = 10000
PNL_PRINT_EVERY = 30
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
    """Return (buy_qtys, sell_qtys) = remaining size of our open orders per side."""
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
    offset = (half + layer) * TICK_SIZE
    bid = round_tick(fair - offset)
    ask = round_tick(fair + offset)
    if QUEUE_JUMP and layer == 0:                 # rest 1 tick inside the crowd
        jb = round_tick(best_bid + TICK_SIZE)
        ja = round_tick(best_ask - TICK_SIZE)
        if bid < jb < fair:
            bid = jb
        if fair < ja < ask:
            ask = ja
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
        print("Connected. ALGO2 MM v3 (limit %d, target $%d). Press STOP to halt."
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
                    # ---- FULL REPRICE: market moved (or late) -> re-quote both sides ----
                    cancel_all(s)
                    room_buy = max(0, EFFECTIVE_LIMIT - pos)
                    room_sell = max(0, EFFECTIVE_LIMIT + pos)
                    if winding_down:                 # only reduce inventory late
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
                    # ---- CALM MARKET: top up ONLY the filled side, keep the ----
                    # ---- resting exit order so it still captures the spread. ----
                    buys, sells = get_resting_orders(s)
                    bid_price, ask_price = quote_prices(fair, half, best_bid, best_ask, 0)
                    # account for existing resting size so we never breach the limit
                    room_buy = EFFECTIVE_LIMIT - pos - sum(buys)
                    room_sell = EFFECTIVE_LIMIT + pos - sum(sells)
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
