"""Close idle database connections after every test so a long single-process run cannot exhaust PostgreSQL's connection limit."""
import pytest

from dewako.persistence.db import Database


@pytest.fixture(autouse=True)
def _close_idle_db_connections():
    yield
    Database.close_all_idle()
