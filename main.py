"""cbBTC/USDC 1-minute capitulation-bounce bot for make.fun bars-replay/v1.

Paper trading only: the harness trades a simulated $1,000 against real
on-chain prices. No real money is ever touched.

Why this design
---------------
A round trip in this replay costs roughly 30 bps (4 bps fee + 2 bps
slippage per side plus constant-product impact on a ~$950 order), and
1-minute cbBTC spends most of its time chopping or bleeding, where
trend-following entries are whipsawed to death by those costs. The one
recurring pattern that clears the cost hurdle is the capitulation
bounce: after a fast, deep flush, forced selling exhausts and price
snaps back 1% or more within a few hours.

- Entry ("washout"): the close is at least 2.8% under the highest close
  of the last 360 bars (a real flush, not a dip), it has bounced between
  0.35% and 1.0% off the lowest close of those 360 bars (the fall has
  stopped but the snap-back has not run yet), and the bar does not close
  down. Buys 95% of cash, capped by the harness weight room.
- Exit (whole position, first match wins): take-profit at +1.1% over
  cost, stop-loss at -0.9% under cost, or a 180-bar timeout — a bounce
  that has not paid in 3 hours is not coming.
- Equity guard: if equity ever falls 12% under its peak, flatten and
  sit out 240 bars (the race ranks an entry below the bar when its
  drawdown exceeds 25%; this guard keeps a bad day far from that line).
- Cooldown: 12 bars after any exit before a new entry.
- Every order caps slippage at 100 bps past the deciding close.

Deterministic: floats only in fixed IEEE order, no clock, no randomness,
no files, no network. Runs through the vendored bars SDK shim
(vendor/makefun_bars.py, a byte-for-byte copy of the kit's SDK).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
LOOKBACK = 360            # bars of history behind the flush test (6 hours)
FLUSH_MIN = 0.028         # close must be >= 2.8% under the lookback high
BOUNCE_LO = 0.0035        # ... and 0.35% to 1.0% above the lookback low
BOUNCE_HI = 0.010
TAKE_PROFIT = 1.011       # bank the bounce at +1.1% over cost
STOP_LOSS = 0.991         # cut it at -0.9% under cost
TIMEOUT_BARS = 180        # a bounce that takes > 3 hours is not a bounce
COOLDOWN_BARS = 12
DD_GUARD = 0.88           # flatten at 12% equity drawdown from the peak
DD_SITOUT = 240
BUY_FRACTION = 0.95
MAX_SLIPPAGE_BPS = 100


class Bot:
    def __init__(self):
        self.closes = []
        self.cooldown_until = -1
        self.hold_bars = 0
        self.peak_equity = 0    # best (cash + position value) seen, quote units
        self.entry_lo = 0       # lookback low the live position bounced from

    def _sell_all(self, state, held, seq, cooldown=COOLDOWN_BARS):
        self.cooldown_until = seq + cooldown
        self.hold_bars = 0
        return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    def on_batch(self, state, batch):
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == SYMBOL:
                bar = b
        if bar is None:
            return []
        close = int(bar["c"])
        self.closes.append(close)
        if state.warming_up:
            return []

        held = state.positions.get(SYMBOL)
        seq = state.seq

        if held is not None:
            self.hold_bars += 1
            value = state.value(SYMBOL, held.qty)
            ratio = value / held.cost if held.cost else 0.0
            equity = state.cash + value
            if equity > self.peak_equity:
                self.peak_equity = equity
            elif self.peak_equity and equity <= self.peak_equity * DD_GUARD:
                return self._sell_all(state, held, seq, cooldown=DD_SITOUT)
            if ratio >= TAKE_PROFIT or ratio <= STOP_LOSS \
                    or self.hold_bars >= TIMEOUT_BARS or close < self.entry_lo:
                return self._sell_all(state, held, seq)
            return []

        # Flat: look for a capitulation bounce.
        if seq <= self.cooldown_until:
            return []
        if len(self.closes) <= LOOKBACK:
            return []
        window = self.closes[-LOOKBACK:]
        hi = max(window)
        lo = min(window)
        if hi <= 0 or lo <= 0:
            return []
        if close / hi - 1.0 > -FLUSH_MIN:
            return []                                 # no real flush yet
        if not lo * (1.0 + BOUNCE_LO) <= close <= lo * (1.0 + BOUNCE_HI):
            return []                                 # still falling, or bounce over
        if close < self.closes[-2]:
            return []                                 # the bar must not close down
        room = state.weight_room(SYMBOL)
        spend = int(state.cash * BUY_FRACTION)
        if spend > room:
            spend = room
        min_notional = int(state.hello["fees"]["min_notional_quote_units"])
        if spend < min_notional:
            return []
        self.hold_bars = 0
        self.entry_lo = lo
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": spend,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


if __name__ == "__main__":
    makefun_bars.run(Bot().on_batch)
