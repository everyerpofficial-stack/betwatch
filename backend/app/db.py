import os
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    if url.startswith("sqlite"):
        # Auto-seed SQLite db if target is missing or unpopulated
        import shutil
        from pathlib import Path
        db_path = url.replace("sqlite:////", "//").replace("sqlite:///", "").replace("sqlite://", "")
        if db_path and not db_path.startswith(":memory:"):
            target = Path(db_path)
            if not target.is_absolute():
                target = Path.cwd() / target
            if not target.exists() or target.stat().st_size < 100000:
                for cand in [
                    Path(__file__).resolve().parent / "betwatch.db",
                    Path(__file__).resolve().parents[1] / "betwatch.db",
                    Path(__file__).resolve().parents[2] / "backend" / "betwatch.db",
                    Path.cwd() / "backend" / "betwatch.db",
                ]:
                    if cand.exists() and cand.is_file() and cand.stat().st_size > 100000 and cand.resolve() != target.resolve():
                        try:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(cand, target)
                            break
                        except Exception:
                            pass

        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(eng, "connect")
        def _pragma(dbapi_conn, _):
            try:
                cur = dbapi_conn.cursor()
                if os.environ.get("VERCEL"):
                    cur.execute("PRAGMA journal_mode=DELETE")
                else:
                    cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
            except Exception:
                pass

        return eng
    return create_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True)


engine = _make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
