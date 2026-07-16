# -*- coding: utf-8 -*-
"""
RIT ALGO2 - Algorithmic Market Making - OPTIMIZED (trading-only, Spyder-ready)
=============================================================================

Pure `requests` + standard library. No pandas, no numpy, no MODE toggle.
Same structure as the standard RIT teacher template, so it runs in Spyder 6.

HOW TO RUN
  1. Paste your key into API_KEY below (RIT client -> Settings -> API key).
  2. Confirm TICKER / ORDER_SIZE / MAX_POSITION against your Case Brief PDF.
  3. Open the RIT client, load the ALGO2 case, and press play so it is ACTIVE.
  4. In Spyder press the green Run button. Press the red Stop button to halt
     (it cancels all orders and flattens your position on the way out).

If it still won't run, read the FIRST error line in the Spyder console:
  * "Failed to establish a new connection" / ConnectionError
        -> the RIT client isn't running, or REST API isn't enabled, or the
           port isn't 9999. Fix HOST below.
  * 401 / "Authorization" / ApiException on startup
        -> wrong API_KEY.
  * "No module named 'requests'"
        -> in the Spyder console run:  pip install requests
"""

from time import sleep
import requests

# ============================== CONFIG ====================================
API_KEY = "REPLACE_WITH_YOUR_RIT_API_KEY"
HOST = "http://localhost:9999/v1"      # change the port if your RIT differs

TICKER = "ALGO"                        # <-- confirm in the Case Brief
TICK_SIZE = 0.01                       # smallest price increment

ORDER_SIZE = 100                       # shares per order layer
NUM_LAYERS = 2                         # ladder depth per side
MAX_POSITION = 5000                    # gross position limit from the brief
LAYER_STEP_TICKS = 1                   # ticks between ladder layers

BASE_HALF_SPREAD = 2                   # ticks each side of fair value
VOL_PER_TICK = 0.02                    # typical 1-tick move (from the analyzer)
VOL_WIDEN = 1.5                        # extra half-spread per 1 std of recent vol
MAX_HALF_SPREAD = 8                    # cap so we always quote
QUEUE_JUMP = True                      # post one tick inside the crowd

INVENTORY_SKEW = 0.5                   # 0..1; lean quotes against your position
FLATTEN_AT_TICKS_LEFT = 8             # unwind when this many ticks remain

REPRICE_THRESHOLD_TICKS = 1            # only reprice when fair value moves > this
LOOP_PACING = 0.10                     # seconds between iterations
VOL_LOOKBACK = 20                      # mid samples used for the vol estimate
# ==========================================================================


class ApiException(Exception):
    pass


def get_case(session):
    resp = session.get(HOST + "/case")
    if resp.ok:
        c = resp.json()
        return c["status"], c["tick"], c["ticks_per_period"]
    raise ApiException(
        "Cannot read /case (HTTP %s). Check that RIT is running and API_KEY is correct."
        % resp.status_code)


def get_security(session):
    resp = session.get(HOST + "/securities", params={"ticker": TICKER})
    if resp.ok:
        return resp.json()[0]
    raise ApiException("Cannot read /securities for %s (HTTP %s)."
                       % (TICKER, resp.status_code))


def place(session, action, price, qty):
    session.post(HOST + "/orders", params={
        "ticker": TICKER, "type": "LIMIT", "quantity": qty,
        "action": action, "price": round(price, 4)})


def market(session, action, qty):
    session.post(HOST + "/orders", params={
        "ticker": TICKER, "type": "MARKET", "quantity": qty, "action": action})


def cancel_all(session):
    session.post(HOST + "/commands/cancel", params={"ticker": TICKER})


def round_tick(price):
    return round(round(price / TICK_SIZE) * TICK_SIZE, 4)


def flatten(session, position):
    if position:
        cancel_all(session)
        market(session, "SELL" if position > 0 else "BUY", abs(position))


def main():
    mid_hist = []
    last_fair = None

    with requests.Session() as s:
        s.headers.update({"X-API-Key": API_KEY})

        # fail fast with a clear message if key/connection are wrong
        get_case(s)
        print("Connected. Market maker running on %s. Press STOP to halt." % TICKER)

        try:
            while True:
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
                if len(mid_hist) > VOL_LOOKBACK:
                    mid_hist.pop(0)

                # end-of-period risk unwind
                if tpp - tick <= FLATTEN_AT_TICKS_LEFT:
                    flatten(s, pos)
                    sleep(LOOP_PACING)
                    continue

                # dynamic half-spread from recent volatility
                if len(mid_hist) >= 3:
                    diffs = [abs(mid_hist[i + 1] - mid_hist[i])
                             for i in range(len(mid_hist) - 1)]
                    vol_ticks = (sum(diffs) / len(diffs)) / TICK_SIZE
                else:
                    vol_ticks = VOL_PER_TICK / TICK_SIZE
                extra = max(0.0, vol_ticks - VOL_PER_TICK / TICK_SIZE)
                half = min(MAX_HALF_SPREAD,
                           max(BASE_HALF_SPREAD, BASE_HALF_SPREAD + VOL_WIDEN * extra))

                # fair value with inventory skew (leans quotes toward flattening)
                inv = max(-1.0, min(1.0, pos / float(MAX_POSITION)))
                fair = mid - INVENTORY_SKEW * inv * half * TICK_SIZE

                # sticky orders: keep queue priority unless fair value really moved
                if (last_fair is not None and
                        abs(fair - last_fair) < REPRICE_THRESHOLD_TICKS * TICK_SIZE):
                    sleep(LOOP_PACING)
                    continue
                last_fair = fair

                cancel_all(s)
                can_buy = pos < MAX_POSITION
                can_sell = pos > -MAX_POSITION

                for layer in range(NUM_LAYERS):
                    offset = (half + layer * LAYER_STEP_TICKS) * TICK_SIZE
                    bid_price = round_tick(fair - offset)
                    ask_price = round_tick(fair + offset)

                    # queue jump: step inside the crowd on the front layer only,
                    # never crossing and never quoting through fair value
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
                        place(s, "BUY", bid_price, ORDER_SIZE)
                    if can_sell:
                        place(s, "SELL", ask_price, ORDER_SIZE)

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
