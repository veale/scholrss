"""
ScholRSS MCP Server — exposes cached scholarly article data to LLMs.

Tools:
  - latest_articles: Get the most recent articles across all tracked journals
  - search_articles: Search article titles and abstracts by keyword
  - list_journals:   List all tracked journals with article counts

Run standalone:  python mcp_server.py
Or via MCP:      add to your claude_desktop_config.json / claude code settings
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DATA_DIR = Path(os.environ.get("SCHOLRSS_DATA_DIR",
                os.environ.get("DATA_DIR", "./data")))
CACHE_DIR = DATA_DIR / "cache"
JOURNALS_FILE = DATA_DIR / "journals.json"

mcp = FastMCP("scholrss", instructions=(
    "ScholRSS provides access to cached scholarly articles from tracked academic journals. "
    "Use `list_journals` to see what's tracked, `latest_articles` for recent papers, "
    "and `search_articles` to find papers by keyword in titles/abstracts."
))


def _load_journals():
    if JOURNALS_FILE.exists():
        return json.loads(JOURNALS_FILE.read_text())
    return {}


def _load_all_works():
    """Load all cached works across all journals."""
    works = []
    if not CACHE_DIR.exists():
        return works
    journals = _load_journals()
    for cache_file in CACHE_DIR.glob("*.json"):
        cache = json.loads(cache_file.read_text())
        journal_title = cache.get("journal", {}).get("title", "Unknown")
        issn = cache.get("issn", "")
        for w in cache.get("works", []):
            w["_journal"] = journal_title
            w["_issn"] = issn
            works.append(w)
    return works


def _format_article(w):
    """Format a single article as a compact text block."""
    authors = ", ".join(w.get("authors", [])[:5])
    if len(w.get("authors", [])) > 5:
        authors += f" et al. ({len(w['authors'])} authors)"
    date = w.get("date", "")[:10]
    abstract = w.get("abstract", "").strip()
    if len(abstract) > 1000:
        abstract = abstract[:997] + "..."
    lines = [
        f"**{w.get('title', 'Untitled')}**",
        f"  {authors}" if authors else None,
        f"  {w.get('_journal', '')} | {date}",
        f"  DOI: {w['doi']} — https://doi.org/{w['doi']}" if w.get("doi") else None,
        f"  {abstract}" if abstract else "  [No abstract available]",
    ]
    return "\n".join(l for l in lines if l is not None)


@mcp.tool()
def list_journals() -> str:
    """List all tracked journals with article counts and last update times."""
    journals = _load_journals()
    if not journals:
        return "No journals are currently tracked in ScholRSS."
    lines = []
    for issn, info in journals.items():
        cache_path = CACHE_DIR / f"{issn.replace('-', '')}.json"
        if cache_path.exists():
            cache = json.loads(cache_path.read_text())
            count = len(cache.get("works", []))
            with_abstract = sum(1 for w in cache.get("works", []) if w.get("abstract"))
            updated = cache.get("updated", "never")[:16]
        else:
            count = 0
            with_abstract = 0
            updated = "not yet fetched"
        lines.append(
            f"- **{info.get('title', issn)}** ({issn})\n"
            f"  {info.get('publisher', '')} | {count} articles, {with_abstract} with abstracts | updated {updated}"
        )
    return f"**Tracked journals ({len(journals)}):**\n\n" + "\n".join(lines)


@mcp.tool()
def latest_articles(count: int = 20, journal: str | None = None) -> str:
    """Get the most recent articles across all tracked journals, sorted by date.

    Args:
        count: Number of articles to return (1-100, default 20)
        journal: Optional filter — journal name or ISSN to restrict results to
    """
    count = max(1, min(100, count))
    works = _load_all_works()
    if not works:
        return "No cached articles found. Make sure ScholRSS has tracked journals with fetched feeds."

    if journal:
        q = journal.lower()
        works = [w for w in works
                 if q in w.get("_journal", "").lower() or q in w.get("_issn", "").lower()]
        if not works:
            return f"No articles found matching journal '{journal}'."

    works.sort(key=lambda w: w.get("date", ""), reverse=True)
    works = works[:count]

    header = f"**Latest {len(works)} articles"
    if journal:
        header += f" from '{journal}'"
    header += ":**\n"

    return header + "\n---\n".join(_format_article(w) for w in works)


@mcp.tool()
def search_articles(query: str, count: int = 20) -> str:
    """Search article titles and abstracts by keyword across all tracked journals.

    Args:
        query: Search term(s) to match against titles and abstracts (case-insensitive)
        count: Maximum number of results to return (1-100, default 20)
    """
    count = max(1, min(100, count))
    works = _load_all_works()
    if not works:
        return "No cached articles found. Make sure ScholRSS has tracked journals with fetched feeds."

    terms = query.lower().split()
    scored = []
    for w in works:
        title = w.get("title", "").lower()
        abstract = w.get("abstract", "").lower()
        text = title + " " + abstract
        if all(t in text for t in terms):
            # Score: title matches count more, plus recency
            score = sum(2 if t in title else 1 for t in terms)
            scored.append((score, w))

    if not scored:
        return f"No articles found matching '{query}'."

    scored.sort(key=lambda x: (-x[0], x[1].get("date", "")), reverse=False)
    scored.sort(key=lambda x: x[0], reverse=True)
    results = [w for _, w in scored[:count]]

    return f"**{len(results)} results for '{query}':**\n\n" + "\n---\n".join(
        _format_article(w) for w in results
    )


if __name__ == "__main__":
    mcp.run()
