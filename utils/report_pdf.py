"""The violations report as a PDF. Pure rendering: a Report goes in, bytes come out.

Nothing here touches the database, aiogram or the clock. utils/violation_report.py gathers
the numbers into a Report; this module only draws them, which keeps the layout testable
without a bot token and keeps the two concerns from growing into each other.

The look is the "Report file · PDF" pair of A4 pages in the Fleet Alerts design: a masthead
with the company and the window, four fleet-wide figures, the by-type bars, then the units
that had the most alerts, each with every event under it. Coordinates below are the design's
own CSS pixels, y measured from the top of an 794 x 1123 page, and scaled onto the real A4
sheet in one place (_Pen) so a number in this file can be compared with the artboard directly.

Only the built-in Helvetica is used, so the file needs no font on the server. Its
encoding is Windows-1252: anything outside it (a roster name in another script) is drawn
as "?" rather than crashing the whole report over one character.
"""

from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

# ── the data ────────────────────────────────────────────────────────────────────

@dataclass
class Event:
    when: datetime          # already in the report's own time zone
    label: str
    color: str              # "#rrggbb"


@dataclass
class UnitBlock:
    unit: str
    total: int
    events: list[Event] = field(default_factory=list)


@dataclass
class Report:
    company: str
    period: str             # "Last Week"
    range_text: str         # "Oct 05 — Oct 11, 2026"
    generated: datetime
    alerts: int
    units_with_alerts: int
    speeding: int
    crashes: int
    types: list[tuple[str, str, int]]       # (label, color, count), biggest first
    units: list[UnitBlock]
    footer_name: str = ""   # defaults to the company


# ── the page ────────────────────────────────────────────────────────────────────

PAGE_W, PAGE_H = 794, 1123          # the artboard, CSS px
SCALE = A4[0] / PAGE_W              # CSS px -> PDF points

MARGIN_X = 52
TOP = 48
FOOT_TOP = PAGE_H - 40 - 14         # y of the footer's text line
CONTENT_BOTTOM = FOOT_TOP - 12 - 12  # footer rule, plus air above it
CONTENT_W = PAGE_W - 2 * MARGIN_X

INK = "#101828"
T2 = "#475467"
T3 = "#667085"
LINE = "#E4E7EC"
SUNK = "#F6F7F9"
ACCENT = "#2F5BEA"
CRASH = "#8B1220"

ROW_H = 22
UNIT_HEAD_H = 20 + 6 + 1 + 4        # the line, its padding, its rule, the gap under it
UNIT_GAP = 16
MIN_ROWS_WITH_HEAD = 2              # never leave a unit's name alone at a page bottom

BOLD = "Helvetica-Bold"
REG = "Helvetica"


def _latin(text: str) -> str:
    """Text the built-in font can draw. Anything else becomes "?"."""
    return text.encode("cp1252", "replace").decode("cp1252")


def _fit(text: str, font: str, size: float, width: float) -> str:
    """Shortened with an ellipsis until it fits `width` px at `size` px."""
    text = _latin(text)
    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + "...", font, size) > width:
        text = text[:-1]
    return text.rstrip() + "..."


class _Pen:
    """A Canvas that speaks the design's units: px, y down from the top of the page."""

    def __init__(self, canvas: Canvas):
        self.c = canvas
        self.c.scale(SCALE, SCALE)

    def fill(self, color: str):
        self.c.setFillColor(HexColor(color))

    def rect(self, x, y, w, h, color, radius=0):
        self.fill(color)
        if radius:
            self.c.roundRect(x, PAGE_H - y - h, w, h, radius, stroke=0, fill=1)
        else:
            self.c.rect(x, PAGE_H - y - h, w, h, stroke=0, fill=1)

    def circle(self, cx, cy, r, color):
        self.fill(color)
        self.c.circle(cx, PAGE_H - cy, r, stroke=0, fill=1)

    def text(self, x, baseline, string, font=REG, size=12, color=INK, *,
             align="left", spacing=0.0):
        """`baseline` is where the letters sit, measured from the top."""
        self.fill(color)
        string = _latin(string)
        t = self.c.beginText()
        t.setFont(font, size)
        t.setCharSpace(spacing)
        width = stringWidth(string, font, size) + spacing * len(string)
        if align == "right":
            x -= width
        t.setTextOrigin(x, PAGE_H - baseline)
        t.textOut(string)
        self.c.drawText(t)


# ── pieces ──────────────────────────────────────────────────────────────────────

def _masthead(pen: _Pen, report: Report) -> float:
    pen.text(MARGIN_X, TOP + 12, "VIOLATIONS REPORT", BOLD, 11, ACCENT, spacing=1.3)
    pen.text(MARGIN_X, TOP + 52,
             _fit(report.company, BOLD, 30, CONTENT_W - 200), BOLD, 30, INK)
    pen.text(MARGIN_X, TOP + 76, f"{report.period} · {report.range_text}", REG, 14, T2)
    right = PAGE_W - MARGIN_X
    pen.text(right, TOP + 62, "Generated", REG, 11, T3, align="right")
    pen.text(right, TOP + 78,
             report.generated.strftime("%b %d, %Y · ") + _clock(report.generated) + " ET",
             BOLD, 11, T2, align="right")
    pen.rect(MARGIN_X, TOP + 96, CONTENT_W, 3, ACCENT)
    return TOP + 99


def _clock(when: datetime) -> str:
    return when.strftime("%I:%M %p").lstrip("0")


def _stats(pen: _Pen, report: Report, y: float) -> float:
    y += 20
    gap = 10
    w = (CONTENT_W - 3 * gap) / 4
    cells = [
        (report.alerts, "Alerts", INK),
        (report.units_with_alerts, "Units with alerts", INK),
        (report.speeding, "Speeding", INK),
        (report.crashes, "Crashes", CRASH),
    ]
    for i, (value, label, color) in enumerate(cells):
        x = MARGIN_X + i * (w + gap)
        pen.rect(x, y, w, 72, SUNK, radius=12)
        pen.text(x + 14, y + 12 + 22, f"{value:,}", BOLD, 26, color)
        pen.text(x + 14, y + 12 + 30 + 2 + 12, label, REG, 11.5, T2)
    return y + 72


def _heading(pen: _Pen, y: float, label: str):
    pen.text(MARGIN_X, y + 12, label.upper(), BOLD, 12, T3, spacing=0.7)


def _by_type(pen: _Pen, report: Report, y: float) -> float:
    if not report.types:
        return y
    y += 22
    _heading(pen, y, "By event type")
    y += 16 + 6
    peak = max(count for *_, count in report.types) or 1
    track_x = MARGIN_X + 18 + 130 + 10
    track_w = CONTENT_W - (18 + 130 + 10) - 10 - 22
    for label, color, count in report.types:
        mid = y + ROW_H / 2
        pen.circle(MARGIN_X + 4, mid, 4, color)
        pen.text(MARGIN_X + 18, mid + 4, _fit(label, REG, 12, 124), REG, 12, INK)
        pen.rect(track_x, mid - 3, track_w, 6, SUNK, radius=3)
        pen.rect(track_x, mid - 3, max(6.0, track_w * count / peak), 6, color, radius=3)
        pen.text(MARGIN_X + CONTENT_W, mid + 4, f"{count:,}", BOLD, 12, INK, align="right")
        y += ROW_H
    return y


def _unit_head(pen: _Pen, rank: int, block: UnitBlock, y: float, *, continued: bool):
    name = f"Unit {block.unit}" + (" (continued)" if continued else "")
    pen.text(MARGIN_X, y + 15, str(rank), BOLD, 13, ACCENT)
    pen.text(MARGIN_X + 32, y + 15, _fit(name, BOLD, 15, CONTENT_W - 32 - 110), BOLD, 15, INK)
    n = block.total
    pen.text(MARGIN_X + CONTENT_W, y + 15, f"{n:,} event{'s' if n != 1 else ''}",
             REG, 12.5, T2, align="right")
    pen.rect(MARGIN_X, y + 26, CONTENT_W, 1, LINE)


def _event_row(pen: _Pen, event: Event, y: float, shaded: bool):
    if shaded:
        pen.rect(MARGIN_X, y, CONTENT_W, ROW_H, SUNK)
    base = y + ROW_H / 2 + 4
    x = MARGIN_X + 10
    pen.text(x, base, event.when.strftime("%b %d"), REG, 12, T2)
    pen.text(x + 62, base, _clock(event.when), REG, 12, T2)
    pen.circle(x + 62 + 76 + 4, y + ROW_H / 2, 4, event.color)
    pen.text(x + 62 + 76 + 16, base,
             _fit(event.label, REG, 12, CONTENT_W - 20 - 62 - 76 - 16), REG, 12, INK)


def _running_header(pen: _Pen, report: Report) -> float:
    base = TOP + 12
    left = f"{report.company}"
    pen.text(MARGIN_X, base, _fit(left, BOLD, 11, CONTENT_W - 160), BOLD, 11, INK)
    used = stringWidth(_latin(_fit(left, BOLD, 11, CONTENT_W - 160)), BOLD, 11)
    pen.text(MARGIN_X + used + 6, base, f"· {report.period} · {report.range_text}",
             REG, 11, T3)
    pen.text(MARGIN_X + CONTENT_W, base, "Violations report", REG, 11, T3, align="right")
    pen.rect(MARGIN_X, base + 10, CONTENT_W, 1, LINE)
    return base + 11


def _footer(pen: _Pen, report: Report, page: int, pages: int):
    pen.rect(MARGIN_X, FOOT_TOP - 12, CONTENT_W, 1, LINE)
    name = report.footer_name or report.company
    pen.text(MARGIN_X, FOOT_TOP + 7, f"{_fit(name, REG, 10.5, 400)} · Fleet Alerts",
             REG, 10.5, T3)
    pen.text(MARGIN_X + CONTENT_W, FOOT_TOP + 7, f"Page {page} of {pages}",
             REG, 10.5, T3, align="right")


# ── the document ────────────────────────────────────────────────────────────────

def _draw(report: Report, total_pages: int | None) -> tuple[bytes, int]:
    """One pass. `total_pages` is None on the counting pass, where "of N" is a placeholder."""
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=A4, pageCompression=0)
    canvas.setTitle(f"{report.company} - Violations report - {report.period}")
    canvas.setAuthor("Fleet Alerts")
    pen = _Pen(canvas)
    page = 1

    def new_page() -> float:
        nonlocal page
        _footer(pen, report, page, total_pages or page)
        canvas.showPage()
        page += 1
        pen_reset()
        return _running_header(pen, report)

    def pen_reset():
        # showPage drops the graphics state, scale included.
        canvas.scale(SCALE, SCALE)

    y = _masthead(pen, report)
    y = _stats(pen, report, y)
    y = _by_type(pen, report, y)

    if report.units:
        y += 26
        _heading(pen, y, f"Top {len(report.units)} units · most alerts first"
                 if len(report.units) > 1 else "Units · most alerts first")
        y += 16

    for rank, block in enumerate(report.units, 1):
        events = list(block.events)
        continued = False
        while True:
            need = UNIT_GAP + UNIT_HEAD_H + ROW_H * min(len(events), MIN_ROWS_WITH_HEAD)
            if y + need > CONTENT_BOTTOM:
                y = new_page()
            y += UNIT_GAP
            _unit_head(pen, rank, block, y, continued=continued)
            y += UNIT_HEAD_H
            room = int((CONTENT_BOTTOM - y) // ROW_H)
            shown, events = events[:max(room, 1)], events[max(room, 1):]
            for i, event in enumerate(shown):
                _event_row(pen, event, y, shaded=i % 2 == 0)
                y += ROW_H
            if not events:
                break
            continued = True
            y = new_page()
            y -= UNIT_GAP          # the loop adds the gap back; the page already has air

    note = (f"End of report. The list shows the {len(report.units)} units with the most "
            f"alerts; the totals on page 1 cover all {report.units_with_alerts:,} units "
            f"that had any.") if report.units else \
           "No violations were recorded in this period."
    lines = simpleSplit(_latin(note), REG, 12, CONTENT_W - 32)
    box_h = 28 + 18 * len(lines)
    if y + 28 + box_h > CONTENT_BOTTOM:
        y = new_page()
    y += 28
    pen.rect(MARGIN_X, y, CONTENT_W, box_h, SUNK, radius=12)
    for i, line in enumerate(lines):
        pen.text(MARGIN_X + 16, y + 14 + 13 + 18 * i, line, REG, 12, T2)

    _footer(pen, report, page, total_pages or page)
    canvas.showPage()
    canvas.save()
    return buffer.getvalue(), page


def render(report: Report) -> bytes:
    """The finished PDF.

    Drawn twice: the footer says "Page 1 of N" and N is only known once the units have
    been laid out. The first pass is thrown away; the second repeats it with N filled in.
    """
    _, pages = _draw(report, None)
    data, _ = _draw(report, pages)
    return data
