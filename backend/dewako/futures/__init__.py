"""Paper futures (USDT-margined perpetuals): a separate, PAPER-ONLY accounting model. Nothing here talks to an exchange, and the
spot engine does not import this package. See docs/FUTURES_PAPER_STAGE.md."""
from dewako.futures.paper import (FuturesConfig, FuturesError, Liquidation, MarginMode, PaperFuturesAccount,  # noqa: F401
                                  Position, entry_guard, size_for_risk)
