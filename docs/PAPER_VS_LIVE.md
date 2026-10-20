# Paper vs live

* Every order, position and trade row carries `environment` PAPER or LIVE. Adapters declare their environment; the engine refuses a mismatch. Paper cash lives in `paper_accounts`; live balances are never used for paper.
* **LIVE order gate** (`SafetyController.live_gate`), re-evaluated for every order: live switch ENABLED; trading state SYSTEM_HEALTHY (no kill switch, no reconciliation required, risk healthy); exchange row is LIVE, enabled, verified, key read+trade, no withdraw, credential without withdrawal permission; a clean reconciliation younger than 10 minutes; strategy version RELEASED and enabled (experimental strategies are stored as DRAFT and can never pass). Anything that cannot be read blocks.
* Fresh install: live DISABLED. After a restart: ENABLED becomes BLOCKED unless full startup verification passed (the API process always blocks it until a person re-enables).
* Futures/perpetuals are modelled (`market_type`) but rejected as NOT_IMPLEMENTED.
