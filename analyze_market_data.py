"""
analyze_market_data.py
======================
Crunches the RIT ALGO2 market-data export ("algo 2 market data.xlsx") to extract
the statistical patterns that determine how a market-making bot should be tuned.

Run this LOCALLY (where the Excel file is), then copy the printed
"RECOMMENDED CONFIG" block into rit_market_maker.py.

Usage:
    pip install pandas numpy openpyxl
    python analyze_market_data.py "algo 2 market data (1).xlsx"

What it measures and WHY it matters for market making
-----------------------------------------------------
1. Tick size ................ smallest price increment -> your quoting grid.
2. Spread distribution ...... how wide the market usually is -> your base spread
                              and how much edge is available to capture.
3. Per-tick volatility ...... how far mid moves each tick -> adverse-selection
                              risk. High vol => quote wider / skew harder.
4. Mean reversion (AR1) ..... does price snap back or trend? Strong mean
                              reversion => market making is very profitable and
                              you can lean into inventory. Trending => dangerous,
                              keep inventory tight.
5. Return autocorrelation ... momentum vs. noise in the order flow.
6. Volume / order-flow ...... how fast you'll get filled -> order size & layers.

The script auto-detects columns; if your sheet uses different names, edit the
COLUMN_HINTS below.
"""

import sys
import numpy as np
import pandas as pd

COLUMN_HINTS = {
    "time":  ["time", "tick", "timestamp", "period", "t"],
    "bid":   ["bid", "best_bid", "bidprice"],
    "ask":   ["ask", "best_ask", "askprice", "offer"],
    "last":  ["last", "price", "close", "trade", "mid", "vwap"],
    "volume": ["volume", "vol", "size", "qty", "quantity"],
}


def find_col(cols, hints):
    lower = {c.lower().strip(): c for c in cols}
    # exact-ish match first
    for h in hints:
        for lc, orig in lower.items():
            if lc == h:
                return orig
    # substring match
    for h in hints:
        for lc, orig in lower.items():
            if h in lc:
                return orig
    return None


def infer_tick_size(prices):
    """Smallest nonzero gap between consecutive distinct sorted price levels."""
    p = np.round(np.asarray(prices, dtype=float), 6)
    p = p[~np.isnan(p)]
    diffs = np.diff(np.unique(p))
    diffs = diffs[diffs > 1e-9]
    if len(diffs) == 0:
        return 0.01
    # robust: the mode of small gaps is usually the tick
    small = diffs[diffs <= np.percentile(diffs, 25)]
    return float(np.round(np.median(small if len(small) else diffs), 4))


def ar1(series):
    """Fit x_{t+1} = a + b*x_t. Returns (b, half_life_in_ticks).
    b < 1 => mean reverting. half_life = ln(2)/(-ln(b))."""
    x = np.asarray(series, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) < 10:
        return np.nan, np.nan
    x0, x1 = x[:-1], x[1:]
    b, a = np.polyfit(x0, x1, 1)
    if 0 < b < 1:
        hl = np.log(2) / (-np.log(b))
    else:
        hl = np.inf
    return float(b), float(hl)


def main(path):
    print(f"Loading {path} ...")
    # try every sheet, keep the widest/most-numeric one
    xls = pd.ExcelFile(path)
    best = None
    for sh in xls.sheet_names:
        df = xls.parse(sh)
        score = df.select_dtypes("number").shape[1] * len(df)
        if best is None or score > best[0]:
            best = (score, sh, df)
    _, sheet, df = best
    print(f"Using sheet: {sheet!r}  shape={df.shape}\n")

    cols = list(df.columns)
    c = {k: find_col(cols, h) for k, h in COLUMN_HINTS.items()}
    print("Detected columns:")
    for k, v in c.items():
        print(f"   {k:8s} -> {v}")
    print()

    # Build a mid-price series
    if c["bid"] and c["ask"]:
        bid = pd.to_numeric(df[c["bid"]], errors="coerce")
        ask = pd.to_numeric(df[c["ask"]], errors="coerce")
        mid = (bid + ask) / 2.0
        spread = (ask - bid)
    elif c["last"]:
        mid = pd.to_numeric(df[c["last"]], errors="coerce")
        bid = ask = None
        spread = None
    else:
        # fall back to first numeric column
        num = df.select_dtypes("number")
        mid = num.iloc[:, 0]
        bid = ask = spread = None
        print("!! Could not detect bid/ask/last; using first numeric column.")

    mid = mid.dropna().reset_index(drop=True)
    prices = mid.values
    rets = np.diff(prices)

    tick = infer_tick_size(prices if bid is None else
                           np.concatenate([bid.dropna().values, ask.dropna().values]))

    # --- statistics -------------------------------------------------------
    print("=" * 60)
    print("MARKET STATISTICS")
    print("=" * 60)
    print(f"Observations ............ {len(prices)}")
    print(f"Price range ............. {np.nanmin(prices):.3f} - {np.nanmax(prices):.3f}")
    print(f"Mean price .............. {np.nanmean(prices):.3f}")
    print(f"Inferred tick size ...... {tick:.4f}")

    per_tick_vol = np.nanstd(rets)
    print(f"Per-tick vol (std dMid).. {per_tick_vol:.4f}  ({per_tick_vol/tick:.2f} ticks)")

    if spread is not None:
        sp = spread.dropna()
        print(f"Spread  mean/median/90% . "
              f"{sp.mean():.4f} / {sp.median():.4f} / {sp.quantile(.9):.4f}  "
              f"({sp.median()/tick:.1f} ticks median)")

    b, hl = ar1(prices)
    print(f"AR(1) coef on price ..... {b:.4f}   half-life = "
          f"{hl:.1f} ticks" if np.isfinite(hl) else
          f"AR(1) coef on price ..... {b:.4f}   (no mean reversion / trending)")

    if len(rets) > 2:
        ac1 = np.corrcoef(rets[:-1], rets[1:])[0, 1]
        print(f"Return autocorr lag1 .... {ac1:+.4f}  "
              f"({'momentum' if ac1 > 0.05 else 'reversal' if ac1 < -0.05 else 'noise'})")

    if c["volume"]:
        v = pd.to_numeric(df[c["volume"]], errors="coerce").dropna()
        print(f"Volume mean/median ...... {v.mean():.0f} / {v.median():.0f}")

    # --- recommended config ----------------------------------------------
    reverting = np.isfinite(hl) and hl < 40
    vol_ticks = per_tick_vol / tick if tick else 1.0
    base_half_spread_ticks = max(1, round(vol_ticks * (0.6 if reverting else 1.2)))
    # skew harder when mean-reverting (safe to hold inventory) is WRONG:
    # skew harder when TRENDING, because inventory is dangerous.
    skew = 0.35 if reverting else 0.7

    print()
    print("=" * 60)
    print("RECOMMENDED CONFIG  (paste into rit_market_maker.py -> Config)")
    print("=" * 60)
    print(f"    TICK_SIZE            = {tick:.4f}")
    print(f"    BASE_HALF_SPREAD     = {base_half_spread_ticks}   # in ticks, each side of fair value")
    print(f"    VOL_PER_TICK         = {per_tick_vol:.4f}")
    print(f"    INVENTORY_SKEW       = {skew:.2f}   # {'trending market -> keep inventory tight' if not reverting else 'mean-reverting -> can lean on inventory'}")
    print(f"    # Market is {'MEAN-REVERTING (great for MM)' if reverting else 'TRENDING/RANDOM-WALK (manage inventory carefully)'}")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print('\nERROR: pass the path to the Excel file, e.g.\n'
              '   python analyze_market_data.py "algo 2 market data (1).xlsx"')
        sys.exit(1)
    main(sys.argv[1])
