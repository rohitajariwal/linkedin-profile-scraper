#!/usr/bin/env python3
"""
LinkedIn Profile Extractor & LaTeX PDF Generator

A Playwright + BeautifulSoup script that logs in to LinkedIn manually,
extracts profile data from the visible page and details pages, then saves:

- linkedin_profile.json
- linkedin_profile.tex
- linkedin_profile.pdf
- linkedin_profile_debug.html

Use this only on your own profile or with permission.
"""

import asyncio
import json
import re
import subprocess
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


# ═══════════════════════════════════════════════════════════════════════════
#  TEXT UTILITIES
# ═══════════════════════════════════════════════════════════════════════════

def clean(text: str) -> str:
    return " ".join((text or "").split()).strip()


def esc(text: str) -> str:
    if not text:
        return ""
    for ch, rep in [
        ("\\", r"\textbackslash{}"),
        ("&",  r"\&"),
        ("%",  r"\%"),
        ("$",  r"\$"),
        ("#",  r"\#"),
        ("{",  r"\{"),
        ("}",  r"\}"),
        ("~",  r"\textasciitilde{}"),
        ("^",  r"\^{}"),
        ("<",  r"\textless{}"),
        (">",  r"\textgreater{}"),
    ]:
        text = text.replace(ch, rep)
    return text


# ═══════════════════════════════════════════════════════════════════════════
#  BROWSER HELPERS
# ═══════════════════════════════════════════════════════════════════════════

async def safe_eval(page, script: str, default=None):
    try:
        return await page.evaluate(script)
    except Exception:
        return default


async def wait_stable(page, timeout: int = 15_000):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    await page.wait_for_timeout(1_000)


async def full_scroll(page, max_steps: int = 40):
    prev, stall = 0, 0
    for _ in range(max_steps):
        await safe_eval(page, "window.scrollBy(0, 800)")
        await page.wait_for_timeout(550)
        curr = await safe_eval(page, "document.body.scrollHeight", prev)
        if curr == prev:
            stall += 1
            if stall >= 3:
                break
        else:
            stall = 0
        prev = curr
    await safe_eval(page, "window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)


async def click_see_more(page):
    """Expand inline '…see more' text-truncation buttons (not 'Show all X' —
    those navigate to a details page and are handled separately)."""
    for pattern in ["see more", "…see more"]:
        try:
            btns = await page.get_by_role("button").filter(
                has_text=re.compile(pattern, re.IGNORECASE)
            ).all()
            for btn in btns:
                try:
                    if not await btn.is_visible():
                        continue
                    before = page.url
                    await btn.click()
                    await page.wait_for_timeout(300)
                    if page.url != before:
                        await page.go_back()
                        await wait_stable(page, 8_000)
                except Exception:
                    pass
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION-CONTAINER LOOKUP
# ═══════════════════════════════════════════════════════════════════════════

# heading text(s) to look for, per logical section key
SECTION_HEADINGS = {
    "about":          ["about"],
    "experience":     ["experience"],
    "education":      ["education"],
    "skills":         ["skills"],
    "certifications": ["licenses", "certifications"],
    "projects":       ["projects"],
    "volunteer":      ["volunteer"],
    "honors":         ["honors", "awards"],
    "publications":   ["publications"],
    "languages":      ["languages"],
    "recommendations":["recommendations"],
}

# URL slug used by LinkedIn's "/details/<slug>/" subpages, per section key.
# Only sections that exist on the profile will resolve — others just
# redirect back to the main profile and are skipped automatically.
DETAILS_SLUGS = {
    "experience":     "experience",
    "education":      "education",
    "skills":         "skills",
    "certifications": "certifications",
    "projects":       "projects",
    "volunteer":      "volunteering-experience",
    "honors":         "honors-and-awards",
    "publications":   "publications",
    "languages":      "languages",
}


def find_section(soup: BeautifulSoup, key: str):
    """Find a profile section's container by its visible heading text
    (e.g. <h2>Experience</h2>), with aria-label as a secondary check.
    This is what actually exists and is stable in LinkedIn's current
    markup — unlike ids/classes, headings are real user-facing text
    LinkedIn can't obfuscate without breaking screen readers."""
    keywords = SECTION_HEADINGS.get(key, [key])

    for sec in soup.find_all("section"):
        h = sec.find(["h1", "h2", "h3", "h4"])
        if h:
            ht = clean(h.get_text()).lower()
            if any(kw in ht for kw in keywords):
                return sec

    for sec in soup.find_all("section"):
        label = (sec.get("aria-label") or "").lower()
        if any(kw in label for kw in keywords):
            return sec

    return None


def section_or_main(soup: BeautifulSoup, key: str):
    """Like find_section, but falls back to <main> (or the whole doc) if
    no matching heading is found — used on /details/ subpages, whose
    exact wrapper structure we haven't been able to inspect directly."""
    sec = find_section(soup, key)
    if sec:
        return sec
    main = soup.find("main")
    return main or soup


# ═══════════════════════════════════════════════════════════════════════════
#  GENERIC TEXT CLASSIFICATION  +  ENTRY GROUPING
#  (validated directly against the uploaded debug HTML — see module
#   docstring. This is the core fix for v4.)
# ═══════════════════════════════════════════════════════════════════════════

_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
_DATE_RE = re.compile(
    rf"\b{_MONTH}[a-z]*\.?\s*\d{{4}}\b|\b\d{{4}}\s*[-–—]\s*(\d{{4}}|present)\b|^\bissued\b",
    re.IGNORECASE,
)
_SKILLS_TAG_RE = re.compile(r"and\s*\+?\d+\s*skills?\b", re.IGNORECASE)
_CREDENTIAL_RE = re.compile(r"^credential id\b", re.IGNORECASE)
_GRADE_RE = re.compile(r"^grade\s*:", re.IGNORECASE)
_ATTACHMENT_RE = re.compile(r"\.(pdf|docx?|pptx?|png|jpe?g)$", re.IGNORECASE)
_ASSOC_RE = re.compile(r"^associated with\b", re.IGNORECASE)
_COUNTER_RE = re.compile(r"^\+\d+$")
_LOCTYPE_RE = re.compile(r"·\s*(hybrid|remote|on-?site)\b", re.IGNORECASE)
_PRONOUN_RE = re.compile(r"^(he/him|she/her|they/them|ze/zir)$", re.IGNORECASE)

# Common sentence-starters that signal "this is prose, not a title" — used
# to stop a short plain-text description from being mistaken for the start
# of a brand-new entry.
_SENTENCE_STARTERS = {
    "it", "this", "the", "a", "an", "working", "developed", "implemented",
    "conducted", "designed", "built", "created", "led", "managed",
    "responsible", "worked", "i", "currently", "assisting", "handling",
    "monitoring", "administered", "managing",
}


def _classify(t: str) -> str:
    if _COUNTER_RE.match(t):
        return "IGNORE"
    if _ATTACHMENT_RE.search(t):
        return "ATTACHMENT"
    if _CREDENTIAL_RE.match(t):
        return "CREDENTIAL"
    if _GRADE_RE.match(t):
        return "GRADE"
    if _ASSOC_RE.match(t):
        return "ASSOC"
    if _SKILLS_TAG_RE.search(t):
        return "SKILLS_TAG"
    if _DATE_RE.search(t):
        return "DATE"
    if _LOCTYPE_RE.search(t):
        return "LOCATION"
    if "•" in t or len(t) > 100:
        return "DESCRIPTION"
    return "OTHER"


def _word_set(t: str) -> set:
    return set(re.findall(r"[a-z']+", t.lower()))


def _is_redundant(t: str, description_parts: list) -> bool:
    """True if *t* is essentially a short restatement of text already
    captured as this entry's description (e.g. a media-embed caption
    that duplicates the project blurb) — used to avoid spawning a
    spurious extra entry out of it."""
    tw = _word_set(t)
    if not tw or len(tw) > 14:
        return False
    cw = _word_set(" ".join(description_parts))
    if not cw:
        return False
    return (len(tw & cw) / len(tw)) > 0.7


def _starts_with_stopword(t: str) -> bool:
    words = t.split()
    return bool(words) and words[0].lower() in _SENTENCE_STARTERS


def group_entries(texts: list) -> list:
    """Read a section's <p> texts in document order and group them into
    structured entries: {title, org, date, location, description, extra}.

    State machine, one pass:
      • The first OTHER-classified line starts a new entry as its title.
      • The next OTHER line (if the entry has no date yet) becomes org.
      • A DATE line locks the entry's date in.
      • After a date is locked in, a *plausible new title* (another OTHER
        line) starts the next entry — unless it looks like prose
        continuing the current entry (starts with a sentence-starter
        word, or is a near-duplicate of text already in the
        description), in which case it's folded into the description
        instead.
      • LOCATION / SKILLS_TAG / CREDENTIAL / GRADE / ATTACHMENT / ASSOC
        lines are metadata and never start a new entry.
      • Entries that end up with no org, no date, and no description
        (i.e. just a stray caption/skill-chip line) are dropped — they're
        noise from LinkedIn's media-embed previews, not real entries.
    """
    entries, cur, have_date = [], None, False

    def push():
        nonlocal cur
        if cur:
            entries.append(cur)
        cur = None

    for raw in texts:
        t = clean(raw)
        if not t:
            continue
        cls = _classify(t)

        if cls == "IGNORE":
            continue

        if cls == "OTHER":
            if cur and have_date and _is_redundant(t, cur["description"]):
                cur["description"].append(t)
                continue
            if cur and have_date and _starts_with_stopword(t):
                cur["description"].append(t)
                continue
            if cur is None or have_date:
                push()
                cur = {"title": t, "org": "", "date": "", "location": "",
                       "description": [], "extra": []}
                have_date = False
            elif not cur["org"]:
                cur["org"] = t
            else:
                cur["description"].append(t)

        elif cls == "DATE":
            if cur is None:
                continue
            if not cur["date"]:
                cur["date"] = t
                have_date = True
            else:
                cur["extra"].append(t)

        elif cls == "LOCATION":
            if cur:
                if not cur["location"]:
                    cur["location"] = t
                else:
                    cur["extra"].append(t)

        elif cls == "DESCRIPTION":
            if cur:
                cur["description"].append(t)

        else:  # ASSOC / SKILLS_TAG / CREDENTIAL / GRADE / ATTACHMENT
            if cur:
                cur["extra"].append(t)

    push()
    return [e for e in entries if e["org"] or e["date"] or e["description"]]


def texts_in(container) -> list:
    if not container:
        return []
    return [clean(p.get_text()) for p in container.find_all("p")]


_TRAILING_MORE_RE = re.compile(r"[…\.]*\s*more\s*$", re.IGNORECASE)


def _strip_trailing_more(text: str) -> str:
    """LinkedIn truncates long text with a trailing '… more' (note the
    space before 'more') for its own in-page 'see more' toggle; since we
    already have the full text, that trailing artifact is just noise."""
    return clean(_TRAILING_MORE_RE.sub("", text))


def entry_to_record(e: dict) -> dict:
    desc = _strip_trailing_more(" ".join(e["description"]))
    skills_extra = [x for x in e["extra"] if _SKILLS_TAG_RE.search(x)]
    other_extra = [x for x in e["extra"] if not _SKILLS_TAG_RE.search(x)]
    return {
        "title":       e["title"],
        "org":         e["org"],
        "date":        e["date"],
        "location":    e["location"],
        "description": clean(desc),
        "skills":      skills_extra,
        "extra":       other_extra,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  HEADER  /  ABOUT  /  SKILLS  /  LANGUAGES
# ═══════════════════════════════════════════════════════════════════════════

def extract_header(soup: BeautifulSoup) -> dict:
    """name / headline / location.

    LinkedIn's current build doesn't render an <h1> on the profile header
    at all — the name lives in an <h2> instead. We find that <h2>, walk
    up to its nearest <section>, then read the following <p> tags in
    order: skip the pronoun line ("He/Him"), the first remaining line is
    the headline, and the first later line that contains a comma but no
    middle-dot ("·") is the location (the company/school summary line
    that appears in between uses a "·" to join two names, so it's
    skipped automatically). <title>/meta tags are kept as a fallback
    chain for older profile layouts where this doesn't apply.
    """
    hdr = {"name": "", "headline": "", "location": ""}

    name_el, name_text = None, ""
    for h in soup.find_all(["h1", "h2"]):
        t = clean(h.get_text())
        if t and 2 <= len(t.split()) <= 6 and not re.search(r"\d", t) \
           and not re.search(r"[|•·]", t):
            name_el, name_text = h, t
            break

    if name_el:
        hdr["name"] = name_text
        sec = name_el.find_parent("section") or name_el.find_parent("div")
        if sec:
            ps = [clean(p.get_text()) for p in sec.find_all("p")]
            ps = [p for p in ps if p]
            for p in ps:
                if _PRONOUN_RE.match(p) or p == name_text:
                    continue
                if not hdr["headline"]:
                    hdr["headline"] = p
                    continue
                if not hdr["location"] and "," in p and "·" not in p and len(p) < 100:
                    hdr["location"] = p
                    break

    # ── Fallback chain: <title> tag / meta tags ───────────────────────
    if not hdr["name"] or not hdr["headline"]:
        title_tag = soup.find("title")
        raw_title = clean(title_tag.get_text()) if title_tag else ""
        raw_title = re.sub(r"^\(\d+\+?\)\s*", "", raw_title)
        raw_title = re.sub(r"\s*\|\s*LinkedIn\s*$", "", raw_title, flags=re.I)
        if " - " in raw_title:
            name_part, headline_part = raw_title.split(" - ", 1)
        else:
            name_part, headline_part = raw_title, ""
        if not hdr["name"] and name_part:
            hdr["name"] = clean(name_part)
        if not hdr["headline"] and headline_part:
            hdr["headline"] = clean(headline_part)

    if not hdr["headline"]:
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        if og_desc and og_desc.get("content"):
            hdr["headline"] = clean(og_desc["content"]).split(" · ")[0]

    return hdr


def extract_about(soup: BeautifulSoup) -> tuple:
    """Returns (about_text, top_skills_from_about). LinkedIn sometimes
    renders a 'Top skills' chip list directly inside the About section —
    detected and split out instead of polluting the about paragraph."""
    sec = find_section(soup, "about")
    if not sec:
        return "", []

    sec_copy = BeautifulSoup(str(sec), "html.parser")
    for tag in sec_copy.find_all(["h1", "h2", "h3", "h4", "button"]):
        tag.decompose()
    full_text = clean(sec_copy.get_text())

    top_skills, about_text = [], full_text
    m = re.search(r"Top skills(.*)$", full_text, flags=re.IGNORECASE)
    if m:
        about_text = clean(full_text[: m.start()])
        parts = re.split(r"\s*[•·]\s*", m.group(1))
        top_skills = [clean(p) for p in parts if clean(p)]

    about_text = about_text.replace("…more", "").strip()
    return about_text, top_skills


def extract_skills_from_texts(texts: list) -> list:
    """Skill chip lines, filtering out the 'used at <company>' /
    'endorsed by …' context lines LinkedIn renders under each skill."""
    skills = []
    for t in texts:
        t = clean(t)
        if not t or len(t) > 60:
            continue
        if re.search(r"\bat\b", t, re.IGNORECASE):
            continue
        if re.match(r"^[\d·\s]+$", t):
            continue
        if t.lower().startswith(("show all", "endorsed", "skill")):
            continue
        skills.append(t)
    return list(dict.fromkeys(skills))


def extract_languages_from_texts(texts: list) -> list:
    texts = [t for t in texts if t]
    langs, i = [], 0
    while i < len(texts):
        lang = texts[i]
        prof = texts[i + 1] if i + 1 < len(texts) and \
            re.search(r"proficien|native|fluent|basic|profession|elementary",
                       texts[i + 1], re.I) else ""
        langs.append({"language": lang, "proficiency": prof})
        i += 2 if prof else 1
    return langs


async def extract_skills_via_modal(page) -> list:
    """Fallback: click a 'Show all … skills' link/button if present and
    scrape the resulting view. Used in addition to direct /details/skills/
    navigation in case that URL doesn't resolve for some account types."""
    skills = []
    try:
        pattern = re.compile(r"show all.{0,25}skill", re.IGNORECASE)
        for role in ("link", "button"):
            loc = page.get_by_role(role, name=pattern)
            if await loc.count():
                before_url = page.url
                await loc.first.click()
                await page.wait_for_timeout(1500)
                await full_scroll(page, max_steps=15)
                html = await page.content()
                soup = BeautifulSoup(html, "html.parser")
                texts = texts_in(soup.find("main") or soup)
                skills = extract_skills_from_texts(texts)
                if page.url != before_url:
                    await page.go_back()
                    await wait_stable(page, 10_000)
                break
    except Exception:
        pass
    return skills


# ═══════════════════════════════════════════════════════════════════════════
#  LATEX GENERATION
# ═══════════════════════════════════════════════════════════════════════════

_PREAMBLE = r"""% LinkedIn Profile — auto-generated
% Compile: pdflatex linkedin_profile.tex
\documentclass[11pt, a4paper]{article}

% ── Packages ────────────────────────────────────────────────
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[a4paper, top=1.8cm, bottom=1.8cm, left=2.2cm, right=2.2cm]{geometry}
\usepackage{titlesec}
\usepackage{xcolor}
\usepackage{hyperref}
\usepackage{parskip}
\usepackage{microtype}
\usepackage{tabularx}
\usepackage{enumitem}

% ── Colours ─────────────────────────────────────────────────
\definecolor{liblue}{RGB}{0, 119, 181}
\definecolor{darkgray}{RGB}{45, 45, 45}
\definecolor{midgray}{RGB}{90, 90, 90}
\definecolor{lightgray}{RGB}{140, 140, 140}

% ── Font ────────────────────────────────────────────────────
\renewcommand{\familydefault}{\sfdefault}

% ── Section heading style ────────────────────────────────────
\titleformat{\section}
  {\large\bfseries\color{liblue}}{}{0pt}{}
  [{\color{liblue}\titlerule[0.9pt]}]
\titlespacing{\section}{0pt}{14pt}{5pt}

% ── Entry commands ───────────────────────────────────────────
\newcommand{\entryheader}[3]{%
  \begin{tabularx}{\linewidth}{@{}Xr@{}}
    \textbf{#1} & \small\color{lightgray}{#3}\\
    \small\textit{\color{midgray}#2} &\\
  \end{tabularx}%
}
\newcommand{\entrydetail}[1]{%
  \vspace{2pt}\small\color{darkgray}#1\par
}
\newcommand{\entryspace}{\vspace{6pt}}

% ── Hyperlinks ───────────────────────────────────────────────
\hypersetup{colorlinks=true, urlcolor=liblue, linkcolor=liblue}
\pagestyle{empty}
"""


def latex_entry(e: dict) -> str:
    title = esc(e.get("title", ""))
    org   = esc(e.get("org",   ""))
    date  = esc(e.get("date",  ""))
    loc   = esc(e.get("location", ""))
    desc  = esc(e.get("description", ""))
    lines = [r"\entryheader" + "{" + title + "}{" + org + "}{" + date + "}"]
    detail_bits = []
    if loc:
        detail_bits.append(loc)
    if desc:
        detail_bits.append(desc)
    if detail_bits:
        lines.append(r"\entrydetail{" + " — ".join(detail_bits) + "}")
    return "\n".join(lines)


def build_section(title: str, body: str) -> str:
    return "\n\\section{" + esc(title) + "}\n" + body + "\n"


def generate_latex(data: dict) -> str:
    name     = esc(data.get("name",     "Your Name"))
    headline = esc(data.get("headline", ""))
    location = esc(data.get("location", ""))
    about    = esc(data.get("about",    ""))

    parts = []
    if about:
        parts.append(build_section("About", about))

    for key, title in [
        ("experience",     "Experience"),
        ("education",      "Education"),
        ("certifications", "Certifications \\& Licences"),
        ("projects",       "Projects"),
        ("volunteer",      "Volunteer Experience"),
        ("honors",         "Honors \\& Awards"),
        ("publications",   "Publications"),
    ]:
        entries = data.get(key, [])
        if entries:
            parts.append(build_section(
                title,
                "\n\\entryspace\n".join(latex_entry(e) for e in entries),
            ))

    if data.get("skills"):
        parts.append(build_section("Skills", ", ".join(esc(s) for s in data["skills"])))

    if data.get("languages"):
        items = []
        for lang in data["languages"]:
            l = esc(lang.get("language", ""))
            p = esc(lang.get("proficiency", ""))
            items.append("\\textbf{" + l + "}" + (": " + p if p else ""))
        parts.append(build_section("Languages", " \\quad ".join(items)))

    body = "\n".join(parts)
    header_block = (
        "\\begin{center}\n"
        "  {\\huge\\bfseries\\color{darkgray} " + name     + "}\\\\[5pt]\n"
        "  {\\large\\color{midgray} "            + headline + "}\\\\[2pt]\n"
        "  {\\small\\color{lightgray} "          + location + "}\n"
        "\\end{center}\n"
    )

    return (
        _PREAMBLE
        + "\n\\begin{document}\n\n"
        + header_block
        + "\n\\vspace{6pt}\n"
        + body
        + "\n\\end{document}\n"
    )


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def fetch_details_html(context, base_profile_url: str, slug: str):
    """Visit https://www.linkedin.com/in/<you>/details/<slug>/ to get the
    FULL list for a section (the main profile page only ever shows the
    first 2–3 items). Returns the page HTML, or None if the subpage
    didn't load / doesn't exist for this profile — callers should keep
    whatever they already extracted from the main page in that case."""
    url = base_profile_url.rstrip("/") + f"/details/{slug}/"
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await wait_stable(page, 10_000)
        if "/details/" not in page.url:
            return None  # redirected away — section doesn't exist
        await full_scroll(page)
        html = await page.content()
        return html
    except Exception:
        return None
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def main():
    SEP = "─" * 60
    print(f"\n{SEP}")
    print("  LinkedIn Profile Extractor & LaTeX PDF Generator  v4")
    print(SEP)

    async with async_playwright() as p:
        print("\n▶  Launching Chromium …")
        browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
        context = await browser.new_context(
            viewport=None,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        print("▶  Opening LinkedIn …\n")
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        print(SEP)
        print("  STEPS TO FOLLOW IN THE BROWSER:")
        print(SEP)
        print("  1.  Log in to LinkedIn")
        print("  2.  Click your profile photo  →  'View Profile'")
        print("  3.  Scroll the profile page down once to help load content")
        print("  4.  Return to this terminal and press ENTER")
        print(SEP)

        while True:
            input("\n  >>> Press ENTER once you are ON your LinkedIn profile page … \n")
            print("  ⏳ Waiting for page to stabilise …")
            await wait_stable(page, 12_000)
            url = page.url
            print(f"  Current URL: {url}")
            if "linkedin.com/in/" in url:
                print("  ✅ Profile page detected!\n")
                break
            print("\n  ⚠  That doesn't look like a /in/ profile URL.")
            print("     Please navigate to your profile and press ENTER again.\n")

        m = re.search(r"(https://www\.linkedin\.com/in/[^/?#]+)", page.url)
        base_profile_url = m.group(1) if m else page.url.rstrip("/")

        print("  ↕  Scrolling main profile page to load lazy content …")
        await full_scroll(page)
        await click_see_more(page)
        await full_scroll(page)

        print("  📄 Parsing main profile page …")
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        debug_path = Path("linkedin_profile_debug.html")
        debug_path.write_text(html, encoding="utf-8")
        print(f"  🐛 Debug HTML saved → {debug_path}")

        # ── Baseline extraction from the main page (always available) ───
        data = {}
        data.update(extract_header(soup))
        about_text, about_skills = extract_about(soup)
        data["about"] = about_text

        for key in ("experience", "education", "certifications", "projects",
                    "volunteer", "honors", "publications"):
            sec = find_section(soup, key)
            entries = group_entries(texts_in(sec))
            data[key] = [entry_to_record(e) for e in entries]

        page_skills = extract_skills_from_texts(texts_in(find_section(soup, "skills")))
        data["languages"] = extract_languages_from_texts(texts_in(find_section(soup, "languages")))

        modal_skills = await extract_skills_via_modal(page)

        # ── Visit each /details/<section>/ subpage for the FULL list ────
        print("\n  🔎 Fetching full section lists from /details/ subpages …")
        for key, slug in DETAILS_SLUGS.items():
            if key != "skills" and not find_section(soup, key):
                continue  # this profile has no such section at all
            print(f"     • {key} …", end=" ", flush=True)
            dhtml = await fetch_details_html(context, base_profile_url, slug)
            if not dhtml:
                print("(skipped — page unavailable, keeping main-page data)")
                continue
            dsoup = BeautifulSoup(dhtml, "html.parser")
            container = section_or_main(dsoup, key)
            texts = texts_in(container)

            if key == "skills":
                full_skills = extract_skills_from_texts(texts)
                if full_skills:
                    data["skills"] = full_skills
                    print(f"{len(full_skills)} skills")
                else:
                    print("0 found (keeping fallback)")
            elif key == "languages":
                full_langs = extract_languages_from_texts(texts)
                if full_langs:
                    data["languages"] = full_langs
                    print(f"{len(full_langs)} entries")
                else:
                    print("0 found (keeping fallback)")
            else:
                entries = group_entries(texts)
                if entries:
                    data[key] = [entry_to_record(e) for e in entries]
                    print(f"{len(entries)} entries")
                else:
                    print("0 found (keeping fallback)")

        if "skills" not in data or not data["skills"]:
            data["skills"] = page_skills
        data["skills"] = list(dict.fromkeys(
            data.get("skills", []) + modal_skills + about_skills
        ))

        await browser.close()

        # ── Summary ───────────────────────────────────────────────────
        print(f"\n  ✅ Extraction complete!")
        print(f"     Name        : {data.get('name',     '—')}")
        print(f"     Headline    : {data.get('headline', '—')}")
        print(f"     Location    : {data.get('location', '—')}")
        print(f"     Experience  : {len(data.get('experience',     []))} entries")
        print(f"     Education   : {len(data.get('education',      []))} entries")
        print(f"     Skills      : {len(data.get('skills',         []))} items")
        print(f"     Certif.     : {len(data.get('certifications', []))} entries")
        print(f"     Projects    : {len(data.get('projects',       []))} entries")
        print(f"     Languages   : {len(data.get('languages',      []))} entries")

        empty_fields = [k for k in ("name", "headline", "experience", "education")
                         if not data.get(k)]
        if empty_fields:
            print(f"\n  ⚠  Still empty: {', '.join(empty_fields)}")
            print(f"     Open {debug_path} and Ctrl+F the section name to inspect")
            print("     the real markup — LinkedIn may have changed something again.")

        json_path = Path("linkedin_profile.json")
        json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  💾 Raw data  → {json_path}")

        latex = generate_latex(data)
        tex_path = Path("linkedin_profile.tex")
        tex_path.write_text(latex, encoding="utf-8")
        print(f"  📝 LaTeX     → {tex_path}")

        print("\n  🔧 Compiling PDF with pdflatex …")
        try:
            for _ in range(2):
                subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode", str(tex_path)],
                    capture_output=True, text=True, timeout=90,
                )
            pdf_path = Path("linkedin_profile.pdf")
            if pdf_path.exists():
                print(f"  ✅ PDF ready  → {pdf_path.resolve()}")
            else:
                print("  ❌ PDF compilation failed. Check linkedin_profile.log")
                print("     Manual compile:  pdflatex linkedin_profile.tex")
        except FileNotFoundError:
            print("  ⚠  pdflatex not found.")
            print("     Windows  : install MiKTeX  →  https://miktex.org/download")
            print("     Linux    : sudo apt install texlive-latex-base")
            print("     Then run :  pdflatex linkedin_profile.tex")
        except subprocess.TimeoutExpired:
            print("  ⚠  pdflatex timed out. Try running manually.")

        print(f"\n{SEP}\n  Done!\n{SEP}\n")


if __name__ == "__main__":
    asyncio.run(main())
