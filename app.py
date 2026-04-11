import os
import re
import sys
import json
import time
import sqlite3
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, send_from_directory
from feedgen.feed import FeedGenerator

# ── Config ──────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
JOURNALS_FILE = DATA_DIR / "journals.json"
CACHE_DIR = DATA_DIR / "cache"
JOURNALS_DB = Path(os.environ.get("JOURNALS_DB", Path(__file__).parent / "journals" / "journals.db"))
MAILTO = os.environ.get("MAILTO", "scholrss@example.com")
OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8844")
INTERNAL_URL = os.environ.get("INTERNAL_URL", "")
UPDATE_INTERVAL = int(os.environ.get("UPDATE_INTERVAL_HOURS", 24))
LOOKBACK_DAYS_DEFAULT = int(os.environ.get("LOOKBACK_DAYS", 365))
MAX_ARTICLES_DEFAULT = int(os.environ.get("MAX_ARTICLES", 100))
SETTINGS_FILE = DATA_DIR / "settings.json"

# ── Abstract cleaning ──────────────────────────────────────────────────────
# Strip JATS/HTML tags and a leading "Abstract" heading that publishers sometimes
# jam onto the start of the abstract body. The (?i:...) inline flag makes only
# the word case-insensitive — the [A-Z] lookahead below stays case-sensitive so
# we only strip "Abstract" when followed by a separator or a capital letter
# (handles "AbstractThis paper…" without mangling legitimate words like
# "Abstractly speaking…").
_JATS_TAG_RE = re.compile(r"<[^>]+>")
_ABSTRACT_PREFIX_RE = re.compile(
    r"^\s*(?i:abstract)(?=[\s:.\-—]|[A-Z])[\s:.\-—]*"
)

def clean_abstract(text):
    if not text:
        return text
    text = _JATS_TAG_RE.sub("", text)
    text = _ABSTRACT_PREFIX_RE.sub("", text)
    return text.strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scholrss")

app = Flask(__name__)
_journals_lock = threading.Lock()

# ── Helpers ─────────────────────────────────────────────────────────────────

def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

def load_journals():
    with _journals_lock:
        if JOURNALS_FILE.exists():
            return json.loads(JOURNALS_FILE.read_text())
        return {}

def save_journals(journals):
    with _journals_lock:
        JOURNALS_FILE.write_text(json.dumps(journals, indent=2))

def load_settings():
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    return {}

def save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))

def get_lookback_days():
    return load_settings().get("lookback_days", LOOKBACK_DAYS_DEFAULT)

def get_max_articles():
    try:
        val = int(load_settings().get("max_articles", MAX_ARTICLES_DEFAULT))
    except (TypeError, ValueError):
        val = MAX_ARTICLES_DEFAULT
    # CrossRef accepts up to 1000 rows per request; pagination would be needed
    # beyond that, so cap there.
    return max(1, min(1000, val))

def journal_cache_path(feed_id):
    """Resolve the on-disk cache path for a given feed id.

    A ``feed_id`` is either a plain ISSN (``2044-3994`` — unfiltered journals,
    legacy format) or ``<issn>__<slug>`` for filtered feed variants. We keep
    the ``__slug`` portion verbatim so two filtered variants on the same ISSN
    don't collide on disk; only the ISSN hyphens get stripped (preserving the
    legacy filename scheme for unfiltered entries).
    """
    if "__" in feed_id:
        issn_part, slug = feed_id.split("__", 1)
        return CACHE_DIR / f"{issn_part.replace('-', '')}__{slug}.json"
    return CACHE_DIR / f"{feed_id.replace('-', '')}.json"


def _slugify(text, max_len=40):
    """Produce a URL/filename-safe slug from arbitrary text."""
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return slug[:max_len] or "filtered"

def crossref_headers():
    return {"User-Agent": f"ScholRSS/1.0 (mailto:{MAILTO})"}

# ── CrossRef ────────────────────────────────────────────────────────────────

def crossref_search_journal(query):
    """Search for journals by name via CrossRef."""
    url = "https://api.crossref.org/journals"
    params = {"query": query, "rows": 10, "mailto": MAILTO}
    try:
        r = requests.get(url, params=params, headers=crossref_headers(), timeout=15)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
        results = []
        for item in items:
            results.append({
                "title": item.get("title", "Unknown"),
                "publisher": item.get("publisher", ""),
                "issn": item.get("ISSN", []),
                "subjects": [s.get("name", "") for s in item.get("subjects", [])],
            })
        return results
    except Exception as e:
        log.error(f"CrossRef journal search failed: {e}")
        return []

def crossref_journal_from_doi(doi):
    """Look up which journal a DOI belongs to."""
    url = f"https://api.crossref.org/works/{doi}"
    params = {"mailto": MAILTO}
    try:
        r = requests.get(url, params=params, headers=crossref_headers(), timeout=15)
        r.raise_for_status()
        msg = r.json().get("message", {})
        issn_list = msg.get("ISSN", [])
        container = msg.get("container-title", ["Unknown"])
        return {
            "title": container[0] if container else "Unknown",
            "issn": issn_list,
            "publisher": msg.get("publisher", ""),
        }
    except Exception as e:
        log.error(f"CrossRef DOI lookup failed: {e}")
        return None

def _parse_crossref_date(item):
    """Extract the best available publication date from a CrossRef work record.

    CrossRef exposes several date fields with different semantics. We prefer the
    canonical publication dates (print/online), fall back to ``issued`` /
    ``published`` which are the generic publication date fields, and only use
    ``created`` (CrossRef record creation) as a last resort. Each date is a
    nested ``{"date-parts": [[year, month?, day?]]}`` with optional month/day.

    Returns a tz-aware UTC ``datetime``; when the source only has a year we
    anchor to Jan 1, month → day 1, so ordering is still sensible.
    """
    # Ordered from most-to-least authoritative for "when was this published?"
    for field in ("published-print", "published-online", "issued",
                  "published", "created"):
        dp_list = item.get(field, {}).get("date-parts") or []
        if not dp_list:
            continue
        dp = dp_list[0] or []
        if not dp or dp[0] is None:
            continue
        try:
            y = int(dp[0])
            m = int(dp[1]) if len(dp) > 1 and dp[1] is not None else 1
            d = int(dp[2]) if len(dp) > 2 and dp[2] is not None else 1
            # Guard against garbage years (CrossRef occasionally has "0" or far-future)
            if y < 1800 or y > 2200:
                continue
            return datetime(y, m, d, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def crossref_latest_works(issn, from_date, rows=100):
    """Fetch recent works from CrossRef for a given ISSN."""
    url = "https://api.crossref.org/works"
    params = {
        "filter": f"issn:{issn},from-index-date:{from_date},type:journal-article",
        "sort": "indexed",
        "order": "desc",
        "rows": max(1, min(1000, rows)),
        "mailto": MAILTO,
    }
    try:
        r = requests.get(url, params=params, headers=crossref_headers(), timeout=30)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
        works = []
        for item in items:
            doi = item.get("DOI", "")
            title_parts = item.get("title", [])
            title = title_parts[0] if title_parts else "Untitled"

            pub_date = _parse_crossref_date(item) or datetime.now(timezone.utc)

            # Authors
            authors = []
            for a in item.get("author", []):
                name = f"{a.get('given', '')} {a.get('family', '')}".strip()
                if name:
                    authors.append(name)

            abstract = clean_abstract(item.get("abstract", ""))

            works.append({
                "doi": doi,
                "title": title,
                "authors": authors,
                "date": pub_date.isoformat(),
                "abstract": abstract,
                "url": f"https://doi.org/{doi}" if doi else "",
                "source": "crossref",
            })
        return works
    except Exception as e:
        log.error(f"CrossRef works fetch failed for {issn}: {e}")
        return []

# ── OpenAlex ────────────────────────────────────────────────────────────────

def openalex_headers():
    h = {"User-Agent": f"ScholRSS/1.0 (mailto:{MAILTO})"}
    if OPENALEX_API_KEY:
        h["Authorization"] = f"Bearer {OPENALEX_API_KEY}"
    return h

def openalex_params():
    p = {}
    if OPENALEX_API_KEY:
        p["api_key"] = OPENALEX_API_KEY
    else:
        p["mailto"] = MAILTO
    return p

def reconstruct_abstract(inverted_index):
    """Reconstruct abstract from OpenAlex inverted index format."""
    if not inverted_index:
        return ""
    word_positions = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)

# ── Semantic Scholar ───────────────────────────────────────────────────────

SEMANTIC_SCHOLAR_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")

def semantic_scholar_headers():
    h = {"User-Agent": f"ScholRSS/1.0 (mailto:{MAILTO})"}
    if SEMANTIC_SCHOLAR_API_KEY:
        h["x-api-key"] = SEMANTIC_SCHOLAR_API_KEY
    return h

def semantic_scholar_batch_abstracts(dois):
    """Fetch abstracts for up to 500 DOIs in a single batch request."""
    if not dois:
        return {}
    url = "https://api.semanticscholar.org/graph/v1/paper/batch"
    params = {"fields": "externalIds,abstract"}
    payload = {"ids": [f"DOI:{doi}" for doi in dois]}
    results = {}
    try:
        r = requests.post(url, params=params, json=payload,
                          headers=semantic_scholar_headers(), timeout=30)
        if r.status_code == 200:
            for item in r.json():
                if item and item.get("abstract"):
                    ext = item.get("externalIds", {})
                    doi = ext.get("DOI", "")
                    if doi:
                        results[doi.lower()] = item["abstract"]
        else:
            log.warning(f"Semantic Scholar batch returned {r.status_code}")
    except Exception as e:
        log.error(f"Semantic Scholar batch lookup failed: {e}")
    return results

def semantic_scholar_search(query, from_date, venue=None, limit=100):
    """Search Semantic Scholar for papers matching a keyword query.

    Returns a list of work dicts in the same shape as crossref_latest_works.
    Useful for sources like SSRN where S2 crawls directly and catches papers
    that never get DOIs/CrossRef registration.
    """
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    # Parse from_date (YYYY-MM-DD) into year range for the year= filter
    try:
        from_year = int(from_date[:4])
    except (ValueError, TypeError):
        from_year = datetime.now(timezone.utc).year
    current_year = datetime.now(timezone.utc).year

    params = {
        "query": query,
        "fields": "title,abstract,url,venue,year,externalIds,publicationDate,authors",
        "limit": min(limit, 100),
        "year": f"{from_year}-{current_year}",
    }
    if venue:
        params["venue"] = venue

    works = []
    try:
        r = requests.get(url, params=params,
                         headers=semantic_scholar_headers(), timeout=30)
        if r.status_code != 200:
            log.warning(f"Semantic Scholar search returned {r.status_code}: {r.text[:200]}")
            return works
        data = r.json()
        for item in data.get("data", []):
            ext = item.get("externalIds") or {}
            doi = ext.get("DOI") or ""
            arxiv_id = ext.get("ArXiv") or ""
            ssrn_id = ext.get("SSRN") or ""
            # Build URL: prefer DOI, then arXiv, then SSRN, then S2 URL
            if doi:
                item_url = f"https://doi.org/{doi}"
            elif arxiv_id:
                item_url = f"https://arxiv.org/abs/{arxiv_id}"
                doi = f"10.48550/arXiv.{arxiv_id}"
            elif ssrn_id:
                item_url = f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={ssrn_id}"
            else:
                item_url = item.get("url") or ""

            pub_date_str = item.get("publicationDate") or ""
            try:
                y, m, d = pub_date_str.split("-")[:3]
                pub_date = datetime(int(y), int(m), int(d), tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pub_date = datetime(item.get("year") or current_year, 1, 1,
                                   tzinfo=timezone.utc)

            authors = [a.get("name", "") for a in (item.get("authors") or []) if a.get("name")]
            abstract = clean_abstract(item.get("abstract") or "")

            works.append({
                "doi": doi,
                "title": item.get("title") or "Untitled",
                "authors": authors,
                "date": pub_date.isoformat(),
                "abstract": abstract,
                "url": item_url,
                "source": "semantic_scholar",
            })
    except Exception as e:
        log.error(f"Semantic Scholar search failed: {e}")
    return works


def openalex_enrich_abstract(doi):
    """Try to get an abstract from OpenAlex for a specific DOI."""
    url = f"https://api.openalex.org/works/doi:{doi}"
    params = openalex_params()
    try:
        r = requests.get(url, params=params, headers=openalex_headers(), timeout=15)
        if r.status_code == 200:
            data = r.json()
            inv = data.get("abstract_inverted_index")
            if inv:
                return reconstruct_abstract(inv)
        return ""
    except Exception:
        return ""

def _openalex_work_to_record(w):
    """Normalise an OpenAlex /works result into our internal work dict."""
    doi_url = w.get("doi") or ""
    # Fallback: check ids dict, then synthesise from arXiv ID
    if not doi_url:
        doi_url = (w.get("ids") or {}).get("doi") or ""
    if not doi_url:
        arxiv_id = (w.get("ids") or {}).get("openalex", "")
        # Some arXiv records carry the ID in primary_location or ids
        for loc_key in ("primary_location", "best_oa_location"):
            landing = ((w.get(loc_key) or {}).get("landing_page_url") or "")
            if "arxiv.org/abs/" in landing:
                arxiv_id = landing.split("arxiv.org/abs/")[-1].split("v")[0]
                doi_url = f"https://doi.org/10.48550/arXiv.{arxiv_id}"
                break
    doi = doi_url.replace("https://doi.org/", "") if doi_url else ""
    title = w.get("title") or w.get("display_name") or "Untitled"

    # Date — OpenAlex gives "publication_date" as YYYY-MM-DD
    pub_date_str = w.get("publication_date") or ""
    try:
        y, m, d = pub_date_str.split("-")[:3]
        pub_date = datetime(int(y), int(m), int(d), tzinfo=timezone.utc)
    except (ValueError, TypeError):
        pub_date = datetime.now(timezone.utc)

    # Authors (authorships is an ordered list)
    authors = []
    for auth in w.get("authorships", []) or []:
        name = (auth.get("author") or {}).get("display_name", "")
        if name:
            authors.append(name)

    abstract = clean_abstract(reconstruct_abstract(w.get("abstract_inverted_index")))
    oa_id = (w.get("id") or "").rstrip("/").split("/")[-1]
    url = doi_url or (f"https://openalex.org/{oa_id}" if oa_id else "")

    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "date": pub_date.isoformat(),
        "abstract": abstract,
        "url": url,
        "source": "openalex",
    }

def openalex_filtered_works(issn, from_date, filter_config, limit=100):
    """Fetch recent works from a journal (by ISSN or OpenAlex source ID) that
    match a keyword/author filter, using OpenAlex's /works endpoint with
    server-side filtering.

    ``filter_config`` is a dict with:
      - ``keywords``: list of search terms (matched against title + abstract)
      - ``authors``:  list of author name fragments
      - ``match``:    "any" (OR across keywords) or "all" (AND across keywords)
      - ``openalex_source_id``: optional OpenAlex source ID (e.g. "S4210172589")
        — when set, used instead of the ISSN for source matching (more reliable
        for sources like SSRN that map poorly via ISSN).

    Returns a list of work dicts in the same shape as ``crossref_latest_works``.
    """
    oa_src = (filter_config.get("openalex_source_id") or "").strip()
    if oa_src:
        # Normalise: accept full URL, bare ID, or Sxxxxx
        oa_src = oa_src.rstrip("/").split("/")[-1]
        if not oa_src.upper().startswith("S"):
            oa_src = f"S{oa_src}"
        source_filter = f"primary_location.source.id:https://openalex.org/sources/{oa_src.lower()}"
    else:
        source_filter = f"primary_location.source.issn:{issn}"

    filters = [
        source_filter,
        f"from_publication_date:{from_date}",
        "type:article",
    ]

    keywords = [k.strip() for k in (filter_config.get("keywords") or []) if k and k.strip()]
    authors = [a.strip() for a in (filter_config.get("authors") or []) if a and a.strip()]
    match = (filter_config.get("match") or "any").lower()

    if keywords:
        if match == "all":
            # AND across keywords — each term becomes its own filter clause.
            for kw in keywords:
                filters.append(f"title_and_abstract.search:{kw}")
        else:
            # OR — pipe-separated values inside one filter clause.
            filters.append("title_and_abstract.search:" + "|".join(keywords))

    if authors:
        # OR across author fragments
        filters.append(
            "authorships.author.display_name.search:" + "|".join(authors)
        )

    params = {
        "filter": ",".join(filters),
        "per-page": max(1, min(200, limit)),
        "sort": "publication_date:desc",
    }
    params.update(openalex_params())

    url = "https://api.openalex.org/works"
    try:
        r = requests.get(url, params=params, headers=openalex_headers(), timeout=30)
        if r.status_code != 200:
            log.warning(f"OpenAlex filtered fetch returned {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        results = [_openalex_work_to_record(w) for w in data.get("results", [])]
        return results[:limit]
    except Exception as e:
        log.error(f"OpenAlex filtered fetch failed for {issn}: {e}")
        return []

# ── Feed Update ─────────────────────────────────────────────────────────────

def _enrich_missing_abstracts(works):
    """Run the Semantic Scholar → OpenAlex fallback pipeline on works missing
    abstracts. Mutates ``works`` in place. Shared between the filtered and
    unfiltered update paths so we don't duplicate enrichment logic."""
    missing = [w for w in works if not w["abstract"] and w["doi"]]
    if not missing:
        return
    log.info(f"  {len(missing)} works missing abstracts")

    dois = [w["doi"] for w in missing]
    ss_abstracts = semantic_scholar_batch_abstracts(dois)
    ss_count = 0
    for w in missing:
        abstract = ss_abstracts.get(w["doi"].lower())
        if abstract:
            w["abstract"] = clean_abstract(abstract)
            w["source"] = w["source"] + "+semanticscholar"
            ss_count += 1
    log.info(f"  Semantic Scholar batch: {ss_count}/{len(missing)} abstracts")

    still_missing = [w for w in works if not w["abstract"] and w["doi"]]
    oa_count = 0
    for w in still_missing:
        abstract = openalex_enrich_abstract(w["doi"])
        if abstract:
            w["abstract"] = clean_abstract(abstract)
            w["source"] = w["source"] + "+openalex"
            oa_count += 1
        time.sleep(0.15)  # rate limit
    if still_missing:
        log.info(f"  OpenAlex fallback: {oa_count}/{len(still_missing)} abstracts")


def update_journal_feed(feed_id, journal_info):
    """Fetch and cache latest works for a feed.

    ``feed_id`` is the journals.json key — either a plain ISSN (unfiltered) or
    ``<issn>__<slug>`` (filtered variant). The real ISSN used for upstream API
    calls is taken from ``journal_info['issn']`` (falls back to ``feed_id``
    itself for legacy entries).

    When ``journal_info['filter']`` has keywords/authors, we fetch via
    OpenAlex's filtered /works endpoint so only matching works transit the
    wire — essential for mega-journals/servers like SSRN (1556-5068) or arXiv
    (2331-8422). Otherwise we use the existing CrossRef-primary pipeline.
    """
    real_issn = journal_info.get("issn") or feed_id.split("__", 1)[0]
    from_date = (datetime.now(timezone.utc) - timedelta(days=get_lookback_days())).strftime("%Y-%m-%d")
    max_articles = get_max_articles()
    filter_config = journal_info.get("filter") or {}
    has_filter = bool(
        (filter_config.get("keywords") or []) or (filter_config.get("authors") or [])
    )

    if has_filter:
        log.info(
            f"Updating FILTERED feed '{feed_id}' ({journal_info['title']}, ISSN {real_issn}) "
            f"from {from_date} (max {max_articles}); filter={filter_config}"
        )
        works = openalex_filtered_works(real_issn, from_date, filter_config, limit=max_articles)
        log.info(f"  OpenAlex filtered: {len(works)} matching works")

        # Also search Semantic Scholar if enabled — catches SSRN papers that
        # lack DOIs and never appear in CrossRef/OpenAlex.
        if filter_config.get("use_semantic_scholar"):
            keywords = filter_config.get("keywords") or []
            if keywords:
                query = " ".join(keywords)
                s2_venue = (filter_config.get("s2_venue") or "").strip() or None
                s2_works = semantic_scholar_search(query, from_date,
                                                  venue=s2_venue, limit=max_articles)
                log.info(f"  Semantic Scholar search: {len(s2_works)} results")
                # Merge, deduplicating by DOI (prefer existing OpenAlex record)
                seen_dois = {w["doi"].lower() for w in works if w["doi"]}
                seen_titles = {w["title"].lower().strip() for w in works}
                for sw in s2_works:
                    if sw["doi"] and sw["doi"].lower() in seen_dois:
                        continue
                    if sw["title"].lower().strip() in seen_titles:
                        continue
                    works.append(sw)
                    if sw["doi"]:
                        seen_dois.add(sw["doi"].lower())
                    seen_titles.add(sw["title"].lower().strip())
                log.info(f"  After S2 merge: {len(works)} total works")

        # OpenAlex usually returns abstracts inline, but coverage varies
        # (especially for SSRN). Run the enrichment cascade on anything still
        # missing — safe to do since "missing" will typically be a small set.
        _enrich_missing_abstracts(works)
    else:
        log.info(f"Updating feed '{feed_id}' ({journal_info['title']}, ISSN {real_issn}) from {from_date} (max {max_articles})")
        works = crossref_latest_works(real_issn, from_date, rows=max_articles)
        log.info(f"  CrossRef: {len(works)} works")
        _enrich_missing_abstracts(works)

    # Final abstract cleanup (idempotent — safe if already cleaned upstream).
    for w in works:
        if w["abstract"]:
            w["abstract"] = clean_abstract(w["abstract"])

    # Sort by date descending
    works.sort(key=lambda x: x["date"], reverse=True)
    merged = works
    log.info(f"  Final: {len(merged)} works, {sum(1 for w in merged if w['abstract'])} with abstracts")

    cache = {
        "issn": real_issn,
        "feed_id": feed_id,
        "journal": journal_info,
        "updated": datetime.now(timezone.utc).isoformat(),
        "works": merged,
    }
    journal_cache_path(feed_id).write_text(json.dumps(cache, indent=2))
    return cache

def update_all_feeds():
    """Update all journal feeds."""
    journals = load_journals()
    for issn, info in journals.items():
        try:
            update_journal_feed(issn, info)
            time.sleep(1)  # be polite between journals
        except Exception as e:
            log.error(f"Failed to update {issn}: {e}")

def scheduler_loop():
    """Background thread that updates feeds on schedule."""
    while True:
        log.info("Starting scheduled feed update...")
        try:
            update_all_feeds()
        except Exception as e:
            log.error(f"Scheduled update failed: {e}")
        log.info(f"Feed update complete. Next update in {UPDATE_INTERVAL} hours.")
        time.sleep(UPDATE_INTERVAL * 3600)

# ── RSS Generation ──────────────────────────────────────────────────────────

def generate_feed(issn):
    """Generate an Atom/RSS feed for a journal from cached data."""
    cache_path = journal_cache_path(issn)
    if not cache_path.exists():
        return None

    cache = json.loads(cache_path.read_text())
    journal = cache["journal"]
    works = cache["works"]

    fg = FeedGenerator()
    fg.id(f"{BASE_URL}/feed/{issn}")
    fg.title(f"{journal['title']} — ScholRSS")
    fg.subtitle(f"Latest articles from {journal['title']} via CrossRef + OpenAlex")
    fg.link(href=f"{BASE_URL}/feed/{issn}", rel="self")
    fg.link(href=f"https://doi.org/{issn}", rel="alternate")
    fg.language("en")
    fg.updated(datetime.fromisoformat(cache["updated"]))

    for work in works[:get_max_articles()]:
        fe = fg.add_entry()
        fe.id(work["url"] or work["doi"] or work["title"])
        fe.title(work["title"])
        if work["url"]:
            fe.link(href=work["url"])

        try:
            fe.published(datetime.fromisoformat(work["date"]))
            fe.updated(datetime.fromisoformat(work["date"]))
        except (ValueError, TypeError):
            pass

        if work["authors"]:
            for author_name in work["authors"][:5]:
                fe.author({"name": author_name})

        # Keep the summary compact — the entry <link> already points at the
        # DOI URL, so we don't repeat "DOI: 10.xxxx/..." in the body (wastes
        # tokens when an MCP client ingests the feed).
        summary_parts = []
        if work["authors"]:
            summary_parts.append(", ".join(work["authors"][:5]))
            if len(work["authors"]) > 5:
                summary_parts[-1] += f" et al. ({len(work['authors'])} authors)"
        if work["abstract"]:
            summary_parts.append("")
            summary_parts.append(work["abstract"])

        fe.summary("\n".join(summary_parts) if summary_parts else "No abstract available.")

    return fg

# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/et-book/<path:filename>")
def serve_et_book(filename):
    """Serve the et-book font family from the repo root (used by the UI CSS).

    The fonts are outside Flask's default ``static/`` folder so we expose them
    explicitly. They're copied into the Docker image verbatim.
    """
    return send_from_directory(Path(__file__).parent / "et-book", filename)


@app.route("/")
def index():
    journals = load_journals()
    # Load stats for each journal
    stats = {}
    for issn in journals:
        cp = journal_cache_path(issn)
        if cp.exists():
            cache = json.loads(cp.read_text())
            stats[issn] = {
                "count": len(cache.get("works", [])),
                "with_abstract": sum(1 for w in cache.get("works", []) if w.get("abstract")),
                "updated": cache.get("updated", "never"),
            }
        else:
            stats[issn] = {"count": 0, "with_abstract": 0, "updated": "never"}
    return render_template("index.html", journals=journals, stats=stats,
                           base_url=BASE_URL, internal_url=INTERNAL_URL,
                           lookback_days=get_lookback_days(),
                           max_articles=get_max_articles())

@app.route("/feed/<issn>")
def feed_atom(issn):
    fmt = request.args.get("format", "atom")
    fg = generate_feed(issn)
    if not fg:
        return "Feed not found. Try refreshing first.", 404
    if fmt == "rss":
        return Response(fg.rss_str(pretty=True), mimetype="application/rss+xml")
    return Response(fg.atom_str(pretty=True), mimetype="application/atom+xml")

@app.route("/feed/<issn>/json")
def feed_json(issn):
    cache_path = journal_cache_path(issn)
    if not cache_path.exists():
        return jsonify({"error": "not found"}), 404
    cache = json.loads(cache_path.read_text())
    return jsonify(cache)

@app.route("/opml")
def opml():
    """Generate OPML file of all feeds for easy import into feed readers.

    Pass ?internal=1 to use INTERNAL_URL (for container-to-container readers
    that should bypass the reverse-proxy auth).
    """
    journals = load_journals()
    use_internal = request.args.get("internal") in ("1", "true", "yes")
    root = INTERNAL_URL if (use_internal and INTERNAL_URL) else BASE_URL
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        '<head><title>ScholRSS Feeds</title></head>',
        '<body>',
    ]
    for issn, info in journals.items():
        feed_url = f"{root}/feed/{issn}"
        lines.append(f'  <outline type="rss" text="{info["title"]}" xmlUrl="{feed_url}" />')
    lines.append('</body>')
    lines.append('</opml>')
    return Response("\n".join(lines), mimetype="application/xml",
                    headers={"Content-Disposition": "attachment; filename=scholrss.opml"})

# ── API Routes ──────────────────────────────────────────────────────────────

@app.route("/api/autocomplete", methods=["GET"])
def api_autocomplete():
    """Fast local journal autocomplete from SQLite FTS5 database."""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify([])
    if not JOURNALS_DB.exists():
        return jsonify([])
    try:
        conn = sqlite3.connect(f"file:{JOURNALS_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        # If query looks like an ISSN, try direct lookup first
        issn_pattern = re.match(r'^\d{4}-?\d{3}[\dXx]$', q)
        if issn_pattern:
            issn_q = q.upper()
            if '-' not in issn_q:
                issn_q = issn_q[:4] + '-' + issn_q[4:]
            rows = conn.execute("""
                SELECT j.issn_l, j.title, j.publisher, j.works_count,
                       j.all_issns, j.country, j.is_oa
                FROM issn_map m
                JOIN journals j ON j.issn_l = m.issn_l
                WHERE m.issn = ?
            """, (issn_q,)).fetchall()
        else:
            rows = []

        # Fall back to FTS if no ISSN match
        if not rows:
            terms = q.split()
            fts_query = " ".join(t + "*" if i == len(terms) - 1 else t
                                 for i, t in enumerate(terms))
            rows = conn.execute("""
                SELECT j.issn_l, j.title, j.publisher, j.works_count,
                       j.all_issns, j.country, j.is_oa
                FROM journals_fts fts
                JOIN journals j ON j.issn_l = fts.issn_l
                WHERE journals_fts MATCH ?
                ORDER BY j.works_count DESC
                LIMIT 15
            """, (fts_query,)).fetchall()
        conn.close()
        results = []
        for r in rows:
            issns = [i.strip() for i in (r["all_issns"] or r["issn_l"]).split() if i.strip()]
            results.append({
                "title": r["title"],
                "publisher": r["publisher"] or "",
                "issn": issns,
                "works_count": r["works_count"] or 0,
                "country": r["country"] or "",
                "is_oa": bool(r["is_oa"]),
            })
        return jsonify(results)
    except Exception as e:
        log.error(f"Autocomplete query failed: {e}")
        return jsonify([])

@app.route("/api/search/journal", methods=["GET"])
def api_search_journal():
    """Search journals via CrossRef API (online fallback)."""
    q = request.args.get("q", "")
    if not q:
        return jsonify([])
    results = crossref_search_journal(q)
    return jsonify(results)

@app.route("/api/search/doi", methods=["GET"])
def api_search_doi():
    doi = request.args.get("doi", "").strip()
    if not doi:
        return jsonify({"error": "no doi"}), 400
    # Strip URL prefix if given
    for prefix in ["https://doi.org/", "http://doi.org/", "doi.org/", "doi:"]:
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
    result = crossref_journal_from_doi(doi)
    if result:
        return jsonify(result)
    return jsonify({"error": "not found"}), 404

@app.route("/api/journal", methods=["POST"])
def api_add_journal():
    data = request.get_json()
    issn = data.get("issn", "").strip()
    title = data.get("title", "Unknown Journal")
    publisher = data.get("publisher", "")
    if not issn:
        return jsonify({"error": "ISSN required"}), 400

    journals = load_journals()
    journals[issn] = {
        "issn": issn,
        "title": title,
        "publisher": publisher,
        "added": datetime.now(timezone.utc).isoformat(),
    }
    save_journals(journals)

    # Trigger initial fetch in background
    threading.Thread(target=update_journal_feed, args=(issn, journals[issn]), daemon=True).start()

    return jsonify({"ok": True, "issn": issn})


@app.route("/api/journal/filtered", methods=["POST"])
def api_add_filtered_feed():
    """Create a new filtered feed variant on a (possibly already-tracked) ISSN.

    Body: {issn, title, publisher, label, keywords, authors, match}
    Returns: {ok, feed_id}

    Each call creates a fresh entry with a unique feed_id, so you can stack
    multiple filtered feeds on the same ISSN (e.g. one SSRN "privacy" feed and
    one SSRN "ai safety" feed).
    """
    data = request.get_json() or {}
    issn = (data.get("issn") or "").strip()
    title = data.get("title", "").strip() or "Unknown Journal"
    publisher = data.get("publisher", "")
    label = (data.get("label") or "").strip()
    keywords = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
    authors = [str(a).strip() for a in (data.get("authors") or []) if str(a).strip()]
    match = data.get("match", "any")
    openalex_source_id = (data.get("openalex_source_id") or "").strip()
    use_semantic_scholar = bool(data.get("use_semantic_scholar"))
    s2_venue = (data.get("s2_venue") or "").strip()
    if match not in ("any", "all"):
        match = "any"

    if not issn:
        return jsonify({"error": "ISSN required"}), 400
    if not (keywords or authors):
        return jsonify({"error": "At least one keyword or author is required for a filtered feed"}), 400

    # Derive a slug from the user label, falling back to the first keywords /
    # authors so every filtered feed has a human-recognisable id.
    if label:
        slug = _slugify(label)
    elif keywords:
        slug = _slugify("_".join(keywords[:3]))
    else:
        slug = _slugify("_".join(authors[:2]))

    journals = load_journals()
    feed_id = f"{issn}__{slug}"
    n = 2
    while feed_id in journals:
        feed_id = f"{issn}__{slug}_{n}"
        n += 1

    display_title = f"{title} — {label}" if label else f"{title} [{', '.join((keywords or authors)[:2])}]"
    journals[feed_id] = {
        "issn": issn,
        "title": display_title,
        "publisher": publisher,
        "label": label,
        "added": datetime.now(timezone.utc).isoformat(),
        "filter": {
            "keywords": keywords,
            "authors": authors,
            "match": match,
            "openalex_source_id": openalex_source_id,
            "use_semantic_scholar": use_semantic_scholar,
            "s2_venue": s2_venue,
        },
    }
    save_journals(journals)

    threading.Thread(target=update_journal_feed, args=(feed_id, journals[feed_id]), daemon=True).start()
    return jsonify({"ok": True, "feed_id": feed_id})

@app.route("/api/journal/<issn>/filter", methods=["PUT"])
def api_set_journal_filter(issn):
    """Set or clear the keyword/author filter on a journal.

    Body: {"keywords": [...], "authors": [...], "match": "any"|"all"}
          — or {} / {"keywords": [], "authors": []} to clear the filter.
    """
    data = request.get_json() or {}
    journals = load_journals()
    if issn not in journals:
        return jsonify({"error": "not found"}), 404

    keywords = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
    authors = [str(a).strip() for a in (data.get("authors") or []) if str(a).strip()]
    match = data.get("match", "any")
    openalex_source_id = (data.get("openalex_source_id") or "").strip()
    use_semantic_scholar = bool(data.get("use_semantic_scholar"))
    s2_venue = (data.get("s2_venue") or "").strip()
    if match not in ("any", "all"):
        match = "any"

    if keywords or authors:
        journals[issn]["filter"] = {
            "keywords": keywords,
            "authors": authors,
            "match": match,
            "openalex_source_id": openalex_source_id,
            "use_semantic_scholar": use_semantic_scholar,
            "s2_venue": s2_venue,
        }
    else:
        # Empty → clear filter
        journals[issn].pop("filter", None)
    save_journals(journals)

    # Re-fetch with the new filter so the cache reflects it immediately.
    threading.Thread(target=update_journal_feed, args=(issn, journals[issn]), daemon=True).start()
    return jsonify({"ok": True, "filter": journals[issn].get("filter")})


@app.route("/api/journal/<issn>/title", methods=["PUT"])
def api_rename_journal(issn):
    """Rename a journal/feed title."""
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    journals = load_journals()
    if issn not in journals:
        return jsonify({"error": "not found"}), 404
    journals[issn]["title"] = title
    save_journals(journals)
    return jsonify({"ok": True, "title": title})


@app.route("/api/journal/<issn>", methods=["DELETE"])
def api_delete_journal(issn):
    journals = load_journals()
    if issn in journals:
        del journals[issn]
        save_journals(journals)
        cp = journal_cache_path(issn)
        if cp.exists():
            cp.unlink()
    return jsonify({"ok": True})

@app.route("/api/journal/bulk", methods=["POST"])
def api_bulk_import():
    """Bulk import journals by ISSN. Accepts {"issns": ["1234-5678", ...]}."""
    data = request.get_json()
    issns = data.get("issns", [])
    if not issns:
        return jsonify({"error": "No ISSNs provided"}), 400

    def do_bulk_import(issn_list):
        try:
            log.info(f"Bulk import started for {len(issn_list)} ISSNs")
            journals = load_journals()
            added = []
            for issn in issn_list:
                issn = issn.strip()
                if not issn or issn in journals:
                    log.info(f"  Skipping {issn} (empty or already tracked)")
                    continue
                # Look up journal metadata from CrossRef
                try:
                    url = f"https://api.crossref.org/journals/{issn}"
                    r = requests.get(url, params={"mailto": MAILTO},
                                     headers=crossref_headers(), timeout=15)
                    if r.status_code == 200:
                        msg = r.json().get("message", {})
                        title = msg.get("title", issn)
                        publisher = msg.get("publisher", "")
                    else:
                        log.warning(f"  CrossRef lookup for {issn} returned {r.status_code}")
                        title = issn
                        publisher = ""
                except Exception as e:
                    log.error(f"  CrossRef lookup failed for {issn}: {e}")
                    title = issn
                    publisher = ""

                journals[issn] = {
                    "issn": issn,
                    "title": title,
                    "publisher": publisher,
                    "added": datetime.now(timezone.utc).isoformat(),
                }
                added.append(issn)
                log.info(f"  Added {title} ({issn})")
                time.sleep(0.5)  # rate limit CrossRef lookups

            save_journals(journals)
            # Trigger feed updates for all newly added journals
            for issn in added:
                try:
                    update_journal_feed(issn, journals[issn])
                except Exception as e:
                    log.error(f"  Feed update failed for {issn}: {e}")
                time.sleep(1)
            log.info(f"Bulk import complete: {len(added)} journals added")
        except Exception as e:
            log.error(f"Bulk import failed: {e}", exc_info=True)

    threading.Thread(target=do_bulk_import, args=(issns,), daemon=True).start()
    return jsonify({"ok": True, "message": f"Importing {len(issns)} ISSNs in background"})

@app.route("/api/refresh/<issn>", methods=["POST"])
def api_refresh(issn):
    journals = load_journals()
    if issn not in journals:
        return jsonify({"error": "not found"}), 404
    threading.Thread(target=update_journal_feed, args=(issn, journals[issn]), daemon=True).start()
    return jsonify({"ok": True, "message": "Refresh started"})

@app.route("/api/refresh-all", methods=["POST"])
def api_refresh_all():
    threading.Thread(target=update_all_feeds, daemon=True).start()
    return jsonify({"ok": True, "message": "Refreshing all feeds"})

@app.route("/api/update-journal-db", methods=["POST"])
def api_update_journal_db():
    """Re-run journal_merge.py to rebuild the local journal database."""
    import subprocess, shutil
    merge_script = Path(__file__).parent / "journal_merge.py"
    if not merge_script.exists():
        return jsonify({"error": "journal_merge.py not found"}), 404

    def do_update():
        try:
            db_dir = JOURNALS_DB.parent
            db_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["JOURNAL_DATA_DIR"] = str(db_dir)
            log.info("Starting journal database update...")
            result = subprocess.run(
                [sys.executable, str(merge_script), "--download", "--all", "--merge"],
                env=env, capture_output=True, text=True, timeout=3600,
            )
            if result.returncode == 0:
                log.info("Journal database update completed successfully")
            else:
                log.error(f"Journal database update failed: {result.stderr[-500:]}")
            # Clean up raw data directory
            raw_dir = db_dir / "raw"
            if raw_dir.exists():
                shutil.rmtree(raw_dir)
                log.info("Cleaned up raw data directory")
        except Exception as e:
            log.error(f"Journal database update failed: {e}", exc_info=True)

    threading.Thread(target=do_update, daemon=True).start()
    return jsonify({"ok": True, "message": "Journal database update started in background"})

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    settings = load_settings()
    return jsonify({
        "lookback_days": settings.get("lookback_days", LOOKBACK_DAYS_DEFAULT),
        "max_articles": get_max_articles(),
    })

@app.route("/api/settings", methods=["PUT"])
def api_put_settings():
    data = request.get_json()
    settings = load_settings()
    if "lookback_days" in data:
        val = int(data["lookback_days"])
        if val < 1 or val > 3650:
            return jsonify({"error": "lookback_days must be between 1 and 3650"}), 400
        settings["lookback_days"] = val
    if "max_articles" in data:
        val = int(data["max_articles"])
        if val < 1 or val > 1000:
            return jsonify({"error": "max_articles must be between 1 and 1000"}), 400
        settings["max_articles"] = val
    save_settings(settings)
    return jsonify({"ok": True})

# ── Main ────────────────────────────────────────────────────────────────────

# ── Startup ─────────────────────────────────────────────────────────────────

ensure_dirs()


def _migrate_clean_existing_abstracts():
    """One-shot pass over cached articles to normalise abstracts.

    Strips JATS/HTML tags and any leading "Abstract" heading. Idempotent —
    running it repeatedly is a no-op once abstracts are already clean.
    """
    if not CACHE_DIR.exists():
        return
    total_changed = 0
    for cache_file in CACHE_DIR.glob("*.json"):
        try:
            cache = json.loads(cache_file.read_text())
        except Exception as e:
            log.warning(f"Skipping unreadable cache {cache_file.name}: {e}")
            continue
        changed = False
        for w in cache.get("works", []):
            orig = w.get("abstract") or ""
            if not orig:
                continue
            cleaned = clean_abstract(orig)
            if cleaned != orig:
                w["abstract"] = cleaned
                changed = True
        if changed:
            cache_file.write_text(json.dumps(cache, indent=2))
            total_changed += 1
    if total_changed:
        log.info(f"Abstract migration: cleaned {total_changed} cache file(s)")


_migrate_clean_existing_abstracts()


def _migrate_journals_backfill_issn():
    """Ensure every journal entry has an explicit ``issn`` field.

    Legacy entries pre-dated filtered variants and relied on the dict key
    being the ISSN. New code paths read ``info['issn']`` directly (the feed
    key may now be ``<issn>__<slug>`` for filtered variants), so we backfill
    the field here. Idempotent.
    """
    journals = load_journals()
    changed = False
    for key, info in list(journals.items()):
        if not isinstance(info, dict):
            continue
        if "issn" not in info:
            info["issn"] = key  # legacy entries were always keyed by plain ISSN
            changed = True
    if changed:
        save_journals(journals)
        log.info("Backfilled 'issn' field on existing journal entries")


_migrate_journals_backfill_issn()


def _migrate_fix_openalex_urls():
    """Replace openalex.org URLs with DOI URLs where a DOI exists."""
    if not CACHE_DIR.exists():
        return
    for cache_file in CACHE_DIR.glob("*.json"):
        try:
            cache = json.loads(cache_file.read_text())
        except Exception:
            continue
        changed = False
        for w in cache.get("works", []):
            url = w.get("url") or ""
            if "openalex.org" in url and w.get("doi"):
                w["url"] = f"https://doi.org/{w['doi']}"
                changed = True
        if changed:
            cache_file.write_text(json.dumps(cache, indent=2))
    log.info("Migrated cached URLs: replaced openalex.org links with DOI URLs")


_migrate_fix_openalex_urls()

# Start background scheduler only once (gunicorn may fork multiple workers)
# We use an env flag to ensure only one scheduler runs
if not os.environ.get("_SCHOLRSS_SCHEDULER_STARTED"):
    os.environ["_SCHOLRSS_SCHEDULER_STARTED"] = "1"
    _scheduler = threading.Thread(target=scheduler_loop, daemon=True)
    _scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8844, debug=False)
