"""
carousel_slides.py
Sports D3c0d3d weekly LinkedIn CAROUSEL: a cover slide plus one slide per news
pick, six 1200x1200 pages in total, plus the six-page PDF that LinkedIn accepts
as a document post.

Added 5 September 2026 when the Friday brief became a carousel and the AI take
moved to its own Wednesday video. Imported by weekly_cover.py, which owns the
Supabase side; this module is pure rendering and knows nothing about the task
row.

Design rules inherited from the cover (do not diverge):
  - navy #0B1B2B, Bebas Neue, logo from assets/cover/
  - the four brand accents are assigned BY POSITION, so a company keeps one
    colour across the post, the cover, its slide and the Skywork video. Reorder
    the layout if you must; never reorder the picks.
  - no CTA pointing at a "brief" page. There is no such page. The footer is
    IRELAND'S SPORTSTECH INTELLIGENCE / SPORTSD3C0D3D.IE.
"""

import base64
from datetime import date
from pathlib import Path

NAVY = "#0B1B2B"
BRAND_COLOURS = ["#C15BE6", "#22D3A5", "#F59E0B", "#00B4D8"]
GRADIENTS = [
    "linear-gradient(135deg,#3b1d52,#0B1B2B)",
    "linear-gradient(135deg,#0f4a3c,#0B1B2B)",
    "linear-gradient(135deg,#5a3b0a,#0B1B2B)",
    "linear-gradient(135deg,#0a3d52,#0B1B2B)",
]
ASSETS = Path(__file__).parent / "assets" / "cover"


def _data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _base_css(font: str) -> str:
    return f"""
@font-face {{ font-family:'Bebas'; src:url('{font}'); }}
*{{margin:0;padding:0;box-sizing:border-box}}
.page{{width:1200px;height:1200px;background:{NAVY};font-family:'Bebas',sans-serif;
position:relative;overflow:hidden;color:#fff}}
.header{{display:flex;align-items:center;justify-content:space-between;padding:34px 56px 18px 56px}}
.header img{{height:100px}}
.hright{{text-align:right}}
.hright .w{{font-size:84px;color:#fff;letter-spacing:5px;line-height:.9}}
.hright .d{{font-size:30px;color:#00B4D8;letter-spacing:7px;margin-top:6px}}
.bars{{display:flex;gap:14px;padding:0 56px;margin-bottom:22px}}
.bars div{{height:14px;border-radius:7px}}
.b1{{width:300px;background:#C15BE6}}.b2{{width:150px;background:#22D3A5}}
.b3{{width:420px;background:#F59E0B}}.b4{{flex:1;background:#00B4D8}}
.footer{{position:absolute;bottom:0;left:0;right:0;background:#08131f;padding:16px 56px;
display:flex;justify-content:space-between;align-items:center}}
.footer .t{{font-size:30px;color:#7f93a3;letter-spacing:8px}}
.footer .n{{font-size:30px;color:#00B4D8;letter-spacing:4px}}
.lead{{padding:0 56px;margin-bottom:26px}}
.lead .k{{font-size:96px;letter-spacing:3px;line-height:.95}}
.lead .s{{font-size:32px;color:#7f93a3;letter-spacing:6px;margin-top:14px}}
.list{{padding:0 56px}}
.crow{{display:flex;gap:22px;align-items:stretch;margin-bottom:18px}}
.rail{{width:10px;border-radius:5px;flex:none}}
.ctext{{flex:1;padding-top:2px}}
.cco{{font-size:34px;letter-spacing:2px}}
.cslug{{font-size:38px;color:#fff;letter-spacing:1.5px;line-height:1.05;margin-top:2px}}
.swipe{{position:absolute;bottom:96px;left:56px;right:56px;display:flex;
justify-content:flex-end;align-items:center;gap:14px;color:#7f93a3;
font-size:30px;letter-spacing:6px}}
.arrow{{font-size:36px;color:#00B4D8;letter-spacing:0}}
.photo{{position:absolute;top:0;left:0;right:0;height:560px;overflow:hidden;background:#122638}}
.photo img{{width:100%;height:100%;object-fit:cover;display:block;filter:saturate(1.05)}}
.pshade{{position:absolute;inset:0;background:linear-gradient(180deg,rgba(11,27,43,.72) 0%,
rgba(11,27,43,.1) 26%,rgba(11,27,43,.15) 62%,{NAVY} 100%)}}
.topbar{{position:absolute;top:34px;left:56px;right:56px;display:flex;
justify-content:space-between;align-items:center;z-index:3}}
.topbar img{{height:64px}}
.idx{{font-size:34px;color:#fff;letter-spacing:4px;background:rgba(8,19,31,.75);
padding:4px 18px 1px 18px;border-radius:10px}}
.sbody{{position:absolute;top:560px;bottom:72px;left:0;right:0;padding:0 56px;
display:flex;flex-direction:column;justify-content:center}}
.crail{{width:160px;height:12px;border-radius:6px;margin-bottom:34px}}
.co{{align-self:flex-start;font-size:46px;color:{NAVY};
padding:5px 24px 1px 24px;border-radius:12px;letter-spacing:2px}}
.slug{{font-size:92px;color:#fff;letter-spacing:1.5px;line-height:1.0;margin-top:30px}}
"""


def _footer() -> str:
    return ('<div class="footer"><div class="t">IRELAND\'S SPORTSTECH INTELLIGENCE</div>'
            '<div class="n">SPORTSD3C0D3D.IE</div></div>')


def cover_page(picks: list[dict], logo: str, issue_date: date) -> str:
    """Slide 1: contents stack, one line per story, swipe cue.

    Deliberately NOT the 2x2 grid used by the standalone cover image: a grid of
    five thumbnails competes with the five slides that follow it.
    """
    rows = ""
    for i, p in enumerate(picks):
        c = BRAND_COLOURS[i % 4]
        rows += (
            f'<div class="crow"><div class="rail" style="background:{c}"></div>'
            f'<div class="ctext"><div class="cco" style="color:{c}">'
            f'{(p.get("company") or "").upper()}</div>'
            f'<div class="cslug">{(p.get("slug") or "").upper()}</div></div></div>'
        )
    n = {1: "ONE", 2: "TWO", 3: "THREE", 4: "FOUR", 5: "FIVE"}.get(len(picks), str(len(picks)))
    stories = "STORY" if len(picks) == 1 else "STORIES"
    d = issue_date.strftime("%d %B %Y").upper().lstrip("0")
    return f"""<div class="page">
<div class="header"><img src="{logo}">
<div class="hright"><div class="w">WEEKLY</div><div class="d">{d}</div></div></div>
<div class="bars"><div class="b1"></div><div class="b2"></div><div class="b3"></div><div class="b4"></div></div>
<div class="lead"><div class="k">{n} {stories}<br>FROM IRISH SPORTSTECH</div>
<div class="s">THIS WEEK</div></div>
<div class="list">{rows}</div>
<div class="swipe">SWIPE <span class="arrow">&#8250;&#8250;</span></div>
{_footer()}</div>"""


def story_page(pick: dict, i: int, total: int, img: str | None, logo: str) -> str:
    """Slide i+2: photo, accent rail, company chip, headline."""
    c = BRAND_COLOURS[i % 4]
    g = GRADIENTS[i % 4]
    inner = (
        f'<img src="{img}">' if img
        else f'<div style="position:absolute;inset:0;background:{g}"></div>'
    )
    return f"""<div class="page">
<div class="photo">{inner}<div class="pshade"></div></div>
<div class="topbar"><img src="{logo}"><div class="idx">{i + 2} / {total}</div></div>
<div class="sbody"><div class="crail" style="background:{c}"></div>
<div class="co" style="background:{c}">{(pick.get("company") or "").upper()}</div>
<div class="slug">{(pick.get("slug") or "").upper()}</div></div>
{_footer()}</div>"""


def build_pages(picks: list[dict], images: dict[str, str],
                issue_date: date | None = None) -> tuple[str, list[str]]:
    """Return (css, [page_html, ...]) with the cover first, then one per pick."""
    issue_date = issue_date or date.today()
    logo = _data_url(ASSETS / "logo_horizontal_white.png", "image/png")
    font = _data_url(ASSETS / "BebasNeue.ttf", "font/ttf")
    total = len(picks) + 1
    pages = [cover_page(picks, logo, issue_date)]
    for i, p in enumerate(picks):
        pages.append(story_page(p, i, total, images.get(p.get("news_url") or ""), logo))
    return _base_css(font), pages


def _doc(css: str, body: str, paged: bool) -> str:
    extra = (
        "@page{size:1200px 1200px;margin:0}"
        ".page{page-break-after:always;break-after:page}"
        ".page:last-child{page-break-after:auto;break-after:auto}"
        if paged else "body{margin:0}"
    )
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>{css}{extra}'
            f"</style></head><body>{body}</body></html>")


def render_carousel(picks: list[dict], images: dict[str, str],
                    out_dir: str, issue_date: date | None = None) -> tuple[list[Path], Path]:
    """Render the slides as PNGs and the whole set as one PDF.

    Returns ([png_paths in order], pdf_path). Uses the Playwright chromium the
    cover render already depends on; no extra PDF library.
    """
    from playwright.sync_api import sync_playwright

    issue_date = issue_date or date.today()
    css, pages = build_pages(picks, images, issue_date)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = issue_date.isoformat()
    png_paths: list[Path] = []
    pdf_path = out / f"sd3-carousel-{stamp}.pdf"

    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1200, "height": 1200})
        for n, html in enumerate(pages):
            f = out / f"slide-{n + 1:02d}.html"
            f.write_text(_doc(css, html, paged=False), encoding="utf-8")
            page.goto(f.as_uri())
            page.wait_for_timeout(400)
            png = out / f"sd3-carousel-{stamp}-{n + 1:02d}.png"
            page.screenshot(path=str(png))
            png_paths.append(png)

        combined = out / "carousel.html"
        combined.write_text(_doc(css, "".join(pages), paged=True), encoding="utf-8")
        page.goto(combined.as_uri())
        page.wait_for_timeout(600)
        page.pdf(path=str(pdf_path), width="1200px", height="1200px",
                 print_background=True, margin={"top": "0", "right": "0",
                                                "bottom": "0", "left": "0"})
        b.close()

    return png_paths, pdf_path
