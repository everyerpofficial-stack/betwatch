import os
import shutil
import sys
from pathlib import Path

# Add project root and backend to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"

for p in (str(BACKEND_DIR), str(ROOT_DIR), str(Path.cwd() / "backend"), str(Path.cwd()), "/var/task/backend", "/var/task"):
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
        if not os.path.exists(tmp_db) or os.path.getsize(tmp_db) == 0:
            for candidate in (
                BACKEND_DIR / "betwatch.db",
                Path.cwd() / "backend" / "betwatch.db",
                ROOT_DIR / "betwatch.db",
                Path("/var/task/backend/betwatch.db"),
            ):
                if candidate.exists() and candidate.is_file():
                    try:
                        shutil.copyfile(candidate, tmp_db)
                        break
                    except Exception as e:
                        print(f"Warning: could not copy bundled db: {e}")

from app.main import app


@app.get("/api/debug-env", include_in_schema=False)
def debug_env():
    import os
    res = {
        "cwd": os.getcwd(),
        "cwd_list": os.listdir(".") if os.path.exists(".") else [],
        "var_task": os.listdir("/var/task") if os.path.exists("/var/task") else [],
        "tmp_db_size": os.path.getsize("/tmp/betwatch.db") if os.path.exists("/tmp/betwatch.db") else 0,
        "routes": [getattr(r, "path", str(r)) for r in app.routes],
    }
    return res
