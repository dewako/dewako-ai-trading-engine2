# Paper run on a Linux server (for example an Oracle Cloud Always Free VM)

`scripts/linux/dewako_paper.sh` is the Linux counterpart of `scripts/windows/dewako_paper.ps1`: the same settings, the same bot
(`dewako-eth-paper`: Dewako ETH 1.0, ETH/USDT, 4h, PAPER) and the same `status` output. systemd replaces the scheduled tasks and the
watchdog. It uses no exchange keys; live trading stays locked; the app listens on `127.0.0.1` only, so no inbound firewall rule is needed.

## Use

```bash
bash dewako_paper.sh setup     # first time, and to update (fast-forwards main, reinstalls the pinned versions, restarts)
bash dewako_paper.sh status
bash dewako_paper.sh stop      # also disables the service at boot until setup is run again
```

Everything lives under `$DEWAKO_ROOT` (default `~/dewako`): the repo clone, `.venv`, and `paper-state/` (database, `logs/`, `secrets.env`
with mode 600). Each start writes `paper-state/logs/engine-<stamp>.log`; `runs.log` records every start and exit with its exit code.
`journalctl -u dewako-paper` shows the service itself.

## What was and was not tested (2026-11-21)

Tested by `bash scripts/linux/dewako_paper.sh selftest` (runs in CI; no network, no installs): secrets generation, mode and idempotence;
the environment block (paper on, code plugins off, quote guard, public market data); the systemd unit text (single `Restart=always`,
no new privileges, 127.0.0.1 only); the real HTTP/JSON code against a stand-in API (admin and bot creation, a second run creating no
second bot, the status lines, the database/API count MISMATCH rule, a failed call reported instead of shown as 0, NOT RUNNING); the
`run` wrapper (per-start log, runs.log, exit code passed on, SIGTERM forwarded). Each class of check was mutation-tested: a deliberate
break made the self-test fail.

Not tested (never run on a server): `setup` end to end, the systemd install, pip on the server's CPU architecture, live market data from
the server's network, and the engine itself under this script. The first `setup` on a new machine is that test; read its output.

## Arm (Ampere A1) servers

The runtime needs only fastapi, uvicorn[standard], pydantic, cryptography and httpx plus the standard library, and the database is
SQLite. `setup` installs with `--only-binary=:all:`: if any pinned package has no prebuilt wheel for the machine, it stops and names it
instead of compiling. CI has never run on Arm.

## Oracle Always Free notes

* Instances idle for 7 days may be reclaimed (CPU 95th percentile and network under 20%; memory too on A1). A paper bot is nearly idle.
  Copy `paper-state/` off the server now and then.
* "Out of host capacity" at create time is a shortage of free shapes in the home region; retry or try another availability domain.
* Limits were reduced in June 2026 (A1: 2 cores, 12 GB); they can change again.
