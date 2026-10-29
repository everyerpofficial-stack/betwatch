"""PDF report generator for BetWatch using ReportLab.
Produces publication-quality intelligence dossiers:
1. Subject / Dashboard report (GET /api/reports/subject.pdf)
2. Channel Profile dossier (GET /api/reports/channel/{id}.pdf)
"""
from __future__ import annotations

import io
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, List, Optional
import xml.sax.saxutils as saxutils

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import Paragraph as _Paragraph
from sqlalchemy.orm import Session

try:
    from .channel_profile import build_channel_profile_data
    from .dashboard import Filters, build_dashboard
except ImportError:
    from app.services.channel_profile import build_channel_profile_data
    from app.services.dashboard import Filters, build_dashboard

# ---------------------------------------------------------------- Font setup
# Latin text uses a system TTF (or built-in Helvetica). The bundled Noto Sans
# Devanagari has no Latin letters, so it is only applied to Devanagari runs.
_FONT_REGULAR = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"

for _path, _bpath in [
    ("C:\\Windows\\Fonts\\arial.ttf", "C:\\Windows\\Fonts\\arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]:
    if os.path.exists(_path):
        try:
            pdfmetrics.registerFont(TTFont("CustomSans", _path))
            _FONT_REGULAR = "CustomSans"
            if os.path.exists(_bpath):
                pdfmetrics.registerFont(TTFont("CustomSans-Bold", _bpath))
                _FONT_BOLD = "CustomSans-Bold"
            else:
                _FONT_BOLD = "CustomSans"
            # Lets <b> inside regular paragraphs resolve to the bold face
            pdfmetrics.registerFontFamily("CustomSans", normal=_FONT_REGULAR, bold=_FONT_BOLD)
            break
        except Exception:
            pass

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FONT_DIR = os.path.join(_APP_DIR, "fonts")
_DEVA_REG = os.path.join(_FONT_DIR, "NotoSansDevanagari-Regular.ttf")
_DEVA_BOLD = os.path.join(_FONT_DIR, "NotoSansDevanagari-Bold.ttf")

_DEVA_FONT: Optional[str] = None
_DEVA_FONT_BOLD: Optional[str] = None
if os.path.exists(_DEVA_REG):
    try:
        pdfmetrics.registerFont(TTFont("NotoDeva", _DEVA_REG))
        _DEVA_FONT = _DEVA_FONT_BOLD = "NotoDeva"
        if os.path.exists(_DEVA_BOLD):
            pdfmetrics.registerFont(TTFont("NotoDeva-Bold", _DEVA_BOLD))
            _DEVA_FONT_BOLD = "NotoDeva-Bold"
    except Exception:
        pass

_DEVA_RUN = re.compile(r"[\u0900-\u097F\u1CD0-\u1CFF\uA8E0-\uA8FF\u200C\u200D]+")
_MARKUP_TAG = re.compile(r"(<[^>]*>)")
_BOLD_TAG = re.compile(r"<(/?)(?:b|strong)\s*>", re.IGNORECASE)
_MATH_ALNUM = re.compile(r"[\U0001D400-\U0001D7FF]")


def _with_script_fonts(markup: str, style: Optional[ParagraphStyle]) -> str:
    """Wrap Devanagari runs in a <font> tag for the Devanagari face, keeping bold state."""
    if not _DEVA_FONT or not _DEVA_RUN.search(markup):
        return markup
    bold_depth = 1 if style is not None and style.fontName == _FONT_BOLD else 0
    out = []
    for part in _MARKUP_TAG.split(markup):
        if part.startswith("<"):
            m = _BOLD_TAG.fullmatch(part)
            if m:
                bold_depth += -1 if m.group(1) else 1
        elif part:
            face = _DEVA_FONT_BOLD if bold_depth > 0 else _DEVA_FONT
            part = _DEVA_RUN.sub(lambda r: f'<font face="{face}">{r.group(0)}</font>', part)
        out.append(part)
    return "".join(out)


class Paragraph(_Paragraph):
    """Paragraph that renders Devanagari text with the bundled Devanagari font."""

    def __init__(self, text, style=None, *args, **kwargs):
        # Paragraph.split() re-instantiates with text=None and pre-built frags
        if isinstance(text, str):
            text = _with_script_fonts(text, style)
        super().__init__(text, style, *args, **kwargs)


def clean(text: Any) -> str:
    """Safely escape text for ReportLab Paragraph XML parsing and strip non-printable chars."""
    if text is None:
        return ""
    s = str(text)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    # "Fancy" math-alphabet letters (common in YouTube titles) have no glyphs in our fonts
    s = _MATH_ALNUM.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), s)
    return saxutils.escape(s.strip())


# ---------------------------------------------------------------- Canvas with Page X of Y
class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict[str, Any]] = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_page_decorations(num_pages)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def _draw_page_decorations(self, total_pages: int):
        self.saveState()
        self.setFont(_FONT_REGULAR, 7.5)
        self.setFillColor(colors.HexColor("#64748b"))

        # Running Header (pages > 1)
        if self._pageNumber > 1:
            self.drawString(36, 11 * inch - 28, "BETWATCH INTELLIGENCE DOSSIER")
            self.drawRightString(8.5 * inch - 36, 11 * inch - 28, "CONFIDENTIAL & PROPRIETARY")
            self.setStrokeColor(colors.HexColor("#e2e8f0"))
            self.setLineWidth(0.5)
            self.line(36, 11 * inch - 32, 8.5 * inch - 36, 11 * inch - 32)

        # Running Footer (all pages)
        self.setStrokeColor(colors.HexColor("#e2e8f0"))
        self.setLineWidth(0.5)
        self.line(36, 36, 8.5 * inch - 36, 36)

        footer_text = "BetWatch Intelligence Platform • Open-source monitoring • Real publisher feeds only"
        self.drawString(36, 24, footer_text)
        page_str = f"Page {self._pageNumber} of {total_pages}"
        self.drawRightString(8.5 * inch - 36, 24, page_str)
        self.restoreState()


# ---------------------------------------------------------------- Styles
def get_pdf_styles():
    ss = getSampleStyleSheet()

    c_navy = colors.HexColor("#0f172a")
    c_muted = colors.HexColor("#64748b")
    c_body = colors.HexColor("#1e293b")

    styles = {
        "DocTitle": ParagraphStyle(
            "DocTitle",
            parent=ss["Normal"],
            fontName=_FONT_BOLD,
            fontSize=18,
            leading=22,
            textColor=c_navy,
            spaceAfter=4,
        ),
        "DocMeta": ParagraphStyle(
            "DocMeta",
            parent=ss["Normal"],
            fontName=_FONT_REGULAR,
            fontSize=8,
            leading=11,
            textColor=c_muted,
            spaceAfter=12,
        ),
        "SectionHeading": ParagraphStyle(
            "SectionHeading",
            parent=ss["Normal"],
            fontName=_FONT_BOLD,
            fontSize=11,
            leading=14,
            textColor=c_navy,
            spaceBefore=10,
            spaceAfter=4,
            keepWithNext=True,
        ),
        "Body": ParagraphStyle(
            "Body",
            parent=ss["Normal"],
            fontName=_FONT_REGULAR,
            fontSize=8.5,
            leading=11.5,
            textColor=c_body,
        ),
        "BodyBold": ParagraphStyle(
            "BodyBold",
            parent=ss["Normal"],
            fontName=_FONT_BOLD,
            fontSize=8.5,
            leading=11.5,
            textColor=c_body,
        ),
        "Muted": ParagraphStyle(
            "Muted",
            parent=ss["Normal"],
            fontName=_FONT_REGULAR,
            fontSize=7.5,
            leading=10,
            textColor=c_muted,
        ),
        "KPILabel": ParagraphStyle(
            "KPILabel",
            parent=ss["Normal"],
            fontName=_FONT_BOLD,
            fontSize=7.5,
            leading=9,
            textColor=c_muted,
            alignment=1,  # Center
        ),
        "KPIVal": ParagraphStyle(
            "KPIVal",
            parent=ss["Normal"],
            fontName=_FONT_BOLD,
            fontSize=15,
            leading=17,
            textColor=c_navy,
            alignment=1,  # Center
        ),
        "TableCell": ParagraphStyle(
            "TableCell",
            parent=ss["Normal"],
            fontName=_FONT_REGULAR,
            fontSize=8,
            leading=10.5,
            textColor=c_body,
        ),
        "TableCellBold": ParagraphStyle(
            "TableCellBold",
            parent=ss["Normal"],
            fontName=_FONT_BOLD,
            fontSize=8,
            leading=10.5,
            textColor=c_navy,
        ),
        "TableCellMuted": ParagraphStyle(
            "TableCellMuted",
            parent=ss["Normal"],
            fontName=_FONT_REGULAR,
            fontSize=7.5,
            leading=9.5,
            textColor=c_muted,
        ),
        "ToneNeg": ParagraphStyle(
            "ToneNeg",
            parent=ss["Normal"],
            fontName=_FONT_BOLD,
            fontSize=7.5,
            leading=9.5,
            textColor=colors.HexColor("#dc2626"),
        ),
        "ToneNeu": ParagraphStyle(
            "ToneNeu",
            parent=ss["Normal"],
            fontName=_FONT_BOLD,
            fontSize=7.5,
            leading=9.5,
            textColor=colors.HexColor("#475569"),
        ),
        "TonePos": ParagraphStyle(
            "TonePos",
            parent=ss["Normal"],
            fontName=_FONT_BOLD,
            fontSize=7.5,
            leading=9.5,
            textColor=colors.HexColor("#16a34a"),
        ),
    }
    return styles


# ---------------------------------------------------------------- Report A: Subject PDF
def generate_subject_pdf(db: Session, f: Filters) -> bytes:
    """Generates a comprehensive PDF intelligence report for the filtered dashboard state."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=40,
        bottomMargin=46,
    )

    styles = get_pdf_styles()
    story: list[Any] = []

    # 1. Dashboard data retrieval
    dash_filter = Filters(**{**f.__dict__, "limit": 200, "offset": 0})
    dash = build_dashboard(db, dash_filter)
    kpis = dash.get("k", {})
    stories = dash.get("stories", [])
    channels = dash.get("channels", [])

    now_str = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    # Title & Metadata
    subject_label = f.outlet if f.outlet else (f.q if f.q else (dash.get("heading") or "All Monitored Topics"))
    story.append(Paragraph(f"BetWatch Intelligence Report: {clean(subject_label)}", styles["DocTitle"]))

    filter_desc = []
    if f.q:
        filter_desc.append(f'Search: "{f.q}"')
    if f.cat and f.cat != "All":
        filter_desc.append(f"Category: {f.cat}")
    if f.time:
        filter_desc.append(f"Time: {f.time}")
    if f.outlet:
        filter_desc.append(f"Channel: {f.outlet}")
    if f.journo:
        filter_desc.append(f"Journalist: {f.journo}")
    if f.tone and f.tone != "All":
        filter_desc.append(f"Tone: {f.tone}")
    filter_txt = " • ".join(filter_desc) if filter_desc else "Scope: Comprehensive Monitored Feeds"

    story.append(Paragraph(f"Generated: {now_str} • {clean(filter_txt)}", styles["DocMeta"]))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cbd5e1"), spaceAfter=10))

    # 2. KPI Cards Row
    kpi_cards = [
        [
            Paragraph(str(kpis.get("stories", 0)), styles["KPIVal"]),
            Paragraph(str(kpis.get("posts", 0)), styles["KPIVal"]),
            Paragraph(str(kpis.get("outlets", 0)), styles["KPIVal"]),
            Paragraph(f"{kpis.get('negPct', 0)}%", styles["KPIVal"]),
            Paragraph(clean(kpis.get("first", "—")), styles["KPIVal"]),
        ],
        [
            Paragraph("STORIES TRACKED", styles["KPILabel"]),
            Paragraph("TOTAL POSTS", styles["KPILabel"]),
            Paragraph("ACTIVE CHANNELS", styles["KPILabel"]),
            Paragraph("CRACKDOWN TONE", styles["KPILabel"]),
            Paragraph("FIRST PUBLISHER", styles["KPILabel"]),
        ],
    ]
    t_kpis = Table(kpi_cards, colWidths=[108, 108, 108, 108, 108])
    t_kpis.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(t_kpis)
    story.append(Spacer(1, 14))

    # 3. Top Outlets Table (channels are pre-sorted by posts matching the report filters)
    top_channels = [ch for ch in channels if ch.get("n", 0) > 0]
    if top_channels:
        story.append(Paragraph("Key Monitored Channels & Volume", styles["SectionHeading"]))
        ch_rows = [
            [
                Paragraph("Outlet Channel", styles["TableCellBold"]),
                Paragraph("Coverage Posts", styles["TableCellBold"]),
                Paragraph("Share of Monitored Posts", styles["TableCellBold"]),
            ]
        ]
        total_p = max(sum(ch["n"] for ch in top_channels), 1)
        for ch in top_channels[:10]:
            c_cnt = ch["n"]
            pct = round((c_cnt / total_p) * 100, 1)
            ch_rows.append(
                [
                    Paragraph(clean(ch.get("name", "—")), styles["TableCellBold"]),
                    Paragraph(str(c_cnt), styles["TableCell"]),
                    Paragraph(f"{pct}%", styles["TableCell"]),
                ]
            )

        t_ch = Table(ch_rows, colWidths=[280, 130, 130])
        t_ch.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(t_ch)
        story.append(Spacer(1, 14))

    # 4. Detailed Story Clusters
    story.append(Paragraph(f"Story Clusters Breakdown ({len(stories)} stories)", styles["SectionHeading"]))
    story.append(
        Paragraph(
            "Multi-source clusters grouped by semantic topic similarity, showing publisher timeline.",
            styles["Muted"],
        )
    )
    story.append(Spacer(1, 6))

    if not stories:
        story.append(Paragraph("No stories matched the selected filters.", styles["Muted"]))
    else:
        for idx, s in enumerate(stories[:40], 1):
            s_tone = (s.get("tone") or "neutral").lower()
            tone_style = (
                styles["ToneNeg"]
                if s_tone == "negative"
                else (styles["TonePos"] if s_tone == "positive" else styles["ToneNeu"])
            )
            tone_label = s_tone.upper()

            cluster_box = []
            hdr = f"<b>#{idx}. {clean(s.get('title', 'Untitled'))}</b>"
            cluster_box.append([Paragraph(hdr, styles["BodyBold"]), Paragraph(f"[{tone_label}]", tone_style)])

            first_pub = s.get("first_name") or "Unknown"
            first_at = s.get("date_label") or s.get("first_seen") or ""
            cov_count = s.get("outlet_count") or len(s.get("posts", []))
            cat_list = ", ".join(s.get("cats", ["General"]))

            sub_line = (
                f"First reported by <b>{clean(first_pub)}</b> ({clean(first_at)}) • "
                f"Coverage: <b>{cov_count}</b> outlets • Category: <b>{clean(cat_list)}</b>"
            )
            cluster_box.append([Paragraph(sub_line, styles["Muted"]), Paragraph("", styles["Muted"])])

            # Bullet points of top posts in cluster
            post_items = []
            for p in s.get("posts", [])[:4]:
                p_outlet = clean(p.get("o", "Unknown"))
                p_head = clean(p.get("h", ""))
                p_time = clean(p.get("published_at", ""))
                bullet = f"• <b>{p_outlet}</b>: {p_head}"
                if p_time:
                    bullet += f" <font color='#64748b'>({p_time})</font>"
                post_items.append(Paragraph(bullet, styles["TableCell"]))

            t_cluster = Table(cluster_box, colWidths=[460, 80])
            t_cluster.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )

            flowables = [t_cluster]
            if post_items:
                flowables.append(Spacer(1, 3))
                for pi in post_items:
                    flowables.append(pi)
            flowables.append(Spacer(1, 8))

            story.append(KeepTogether(flowables))

    # 5. Appendix & Methodology
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cbd5e1"), spaceAfter=8))
    story.append(Paragraph("Audit & Methodology Statement", styles["SectionHeading"]))
    statement = (
        "This dossier is compiled autonomously by BetWatch using deterministic data pipelines. "
        "Post feeds are polled strictly from authenticated RSS feeds, Google News syndicated feeds, and the YouTube API. "
        "No artificial or synthetic posts are fabricated. Sentiment classification is based on keyword crack-down lexicons. "
        "For legal verification or source audit, refer to the immutable URL links provided for each post."
    )
    story.append(Paragraph(statement, styles["Muted"]))

    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()


# ---------------------------------------------------------------- Report B: Channel PDF
def generate_channel_pdf(db: Session, outlet_id: int) -> bytes:
    """Generates an in-depth intelligence dossier for a single media channel."""
    profile = build_channel_profile_data(db, outlet_id)
    if not profile or not profile.get("outlet"):
        raise ValueError(f"Channel {outlet_id} not found")

    o = profile["outlet"]
    entity = profile.get("profile", {})
    audience = profile.get("audience", [])
    track = profile.get("track_record", {})
    analytics = profile.get("coverage", {})
    posts = profile.get("posts", [])
    wiki = profile.get("wikidata", {})

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=40,
        bottomMargin=46,
    )

    styles = get_pdf_styles()
    story: list[Any] = []
    now_str = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    # 1. Header / Hero
    story.append(Paragraph(f"Channel Dossier: {clean(o['name'])}", styles["DocTitle"]))
    story.append(
        Paragraph(
            f"Domain: {clean(o['domain'])} • Country: {clean(o.get('country') or 'unknown')} • Generated: {now_str}",
            styles["DocMeta"],
        )
    )
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cbd5e1"), spaceAfter=10))

    # 2. Entity Overview (Wikidata / Corporate)
    story.append(Paragraph("Corporate & Identity Profile", styles["SectionHeading"]))
    entity_data = [
        [
            Paragraph("Legal / Registered Name:", styles["TableCellBold"]),
            Paragraph(clean(entity.get("legal_name") or o["name"]), styles["TableCell"]),
            Paragraph("Founded:", styles["TableCellBold"]),
            Paragraph(clean(entity.get("founded_year") or "Not recorded"), styles["TableCell"]),
        ],
        [
            Paragraph("Parent Organization:", styles["TableCellBold"]),
            Paragraph(clean(entity.get("parent_org") or "Independent / Unlisted"), styles["TableCell"]),
            Paragraph("Headquarters:", styles["TableCellBold"]),
            Paragraph(clean(entity.get("headquarters") or "Not recorded"), styles["TableCell"]),
        ],
        [
            Paragraph("Stock Ticker:", styles["TableCellBold"]),
            Paragraph(clean(entity.get("stock_ticker") or "Private / Unlisted"), styles["TableCell"]),
            Paragraph("Wikidata Entity:", styles["TableCellBold"]),
            Paragraph(clean(wiki.get("qid") or "Not linked"), styles["TableCell"]),
        ],
    ]
    t_entity = Table(entity_data, colWidths=[135, 135, 135, 135])
    t_entity.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(t_entity)
    story.append(Spacer(1, 12))

    # 3. Audience & Reach Metrics (Golden rule: no fake data)
    story.append(Paragraph("Audience Reach & Verification Status", styles["SectionHeading"]))
    aud_rows = [
        [
            Paragraph("Platform", styles["TableCellBold"]),
            Paragraph("Metric", styles["TableCellBold"]),
            Paragraph("Status / Value", styles["TableCellBold"]),
            Paragraph("Verification Source / Reason", styles["TableCellBold"]),
        ]
    ]
    for a in audience:
        val_str = a.get("value")
        status = a.get("status", "Not available")
        if val_str:
            status_p = Paragraph(f"<b>{clean(val_str)}</b>", styles["TableCellBold"])
            src_str = f"Source: {clean(a.get('source', ''))}"
            src_p = Paragraph(src_str, styles["TableCellMuted"])
        else:
            status_p = Paragraph("<i>Not available</i>", styles["TableCellMuted"])
            reason_str = clean(a.get("reason") or "No API key configured")
            src_p = Paragraph(f"<font color='#94a3b8'>{reason_str}</font>", styles["TableCellMuted"])

        aud_rows.append(
            [
                Paragraph(clean(a.get("platform", "")), styles["TableCellBold"]),
                Paragraph(clean(a.get("metric", "")), styles["TableCell"]),
                status_p,
                src_p,
            ]
        )

    t_aud = Table(aud_rows, colWidths=[90, 100, 110, 240])
    t_aud.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(t_aud)
    story.append(Spacer(1, 12))

    # 4. Coverage Analytics
    story.append(Paragraph("Betting Coverage Analytics", styles["SectionHeading"]))
    first_post_info = analytics.get("first_post") or {}
    latest_post_info = analytics.get("latest_post") or {}
    tone_split = analytics.get("tone_split") or {}

    an_kpis = [
        [
            Paragraph(str(analytics.get("total_posts", 0)), styles["KPIVal"]),
            Paragraph(clean(first_post_info.get("date") or "—"), styles["KPIVal"]),
            Paragraph(clean(latest_post_info.get("date") or "—"), styles["KPIVal"]),
        ],
        [
            Paragraph("TOTAL BETTING POSTS", styles["KPILabel"]),
            Paragraph("FIRST CAPTURED POST", styles["KPILabel"]),
            Paragraph("LATEST CAPTURED POST", styles["KPILabel"]),
        ],
    ]
    t_an = Table(an_kpis, colWidths=[180, 180, 180])
    t_an.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(t_an)
    story.append(Spacer(1, 4))

    tone_txt = f"Negative: {tone_split.get('Negative', 0)} • Neutral: {tone_split.get('Neutral', 0)} • Positive: {tone_split.get('Positive', 0)}"
    story.append(Paragraph(f"<b>Tone distribution:</b> {tone_txt}", styles["Muted"]))
    story.append(Spacer(1, 12))

    # 5. Track Record & Fact Checks
    story.append(Paragraph("Track Record & Regulatory History", styles["SectionHeading"]))
    legal_cases = track.get("legal_cases", []) or []
    wins_losses = track.get("wins_losses", []) or []
    fc_info = track.get("fact_checks", {}) or {}
    fc_claims = fc_info.get("claims", []) or []

    tr_rows = [
        [
            Paragraph("Date", styles["TableCellBold"]),
            Paragraph("Category", styles["TableCellBold"]),
            Paragraph("Record / Detail", styles["TableCellBold"]),
        ]
    ]
    for tr in legal_cases:
        tr_rows.append([
            Paragraph(clean(tr.get("as_of") or "—"), styles["TableCellMuted"]),
            Paragraph("Legal Case", styles["TableCellBold"]),
            Paragraph(clean(tr.get("fact", "")), styles["TableCell"]),
        ])
    for tr in wins_losses:
        tr_rows.append([
            Paragraph(clean(tr.get("as_of") or "—"), styles["TableCellMuted"]),
            Paragraph("Ruling / Win-Loss", styles["TableCellBold"]),
            Paragraph(clean(tr.get("fact", "")), styles["TableCell"]),
        ])
    for fc in fc_claims:
        tr_rows.append([
            Paragraph(clean(fc.get("date") or "—"), styles["TableCellMuted"]),
            Paragraph(f"Fact Check: {clean(fc.get('rating') or 'Claim')}", styles["TableCellBold"]),
            Paragraph(clean(fc.get("text", "")), styles["TableCell"]),
        ])

    if len(tr_rows) == 1:
        msg = "No adverse legal cases, rulings, or regulatory penalties recorded in database for this channel."
        if fc_info.get("status") == "Not available":
            msg += f" (Fact-checks: {fc_info.get('reason', 'API key required')})"
        story.append(Paragraph(msg, styles["Muted"]))
    else:
        t_track = Table(tr_rows, colWidths=[80, 110, 350])
        t_track.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(t_track)

    story.append(Spacer(1, 12))

    # 6. Full Collected Posts List
    story.append(Paragraph(f"Collected Posts Archive ({len(posts)} posts)", styles["SectionHeading"]))
    if not posts:
        story.append(Paragraph("No posts captured for this channel yet.", styles["Muted"]))
    else:
        post_rows = [
            [
                Paragraph("Date", styles["TableCellBold"]),
                Paragraph("Headline", styles["TableCellBold"]),
                Paragraph("Tone", styles["TableCellBold"]),
            ]
        ]
        for p in posts[:120]:
            p_tone = (p.get("tone") or "neutral").lower()
            tone_s = (
                styles["ToneNeg"]
                if p_tone == "negative"
                else (styles["TonePos"] if p_tone == "positive" else styles["ToneNeu"])
            )
            date_fmt = (p.get("published_at_fmt") or "")[:10]
            post_rows.append(
                [
                    Paragraph(clean(date_fmt), styles["TableCellMuted"]),
                    Paragraph(clean(p.get("title", "")), styles["TableCell"]),
                    Paragraph(clean(p_tone.upper()), tone_s),
                ]
            )

        t_posts = Table(post_rows, colWidths=[70, 410, 60])
        t_posts.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(t_posts)

    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()
