# -*- coding: utf-8 -*-
"""
RIT ALGO2 - Algorithmic Market Making - OPTIMIZED (v2, fill-aware)
=================================================================
Tuned to the ALGO2 Case Brief (Rotman, Build 1.00) AND to the observed
leaderboard from a real run. Pure requests + stdlib, runs in Spyder 6.

CASE FACTS
  ALGO, $0.01 tick, 300s heat, 5,000 shares/order, 25,000 gross/net limit,
  1c/share commission on MARKET orders (active), 0.5c/share REBATE on LIMIT
  fills (passive), 10c/share FINE for exceeding 25,000.

WHAT THE LEADERBOARD TAUGHT US
  * The winners did 1.3-1.7M shares of volume; ~2/3 of their profit was REBATES,
    not spread. => harvest passive fills, quote tight, stay at the top of book.
  * The best loser-by-effort (Cassy: 2.2M volume, only $742) paid huge
    commissions => NEVER take liquidity. Passive limit orders only.
  * The market was ultra-calm (a ~5c range all heat). => you must keep quoting
    even when the price is frozen, or you go idle.

THE KEY FIX vs v1 ("stops trading after ~100s")
  v1 only reposted when FAIR VALUE MOVED. In a calm market fair stops moving, so
  after your orders filled they were never replaced and the bot went idle. v2 is
  FILL-AWARE: it counts its own open orders every loop and reposts the instant a
  fill is detected (open count < what we placed), independent of price movement,
  plus a 1s heartbeat. This keeps you trading through the calm second half.

EDGES: rebate-first passive quoting, queue jump (rest 1 tick inside the crowd),
inventory skew, hard 23,000 safety cap, wind-down + flatten.
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
POSITION_BUFFER = 2000                 # effective limit 23,000 -> avoids the fine

# ---- Quoting (tight, to maximize fills & rebates in a calm market) ----
BASE_HALF_SPREAD = 1                    # ticks each side; 1 = quote tight
VOL_WIDEN = 1.5                        # widen only when volatility actually spikes
VOL_PER_TICK = 0.01                    # ALGO is calm; measured ~1 tick moves
MAX_HALF_SPREAD = 6
QUEUE_JUMP = True

# ---- Mean-reversion (weak here; price is pinned) ----
MEAN_REVERT_WEIGHT = 0.20              # SET TO 0 IF A PATH TRENDS
MEAN_LOOKBACK = 30

# ---- Inventory / timing (300s heat) ----
INVENTORY_SKEW = 0.5
WIND_DOWN_AT_SEC_LEFT = 25
FLATTEN_AT_SEC_LEFT = 6

# ---- Repost logic (the fix) ----
REPRICE_THRESHOLD_TICKS = 1            # reprice when fair moves this much
REFRESH_MAX_SEC = 1.0                  # heartbeat: repost at least this often
LOOP_PACING = 0.06                     # poll fast to react to fills quickly

# ---- Misc ----
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


def get_open_order_count(session):
    resp = session.get(HOST + "/orders", params={"status": "OPEN"})
    if resp.ok:
        return sum(1 for o in resp.json() if o.get("ticker") == TICKER)
    return 0


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


def pnl_of(sec):
    r = sec.get("realized")
    u = sec.get("unrealized")
    if r is not None or u is not None:
        return (r or 0.0) + (u or 0.0)
    return None


def main():
    mid_hist = []
    last_fair = None
    last_posted = 0
    last_repost_t = 0.0
    loops = 0

    with requests.Session() as s:
        s.headers.update({"X-API-Key": API_KEY})
        get_case(s)  # fail fast on bad key/connection
        print("Connected. ALGO2 MM v2 (limit %d, target $%d). Press STOP to halt."
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

                # ---- final safety flatten (accept the 1c market cost) ----
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

                # ---- THE FIX: repost if filled, if fair moved, or on heartbeat ----
                n_open = get_open_order_count(s)
                now = monotonic()
                need_repost = (
                    last_fair is None
                    or n_open < last_posted                      # a fill happened
                    or abs(fair - last_fair) >= REPRICE_THRESHOLD_TICKS * TICK_SIZE
                    or (now - last_repost_t) >= REFRESH_MAX_SEC   # heartbeat
                    or winding_down
                )
                if not need_repost:
                    sleep(LOOP_PACING)
                    continue

                cancel_all(s)

                room_buy = max(0, EFFECTIVE_LIMIT - pos)
                room_sell = max(0, EFFECTIVE_LIMIT + pos)
                if winding_down:                     # only reduce inventory late
                    if pos > 0:
                        room_buy = 0
                    elif pos < 0:
                        room_sell = 0

                posted = 0
                for layer in range(NUM_LAYERS):
                    offset = (half + layer) * TICK_SIZE
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
                    posted += place(s, "BUY", bid_price, min(ORDER_SIZE, room_buy - layer * ORDER_SIZE))
                    posted += place(s, "SELL", ask_price, min(ORDER_SIZE, room_sell - layer * ORDER_SIZE))

                last_fair = fair
                last_posted = posted
                last_repost_t = now
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
