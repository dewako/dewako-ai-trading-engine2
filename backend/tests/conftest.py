"""Close idle database connections after every test so a long single-process run cannot exhaust PostgreSQL's connection limit."""
import pytest

from dewako.persistence.db import Database


@pytest.fixture(autouse=True)
def _close_idle_db_connections():
    yield
    Database.close_all_idle()


@pytest.fixture(autouse=True)
def _healthy_workers_stand_in(request, monkeypatch):
    """TEST FIXTURE STAND-IN, not production behaviour. Since Stage 1 / P0-5 the live gate FAILS CLOSED when no worker supervisor is registered
    (SafetyController.workers_healthy is None). Most engine tests build bare SafetyControllers without a supervisor, so each new controller
    starts with a healthy-workers stand-in, exactly as hardening_helpers.make_live_ready already does. A test that assigns
    `safety.workers_healthy` itself (including test_stage1_p0_misc, which sets None) overrides this. The Application sets the real supervisor."""
    if request.module.__name__ in ("test_live_activation", "test_stage1_p0_misc"):
        return                      # these modules test the unset/failed-worker behaviour itself and must see the real default (None)
    from dewako.safety.controller import SafetyController
    real_init = SafetyController.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        self.workers_healthy = lambda: (True, "")
    monkeypatch.setattr(SafetyController, "__init__", init)
