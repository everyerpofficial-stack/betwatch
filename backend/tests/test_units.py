"""Unit tests: normalisation, dedup keys, classification, byline parsing, SSRF guard."""
from app.ingest.classify import categorize, looks_live, tone_of
from app.ingest.enrich import author_from_html
from app.ingest.outlets import guess_country, lookup
from app.ingest.text import SearchQuery, canonical_url, contains_term, fingerprint, strip_outlet_suffix
from app.security import is_public_http_url


def test_canonical_url_strips_tracking_amp_and_www():
    a = canonical_url("https://www.example.com/story/123/amp/?utm_source=rss&id=5#top")
    b = canonical_url("http://example.com/story/123?id=5")
    assert a == b == "https://example.com/story/123?id=5"


def test_fingerprint_same_title_same_outlet():
    assert fingerprint("ED raids  Mahadev app!", "TOI") == fingerprint("ed raids mahadev app", "toi")
    assert fingerprint("ED raids Mahadev app", "TOI") != fingerprint("ED raids Mahadev app", "The Hindu")


def test_strip_google_outlet_suffix():
    assert strip_outlet_suffix("Rules take effect - The Hindu", "The Hindu") == "Rules take effect"


def test_contains_term_whole_words():
    assert contains_term("Mahadev app case", "mahadev app")
    assert not contains_term("bets placed", "bet")
    assert contains_term("Online Gaming Act notified", "online gaming act")


def test_search_matches_whole_words_not_word_insides():
    assert not SearchQuery("IPL").matches("Multiple principle triple")
    assert SearchQuery("IPL").matches("#IPL2026 final") and SearchQuery("IPL").matches("IPL auction")
    assert not SearchQuery("ED").matches("Police arrested accused") and SearchQuery("ED").matches("ED's probe widens")
    assert not SearchQuery("law").matches("lawyer argues") and SearchQuery("law").matches("new laws")
    assert SearchQuery("app").matches("apps blocked") and SearchQuery("india").matches("Indian government")
    assert not SearchQuery("IT Act").matches("its act")


def test_search_words_any_order_phrases_and_or():
    assert SearchQuery("Online Gaming Act").matches("Act on online gaming passed")
    assert not SearchQuery('"Online Gaming Act"').matches("Act on online gaming passed")
    assert SearchQuery('"Online Gaming Act"').matches("The Online Gaming Act, 2025")
    q = SearchQuery("betting law OR gambling law")
    assert q.matches("New gambling law in Goa") and not q.matches("Gambling crackdown")
    assert not SearchQuery("#").matches("anything")


def test_search_hindi_and_unicode_forms():
    assert SearchQuery("सट्टा").matches("महादेव सट्‌टा ऐप")  # zero-width non-joiner inside the word
    assert not SearchQuery("सट्टा").matches("सट्टेबाजी का धंधा")
    assert SearchQuery("Dream11").matches("𝐃𝐫𝐞𝐚𝐦𝟏𝟏 contest")
    assert "%सट्%टा%" in SearchQuery("सट्टा").sql_patterns()[0]  # LIKE tolerates the joiner too


def test_search_names_subject_only_when_distinctive():
    assert SearchQuery("Mahadev").names_subject("Mahadev betting", ["mahadev app"])
    assert SearchQuery("online gaming act").names_subject("Online Gaming Act", [])
    assert not SearchQuery("app").names_subject("Mahadev betting", ["mahadev betting app"])
    assert not SearchQuery("betting app").names_subject("Online betting", ["betting app"])  # generic alias
    assert not SearchQuery("सट्टा").names_subject("Online betting", ["ऑनलाइन सट्टा"])


def test_categories():
    cats = categorize("Police arrest five in illegal betting app racket; ED files chargesheet")
    assert "Betting" in cats and "Law" in cats and "Gaming app" in cats
    assert "Stock market" in categorize("Sensex falls 500 points as investors sell shares")


def test_tone_crackdown_is_negative():
    assert tone_of("Police raid illegal betting racket, five arrested")[0] == "Negative"
    assert tone_of("Court grants relief to gaming company, ban quashed")[0] == "Positive"


def test_live_detection():
    assert looks_live("India News Live Updates, 25 October")
    assert looks_live("LIVE: Barcelona vs Real Madrid")
    assert not looks_live("Liverpool beat Chelsea")
    assert not looks_live("How online betting apps deliver lives into debt")


def test_outlet_registry_merges_names_and_domains():
    assert lookup("The Times of India", None)[0] == "Times of India"
    assert lookup(None, "timesofindia.indiatimes.com")[0] == "Times of India"
    assert lookup(None, "edition.cnn.com")[0] == "CNN"
    assert guess_country("example.co.in") == "IN"


def test_youtube_relative_age_understands_abbreviations():
    from datetime import datetime, timedelta
    from app.ingest.adapters import parse_relative_age
    now = datetime(2026, 10, 27, 12, 0)
    # YouTube now abbreviates; the old parser only knew full words and stamped these as "now".
    assert parse_relative_age("3 yr ago", now) == now - timedelta(days=3 * 365)
    assert parse_relative_age("2 mo ago", now) == now - timedelta(days=60)
    assert parse_relative_age("3 wk ago", now) == now - timedelta(weeks=3)
    assert parse_relative_age("5 hr ago", now) == now - timedelta(hours=5)
    assert parse_relative_age("10 min ago", now) == now - timedelta(minutes=10)
    assert parse_relative_age("Streamed 1 day ago", now) == now - timedelta(days=1)
    assert parse_relative_age("2 months ago", now) == now - timedelta(days=60)
    assert parse_relative_age("", now) is None


def test_instagram_description_date_in_every_layout():
    from datetime import datetime
    from app.ingest.adapters import split_instagram_description
    for desc in ('2,497 likes, 58 comments - mib_india on August 20, 2025: "TEST caption."',
                 '2,497 likes, 58 comments - mib_india - August 20, 2025: "TEST caption."',
                 'mib_india on August 20, 2025: "TEST caption."'):
        assert split_instagram_description(desc) == (datetime(2025, 8, 20), "TEST caption")
    assert split_instagram_description("TEST caption without a date") == (None, "TEST caption without a date")


def test_channel_wikidata_matching_is_exact_and_skips_platforms():
    from app.models import OutletProfile, Outlet
    from app.services.channel_profile import is_platform_outlet, known_qid, profile_looks_wrong
    assert known_qid("Aaj Tak · YouTube") == "Q3396358"
    assert known_qid("NDTV Profit") is None  # not NDTV
    assert known_qid("DREAM11 EXPERT · YouTube") is None  # a fan channel, not the Dream11 company
    yt = Outlet(name="NTV Telugu · YouTube", domain="youtube.com")
    assert is_platform_outlet(yt) and not is_platform_outlet(Outlet(name="The Hindu", domain="thehindu.com"))
    # Saved by the old matcher: the channel was given YouTube Inc.'s own profile.
    assert profile_looks_wrong(yt, OutletProfile(wikidata_qid="Q866", official_website="https://www.youtube.com/"))
    # A wrong hard-coded QID ("Economic Times" -> a German teacher) is re-matched; admin-confirmed ones are kept.
    et = Outlet(name="Economic Times", domain="economictimes.indiatimes.com")
    assert profile_looks_wrong(et, OutletProfile(wikidata_qid="Q970653"))
    assert not profile_looks_wrong(et, OutletProfile(wikidata_qid="Q970653", confirmed_by_admin=True))
    assert not profile_looks_wrong(et, OutletProfile(wikidata_qid="Q1954757"))


def test_author_from_jsonld_preferred_over_handles():
    html = """<html><head><meta name="twitter:creator" content="@outlet">
    <script type="application/ld+json">{"@type":"NewsArticle","author":[{"@type":"Person","name":"Asha Rao"}]}</script>
    </head></html>"""
    assert author_from_html(html) == "Asha Rao"


def test_author_meta_rejects_outlet_and_handles():
    assert author_from_html('<meta name="author" content="Fixture Times">', "Fixture Times") is None
    assert author_from_html('<meta name="author" content="@fixture">') is None
    assert author_from_html('<meta name="author" content="By Ravi Kumar">') == "Ravi Kumar"


def test_ssrf_guard():
    assert not is_public_http_url("http://127.0.0.1/admin")
    assert not is_public_http_url("http://localhost:8000/")
    assert not is_public_http_url("http://10.0.0.5/")
    assert not is_public_http_url("http://169.254.169.254/latest/meta-data")
    assert not is_public_http_url("file:///etc/passwd")
    assert not is_public_http_url("http://user:pass@example.com/")
