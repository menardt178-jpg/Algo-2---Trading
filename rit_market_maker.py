"""
RIT ALGO2 - Algorithmic Market Making - OPTIMIZED
=================================================

An upgraded market-making bot for the Rotman Interactive Trader (RIT) ALGO2 case.
It keeps the reliable structure of the teacher's base script but adds the edges
that top students use to out-trade a room full of identical bots:

  1. INVENTORY SKEWING   - shifts quotes off the mid based on current position so
                           inventory mean-reverts toward zero on its own. This is
                           the single biggest P&L improvement and the main reason
                           tuned bots beat the default.
  2. QUEUE JUMPING        - posts one tick INSIDE the crowd (best_bid+tick /
     ("front-running"       best_ask-tick) when that price is still profitable, so
      the classroom)        you win time priority and get filled before students
                           quoting at the stale top-of-book. Pure sim edge.
  3. DYNAMIC SPREAD       - widens automatically when short-term volatility spikes
                           (news/tenders) to avoid adverse selection; tightens when
                           calm to capture more flow.
  4. ORDER LADDERING      - posts several layers to capture more of the queue and
                           earn more rebate without over-committing at one price.
  5. STICKY ORDERS        - only cancels/reposts when the fair price actually moved
                           past a threshold, preserving queue priority (re-posting
                           sends you to the back of the line).
  6. HARD RISK LIMITS     - respects gross/net position limits and flattens
                           inventory before the period ends to avoid close-out risk.
  7. RATE-LIMIT AWARE     - one tight loop, minimal API calls, small pacing sleep.

------------------------------------------------------------------------------
BEFORE YOU RUN
------------------------------------------------------------------------------
* Run analyze_market_data.py on the Excel export first, then paste the printed
  RECOMMENDED CONFIG values below.
* Set API_KEY (RIT client -> menu -> API key).
* CONFIRM these against your Case Brief PDF, because they vary per case:
     TICKER, ORDER_SIZE, MAX_POSITION (gross limit), TICK_SIZE, and whether the
     case pays a REBATE for passive fills.
* Requires:  pip install requests
------------------------------------------------------------------------------
"""

import signal
import time
from collections import deque

import requests


# ============================== CONFIG =====================================
class Config:
    # --- connection ---
    API_KEY = "REPLACE_WITH_YOUR_RIT_API_KEY"
    HOST = "http://localhost:9999/v1"

    # --- instrument (CONFIRM against the case brief) ---
    TICKER = "ALGO"
    TICK_SIZE = 0.01           # smallest price increment (analyze_market_data.py infers this)

    # --- sizing & risk ---
    ORDER_SIZE = 100           # shares per order layer
    NUM_LAYERS = 2             # ladder depth on each side
    MAX_POSITION = 5000        # gross position limit from the case brief
    LAYER_STEP_TICKS = 1       # ticks between ladder layers

    # --- quoting ---
    BASE_HALF_SPREAD = 2       # ticks each side of fair value (from analyzer)
    VOL_PER_TICK = 0.02        # measured 1-tick move (from analyzer); drives dynamic spread
    VOL_WIDEN = 1.5            # extra half-spread ticks per 1 std of recent vol
    MAX_HALF_SPREAD = 8        # cap so we always quote something
    QUEUE_JUMP = True          # post one tick inside the crowd to win priority

    # --- inventory management ---
    INVENTORY_SKEW = 0.5       # 0..1; how hard to lean quotes against your position
    FLATTEN_AT_TICKS_LEFT = 8  # start unwinding when this many ticks remain in the period

    # --- order maintenance ---
    REPRICE_THRESHOLD_TICKS = 1  # only cancel/repost if fair value moved > this many ticks
    LOOP_PACING = 0.10           # seconds between iterations (respect RIT rate limits)
    VOL_LOOKBACK = 20            # mid observations used for the vol estimate


# ============================ API WRAPPER ==================================
class RIT:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update({"X-API-Key": cfg.API_KEY})

    def _get(self, path, **params):
        r = self.s.get(f"{self.cfg.HOST}{path}", params=params, timeout=5)
        r.raise_for_status()
        return r.json()

    def case(self):
        """Returns (status, tick, ticks_per_period)."""
        j = self._get("/case")
        return j["status"], j["tick"], j["ticks_per_period"]

    def security(self, ticker):
        j = self._get("/securities", ticker=ticker)
        return j[0]

    def open_orders(self, ticker):
        return self._get("/orders", status="OPEN")

    def limits(self):
        try:
            return self._get("/limits")
        except Exception:
            return None

    def place(self, action, price, qty):
        r = self.s.post(
            f"{self.cfg.HOST}/orders",
            params={
                "ticker": self.cfg.TICKER,
                "type": "LIMIT",
                "quantity": qty,
                "action": action,          # BUY / SELL
                "price": round(price, 4),
            },
            timeout=5,
        )
        return r.status_code == 200

    def market(self, action, qty):
        self.s.post(
            f"{self.cfg.HOST}/orders",
            params={"ticker": self.cfg.TICKER, "type": "MARKET",
                    "quantity": qty, "action": action},
            timeout=5,
        )

    def cancel_all(self):
        self.s.post(f"{self.cfg.HOST}/commands/cancel",
                    params={"ticker": self.cfg.TICKER}, timeout=5)


# ========================= MARKET-MAKING LOGIC =============================
class MarketMaker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = RIT(cfg)
        self.mid_hist = deque(maxlen=cfg.VOL_LOOKBACK)
        self.last_fair = None
        self.running = True
        signal.signal(signal.SIGINT, self._stop)

    def _stop(self, *_):
        self.running = False

    # --- helpers ---------------------------------------------------------
    def recent_vol_ticks(self):
        """Std of recent 1-tick mid changes, expressed in ticks."""
        if len(self.mid_hist) < 3:
            return self.cfg.VOL_PER_TICK / self.cfg.TICK_SIZE
        arr = list(self.mid_hist)
        diffs = [abs(arr[i + 1] - arr[i]) for i in range(len(arr) - 1)]
        mean = sum(diffs) / len(diffs)
        return mean / self.cfg.TICK_SIZE

    def dynamic_half_spread(self):
        c = self.cfg
        extra = max(0.0, self.recent_vol_ticks() - c.VOL_PER_TICK / c.TICK_SIZE)
        hs = c.BASE_HALF_SPREAD + c.VOL_WIDEN * extra
        return min(c.MAX_HALF_SPREAD, max(c.BASE_HALF_SPREAD, hs))

    def round_tick(self, price):
        t = self.cfg.TICK_SIZE
        return round(round(price / t) * t, 4)

    def flatten(self, position):
        """Aggressively unwind before the period closes."""
        if position == 0:
            return
        action = "SELL" if position > 0 else "BUY"
        self.api.cancel_all()
        self.api.market(action, abs(position))

    # --- main loop -------------------------------------------------------
    def run(self):
        c = self.cfg
        print(f"Market maker started on {c.TICKER}. Ctrl-C to stop.")
        while self.running:
            try:
                status, tick, tpp = self.api.case()
                if status != "ACTIVE":
                    time.sleep(0.5)
                    continue

                sec = self.api.security(c.TICKER)
                pos = sec["position"]
                best_bid, best_ask = sec["bid"], sec["ask"]
                if not best_bid or not best_ask:
                    time.sleep(c.LOOP_PACING)
                    continue

                mid = (best_bid + best_ask) / 2.0
                self.mid_hist.append(mid)

                # ---- end-of-period risk unwind ----
                if tpp - tick <= c.FLATTEN_AT_TICKS_LEFT:
                    self.flatten(pos)
                    time.sleep(c.LOOP_PACING)
                    continue

                # ---- fair value with inventory skew ----
                # Shift our reservation price against the position so quotes lean
                # toward flattening. inv in [-1, 1].
                inv = max(-1.0, min(1.0, pos / c.MAX_POSITION))
                half = self.dynamic_half_spread()
                skew_ticks = c.INVENTORY_SKEW * inv * half
                fair = mid - skew_ticks * c.TICK_SIZE

                # ---- sticky orders: only reprice on meaningful moves ----
                if (self.last_fair is not None and
                        abs(fair - self.last_fair) < c.REPRICE_THRESHOLD_TICKS * c.TICK_SIZE):
                    time.sleep(c.LOOP_PACING)
                    continue
                self.last_fair = fair

                self.api.cancel_all()

                # ---- position-aware sizing ----
                can_buy = pos < c.MAX_POSITION
                can_sell = pos > -c.MAX_POSITION

                # ---- build & post the ladder ----
                for layer in range(c.NUM_LAYERS):
                    offset = (half + layer * c.LAYER_STEP_TICKS) * c.TICK_SIZE

                    bid_price = self.round_tick(fair - offset)
                    ask_price = self.round_tick(fair + offset)

                    # QUEUE JUMP: on the front layer, step inside the crowd to win
                    # priority, but never cross and never quote through fair value.
                    if c.QUEUE_JUMP and layer == 0:
                        jump_bid = self.round_tick(best_bid + c.TICK_SIZE)
                        jump_ask = self.round_tick(best_ask - c.TICK_SIZE)
                        if jump_bid < fair and jump_bid > bid_price:
                            bid_price = jump_bid
                        if jump_ask > fair and jump_ask < ask_price:
                            ask_price = jump_ask

                    if bid_price >= ask_price:  # safety: never cross ourselves
                        continue
                    if can_buy:
                        self.api.place("BUY", bid_price, c.ORDER_SIZE)
                    if can_sell:
                        self.api.place("SELL", ask_price, c.ORDER_SIZE)

                time.sleep(c.LOOP_PACING)

            except requests.exceptions.RequestException as e:
                print(f"API error: {e}; retrying...")
                time.sleep(1.0)
            except Exception as e:
                print(f"Unexpected error: {e}")
                time.sleep(0.5)

        print("Stopping: cancelling orders and flattening.")
        try:
            self.api.cancel_all()
            sec = self.api.security(c.TICKER)
            self.flatten(sec["position"])
        except Exception:
            pass


if __name__ == "__main__":
    MarketMaker(Config()).run()
