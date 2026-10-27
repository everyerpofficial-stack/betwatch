"""Known outlets: canonical name, domains, type, country. Used to merge the same channel arriving from
different sources (Google News, GDELT, RSS) and to decide National vs International. Unknown outlets are
created automatically; their country is guessed from the domain TLD."""

KNOWN_OUTLETS = [
    # name, [domains], kind, country, [aliases]
    ("Times of India", ["timesofindia.indiatimes.com"], "News site", "IN", ["The Times of India", "TOI"]),
    ("Economic Times", ["economictimes.indiatimes.com", "m.economictimes.com"], "News site", "IN", ["The Economic Times", "ET"]),
    ("Hindustan Times", ["hindustantimes.com"], "News site", "IN", ["HT"]),
    ("The Hindu", ["thehindu.com"], "News site", "IN", []),
    ("Indian Express", ["indianexpress.com"], "News site", "IN", ["The Indian Express"]),
    ("NDTV", ["ndtv.com", "feeds.feedburner.com/ndtvnews-top-stories"], "TV / video", "IN", ["NDTV News"]),
    ("India Today", ["indiatoday.in"], "TV / video", "IN", []),
    ("Aaj Tak", ["aajtak.in"], "TV / video", "IN", ["AajTak"]),
    ("Mint", ["livemint.com"], "News site", "IN", ["Livemint", "mint"]),
    ("Business Today", ["businesstoday.in"], "News site", "IN", []),
    ("Business Standard", ["business-standard.com"], "News site", "IN", []),
    ("Moneycontrol", ["moneycontrol.com"], "News site", "IN", []),
    ("News18", ["news18.com"], "TV / video", "IN", ["CNN-News18"]),
    ("Zee News", ["zeenews.india.com"], "TV / video", "IN", []),
    ("Republic World", ["republicworld.com"], "TV / video", "IN", ["Republic"]),
    ("Deccan Herald", ["deccanherald.com"], "News site", "IN", []),
    ("The Tribune", ["tribuneindia.com"], "News site", "IN", ["Tribune India"]),
    ("Free Press Journal", ["freepressjournal.in"], "News site", "IN", ["The Free Press Journal"]),
    ("Outlook", ["outlookindia.com"], "News site", "IN", ["Outlook India"]),
    ("Swarajya", ["swarajyamag.com"], "News site", "IN", []),
    ("Patrika", ["patrika.com"], "News site", "IN", []),
    ("Amar Ujala", ["amarujala.com"], "News site", "IN", []),
    ("Dainik Bhaskar", ["bhaskar.com"], "News site", "IN", []),
    ("ANI", ["aninews.in"], "Wire agency", "IN", ["ANI News"]),
    ("PTI", ["ptinews.com"], "Wire agency", "IN", ["Press Trust of India"]),
    ("IANS", ["ianslive.in"], "Wire agency", "IN", ["IANS Live"]),
    ("News On AIR", ["newsonair.gov.in"], "Public broadcaster", "IN", ["All India Radio"]),
    ("The Print", ["theprint.in"], "News site", "IN", ["ThePrint"]),
    ("Scroll", ["scroll.in"], "News site", "IN", ["Scroll.in"]),
    ("The Quint", ["thequint.com"], "News site", "IN", []),
    ("Firstpost", ["firstpost.com"], "News site", "IN", []),
    ("WION", ["wionews.com"], "TV / video", "IN", []),
    ("MediaNama", ["medianama.com"], "Trade press", "IN", []),
    ("Inc42", ["inc42.com"], "Trade press", "IN", []),
    ("Entrackr", ["entrackr.com"], "Trade press", "IN", []),
    ("BBC News", ["bbc.com", "bbc.co.uk"], "News site", "GB", ["BBC"]),
    ("Reuters", ["reuters.com"], "Wire agency", "GB", []),
    ("Associated Press", ["apnews.com"], "Wire agency", "US", ["AP News", "AP"]),
    ("Al Jazeera", ["aljazeera.com"], "News site", "QA", []),
    ("The Guardian", ["theguardian.com"], "News site", "GB", ["Guardian"]),
    ("CNN", ["cnn.com", "edition.cnn.com"], "TV / video", "US", []),
    ("Financial Times", ["ft.com"], "News site", "GB", ["FT"]),
    ("Dhaka Tribune", ["dhakatribune.com"], "News site", "BD", []),
    ("The Daily Star", ["thedailystar.net"], "News site", "BD", []),
    ("SiGMA", ["sigma.world"], "Trade press", "MT", ["SiGMA World"]),
    ("CalvinAyre", ["calvinayre.com"], "Trade press", "CA", ["CalvinAyre.com"]),
    ("Asia Gaming Brief", ["agbrief.com"], "Trade press", "MO", ["AGB"]),
    ("iGaming Business", ["igamingbusiness.com"], "Trade press", "GB", ["iGB"]),
    ("AffPapa", ["affpapa.com"], "Trade press", "MT", []),
    ("Blask", ["blask.com"], "Industry analytics", "MT", []),
    ("Mondaq", ["mondaq.com"], "Legal publisher", "GB", []),
]

TLD_COUNTRY = {".in": "IN", ".co.in": "IN", ".uk": "GB", ".bd": "BD", ".pk": "PK", ".lk": "LK", ".np": "NP",
               ".au": "AU", ".nz": "NZ", ".ca": "CA", ".sg": "SG", ".ae": "AE", ".ph": "PH", ".mt": "MT"}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace("the ", "").split())


NAME_INDEX: dict[str, tuple] = {}
DOMAIN_INDEX: dict[str, tuple] = {}
for row in KNOWN_OUTLETS:
    NAME_INDEX[_norm(row[0])] = row
    for a in row[4]:
        NAME_INDEX[_norm(a)] = row
    for d in row[1]:
        DOMAIN_INDEX[d] = row


def lookup(name: str | None, domain: str | None):
    """Return the KNOWN_OUTLETS row for a name or domain, or None."""
    if domain:
        d = domain.lower().removeprefix("www.")
        while d:
            if d in DOMAIN_INDEX:
                return DOMAIN_INDEX[d]
            if "." not in d:
                break
            d = d.split(".", 1)[1]
    if name:
        return NAME_INDEX.get(_norm(name))
    return None


def guess_country(domain: str | None) -> str | None:
    if not domain:
        return None
    for tld, cc in sorted(TLD_COUNTRY.items(), key=lambda kv: -len(kv[0])):
        if domain.endswith(tld):
            return cc
    return None
