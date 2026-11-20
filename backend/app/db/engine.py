from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import Connection, Engine, create_engine, text

from app.config import get_settings


@lru_cache
def app_engine() -> Engine:
    return create_engine(get_settings().app_database_url, pool_pre_ping=True)


@lru_cache
def owner_engine() -> Engine:
    return create_engine(get_settings().owner_database_url, pool_pre_ping=True)


@contextmanager
def tenant_tx(hospital_id: int) -> Iterator[Connection]:
    """One transaction on the restricted app role, pinned to a single hospital.

    `set_config(..., true)` is SET LOCAL: it lasts only for this transaction, so a pooled
    connection can never leak one tenant's setting into another request. RLS policies read it.
    """
    with app_engine().begin() as conn:
        conn.execute(
            text("SELECT set_config('app.hospital_id', :h, true)"),
            {"h": str(int(hospital_id))},
        )
        yield conn


@contextmanager
def unscoped_tx() -> Iterator[Connection]:
    """App-role transaction with no tenant set: RLS hides every tenant row.
    Used only to call the SECURITY DEFINER login/demo functions."""
    with app_engine().begin() as conn:
        yield conn
