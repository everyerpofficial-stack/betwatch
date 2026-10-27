from pathlib import Path

_DIR = Path(__file__).resolve().parent / "frontend"


def _read(name: str) -> str:
    candidates = [
        _DIR / name,
        Path(__file__).resolve().parents[1] / "frontend" / name,
        Path(__file__).resolve().parents[2] / "frontend" / name,
        Path.cwd() / "frontend" / name,
        Path.cwd() / "public" / name,
        Path(f"/var/task/backend/app/frontend/{name}"),
        Path(f"/var/task/frontend/{name}"),
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            try:
                return p.read_text(encoding="utf-8")
            except Exception:
                pass
    return ""


INDEX_HTML = _read("index.html")
ADMIN_HTML = _read("admin.html")
CHANNEL_HTML = _read("channel.html")
STORY_HTML = _read("story.html")
STYLES_CSS = _read("styles.css")
APP_JS = _read("app.js")
