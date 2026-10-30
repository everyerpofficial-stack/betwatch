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


# ---------------------------------------------------------------- dashboard search
# Letters, digits and combining marks. \w alone drops Indic vowel signs and virama, splitting Hindi words apart.
_SEARCH_WORD = re.compile(r"[\ẁ-ͯऀ-෿᳐-᳿꣠-ꣿ]+")
# Topic words nearly every post here contains. A subject tag only satisfies a query that also has a distinctive
# word (or names the subject exactly), so "app" does not return every post tagged "Mahadev betting app".
GENERIC_TOPIC_WORDS = frozenset(
    "app apps application betting bet bets online gaming game games gambling casino law laws act bill case book "
    "india indian fantasy sports sport real money ban rules rule platform platforms news "
    "सट्टा सट्टे सट्टेबाजी बेटिंग जुआ गैंबलिंग ऑनलाइन गेमिंग गेम ऐप कानून बिल एक्ट नियम विधेयक भारत".split()
)


def search_words(text: str) -> list[str]:
    """Search tokens: NFKC + casefold, zero-width joiners removed, split on anything not a letter, digit or mark."""
    t = unicodedata.normalize("NFKC", text or "").casefold().replace("‌", "").replace("‍", "")
    return _SEARCH_WORD.findall(t)


def _word_hit(qw: str, w: str) -> bool:
    """Whole word, a plural (apps), or a longer word it starts: 4+ letters (india→indian) or digits (ipl→ipl2026)."""
    if w == qw:
        return True
    if not w.startswith(qw):
        return False
    rest = w[len(qw):]
    return len(qw) >= 4 or rest.isdigit() or (len(qw) >= 3 and rest in ("s", "es"))


class SearchQuery:
    """A dashboard search: alternatives split by OR, each a list of words and "quoted phrases" that must all appear.

    Words match whole words (see _word_hit), never the inside of another word, so "IPL" does not match "multiple".
    """

    def __init__(self, raw: str):
        self.raw = (raw or "").strip()
        self.norm = " ".join(search_words(self.raw))
        self.alternatives: list[list[list[str]]] = []  # alternative -> terms -> words (a multi-word term is a phrase)
        for part in re.split(r"\s+OR\s+|\|", self.raw):
            terms = []
            for phrase, token in re.findall(r'"([^"]*)"|(\S+)', part):
                ws = search_words(phrase if phrase else re.sub(r"['’]s$", "", token, flags=re.IGNORECASE))
                if ws:
                    terms.append(ws)  # "e-game" or "U.S." stay together as a phrase
            if terms:
                self.alternatives.append(terms)

    @classmethod
    def any_phrase(cls, phrases: list[str]) -> "SearchQuery":
        """Matches text containing any one of the phrases (a subject's name and aliases)."""
        sq = cls("")
        sq.alternatives = [[ws] for p in phrases if (ws := search_words(p))]
        return sq

    @staticmethod
    def _has(term: list[str], words: list[str]) -> bool:
        n = len(term)
        return any(all(_word_hit(term[i], words[j + i]) for i in range(n)) for j in range(len(words) - n + 1))

    def matches_words(self, words: list[str]) -> bool:
        return any(all(self._has(t, words) for t in alt) for alt in self.alternatives)

    def matches(self, text: str) -> bool:
        return self.matches_words(search_words(text))

    def names_subject(self, name: str, aliases: list[str]) -> bool:
        """Whether this search is about the tracked subject: it is the subject's name, or a distinctive search
        ("mahadev", "proga") found in its name or an alias. A generic search ("betting app") names no subject."""
        if self.norm and " ".join(search_words(name)) == self.norm:
            return True
        for term in [name, *aliases]:
            ws = search_words(term)
            for alt in self.alternatives:
                distinctive = any(w not in GENERIC_TOPIC_WORDS for t in alt for w in t)
                if distinctive and all(self._has(t, ws) for t in alt):
                    return True
        return False

    def sql_patterns(self) -> list[list[str]]:
        """Per alternative, LIKE patterns every matching text satisfies (a superset; matches_words decides).

        Stored Hindi may have a zero-width joiner after a virama (सट्‌टा), so the pattern allows anything there.
        Words with a nukta are left out: a feed may store the precomposed letter, which LIKE would not match."""
        return [[f"%{w.replace(chr(0x94D), chr(0x94D) + '%')}%" for t in alt for w in t if "़" not in w]
                for alt in self.alternatives]
