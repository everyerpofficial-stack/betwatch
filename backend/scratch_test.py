import json
import re
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
import httpx


def parse_insta(url: str) -> dict:
    try:
        headers = {
            "User-Agent": "facebookexternalhit/1.1",
            "Accept": "*/*",
        }
        r = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")

        def get_meta(attr, val):
            tag = soup.find("meta", {attr: val})
            return tag["content"] if tag and tag.get("content") else None

        og_title = get_meta("property", "og:title") or ""
        tw_title = get_meta("name", "twitter:title") or ""
        og_desc = get_meta("property", "og:description") or get_meta("name", "description") or ""
        og_url = get_meta("property", "og:url") or str(r.url)
        og_img = get_meta("property", "og:image") or get_meta("name", "twitter:image")

        name = None
        handle = None

        # 1. Try twitter:title e.g. 'Mumbai Insights (@mumbai.insight) • Instagram reel'
        m_tw = re.search(r"^(.*?)\s*\(@([a-zA-Z0-9._]+)\)", tw_title)
        if m_tw:
            name = m_tw.group(1).strip()
            handle = m_tw.group(2).strip()

        # 2. Extract handle from canonical / og:url e.g. instagram.com/mumbai.insight/reel/...
        if not handle:
            m_u = re.search(r"instagram\.com/([a-zA-Z0-9._]+)/(?:reel|p)/", og_url)
            if m_u and m_u.group(1) not in ("reel", "p", "tv"):
                handle = m_u.group(1)

        # 3. Extract display name from og:title e.g. 'Mumbai Insights on Instagram: ...'
        if not name and " on Instagram:" in og_title:
            name = og_title.split(" on Instagram:", 1)[0].strip()
        elif not name and " on Instagram" in og_title:
            name = og_title.split(" on Instagram", 1)[0].strip()

        # 4. Extract date and caption
        # og:description format: '29 likes, 0 comments - mumbai.insight on October 23, 2026: "Bhiwandi Police..."'
        pub_date = None
        caption = None
        m_desc = re.search(r"-\s+[a-zA-Z0-9._]+\s+on\s+([A-Za-z]+\s+\d{1,2},\s*\d{4}):\s*[\"|\']?(.*)$", og_desc, re.DOTALL)
        if m_desc:
            date_str = m_desc.group(1).strip()
            caption = m_desc.group(2).strip().rstrip("\"' .")
            try:
                pub_date = dateparser.parse(date_str)
            except Exception:
                pass
        else:
            caption = og_desc

        if not caption and " on Instagram: " in og_title:
            caption = og_title.split(" on Instagram: ", 1)[1].strip().strip("\"'")

        first_line = caption.split("\n")[0] if caption else ""
        title = first_line[:200] if first_line else (og_title[:200] or "Instagram post")

        return {
            "name": name or handle or "Instagram User",
            "handle": handle,
            "title": title,
            "caption": caption,
            "date": pub_date.isoformat() if pub_date else None,
            "img": og_img,
            "url": og_url,
        }
    except Exception as e:
        return {"err": str(e)}


if __name__ == "__main__":
    test_url = "https://www.instagram.com/reel/De1S61WkyxW/"
    res = parse_insta(test_url)
    print(json.dumps(res, indent=2))

