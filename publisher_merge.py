#!/usr/bin/env python3
"""Download OpenAlex publisher metadata into an FTS-enabled SQLite DB."""

import argparse
import gzip
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("publisher_merge")

DATA_DIR = Path(os.environ.get("JOURNAL_DATA_DIR", "./journal_data"))
RAW_DIR = DATA_DIR / "raw"
PUBLISHERS_DIR = RAW_DIR / "publishers"
DB_PATH = DATA_DIR / "bookpublishers.db"


def ensure_dirs():
    PUBLISHERS_DIR.mkdir(parents=True, exist_ok=True)


def fetch_url(url, headers=None, timeout=60):
    hdrs = {"User-Agent": "PublisherMerge/1.0"}
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


def fetch_json(url, timeout=30):
    resp = fetch_url(url, timeout=timeout)
    return json.loads(resp.read().decode("utf-8"))


def download_publishers():
    log.info("=== Downloading OpenAlex publishers snapshot ===")
    manifest_url = "https://openalex.s3.amazonaws.com/data/publishers/manifest"
    log.info(f"  Fetching manifest {manifest_url}")
    manifest = fetch_json(manifest_url, timeout=30)
    entries = manifest.get("entries", [])
    log.info(f"  Found {len(entries)} snapshot files")

    total_bytes = 0
    for i, entry in enumerate(entries):
        s3_url = entry.get("url")
        if not s3_url:
            continue
        https_url = s3_url.replace("s3://openalex/", "https://openalex.s3.amazonaws.com/")
        rel_path = s3_url.split("publishers/", 1)[-1]
        local_path = PUBLISHERS_DIR / rel_path
        local_path.parent.mkdir(parents=True, exist_ok=True)

        record_count = entry.get("meta", {}).get("record_count", "?")
        log.info(f"  [{i+1}/{len(entries)}] {rel_path} ({record_count} records)")

        try:
            resp = fetch_url(https_url, timeout=120)
            data = resp.read()
            with open(local_path, "wb") as f:
                f.write(data)
            total_bytes += len(data)
        except Exception as e:
            log.error(f"  Failed to download {rel_path}: {e}")

    log.info(f"  Downloaded {total_bytes / 1024 / 1024:.1f} MB total")


def parse_id(raw_id):
    if not raw_id:
        return ""
    return raw_id.rstrip("/").split("/")[-1]


def merge_publishers():
    log.info("=== Merging OpenAlex publishers into SQLite ===")
    gz_files = sorted(PUBLISHERS_DIR.rglob("*.gz"))
    if not gz_files:
        log.warning("  No publisher snapshot files found, skipping merge")
        return

    records = {}
    for gz_path in gz_files:
        log.info(f"  Reading {gz_path.relative_to(PUBLISHERS_DIR)}")
        try:
            with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    publisher_id = parse_id(rec.get("id"))
                    if not publisher_id:
                        continue
                    records[publisher_id] = {
                        "display_name": rec.get("display_name") or "",
                        "alt_names": " ".join(sorted(rec.get("alternate_titles") or [])),
                        "hierarchy_level": int(rec.get("hierarchy_level") or 0),
                        "parent_id": parse_id(rec.get("parent_publisher")),
                        "lineage": " ".join(parse_id(x) for x in (rec.get("lineage") or []) if x),
                        "country_codes": ",".join(rec.get("country_codes") or []),
                        "works_count": int(rec.get("works_count") or 0),
                        "sources_count": int(rec.get("sources_count") or 0),
                        "cited_by_count": int(rec.get("cited_by_count") or 0),
                        "homepage": rec.get("homepage_url") or "",
                    }
        except Exception as e:
            log.error(f"  Failed to read {gz_path}: {e}")

    if not records:
        log.warning("  No publisher records parsed, skipping merge")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("DROP TABLE IF EXISTS publishers")
    cur.execute("DROP TABLE IF EXISTS publishers_fts")
    cur.execute("CREATE TABLE publishers (\n"
                "    publisher_id TEXT PRIMARY KEY,\n"
                "    display_name TEXT NOT NULL,\n"
                "    alt_names TEXT,\n"
                "    hierarchy_level INTEGER DEFAULT 0,\n"
                "    parent_id TEXT,\n"
                "    lineage TEXT,\n"
                "    country_codes TEXT,\n"
                "    works_count INTEGER DEFAULT 0,\n"
                "    sources_count INTEGER DEFAULT 0,\n"
                "    cited_by_count INTEGER DEFAULT 0,\n"
                "    homepage TEXT\n"
                ")")
    cur.execute("CREATE VIRTUAL TABLE publishers_fts USING fts5(\n"
                "    publisher_id,\n"
                "    display_name,\n"
                "    alt_names,\n"
                "    content='publishers',\n"
                "    content_rowid='rowid'\n"
                ")")

    cur.execute("CREATE INDEX idx_works_count ON publishers(works_count DESC)")

    for pid, data in records.items():
        cur.execute("INSERT INTO publishers (publisher_id, display_name, alt_names, hierarchy_level, parent_id, lineage, country_codes, works_count, sources_count, cited_by_count, homepage) \n"
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            pid, data["display_name"], data["alt_names"], data["hierarchy_level"], data["parent_id"],
            data["lineage"], data["country_codes"], data["works_count"], data["sources_count"],
            data["cited_by_count"], data["homepage"],
        ))

    cur.execute("INSERT INTO publishers_fts (rowid, publisher_id, display_name, alt_names)\n"
                "SELECT rowid, publisher_id, display_name, alt_names FROM publishers")

    conn.commit()
    conn.close()

    db_size = DB_PATH.stat().st_size / 1024 / 1024
    log.info(f"  Merge complete — {len(records)} publishers, DB size {db_size:.1f} MB")


def main():
    parser = argparse.ArgumentParser(description="Build book publisher autocomplete DB")
    parser.add_argument("--download", action="store_true", help="Download OpenAlex snapshots")
    parser.add_argument("--merge", action="store_true", help="Merge downloaded data into SQLite")
    parser.add_argument("--data-dir", help="Override journal data directory")
    args = parser.parse_args()

    if args.data_dir:
        global DATA_DIR, RAW_DIR, PUBLISHERS_DIR, DB_PATH
        DATA_DIR = Path(args.data_dir)
        RAW_DIR = DATA_DIR / "raw"
        PUBLISHERS_DIR = RAW_DIR / "publishers"
        DB_PATH = DATA_DIR / "bookpublishers.db"

    ensure_dirs()

    if args.download:
        download_publishers()
    if args.merge:
        merge_publishers()
    if not (args.download or args.merge):
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
