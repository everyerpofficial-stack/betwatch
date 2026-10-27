"""Test setup. Uses a throwaway SQLite file. All XML/JSON under tests/fixtures/ are TEST FIXTURES, not real data."""
import os
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="betwatch-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ADMIN_TOKEN"] = "test-token"
os.environ["ENRICH_BYLINES"] = "false"
os.environ["ANTHROPIC_API_KEY"] = ""

import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def fixture_bytes():
    return lambda name: (FIXTURES / name).read_bytes()
