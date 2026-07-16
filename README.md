# RIT ALGO2 — Optimized Algorithmic Market Making

An upgraded bot for the Rotman Interactive Trader (RIT) **ALGO2 – Algorithmic
Market Making** case, plus a data-cruncher that tunes it from the market-data
export. Built to out-trade a classroom full of students running the identical
base script.

> **Context / ethics:** This is a *classroom simulation*. "Front-running" here
> means posting smarter, faster quotes than classmates who run the default code
> inside a game. It has nothing to do with real-market front-running (trading
> ahead of client orders on private information), which is illegal. Everything
> below is legitimate competitive optimization inside the RIT sandbox.

---

## Files

| File | What it does |
|------|--------------|
| `analyze_market_data.py` | Reads your `algo 2 market data.xlsx`, extracts the statistical patterns (tick size, spread, volatility, mean-reversion, order flow) and prints a tuned config block. Run this **first**, locally. |
| `rit_market_maker.py` | The optimized live bot. Paste the analyzer's config into it, set your API key, run it against the RIT client. |

---

## Quick start

```bash
pip install requests pandas numpy openpyxl

# 1) Crunch the data (do this where the Excel file lives)
python analyze_market_data.py "algo 2 market data (1).xlsx"

# 2) Paste the printed RECOMMENDED CONFIG into rit_market_maker.py -> class Config
#    Set API_KEY, confirm TICKER / ORDER_SIZE / MAX_POSITION from the Case Brief.

# 3) With the RIT client running and the case ACTIVE:
python rit_market_maker.py
```

---

## Why this beats the default script

The base script most students run quotes a fixed spread around the mid and
re-posts every loop. Every copy of it therefore sits at the **same price levels
with the same size**, and they all fight over the same fills while accumulating
uncontrolled inventory. The edges below attack exactly those weaknesses.

### 1. Inventory skewing (the biggest single win)
Instead of quoting symmetrically around the mid, the bot shifts its *fair value*
against its current position:

```
inv  = position / MAX_POSITION            # -1 .. +1
fair = mid - INVENTORY_SKEW * inv * half_spread
```

Long inventory → both quotes drift down, so your ask gets hit and your bid
doesn't → inventory mean-reverts to zero on its own. This slashes the
adverse-selection losses that blow up the default bot when the price trends, and
lets you keep quoting instead of hitting the position limit and going flat.

### 2. Queue jumping (the "front-run")
On the front layer the bot posts **one tick inside** the current best bid/ask
(`best_bid + tick`, `best_ask - tick`) whenever that price is still profitable
relative to fair value. Because everyone running the default sits *at* the
top-of-book, you win **time priority** and get filled first. It never crosses the
spread and never quotes through fair value, so it stays a market-*making* edge,
not an aggressive one.

### 3. Dynamic spread
`recent volatility` is measured live from the mid history. When it spikes
(news, tenders) the half-spread widens automatically to avoid getting run over;
when the tape is calm it tightens to capture more flow. The default's fixed
spread is either too tight in volatility (bleeds) or too wide when calm (misses
rebate).

### 4. Order laddering
Posts `NUM_LAYERS` price levels per side instead of one, capturing more of the
queue and more passive rebate without over-committing at a single price.

### 5. Sticky orders
Re-posting sends you to the **back** of the price-time queue. The bot only
cancels and reposts when fair value has moved past `REPRICE_THRESHOLD_TICKS`,
so it holds its queue priority through noise — a subtle but real fill-rate edge.

### 6. Hard risk control
Respects the gross position limit on both sides and **flattens inventory** in the
last `FLATTEN_AT_TICKS_LEFT` ticks so you don't eat close-out costs or a penalty
at the buzzer.

### 7. Rate-limit awareness
One tight loop, minimal API calls, and a small pacing sleep so RIT doesn't
throttle or reject your orders — being *fast* is itself an edge when others spam
the API and get rate-limited.

---

## Tuning cheat-sheet

The analyzer tells you which regime you're in; tune accordingly:

| Analyzer says | Meaning | What to do |
|---------------|---------|-----------|
| Strong mean-reversion (short AR half-life) | Price snaps back | MM is very profitable. `INVENTORY_SKEW ≈ 0.3–0.4`, tighter spread — you can afford to hold inventory. |
| Trending / random walk | Price drifts | Inventory is dangerous. `INVENTORY_SKEW ≈ 0.6–0.8`, wider spread, smaller size. |
| High per-tick vol | Choppy | Raise `BASE_HALF_SPREAD` and `VOL_WIDEN`. |
| Thin volume | Slow fills | Fewer layers, lean harder on queue jumping. |

---

## ⚠️ Must-confirm before you trade

These vary per case and are **not** guessable from the data — read them off your
**Case Brief PDF** and set them in `Config`:

- `TICKER` — the security symbol.
- `ORDER_SIZE` and the **max order size** allowed per order.
- `MAX_POSITION` — the gross (and net) position limit.
- `TICK_SIZE` — usually 0.01 (the analyzer infers it; confirm anyway).
- Whether the case pays a **rebate** for passive fills (changes how aggressively
  you should quote — more rebate → quote tighter/more).
- The RIT REST endpoint/port (default `http://localhost:9999/v1`).

The RIT REST API field/endpoint names (`/case`, `/securities`, `/orders`,
`/commands/cancel`, `/limits`) follow the standard v1 API; if your build differs,
adjust the thin wrapper in the `RIT` class only.
