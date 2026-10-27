import os
import shutil
import sys
from pathlib import Path

# Add project root and backend to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"

for p in (str(BACKEND_DIR), str(ROOT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Serverless environment configuration
if os.environ.get("VERCEL"):
    # If no external PostgreSQL DATABASE_URL is configured, default to /tmp/betwatch.db
    current_db = os.environ.get("DATABASE_URL", "")
    if not current_db or current_db.startswith("sqlite:///."):
        tmp_db = "/tmp/betwatch.db"
        os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"

        # Copy bundled database to /tmp if available and not yet present in /tmp
        bundled_db = BACKEND_DIR / "betwatch.db"
        if bundled_db.exists() and not os.path.exists(tmp_db):
            try:
                shutil.copyfile(bundled_db, tmp_db)
            except Exception as e:
                print(f"Warning: could not copy bundled db: {e}")

    # Serverless functions cannot run continuous background scheduler threads
    os.environ["EMBEDDED_WORKER"] = "false"

from app.main import app
