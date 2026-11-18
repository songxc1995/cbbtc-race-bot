"""cbbtc-race-bot: a two-leg, long-only cbBTC/USDC bot for make.fun's
"Race a BTC trading bot live for 24 hours" (agent-fun/bars-replay/v1).

The bot reads one 1-minute bar per batch and answers with at most one order.
Every price it uses is an integer at the protocol's scales, every decision is
a pure function of the bars seen so far: no clock, no network, no randomness,
so two runs on the same data give the same orders.

Strategy (all thresholds in basis points, all returns over the last 60 bars):

  Leg 1 — capitulation dip-buy. When the trailing 60-bar return falls to
  -200 bps or worse, the market has just had a roughly three-sigma hourly
  drop. Sharp hourly drops in BTC tend to bounce, so the bot buys with all
  its cash, takes profit at +100 bps from its fill price, and cuts the trade
  at -80 bps if the drop keeps going instead of bouncing.

  Leg 2 — momentum. When the trailing 60-bar return rises to +60 bps or
  better, an up-move is under way, so the bot buys with all its cash and
  rides it until the trailing 60-bar return turns to -20 bps or worse, or
  the position falls 80 bps under its fill price.

Only one leg can hold at a time (the replay allows one open position), and
each leg exits on its own rules: the dip leg never exits on the momentum
leg's reversal signal, and vice versa. Both legs size at 100% of cash, so a
fill's own price impact stays small next to the moves they chase (a full-size
round trip costs about 26 bps in fees, slippage and impact, while each leg
targets moves several times that).

Risk controls: a hard stop 80 bps under the fill price on either leg, a
wide 300 bps max-slippage cap on every order (far above the largest 1-minute
move in the dev window, so ordinary fills are never refused, but a freak gap
cannot fill at any price), and no trade at all until 60 bars of closes exist.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"

# Lookback for the trailing return, in bars.
LOOKBACK_BARS = 60
# Dip leg: enter when the trailing return is at or below this (bps).
DIP_TRIGGER_BPS = -200
# Dip leg: take profit / stop loss from the fill price (bps).
DIP_TAKE_BPS = 100
DIP_STOP_BPS = 80
# Dip leg re-arm level (bps): after a dip trade exits, the leg stays off
# until the trailing 60-bar return has recovered above this, so one long
# crash cannot trigger a chain of buy-stop-buy-stop round trips.
DIP_REARM_BPS = -100
# Momentum leg: enter when the trailing return is at or above this (bps).
MOM_ENTER_BPS = 60
# Momentum leg: exit when the trailing return falls to this (bps).
MOM_EXIT_BPS = -20
# Momentum leg: stop loss from the fill price (bps).
MOM_STOP_BPS = 80
# Slippage cap on every order (bps from the latest close). Wide enough that
# no ordinary 1-minute move refuses a fill; narrow enough that a freak gap
# cannot fill at any price.
MAX_SLIPPAGE_BPS = 300


class Bot:
    """Keeps the closes, the open position's fill price and which leg holds it."""

    def __init__(self):
        # Closes of the CBBTC bars seen so far, in batch order.
        self.closes = []
        # Fill price of the open position (raw price units), or None.
        self.entry_price = None
        # Which leg holds the open position: "dip", "mom", or None.
        self.leg = None
        # Which leg a buy decided on the previous batch belongs to, until
        # its fill (or rejection) is reported in the next batch.
        self.pending_leg = None
        # Whether the dip leg may fire: disarmed when a dip trade exits,
        # re-armed once the trailing return has recovered above DIP_REARM_BPS.
        self.dip_armed = True

    def on_batch(self, state, batch):
        # Fold in the previous batch's fills (or rejections) first.
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("status") == "filled" and fill.get("side") == "buy":
                self.entry_price = int(fill["price"])
                self.leg = self.pending_leg
                self.pending_leg = None
            elif fill.get("status") == "filled" and fill.get("side") == "sell":
                self.entry_price = None
                self.leg = None
            elif fill.get("status") == "rejected" and fill.get("side") == "buy":
                # The buy never filled: forget it so a later signal can try again.
                self.pending_leg = None

        bars = [b for b in batch.get("bars", []) if b.get("symbol") == SYMBOL]
        if not bars or state.warming_up:
            return []
        self.closes.append(int(bars[0]["c"]))

        # A buy decided last batch is still awaiting its fill: decide nothing.
        if self.pending_leg is not None:
            return []

        holding = state.positions.get(SYMBOL) is not None and state.positions[SYMBOL].qty > 0
        if len(self.closes) <= LOOKBACK_BARS:
            return []

        ret60 = (self.closes[-1] - self.closes[-1 - LOOKBACK_BARS]) * 10000 // self.closes[-1 - LOOKBACK_BARS]

        if not holding:
            if state.cash < 1000000:
                return []
            if not self.dip_armed and ret60 > DIP_REARM_BPS:
                self.dip_armed = True
            if self.dip_armed and ret60 <= DIP_TRIGGER_BPS:
                self.pending_leg = "dip"
                return [{"side": "buy", "symbol": SYMBOL,
                         "notional_quote_units": state.cash,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            if ret60 >= MOM_ENTER_BPS:
                self.pending_leg = "mom"
                return [{"side": "buy", "symbol": SYMBOL,
                         "notional_quote_units": state.cash,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            return []

        # Holding: wait until the buy's fill has been reported.
        if self.entry_price is None:
            return []
        change_bps = (self.closes[-1] - self.entry_price) * 10000 // self.entry_price
        sell = None
        if self.leg == "dip":
            if change_bps >= DIP_TAKE_BPS or change_bps <= -DIP_STOP_BPS:
                sell = True
                self.dip_armed = False
        else:
            if change_bps <= -MOM_STOP_BPS or ret60 <= MOM_EXIT_BPS:
                sell = True
        if sell:
            return [{"side": "sell", "symbol": SYMBOL,
                     "qty": state.positions[SYMBOL].qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []


makefun_bars.run(Bot().on_batch)
