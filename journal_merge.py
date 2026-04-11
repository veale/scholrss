#!/usr/bin/env python3
"""
journal_merge.py — Download and merge journal metadata from multiple open sources
into a single SQLite database with full-text search.

Sources:
  1. OpenAlex (S3 snapshot — sources entity)
  2. CrossRef (journal list API)
  3. DOAJ (public CSV dump)
  4. Sherpa Romeo (API, needs free key)
  5. Fatcat / Internet Archive (bulk export)
  6. NLM Catalog (FTP dump)

Usage:
  # Download everything (skip Sherpa Romeo)
  python journal_merge.py --download --all

  # Download everything including Sherpa Romeo
  python journal_merge.py --download --all --sherpa-key YOUR_KEY

  # Download only specific sources
  python journal_merge.py --download --sources openalex crossref doaj

  # Just rebuild the merged DB from already-downloaded data
  python journal_merge.py --merge

  # Download and merge in one go
  python journal_merge.py --download --all --merge

  # Re-run later to add Sherpa Romeo to existing DB
  python journal_merge.py --download --sources sherpa --sherpa-key YOUR_KEY --merge
"""

import argparse
import csv
import gzip
import io
import json
import logging
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("journal_merge")

DATA_DIR = Path(os.environ.get("JOURNAL_DATA_DIR", "./journal_data"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "journals.db"

SOURCES = ["openalex", "crossref", "doaj", "sherpa", "fatcat", "nlm"]

# ── Utilities ───────────────────────────────────────────────────────────────

def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for s in SOURCES:
        (RAW_DIR / s).mkdir(exist_ok=True)


def fetch_url(url, headers=None, timeout=60):
    """Fetch a URL with basic retry logic."""
    hdrs = {"User-Agent": "JournalMerge/1.0"}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs)
    for attempt in range(3):
        try:
            resp = urlopen(req, timeout=timeout)
            return resp
        except (URLError, HTTPError) as e:
            log.warning(f"  Attempt {attempt+1} failed for {url}: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise Exception(f"Failed to fetch {url} after 3 attempts")


def fetch_json(url, headers=None, timeout=60):
    resp = fetch_url(url, headers=headers, timeout=timeout)
    return json.loads(resp.read().decode("utf-8"))


def normalize_issn(issn):
    """Normalize ISSN to XXXX-XXXX format."""
    if not issn:
        return None
    issn = issn.strip().upper().replace(" ", "")
    if len(issn) == 8 and "-" not in issn:
        issn = issn[:4] + "-" + issn[4:]
    if len(issn) == 9 and issn[4] == "-":
        return issn
    return None


# ── Download: OpenAlex ──────────────────────────────────────────────────────

def download_openalex():
    """Download OpenAlex sources from S3 snapshot via HTTPS."""
    log.info("=== Downloading OpenAlex sources (S3 snapshot) ===")
    out_dir = RAW_DIR / "openalex"

    # Fetch the manifest to get file list
    manifest_url = "https://openalex.s3.amazonaws.com/data/sources/manifest"
    log.info(f"  Fetching manifest...")

    try:
        manifest = fetch_json(manifest_url, timeout=30)
    except Exception as e:
        log.error(f"  Failed to fetch manifest: {e}")
        return

    entries = manifest.get("entries", [])
    log.info(f"  Found {len(entries)} snapshot files")

    total_bytes = 0
    for i, entry in enumerate(entries):
        s3_url = entry["url"]
        # Convert s3://openalex/... to https://openalex.s3.amazonaws.com/...
        https_url = s3_url.replace("s3://openalex/", "https://openalex.s3.amazonaws.com/")

        # Preserve directory structure: updated_date=YYYY-MM-DD/part_XXXX.gz
        parts = s3_url.split("sources/", 1)
        if len(parts) > 1:
            rel_path = parts[1]  # e.g. "updated_date=2026-02-09/part_0000.gz"
        else:
            rel_path = os.path.basename(s3_url)

        local_path = out_dir / rel_path
        local_path.parent.mkdir(parents=True, exist_ok=True)

        record_count = entry.get("meta", {}).get("record_count", "?")
        log.info(f"  [{i+1}/{len(entries)}] {rel_path} ({record_count} records)...")

        try:
            resp = fetch_url(https_url, timeout=120)
            data = resp.read()
            with open(local_path, "wb") as f:
                f.write(data)
            total_bytes += len(data)
        except Exception as e:
            log.warning(f"  Failed to download {rel_path}: {e}")

    log.info(f"  Downloaded {total_bytes / 1024 / 1024:.1f} MB total")


# ── Download: CrossRef ──────────────────────────────────────────────────────

def download_crossref():
    """Download CrossRef journal list via API pagination."""
    log.info("=== Downloading CrossRef journals ===")
    out_file = RAW_DIR / "crossref" / "journals.jsonl"
    mailto = os.environ.get("MAILTO", "journal-merge@example.com")

    all_journals = []
    offset = 0
    rows = 1000

    while True:
        url = f"https://api.crossref.org/journals?rows={rows}&offset={offset}&mailto={mailto}"
        log.info(f"  Offset {offset} (got {len(all_journals)} so far)...")

        try:
            data = fetch_json(url, timeout=30)
        except Exception as e:
            log.error(f"  Failed at offset {offset}: {e}")
            break

        items = data.get("message", {}).get("items", [])
        if not items:
            break

        for item in items:
            record = {
                "title": item.get("title", ""),
                "publisher": item.get("publisher", ""),
                "issns": item.get("ISSN", []),
                "subjects": [s.get("name", "") for s in item.get("subjects", [])],
                "coverage": item.get("coverage", {}),
            }
            all_journals.append(record)

        offset += rows
        total = data.get("message", {}).get("total-results", 0)
        if offset >= total:
            break

        time.sleep(0.5)  # be polite

    log.info(f"  Downloaded {len(all_journals)} CrossRef journals")

    with open(out_file, "w") as f:
        for j in all_journals:
            f.write(json.dumps(j) + "\n")

    log.info(f"  Saved to {out_file}")


# ── Download: DOAJ ──────────────────────────────────────────────────────────

def download_doaj():
    """Download DOAJ CSV dump."""
    log.info("=== Downloading DOAJ journal list ===")
    out_file = RAW_DIR / "doaj" / "journals.csv"

    url = "https://doaj.org/csv"
    log.info(f"  Fetching {url} ...")

    try:
        resp = fetch_url(url, timeout=120)
        data = resp.read()
        with open(out_file, "wb") as f:
            f.write(data)
        log.info(f"  Saved to {out_file} ({len(data) / 1024 / 1024:.1f} MB)")
    except Exception as e:
        log.error(f"  Failed to download DOAJ: {e}")


# ── Download: Sherpa Romeo ──────────────────────────────────────────────────

def download_sherpa(api_key):
    """Download Sherpa Romeo journal data via API."""
    log.info("=== Downloading Sherpa Romeo publications ===")
    out_file = RAW_DIR / "sherpa" / "publications.jsonl"

    all_pubs = []
    offset = 0
    limit = 100

    while True:
        url = (
            f"https://v2.sherpa.ac.uk/cgi/retrieve"
            f"?item-type=publication&format=Json"
            f"&api-key={api_key}&limit={limit}&offset={offset}"
        )
        log.info(f"  Offset {offset} (got {len(all_pubs)} so far)...")

        try:
            data = fetch_json(url, timeout=30)
        except Exception as e:
            log.error(f"  Failed at offset {offset}: {e}")
            break

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            title_list = item.get("title", [])
            title = title_list[0].get("title", "") if title_list else ""

            issns = []
            for issn_obj in item.get("issns", []):
                issn = issn_obj.get("issn", "")
                if issn:
                    issns.append(issn)

            publishers = item.get("publishers", [])
            publisher = ""
            if publishers:
                pub_info = publishers[0].get("publisher", {})
                pub_name = pub_info.get("name", [])
                if pub_name:
                    publisher = pub_name[0].get("name", "")

            # Extract OA policy summary
            policies = item.get("publisher_policy", [])
            oa_status = ""
            for pol in policies:
                permitted = pol.get("permitted_oa", [])
                if permitted:
                    oa_status = "has_oa_policy"
                    break

            record = {
                "title": title,
                "issns": issns,
                "publisher": publisher,
                "sherpa_id": item.get("id", ""),
                "oa_status": oa_status,
                "type": item.get("type", ""),
            }
            all_pubs.append(record)

        offset += limit
        time.sleep(0.3)  # rate limit

    log.info(f"  Downloaded {len(all_pubs)} Sherpa Romeo publications")

    with open(out_file, "w") as f:
        for p in all_pubs:
            f.write(json.dumps(p) + "\n")

    log.info(f"  Saved to {out_file}")


# ── Download: Fatcat ────────────────────────────────────────────────────────

def download_fatcat():
    """Download Fatcat container (journal) export."""
    log.info("=== Downloading Fatcat containers ===")
    out_file = RAW_DIR / "fatcat" / "containers.jsonl.gz"

    # The bulk export URL — this is a large-ish file
    url = "https://archive.org/download/fatcat_bulk_exports/container_export.json.gz"
    log.info(f"  Fetching {url} (this may take a few minutes)...")

    try:
        resp = fetch_url(url, timeout=300)
        data = resp.read()
        with open(out_file, "wb") as f:
            f.write(data)
        log.info(f"  Saved to {out_file} ({len(data) / 1024 / 1024:.1f} MB)")
    except Exception as e:
        log.error(f"  Failed to download Fatcat: {e}")
        log.info("  You can manually download from https://archive.org/details/fatcat_bulk_exports")


# ── Download: NLM ───────────────────────────────────────────────────────────

def download_nlm():
    """Download NLM journal list (the simple CSV version)."""
    log.info("=== Downloading NLM journal list ===")
    out_file = RAW_DIR / "nlm" / "journals.csv"

    # NLM provides a simple journal list CSV
    url = "https://ftp.ncbi.nlm.nih.gov/pubmed/J_Medline.txt"
    log.info(f"  Fetching {url} ...")

    try:
        resp = fetch_url(url, timeout=60)
        data = resp.read()
        with open(out_file, "wb") as f:
            f.write(data)
        log.info(f"  Saved to {out_file} ({len(data) / 1024:.0f} KB)")
    except Exception as e:
        log.error(f"  Failed to download NLM: {e}")


# ── Parse helpers ───────────────────────────────────────────────────────────

def parse_openalex():
    """Parse OpenAlex sources from S3 snapshot (gzipped JSONL files)."""
    oa_dir = RAW_DIR / "openalex"

    # Find all .gz files (could be in updated_date=* subdirectories or flat)
    gz_files = sorted(oa_dir.rglob("*.gz"))

    # Also check for the old API-format JSONL as fallback
    jsonl_path = oa_dir / "sources.jsonl"
    if not gz_files and jsonl_path.exists():
        log.info("  Using API-format sources.jsonl")
        gz_files = []  # fall through to jsonl path below
        records = []
        for line in open(jsonl_path):
            src = json.loads(line)
            issn_l = normalize_issn(src.get("issn_l", ""))
            issns = [normalize_issn(i) for i in src.get("issns", [])]
            issns = [i for i in issns if i]
            if not issns and not issn_l:
                continue
            records.append({
                "issn_l": issn_l,
                "issns": issns,
                "title": src.get("title", ""),
                "publisher": src.get("publisher", ""),
                "country": src.get("country", ""),
                "is_oa": src.get("is_oa", False),
                "works_count": src.get("works_count", 0),
                "homepage": src.get("homepage", ""),
                "source": "openalex",
            })
        log.info(f"  Parsed {len(records)} OpenAlex sources")
        return records

    if not gz_files:
        log.warning("  OpenAlex data not found, skipping")
        return []

    log.info(f"  Reading {len(gz_files)} snapshot files...")
    records = []
    seen_ids = set()  # deduplicate across partitions (later partitions win)

    # Process all files; later updated_date partitions override earlier ones
    all_sources = {}
    for gz_path in gz_files:
        try:
            with gzip.open(gz_path, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    src = json.loads(line)
                    src_id = src.get("id", "")
                    # S3 snapshot uses different field names than the API
                    issn_l = normalize_issn(src.get("issn_l", ""))
                    issn_list = src.get("issn", []) or []
                    issns = [normalize_issn(i) for i in issn_list]
                    issns = [i for i in issns if i]

                    if not issns and not issn_l:
                        continue

                    # Use OpenAlex ID as dedup key so later partitions update earlier ones
                    all_sources[src_id or issn_l or issns[0]] = {
                        "issn_l": issn_l,
                        "issns": issns,
                        "title": src.get("display_name", ""),
                        "publisher": src.get("host_organization_name", ""),
                        "country": src.get("country_code", ""),
                        "is_oa": src.get("is_oa", False),
                        "works_count": src.get("works_count", 0),
                        "homepage": src.get("homepage_url", ""),
                        "source": "openalex",
                    }
        except Exception as e:
            log.warning(f"  Error reading {gz_path.name}: {e}")

    records = list(all_sources.values())
    log.info(f"  Parsed {len(records)} OpenAlex sources")
    return records


def parse_crossref():
    """Parse CrossRef journals JSONL."""
    path = RAW_DIR / "crossref" / "journals.jsonl"
    if not path.exists():
        log.warning("  CrossRef data not found, skipping")
        return []

    records = []
    for line in open(path):
        j = json.loads(line)
        issns = [normalize_issn(i) for i in j.get("issns", [])]
        issns = [i for i in issns if i]
        if not issns:
            continue
        records.append({
            "issn_l": issns[0] if issns else None,
            "issns": issns,
            "title": j.get("title", ""),
            "publisher": j.get("publisher", ""),
            "subjects": j.get("subjects", []),
            "source": "crossref",
        })
    log.info(f"  Parsed {len(records)} CrossRef journals")
    return records


def parse_doaj():
    """Parse DOAJ CSV."""
    path = RAW_DIR / "doaj" / "journals.csv"
    if not path.exists():
        log.warning("  DOAJ data not found, skipping")
        return []

    records = []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                issns = []
                for field in ["Journal ISSN (print version)", "Journal EISSN (online version)"]:
                    issn = normalize_issn(row.get(field, ""))
                    if issn:
                        issns.append(issn)
                if not issns:
                    continue
                records.append({
                    "issn_l": issns[0],
                    "issns": issns,
                    "title": row.get("Journal title", ""),
                    "publisher": row.get("Publisher", ""),
                    "country": row.get("Country of publisher", ""),
                    "is_oa": True,
                    "subjects": row.get("Keywords", ""),
                    "license": row.get("Journal license", ""),
                    "source": "doaj",
                })
    except Exception as e:
        log.error(f"  Error parsing DOAJ CSV: {e}")

    log.info(f"  Parsed {len(records)} DOAJ journals")
    return records


def parse_sherpa():
    """Parse Sherpa Romeo JSONL."""
    path = RAW_DIR / "sherpa" / "publications.jsonl"
    if not path.exists():
        log.warning("  Sherpa Romeo data not found, skipping")
        return []

    records = []
    for line in open(path):
        p = json.loads(line)
        issns = [normalize_issn(i) for i in p.get("issns", [])]
        issns = [i for i in issns if i]
        if not issns:
            continue
        records.append({
            "issn_l": issns[0],
            "issns": issns,
            "title": p.get("title", ""),
            "publisher": p.get("publisher", ""),
            "sherpa_id": p.get("sherpa_id", ""),
            "oa_status": p.get("oa_status", ""),
            "source": "sherpa",
        })
    log.info(f"  Parsed {len(records)} Sherpa Romeo publications")
    return records


def parse_fatcat():
    """Parse Fatcat container export."""
    gz_path = RAW_DIR / "fatcat" / "containers.jsonl.gz"
    if not gz_path.exists():
        log.warning("  Fatcat data not found, skipping")
        return []

    records = []
    try:
        with gzip.open(gz_path, "rt", encoding="utf-8") as f:
            for line in f:
                c = json.loads(line)
                issn_l = normalize_issn(c.get("issnl", ""))
                issns = []
                if issn_l:
                    issns.append(issn_l)
                issn_e = normalize_issn(c.get("issne", ""))
                issn_p = normalize_issn(c.get("issnp", ""))
                if issn_e and issn_e not in issns:
                    issns.append(issn_e)
                if issn_p and issn_p not in issns:
                    issns.append(issn_p)
                if not issns:
                    continue
                records.append({
                    "issn_l": issn_l or issns[0],
                    "issns": issns,
                    "title": c.get("name", ""),
                    "publisher": c.get("publisher", ""),
                    "country": c.get("country", ""),
                    "container_type": c.get("container_type", ""),
                    "source": "fatcat",
                })
    except Exception as e:
        log.error(f"  Error parsing Fatcat: {e}")

    log.info(f"  Parsed {len(records)} Fatcat containers")
    return records


def parse_nlm():
    """Parse NLM J_Medline.txt (custom format, not CSV)."""
    path = RAW_DIR / "nlm" / "journals.csv"
    if not path.exists():
        log.warning("  NLM data not found, skipping")
        return []

    records = []
    current = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("---"):
                    if current.get("title"):
                        issns = []
                        issn = normalize_issn(current.get("issn_print", ""))
                        if issn:
                            issns.append(issn)
                        issn = normalize_issn(current.get("issn_online", ""))
                        if issn:
                            issns.append(issn)
                        if issns:
                            records.append({
                                "issn_l": issns[0],
                                "issns": issns,
                                "title": current.get("title", ""),
                                "abbreviation": current.get("abbr", ""),
                                "nlm_id": current.get("nlm_id", ""),
                                "source": "nlm",
                            })
                    current = {}
                elif ": " in line:
                    key, _, val = line.partition(": ")
                    key = key.strip()
                    val = val.strip()
                    if key == "JournalTitle":
                        current["title"] = val
                    elif key == "MedAbbr":
                        current["abbr"] = val
                    elif key == "ISSN (Print)":
                        current["issn_print"] = val
                    elif key == "ISSN (Online)":
                        current["issn_online"] = val
                    elif key == "NlmId":
                        current["nlm_id"] = val
    except Exception as e:
        log.error(f"  Error parsing NLM: {e}")

    log.info(f"  Parsed {len(records)} NLM journals")
    return records


# ── Merge ───────────────────────────────────────────────────────────────────

def merge_all():
    """Merge all parsed sources into a single SQLite database."""
    log.info("=== Merging all sources ===")

    # Parse all available sources
    all_records = {
        "openalex": parse_openalex(),
        "crossref": parse_crossref(),
        "doaj": parse_doaj(),
        "sherpa": parse_sherpa(),
        "fatcat": parse_fatcat(),
        "nlm": parse_nlm(),
    }

    # Build ISSN → merged record lookup
    # Key: any ISSN → points to the canonical record keyed by issn_l
    issn_to_canonical = {}  # issn → issn_l
    merged = {}  # issn_l → merged record

    def get_or_create(issns, issn_l):
        """Find existing canonical record or create new one."""
        # Check if any ISSN already maps to a canonical
        for issn in issns:
            if issn in issn_to_canonical:
                return issn_to_canonical[issn]

        # Create new canonical entry
        canonical_key = issn_l or issns[0]
        for issn in issns:
            issn_to_canonical[issn] = canonical_key
        return canonical_key

    # Process sources in priority order (OpenAlex first as base)
    source_order = ["openalex", "crossref", "doaj", "fatcat", "nlm", "sherpa"]

    for source_name in source_order:
        records = all_records.get(source_name, [])
        for rec in records:
            issns = rec.get("issns", [])
            issn_l = rec.get("issn_l")
            if not issns and not issn_l:
                continue

            all_issns = list(set(filter(None, issns + ([issn_l] if issn_l else []))))
            canonical_key = get_or_create(all_issns, issn_l)

            if canonical_key not in merged:
                merged[canonical_key] = {
                    "issn_l": canonical_key,
                    "issns": set(),
                    "title": "",
                    "alt_titles": set(),
                    "publisher": "",
                    "country": "",
                    "is_oa": False,
                    "works_count": 0,
                    "homepage": "",
                    "subjects": "",
                    "abbreviation": "",
                    "sherpa_id": "",
                    "oa_status": "",
                    "nlm_id": "",
                    "sources": set(),
                }

            m = merged[canonical_key]
            m["issns"].update(all_issns)
            m["sources"].add(source_name)

            # Prefer longer/more complete title
            new_title = rec.get("title", "")
            if new_title and (not m["title"] or len(new_title) > len(m["title"])):
                if m["title"]:
                    m["alt_titles"].add(m["title"])
                m["title"] = new_title
            elif new_title and new_title != m["title"]:
                m["alt_titles"].add(new_title)

            # Fill in blanks
            if not m["publisher"] and rec.get("publisher"):
                m["publisher"] = rec["publisher"]
            if not m["country"] and rec.get("country"):
                m["country"] = rec["country"]
            if rec.get("is_oa"):
                m["is_oa"] = True
            if rec.get("works_count", 0) > m["works_count"]:
                m["works_count"] = rec["works_count"]
            if not m["homepage"] and rec.get("homepage"):
                m["homepage"] = rec["homepage"]
            if not m["abbreviation"] and rec.get("abbreviation"):
                m["abbreviation"] = rec["abbreviation"]
            if not m["sherpa_id"] and rec.get("sherpa_id"):
                m["sherpa_id"] = str(rec["sherpa_id"])
            if not m["oa_status"] and rec.get("oa_status"):
                m["oa_status"] = rec["oa_status"]
            if not m["nlm_id"] and rec.get("nlm_id"):
                m["nlm_id"] = rec["nlm_id"]
            if rec.get("subjects"):
                subj = rec["subjects"]
                if isinstance(subj, list):
                    subj = "; ".join(subj)
                if not m["subjects"]:
                    m["subjects"] = subj

    log.info(f"  Merged into {len(merged)} unique journals")

    # Write to SQLite
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE journals (
            issn_l TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            alt_titles TEXT,
            publisher TEXT,
            country TEXT,
            is_oa INTEGER DEFAULT 0,
            works_count INTEGER DEFAULT 0,
            homepage TEXT,
            subjects TEXT,
            abbreviation TEXT,
            sherpa_id TEXT,
            oa_status TEXT,
            nlm_id TEXT,
            all_issns TEXT,
            sources TEXT
        )
    """)

    # ISSN lookup table (maps any ISSN variant to its issn_l)
    cur.execute("""
        CREATE TABLE issn_map (
            issn TEXT PRIMARY KEY,
            issn_l TEXT NOT NULL,
            FOREIGN KEY (issn_l) REFERENCES journals(issn_l)
        )
    """)

    # FTS5 for fast prefix/full-text search
    cur.execute("""
        CREATE VIRTUAL TABLE journals_fts USING fts5(
            issn_l,
            title,
            alt_titles,
            publisher,
            abbreviation,
            all_issns,
            content='journals',
            content_rowid='rowid'
        )
    """)

    count = 0
    for issn_l, m in merged.items():
        all_issns_str = " ".join(sorted(m["issns"]))
        alt_titles_str = "; ".join(sorted(m["alt_titles"])) if m["alt_titles"] else ""
        sources_str = ",".join(sorted(m["sources"]))

        cur.execute("""
            INSERT INTO journals
            (issn_l, title, alt_titles, publisher, country, is_oa, works_count,
             homepage, subjects, abbreviation, sherpa_id, oa_status, nlm_id,
             all_issns, sources)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            issn_l, m["title"], alt_titles_str, m["publisher"], m["country"],
            1 if m["is_oa"] else 0, m["works_count"], m["homepage"],
            m["subjects"], m["abbreviation"], m["sherpa_id"], m["oa_status"],
            m["nlm_id"], all_issns_str, sources_str,
        ))

        for issn in m["issns"]:
            try:
                cur.execute("INSERT OR IGNORE INTO issn_map (issn, issn_l) VALUES (?, ?)",
                            (issn, issn_l))
            except sqlite3.IntegrityError:
                pass

        count += 1

    # Populate FTS index
    cur.execute("""
        INSERT INTO journals_fts (rowid, issn_l, title, alt_titles, publisher, abbreviation, all_issns)
        SELECT rowid, issn_l, title, alt_titles, publisher, abbreviation, all_issns
        FROM journals
    """)

    conn.commit()

    # Stats
    cur.execute("SELECT COUNT(*) FROM journals")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM issn_map")
    issn_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM journals WHERE is_oa = 1")
    oa_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM journals WHERE sherpa_id != ''")
    sherpa_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM journals WHERE nlm_id != ''")
    nlm_count = cur.fetchone()[0]

    db_size = DB_PATH.stat().st_size / 1024 / 1024

    log.info(f"")
    log.info(f"  ╔══════════════════════════════════════╗")
    log.info(f"  ║  Merge complete                      ║")
    log.info(f"  ╠══════════════════════════════════════╣")
    log.info(f"  ║  Journals:     {total:>8,}              ║")
    log.info(f"  ║  ISSN entries: {issn_count:>8,}              ║")
    log.info(f"  ║  Open Access:  {oa_count:>8,}              ║")
    log.info(f"  ║  Sherpa Romeo: {sherpa_count:>8,}              ║")
    log.info(f"  ║  NLM/PubMed:   {nlm_count:>8,}              ║")
    log.info(f"  ║  DB size:      {db_size:>7.1f} MB            ║")
    log.info(f"  ╚══════════════════════════════════════╝")
    log.info(f"  Saved to {DB_PATH}")

    conn.close()


# ── Search (for testing) ────────────────────────────────────────────────────

def search_journals(query, limit=10):
    """Search the merged database."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Try FTS search
    cur.execute("""
        SELECT j.*
        FROM journals_fts fts
        JOIN journals j ON j.rowid = fts.rowid
        WHERE journals_fts MATCH ?
        ORDER BY j.works_count DESC
        LIMIT ?
    """, (f'"{query}" OR {query}*', limit))

    results = [dict(row) for row in cur.fetchall()]
    conn.close()
    return results


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download and merge journal metadata into SQLite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --download --all --merge
  %(prog)s --download --sources openalex crossref doaj --merge
  %(prog)s --download --all --sherpa-key ABC123 --merge
  %(prog)s --merge  # rebuild from already-downloaded data
  %(prog)s --search "Nature Medicine"
        """
    )
    parser.add_argument("--download", action="store_true", help="Download source data")
    parser.add_argument("--all", action="store_true", help="Download all sources")
    parser.add_argument("--sources", nargs="+", choices=SOURCES, help="Specific sources to download")
    parser.add_argument("--sherpa-key", help="Sherpa Romeo API key")
    parser.add_argument("--merge", action="store_true", help="Merge downloaded data into SQLite")
    parser.add_argument("--search", help="Test search query")
    parser.add_argument("--data-dir", help="Override data directory")

    args = parser.parse_args()

    if args.data_dir:
        global DATA_DIR, RAW_DIR, DB_PATH
        DATA_DIR = Path(args.data_dir)
        RAW_DIR = DATA_DIR / "raw"
        DB_PATH = DATA_DIR / "journals.db"

    ensure_dirs()

    if not any([args.download, args.merge, args.search]):
        parser.print_help()
        sys.exit(1)

    if args.download:
        sources_to_dl = SOURCES if args.all else (args.sources or [])

        if not sources_to_dl:
            log.error("Specify --all or --sources to download")
            sys.exit(1)

        for src in sources_to_dl:
            if src == "openalex":
                download_openalex()
            elif src == "crossref":
                download_crossref()
            elif src == "doaj":
                download_doaj()
            elif src == "sherpa":
                if not args.sherpa_key:
                    log.warning("Skipping Sherpa Romeo — no --sherpa-key provided")
                    log.info("  Get a free key at https://v2.sherpa.ac.uk/cgi/register")
                    continue
                download_sherpa(args.sherpa_key)
            elif src == "fatcat":
                download_fatcat()
            elif src == "nlm":
                download_nlm()

    if args.merge:
        merge_all()

    if args.search:
        if not DB_PATH.exists():
            log.error(f"Database not found at {DB_PATH}. Run --merge first.")
            sys.exit(1)

        results = search_journals(args.search)
        if not results:
            print("No results found.")
        else:
            for r in results:
                oa = " [OA]" if r["is_oa"] else ""
                sources = r["sources"]
                print(f"  {r['issn_l']}  {r['title']}{oa}")
                print(f"           Publisher: {r['publisher'] or '?'}")
                print(f"           Sources: {sources} | Works: {r['works_count']:,}")
                if r["abbreviation"]:
                    print(f"           Abbrev: {r['abbreviation']}")
                print()


if __name__ == "__main__":
    main()
