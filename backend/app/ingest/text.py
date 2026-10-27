"""Normalisation helpers used for deduplication and matching."""
import hashlib
import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "gclid", "fbclid",
    "ref", "ref_src", "cmpid", "ito", "from", "_gl", "ocid", "src", "mc_cid", "mc_eid", "taid",
}

STOPWORDS = set(
    "a an the of to in on for and or but with by at from as is are was were be been has have had it its this that "
    "these those into over after before about says said say new news live updates update latest india indian "
    "amid vs will can could may might not no his her their they he she we you i".split()
)


def canonical_url(url: str) -> str:
    """Lower-case host, drop fragments, tracking params, AMP and trailing slashes."""
    url = (url or "").strip()
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") and host.count(".") >= 2:
        host = host[2:]
    path = re.sub(r"/amp/?$", "", parts.path)
    path = re.sub(r"/amp/", "/", path)
    path = path.rstrip("/") or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if k.lower() not in TRACKING_PARAMS and k.lower() != "amp"]
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))[:1000]


def domain_of(url: str) -> str:
    host = urlsplit(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def clean_text(s: str | None, limit: int | None = None) -> str:
    """Strip HTML, decode entities, collapse whitespace."""
    if not s:
        return ""
    if "<" in s:
        s = BeautifulSoup(s, "html.parser").get_text(" ")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[: limit - 1].rsplit(" ", 1)[0] + "…"
    return s


def strip_outlet_suffix(title: str, outlet: str | None) -> str:
    """Google News titles end with " - Outlet Name"; remove it."""
    if outlet and title.endswith(" - " + outlet):
        return title[: -len(outlet) - 3].strip()
    return re.sub(r"\s[-|–]\s[^-|–]{2,60}$", "", title).strip() if outlet else title


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_tokens(title: str) -> set[str]:
    return {w for w in normalize_title(title).split() if w not in STOPWORDS and len(w) > 1}


def fingerprint(title: str, outlet: str) -> str:
    """Same headline from the same outlet = same post, even if the URL differs (AMP, Google redirect...)."""
    key = f"{(outlet or '').lower()}|{normalize_title(title)}"
    return hashlib.sha256(key.encode()).hexdigest()


def contains_term(text: str, term: str) -> bool:
    """Whole-word, case-insensitive match (works for multi-word terms)."""
    term = term.strip().lower()
    if not term:
        return False
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text.lower()) is not None
