"""Apply SQL migrations idempotently (runs at API start) and ensure the app role exists."""
from pathlib import Path

import psycopg
from psycopg import sql

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _plain_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def migrate(owner_url: str | None = None, app_password: str | None = None) -> list[str]:
    settings = get_settings()
    owner_url = _plain_url(owner_url or settings.owner_database_url)
    app_password = app_password or settings.app_db_password
    applied: list[str] = []
    with psycopg.connect(owner_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'hms_app'").fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE ROLE hms_app LOGIN PASSWORD {}").format(sql.Literal(app_password)))
        else:
            conn.execute(sql.SQL("ALTER ROLE hms_app LOGIN PASSWORD {}").format(sql.Literal(app_password)))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    names = migrate()
    print(f"migrations applied: {names or 'none (up to date)'}")
