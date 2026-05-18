import os
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models import Base

# Railway: примонтируй Volume в /data и задай DATABASE_PATH=/data/fb_bot.db
_raw_path = (os.getenv("DATABASE_PATH") or "./fb_bot.db").strip()
_db_file = Path(_raw_path)
if not str(_db_file).startswith("postgres") and "://" not in _raw_path:
    _db_file.parent.mkdir(parents=True, exist_ok=True)
    DATABASE_URL = f"sqlite+aiosqlite:///{_db_file.as_posix()}"
else:
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{_db_file.as_posix()}")

_connect_args: dict = {}
if DATABASE_URL.startswith("sqlite"):
    _connect_args["timeout"] = 60

engine = create_async_engine(DATABASE_URL, echo=False, connect_args=_connect_args)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


if DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.close()


@asynccontextmanager
async def db_session():
    async with Session() as session:
        yield session


async def _sqlite_add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    if engine.dialect.name != "sqlite":
        return
    res = await conn.execute(text(f"PRAGMA table_info({table})"))
    cols = {row[1] for row in res.fetchall()}
    if column not in cols:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _sqlite_add_column_if_missing(
            conn, "users", "last_account_token", "last_account_token TEXT"
        )
        await _sqlite_add_column_if_missing(
            conn, "blocked_sellers", "country", "country TEXT DEFAULT 'ch'"
        )
