"""
weekly_cover.py
Renders the Sports D3c0d3d weekly LinkedIn cover image (new brand, July 2026).

Runs in GitHub Actions (weekly_cover.yml, Fri 09:20 + 10:20 UTC, DST guard by
task existence) roughly 20 minutes after the Cowork scheduled task has drafted
the weekly news brief and written a Cockpit task (ops.tasks, source_schema
'sd3-weekly-post', source_table 'news-brief') whose notes end with a
machine-readable line:

    PICKS_JSON: [{"company": "...", "slug": "UPPERCASE SLUG", "news_url": "..."}, ...]

(news_url is null for stories that came from the LinkedIn radar rather than
the news scrape.)

Flow:
  1. Find today's news-brief task. None yet -> exit 0 (an early firing).
  2. If the notes already record a cover for the CURRENT picks hash -> exit 0.
     If the picks changed since the last render (Iddo swapped a story in the
     run's chat and the trigger session rewrote PICKS_JSON), re-render and
     overwrite the same storage object, so the attachment stays valid.
  3. Parse PICKS_JSON, join news_items on url, pull mirrored images from
     sd3_cover_assets (base64, mirrored Friday 06:45 UTC by the
     mirror-news-images edge function). Missing image -> branded gradient tile.
  4. Build the 1200x1200 cover HTML (Bebas Neue + logo from assets/cover/),
     render with Playwright chromium.
  5. Upload to the public 'radar' bucket at weekly-covers/YYYY-MM-DD.png,
     insert an ops.task_attachments row on the task, and append the public
     URL to the task notes.
"""

import base64
import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from supabase import create_client

BRAND_COLOURS = ["#C15BE6", "#22D3A5", "#F59E0B", "#00B4D8"]
GRADIENTS = [
    "linear-gradient(135deg,#241030,#3a1a4d)",
    "linear-gradient(135deg,#0d2b24,#14453a)",
    "linear-gradient(135deg,#2e2410,#4d3c14)",
    "linear-gradient(135deg,#0e2334,#123c52)",
]
ASSETS = Path(__file__).parent / "assets" / "cover"
BUCKET = "radar"


def get_client():
    url = os.environ["NEXT_PUBLIC_SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


def find_task(client):
    res = (
        client.schema("ops").table("tasks")
        .select("id,notes,created_at")
        .eq("source_schema", "sd3-weekly-post")
        .eq("source_table", "news-brief")
        .gte("created_at", date.today().isoformat())
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def picks_hash(picks: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(picks, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:8]


def has_cover_attachment(client, task_id: str) -> bool:
    res = (
        client.schema("ops").table("task_attachments")
        .select("id,storage_path")
        .eq("task_id", task_id)
        .execute()
    )
    return any((r.get("storage_path") or "").startswith("weekly-covers/") for r in res.data or [])


def parse_picks(notes: str) -> list[dict]:
    m = re.search(r"PICKS_JSON:\s*(\[.*\])", notes, re.DOTALL)
    if not m:
        return []
    try:
        picks = json.loads(m.group(1))
        return picks if isinstance(picks, list) else []
    except json.JSONDecodeError:
        return []


def fetch_images(client, picks: list[dict]) -> dict[str, str]:
    """Return {news_url: data_url} for picks whose image was mirrored."""
    urls = [p["news_url"] for p in picks if p.get("news_url")]
    if not urls:
        return {}
    items = (
        client.table("news_items").select("id,url").in_("url", urls).execute().data or []
    )
    id_by_url = {i["url"]: i["id"] for i in items}
    out = {}
    for url, nid in id_by_url.items():
        assets = (
            client.table("sd3_cover_assets")
            .select("content_type,image_b64")
            .eq("news_item_id", nid)
            .eq("kind", "news")
            .limit(1)
            .execute()
            .data
        )
        if assets and assets[0].get("image_b64"):
            out[url] = f"data:{assets[0]['content_type']};base64,{assets[0]['image_b64']}"
    return out


def file_data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def tile_html(pick: dict, img: str | None, colour: str, gradient: str, hero: bool) -> str:
    company = (pick.get("company") or "").upper()
    slug = (pick.get("slug") or "").upper()
    inner = (
        f'<img src="{img}">' if img
        else f'<div style="position:absolute;inset:0;background:{gradient}"></div>'
    )
    cls = "tile hero" if hero else "tile"
    return (
        f'<div class="{cls}">{inner}<div class="shade"></div>'
        f'<div class="label"><span class="co" style="background:{colour}">{company}</span>'
        f'<div class="slug">{slug}</div></div></div>'
    )


def build_html(picks: list[dict], images: dict[str, str]) -> str:
    logo = file_data_url(ASSETS / "logo_horizontal_white.png", "image/png")
    font = file_data_url(ASSETS / "BebasNeue.ttf", "font/ttf")
    today = date.today().strftime("%d %B %Y").upper().lstrip("0")

    # Hero = first pick that has an image, else first pick.
    ordered = sorted(
        picks, key=lambda p: 0 if images.get(p.get("news_url") or "") else 1
    )
    hero, rest = ordered[0], ordered[1:5]

    tiles = [tile_html(hero, images.get(hero.get("news_url") or ""), BRAND_COLOURS[0], GRADIENTS[0], True)]
    row_tiles = [
        tile_html(p, images.get(p.get("news_url") or ""), BRAND_COLOURS[(i + 1) % 4], GRADIENTS[(i + 1) % 4], False)
        for i, p in enumerate(rest)
    ]
    rows = ""
    for i in range(0, len(row_tiles), 2):
        rows += f'<div class="row">{"".join(row_tiles[i:i + 2])}</div>'

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@font-face {{ font-family:'Bebas'; src:url('{font}'); }}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:1200px;height:1200px;background:#0B1B2B;font-family:'Bebas',sans-serif;position:relative;overflow:hidden}}
.header{{display:flex;align-items:center;justify-content:space-between;padding:34px 56px 18px 56px}}
.header img{{height:100px}}
.hright{{text-align:right}}
.hright .w{{font-size:84px;color:#fff;letter-spacing:5px;line-height:.9}}
.hright .d{{font-size:30px;color:#00B4D8;letter-spacing:7px;margin-top:6px}}
.bars{{display:flex;gap:14px;padding:0 56px;margin-bottom:22px}}
.bars div{{height:14px;border-radius:7px}}
.b1{{width:300px;background:#C15BE6}}.b2{{width:150px;background:#22D3A5}}.b3{{width:420px;background:#F59E0B}}.b4{{flex:1;background:#00B4D8}}
.grid{{padding:0 56px}}
.tile{{position:relative;border-radius:22px;overflow:hidden;background:#122638}}
.tile img{{width:100%;height:100%;object-fit:cover;display:block;filter:saturate(1.05)}}
.shade{{position:absolute;inset:0;background:linear-gradient(180deg,rgba(11,27,43,0) 35%,rgba(11,27,43,.92) 100%)}}
.label{{position:absolute;left:26px;bottom:20px;right:26px}}
.co{{display:inline-block;font-size:36px;color:#0B1B2B;padding:2px 16px 0 16px;border-radius:10px;letter-spacing:2px}}
.slug{{font-size:30px;color:#fff;letter-spacing:1.5px;margin-top:8px;text-shadow:0 2px 8px rgba(0,0,0,.6)}}
.hero{{height:380px;margin-bottom:20px}}
.row{{display:flex;gap:20px;margin-bottom:20px}}
.row .tile{{flex:1;height:238px}}
.footer{{position:absolute;bottom:0;left:0;right:0;background:#08131f;padding:16px 56px;display:flex;justify-content:space-between;align-items:center}}
.footer .t{{font-size:30px;color:#7f93a3;letter-spacing:8px}}
.footer .n{{font-size:30px;color:#00B4D8;letter-spacing:4px}}
</style></head><body>
<div class="header"><img src="{logo}">
<div class="hright"><div class="w">WEEKLY</div><div class="d">{today}</div></div></div>
<div class="bars"><div class="b1"></div><div class="b2"></div><div class="b3"></div><div class="b4"></div></div>
<div class="grid">{tiles[0]}{rows}</div>
<div class="footer"><div class="t">IRELAND'S SPORTSTECH INTELLIGENCE</div><div class="n">SPORTSD3C0D3D.IE</div></div>
</body></html>"""


def render(html: str, out_path: str):
    from playwright.sync_api import sync_playwright

    Path("/tmp/sd3_cover.html").write_text(html, encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1200, "height": 1200})
        pg.goto("file:///tmp/sd3_cover.html")
        pg.wait_for_timeout(500)
        pg.screenshot(path=out_path)
        b.close()


def main() -> int:
    client = get_client()
    task = find_task(client)
    if not task:
        print("No news-brief task today; nothing to do.")
        return 0
    notes = task.get("notes") or ""
    picks = parse_picks(notes)
    if not picks:
        print("Task found but no PICKS_JSON in notes; nothing to do.")
        return 0
    h = picks_hash(picks)
    if f"Cover picks hash: {h}" in notes:
        print(f"Cover already rendered for picks hash {h}; nothing to do.")
        return 0

    images = fetch_images(client, picks)
    print(f"Picks: {len(picks)}, images available: {len(images)}")
    html = build_html(picks, images)
    out = "/tmp/sd3_weekly_cover.png"
    render(html, out)

    storage_path = f"weekly-covers/{date.today().isoformat()}.png"
    png = Path(out).read_bytes()
    client.storage.from_(BUCKET).upload(
        storage_path, png, {"content-type": "image/png", "upsert": "true"}
    )
    if not has_cover_attachment(client, task["id"]):
        client.schema("ops").table("task_attachments").insert({
            "task_id": task["id"],
            "kind": "file",
            "file_name": f"sd3-weekly-cover-{date.today().isoformat()}.png",
            "mime_type": "image/png",
            "size_bytes": len(png),
            "bucket": BUCKET,
            "storage_path": storage_path,
        }).execute()

    public_url = (
        f"{os.environ['NEXT_PUBLIC_SUPABASE_URL']}/storage/v1/object/public/{BUCKET}/{storage_path}"
    )
    # Replace any previous cover lines, then append the fresh ones.
    kept = [
        ln for ln in notes.splitlines()
        if not ln.startswith("Cover image: ") and not ln.startswith("Cover picks hash: ")
    ]
    new_notes = "\n".join(kept).rstrip() + f"\n\nCover image: {public_url}\nCover picks hash: {h}"
    client.schema("ops").table("tasks").update({"notes": new_notes}).eq("id", task["id"]).execute()
    print(f"Cover uploaded (hash {h}): {public_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
