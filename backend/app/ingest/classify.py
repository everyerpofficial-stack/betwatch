"""Rule-based category and tone classification (no API key needed).

Categories are keyword rules — transparent and editable. Tone uses VADER sentiment on headline + description,
plus a small betting-specific lexicon so crackdown news ("raid", "ban", "arrested") reads as bad news for the
betting industry. If ANTHROPIC_API_KEY is set, app.ai can re-label tone per story.
"""
import re

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from .text import contains_term

CATEGORY_RULES: dict[str, list[str]] = {
    "Betting": ["betting", "bets", "bookie", "bookies", "bookmaker", "sportsbook", "satta", "gambling", "gamble", "casino",
                "casinos", "wager", "wagering", "match-fixing", "match fixing", "spot-fixing", "mahadev app",
                "mahadev book", "mahaadev", "parimatch", "1xbet", "dafabet", "stake.com", "prediction market",
                "prediction markets", "polymarket", "kalshi", "lottery", "igaming", "sports betting", "online betting"],
    "Law": ["act", "bill", "law", "rules", "regulation", "regulator", "ban", "banned", "court", "supreme court",
            "high court", "ed", "enforcement directorate", "pmla", "fir", "arrest", "arrested", "chargesheet",
            "notification", "meity", "ministry", "gazette", "licence", "license", "penalty", "fine", "blocked",
            "raid", "raids", "probe", "police", "verdict", "petition"],
    "Gaming app": ["online gaming", "gaming app", "gaming apps", "gaming platform", "gaming company", "gaming companies",
                   "fantasy sports", "fantasy cricket", "fantasy app", "rummy", "poker", "esports", "e-sports",
                   "dream11", "mpl", "winzo", "gameskraft", "real-money", "real money", "money games", "money gaming",
                   "betting app", "betting apps", "gaming law", "gaming rules", "gaming act"],
    "Company": ["company", "firm", "ceo", "founder", "startup", "acquisition", "acquire", "merger", "funding",
                "revenue", "profit", "layoffs", "shares", "stake", "ltd", "limited", "inc", "group", "operator"],
    "Stock market": ["sensex", "nifty", "stock", "stocks", "shares", "share price", "ipo", "nse", "bse", "sebi",
                     "market cap", "investors", "equity", "dalal street", "nasdaq", "dow jones"],
}

NEG_TERMS = {"raid": -2.0, "raids": -2.0, "arrest": -2.2, "arrested": -2.2, "ban": -1.6, "banned": -1.8,
             "illegal": -2.0, "fraud": -2.5, "scam": -2.5, "laundering": -2.5, "seized": -1.8, "crackdown": -1.6,
             "blocked": -1.4, "fir": -1.4, "chargesheet": -1.8, "probe": -1.2, "addiction": -1.8, "suicide": -2.5,
             "jail": -1.8, "penalty": -1.2, "fine": -0.6}
POS_TERMS = {"legalise": 1.8, "legalize": 1.8, "legalised": 1.8, "licensed": 1.0, "approval": 1.0, "approved": 0.8,
             "growth": 1.2, "record": 0.8, "relief": 1.6, "stay": 0.6, "quashed": 1.6, "acquitted": 1.8,
             "funding": 1.0, "revenue": 0.6, "partnership": 1.0}

_analyzer = SentimentIntensityAnalyzer()
_analyzer.lexicon.update(NEG_TERMS)
_analyzer.lexicon.update(POS_TERMS)


FOCUS_CATEGORIES = {"Betting", "Gaming app"}


def is_relevant(categories: list[str], linked_subjects: list) -> bool:
    """General-news feeds carry everything; keep only betting/gaming items or ones about a tracked subject."""
    return bool(FOCUS_CATEGORIES & set(categories)) or bool(linked_subjects)


def categorize(text: str) -> list[str]:
    cats = []
    for cat, terms in CATEGORY_RULES.items():
        if any(contains_term(text, t) for t in terms):
            cats.append(cat)
    return cats


def tone_of(text: str) -> tuple[str, float]:
    score = _analyzer.polarity_scores(text or "")["compound"]
    if score <= -0.2:
        return "Negative", score
    if score >= 0.2:
        return "Positive", score
    return "Neutral", score


LIVE_RE = re.compile(r"\blive( updates| blog| coverage| news| streaming|\s*:|\s*\|)|^\s*live\b|\blive now\b|\bLIVE\s*$", re.I)


def looks_live(title: str) -> bool:
    return bool(LIVE_RE.search(title or ""))
