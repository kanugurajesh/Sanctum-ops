"""Test setup. Uses a separate database (hms_test) on the same Postgres server.
No test calls the LLM: scenario tests use StaticParser, policy tests call the policy layer directly."""
import os
import re

# Point the app at the test database BEFORE any app module reads settings.
_owner = os.environ.get("OWNER_DATABASE_URL", "postgresql+psycopg://hms_owner:hms_owner_pw@localhost:5433/hms")
_app = os.environ.get("APP_DATABASE_URL", "postgresql+psycopg://hms_app:hms_app_pw@localhost:5433/hms")
TEST_OWNER_URL = re.sub(r"/[^/]+$", "/hms_test", _owner)
os.environ["OWNER_DATABASE_URL"] = TEST_OWNER_URL
os.environ["APP_DATABASE_URL"] = re.sub(r"/[^/]+$", "/hms_test", _app)
os.environ["GROQ_API_KEY"] = ""            # make sure nothing can reach an LLM

import psycopg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

from app.agent.parser import StaticParser  # noqa: E402
from app.db.engine import app_engine, owner_engine, tenant_tx  # noqa: E402
from app.db.migrate import migrate  # noqa: E402
from app.execution import directory  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.seed import seed  # noqa: E402


def _create_test_db():
    admin_url = re.sub(r"/[^/]+$", "/postgres", TEST_OWNER_URL).replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(admin_url, autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = 'hms_test'").fetchone():
            c.execute("CREATE DATABASE hms_test")


@pytest.fixture(scope="session", autouse=True)
def database():
    _create_test_db()
    migrate(TEST_OWNER_URL)
    seed(owner_engine())
    yield


@pytest.fixture
def fresh_db():
    """Reset data for tests that write."""
    seed(owner_engine())
    yield


def principal(user_id: int):
    hid = 1 if user_id <= 7 else 2
    with tenant_tx(hid) as conn:
        cat = directory.load_catalog(conn)
        return directory.load_principal(conn, user_id, hid, cat)


# Seeded user ids
ADMIN_A, BILLING_A, PHARMACIST_A, FRONTDESK_A, DOCTOR_A, DUAL_A, FRONTDESK2_A = 1, 2, 3, 4, 5, 6, 7
ADMIN_B, BILLING_B, RAVI_TEJA_B, RAVI_SHANKAR_B = 8, 9, 10, 11


@pytest.fixture
def sql_log():
    """Captures every SQL statement the app role sends, so tests can assert that a refused
    request never queried a business table."""
    statements: list[str] = []

    def before(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    eng = app_engine()
    event.listen(eng, "before_cursor_execute", before)
    yield statements
    event.remove(eng, "before_cursor_execute", before)


def make_orchestrator(mapping: dict) -> Orchestrator:
    return Orchestrator(StaticParser(mapping))


def owner_scalar(sql: str, **params):
    with owner_engine().begin() as c:
        return c.execute(text(sql), params).scalar()
