"""test_custom_html.py - offline tests for the custom_html parser.

Covers the two parse passes (JSON-LD, anchors) and the false-positive cases
that make a naive careers-page parser useless: nav furniture, self-links,
duplicate hrefs, prose links and malformed JSON-LD.

Run: python jobs_pipeline/test_custom_html.py
"""
import os
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs_pipeline.adapters import custom_html as ch

def parse(html, url="https://example.com/careers"):
    soup = BeautifulSoup(html, "html.parser")
    jobs = ch._parse_jsonld(soup, url)
    strategy = "json-ld"
    if not jobs:
        jobs = ch._parse_anchors(soup, url)
        strategy = "anchors"
    return jobs, strategy

fails = []
def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n        got={got}\n        want={want}")
    if not ok:
        fails.append(name)

# 1. JSON-LD single posting, nested address
JSONLD = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Delivery & Operations Assistant",
 "url":"https://example.com/careers/delivery-ops",
 "description":"<p>Support <b>delivery</b> across teams.</p>",
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"Galway","addressCountry":{"name":"Ireland"}}}}
</script></head><body></body></html>"""
jobs, strat = parse(JSONLD)
check("json-ld: strategy", strat, "json-ld")
check("json-ld: title", [j["title"] for j in jobs], ["Delivery & Operations Assistant"])
check("json-ld: location flattened", [j["location_raw"] for j in jobs], ["Galway, Ireland"])
check("json-ld: html stripped from summary",
      [j["summary"] for j in jobs], ["Support delivery across teams."])

# 2. JSON-LD inside @graph, plus a non-JobPosting sibling that must be ignored
GRAPH = """<script type="application/ld+json">
{"@graph":[{"@type":"Organization","name":"Acme"},
 {"@type":["JobPosting"],"title":"Backend Engineer","url":"/careers/be"}]}
</script>"""
jobs, _ = parse(GRAPH)
check("@graph: only JobPosting picked", [j["title"] for j in jobs], ["Backend Engineer"])
check("@graph: relative url absolutised",
      [j["url"] for j in jobs], ["https://example.com/careers/be"])

# 3. Anchors: real job links kept, nav furniture dropped
ANCHORS = """<html><body>
<nav><a href="/careers">Careers</a><a href="/about">About us</a></nav>
<ul>
  <li><a href="/careers/senior-data-scientist">Senior Data Scientist</a></li>
  <li><a href="/jobs/product-manager-dublin">Product Manager, Dublin</a></li>
  <li><a href="https://boards.greenhouse.io/acme/jobs/55">Sports Scientist</a></li>
</ul>
<a href="/careers/apply">Apply now</a>
<a href="mailto:jobs@example.com">jobs@example.com</a>
<a href="/blog/our-culture-post">We believe culture matters. It shapes everything. Read on.</a>
</body></html>"""
jobs, strat = parse(ANCHORS)
check("anchors: strategy", strat, "anchors")
check("anchors: titles", sorted(j["title"] for j in jobs),
      ["Product Manager, Dublin", "Senior Data Scientist", "Sports Scientist"])

# 3b. Card-style listing: the whole card is one <a>, so anchor text is
# "title + blurb + tags". The title must come from the first inner block,
# not the concatenated anchor text. Regression: Playhera's two live roles
# were silently dropped by the word-count filter before this was handled.
CARD = """<a href="/jobs/regional-creative-producer">
  <div><p>Regional Creative Producer</p>
  <p>Originate, script, and brief hyper-localized ad creatives for Arabic
     sub-markets - from the first concept through to delivery and beyond.</p>
  <div><span>Creative</span><span>Riyadh, KSA</span><span>Full-time</span></div>
  </div></a>"""
jobs, _ = parse(CARD)
check("card anchor: title from inner block", [j["title"] for j in jobs],
      ["Regional Creative Producer"])

# 3c. Heading inside the anchor wins over body text
HEADING = """<a href="/careers/head-of-data"><h3>Head of Data</h3>
<p>We are looking for someone to own our data platform end to end today.</p></a>"""
check("card anchor: heading preferred", [j["title"] for j in parse(HEADING)[0]],
      ["Head of Data"])

# 4. Duplicate hrefs collapse
DUP = """<a href="/careers/x">Data Engineer</a><a href="/careers/x">Data Engineer</a>"""
jobs, _ = parse(DUP)
check("anchors: duplicate href collapsed", len(jobs), 1)

# 5. Self-link to the careers page itself is not a job
SELF = """<a href="/careers">Careers</a><a href="https://example.com/careers/">Open roles</a>"""
jobs, _ = parse(SELF)
check("anchors: self-link ignored", jobs, [])

# 6. Nothing parseable -> empty, not junk
check("empty page -> []", parse("<html><body><p>No openings.</p></body></html>")[0], [])

# 7. Malformed JSON-LD must not raise, falls through to anchors
BAD = """<script type="application/ld+json">{not valid json</script>
<a href="/careers/qa-lead">QA Lead</a>"""
jobs, strat = parse(BAD)
check("bad json-ld falls through to anchors", ([j["title"] for j in jobs], strat),
      (["QA Lead"], "anchors"))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
