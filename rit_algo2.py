# -*- coding: utf-8 -*-
"""
RIT ALGO2 - Algorithmic Market Making - AGGRESSIVE (trading-only)
=================================================================
Pure requests + stdlib. Runs directly in Spyder 6. Tuned for a ~300 tick heat
with a large-size, mean-reversion-assisted market-making strategy.

RUN
  1) paste API_KEY
  2) SET SIZE LIMITS TO YOUR CASE MAX (see the CONFIG note): MAX_POSITION and
     ORDER_SIZE are the #1 driver of P&L. Push them to whatever the Case Brief
     allows -- but do not exceed the gross limit or per-order max size or RIT
     will reject the orders.
  3) open RIT, load the case, press play (ACTIVE)
  4) press Run in Spyder. Red Stop cancels orders and flattens.

MAKING 10k IN 300s -- THE MATH
  pnl ~= ticks_per_roundtrip * shares * roundtrips
  e.g. 2 ticks * 5000 shares * ~50 roundtrips = ~$10,000.
  So you need BIG size and a HIGH fill rate. Everything below serves that:
    - large ORDER_SIZE across several layers,
    - MEAN_REVERT anchor => buy dips / sell rips (extra edge in a
      mean-reverting market, which the data analyzer flagged),
    - queue jumping for priority fills,
    - inventory skew so big size doesn't trap you at the position limit.
  WARNING: big size + mean-revert anchor is dangerous if the market TRENDS.
  Run analyze_market_data.py first; if it says TRENDING, cut MEAN_REVERT_WEIGHT
  to ~0.1 and ORDER_SIZE roughly in half.
"""

from time import sleep
import requests

# ============================== CONFIG ====================================
API_KEY = "REPLACE_WITH_YOUR_RIT_API_KEY"
HOST = "http://localhost:9999/v1"

TICKER = "ALGO"                 # <-- confirm in the Case Brief
TICK_SIZE = 0.01

# ---- SIZE (the main P&L lever -- set to your case maximums) ----
ORDER_SIZE = 1000              # shares per layer (must be <= MAX_ORDER_SIZE)
MAX_ORDER_SIZE = 10000         # per-order max size from the brief (CONFIRM); RIT
                               # rejects any single order larger than this
NUM_LAYERS = 3                 # layers per side -> total working = SIZE*LAYERS
MAX_POSITION = 25000           # gross position limit from the brief (CONFIRM)
LAYER_STEP_TICKS = 1

# ---- Quoting ----
BASE_HALF_SPREAD = 2           # ticks each side of fair value (from analyzer)
VOL_PER_TICK = 0.02            # typical 1-tick move (from analyzer)
VOL_WIDEN = 1.5
MAX_HALF_SPREAD = 8
QUEUE_JUMP = True

# ---- Mean-reversion edge (buy dips / sell rips) ----
MEAN_REVERT_WEIGHT = 0.30      # 0..1 pull of fair value toward the rolling mean
MEAN_LOOKBACK = 30             # samples for the rolling mean

# ---- Inventory management ----
INVENTORY_SKEW = 0.5           # 0..1 lean quotes against your position
FLATTEN_AT_TICKS_LEFT = 8

# ---- Loop / P&L ----
REPRICE_THRESHOLD_TICKS = 1
LOOP_PACING = 0.08             # faster loop = more fills (watch rate limits)
VOL_LOOKBACK = 20
PROFIT_TARGET = 10000          # printed progress only
PNL_PRINT_EVERY = 25           # print P&L every N loops
# ==========================================================================


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
    session.post(HOST + "/orders", params={"ticker": TICKER, "type": "LIMIT",
                 "quantity": qty, "action": action, "price": round(price, 4)})


def market(session, action, qty):
    session.post(HOST + "/orders", params={"ticker": TICKER, "type": "MARKET",
                 "quantity": qty, "action": action})


def cancel_all(session):
    session.post(HOST + "/commands/cancel", params={"ticker": TICKER})


def round_tick(price):
    return round(round(price / TICK_SIZE) * TICK_SIZE, 4)


def pnl_of(sec):
    """Best-effort P&L from the security payload (fields vary by RIT build)."""
    r = sec.get("realized")
    u = sec.get("unrealized")
    if r is not None or u is not None:
        return (r or 0.0) + (u or 0.0)
    return None


def flatten(session, position):
    if position:
        cancel_all(session)
        # split into <= per-order-max chunks so a large flatten isn't rejected
        step = MAX_ORDER_SIZE
        side = "SELL" if position > 0 else "BUY"
        remaining = abs(position)
        while remaining > 0:
            q = min(step, remaining)
            market(session, side, q)
            remaining -= q


def main():
    mid_hist = []
    last_fair = None
    loops = 0

    with requests.Session() as s:
        s.headers.update({"X-API-Key": API_KEY})
        get_case(s)  # fail fast if key/connection are wrong
        print("Connected. AGGRESSIVE market maker on %s (target $%d). Press STOP to halt."
              % (TICKER, PROFIT_TARGET))

        try:
            while True:
                loops += 1
                status, tick, tpp = get_case(s)
                if status != "ACTIVE":
                    sleep(0.5)
                    continue

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

                # progress readout
                if loops % PNL_PRINT_EVERY == 0:
                    p = pnl_of(sec)
                    msg = "tick %d/%d  pos %+d" % (tick, tpp, pos)
                    if p is not None:
                        msg += "  P&L $%.0f / %d  (%.0f%%)" % (p, PROFIT_TARGET,
                                                              100.0 * p / PROFIT_TARGET)
                    print(msg)

                # end-of-period risk unwind
                if tpp - tick <= FLATTEN_AT_TICKS_LEFT:
                    flatten(s, pos)
                    sleep(LOOP_PACING)
                    continue

                # dynamic half-spread from recent volatility
                if len(mid_hist) >= 3:
                    diffs = [abs(mid_hist[i + 1] - mid_hist[i]) for i in range(len(mid_hist) - 1)]
                    vol_ticks = (sum(diffs) / len(diffs)) / TICK_SIZE
                else:
                    vol_ticks = VOL_PER_TICK / TICK_SIZE
                extra = max(0.0, vol_ticks - VOL_PER_TICK / TICK_SIZE)
                half = min(MAX_HALF_SPREAD, max(BASE_HALF_SPREAD, BASE_HALF_SPREAD + VOL_WIDEN * extra))

                # mean-reversion anchor: pull fair toward the rolling mean so we
                # naturally quote to BUY below-average prices and SELL above-average
                rolling_mean = sum(mid_hist) / len(mid_hist)
                anchor = mid + MEAN_REVERT_WEIGHT * (rolling_mean - mid)

                # inventory skew on top of the anchor
                inv = max(-1.0, min(1.0, pos / float(MAX_POSITION)))
                fair = anchor - INVENTORY_SKEW * inv * half * TICK_SIZE

                # sticky orders: hold queue priority unless fair really moved
                if (last_fair is not None and
                        abs(fair - last_fair) < REPRICE_THRESHOLD_TICKS * TICK_SIZE):
                    sleep(LOOP_PACING)
                    continue
                last_fair = fair

                cancel_all(s)
                can_buy = pos < MAX_POSITION
                can_sell = pos > -MAX_POSITION
                # taper size as we approach the limit so we never get stuck flat
                room_buy = max(0, MAX_POSITION - pos)
                room_sell = max(0, MAX_POSITION + pos)

                for layer in range(NUM_LAYERS):
                    offset = (half + layer * LAYER_STEP_TICKS) * TICK_SIZE
                    bid_price = round_tick(fair - offset)
                    ask_price = round_tick(fair + offset)

                    if QUEUE_JUMP and layer == 0:
                        jb = round_tick(best_bid + TICK_SIZE)
                        ja = round_tick(best_ask - TICK_SIZE)
                        if bid_price < jb < fair:
                            bid_price = jb
                        if fair < ja < ask_price:
                            ask_price = ja

                    if bid_price >= ask_price:
                        continue

                    buy_q = min(ORDER_SIZE, room_buy - layer * ORDER_SIZE)
                    sell_q = min(ORDER_SIZE, room_sell - layer * ORDER_SIZE)
                    if can_buy and buy_q > 0:
                        place(s, "BUY", bid_price, buy_q)
                    if can_sell and sell_q > 0:
                        place(s, "SELL", ask_price, sell_q)

                sleep(LOOP_PACING)

        except KeyboardInterrupt:
            print("\nStop requested: cancelling orders and flattening.")
        except requests.exceptions.RequestException as e:
            print("Network/API error: %s" % e)
        finally:
            try:
                cancel_all(s)
                flatten(s, get_security(s)["position"])
                print("Done: orders cancelled, position flattened.")
            except Exception as e:
                print("Cleanup error: %s" % e)


if __name__ == "__main__":
    main()
