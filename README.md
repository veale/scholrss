# ScholRSS

A self-hosted scholarly RSS feed generator that pulls article metadata from **CrossRef**, enriches abstracts via **Semantic Scholar** and **OpenAlex**, and serves RSS/Atom feeds compatible with FreshRSS or any feed reader.

## Features

- **Journal autocomplete** — instant local search across 167K+ journals (SQLite FTS5), with online CrossRef fallback
- **Multiple add methods** — by name, ISSN, DOI lookup, or bulk ISSN import
- **Tiered abstract enrichment** — CrossRef for articles, then Semantic Scholar batch API (one request for all DOIs), then OpenAlex individual lookups for anything still missing
- **RSS/Atom/JSON feeds** — one feed per journal, plus OPML export for bulk import into feed readers
- **Daily auto-refresh** — background scheduler with configurable interval
- **Configurable lookback** — set how far back to fetch articles (default 365 days), adjustable from the UI
- **MCP server** — expose cached articles to LLMs via Model Context Protocol
- **Docker ready** — single container, bind-mount data directory, reverse-proxy friendly

## Quick Start

1. **Clone and configure:**
   ```bash
   cd ScholRSS
   cp docker-compose.example.yml docker-compose.yml
   # Edit docker-compose.yml — set MAILTO, OPENALEX_API_KEY, BASE_URL
   ```

2. **Get API keys:**
   - **OpenAlex** (required) — free key from https://openalex.org/settings/api
   - **Semantic Scholar** (optional, improves abstract coverage) — from https://www.semanticscholar.org/product/api#api-key

3. **Build and run:**
   ```bash
   docker compose up -d --build
   ```

4. **Open the UI** at `http://localhost:8844`

5. **Add journals** — start typing a name in the autocomplete box, or switch tabs for ISSN/DOI/bulk import

6. **Add feeds to your reader:**
   - Individual: `http://localhost:8844/feed/0028-0836`
   - RSS format: `http://localhost:8844/feed/0028-0836?format=rss`
   - Bulk: download OPML from `http://localhost:8844/opml`

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MAILTO` | `scholrss@example.com` | Email for polite API pool access (CrossRef/OpenAlex) |
| `OPENALEX_API_KEY` | _(empty)_ | Free API key from OpenAlex (required) |
| `SEMANTIC_SCHOLAR_API_KEY` | _(empty)_ | Optional API key for faster Semantic Scholar access |
| `BASE_URL` | `http://localhost:8844` | External URL for feed self-links |
| `INTERNAL_URL` | _(empty)_ | Optional internal URL (e.g. `http://scholrss:8844`) shown alongside `BASE_URL` for container-to-container readers that bypass reverse-proxy auth |
| `UPDATE_INTERVAL_HOURS` | `24` | Hours between automatic feed refreshes (used when no daily refresh time is set) |
| `LOOKBACK_DAYS` | `365` | Default lookback window (overridden by UI setting) |
| `MAX_ARTICLES` | `100` | Max articles fetched/cached per journal (1–1000; overridden by UI setting) |
| `BOOK_FETCH_EDITORS` | `1` | When set, book-chapter entries get a best-effort editor list from Crossref (chapter DOI and derived parent-book DOI). Disable to save API calls for chapter-heavy feeds. |
| `DATA_DIR` | `/data` | Where journals config and cache are stored |
| `JOURNALS_DB` | `${DATA_DIR}/journals.db` | Path to the journal autocomplete DB. Override only if you want to share a DB between containers. |
| `BOOK_PUBLISHERS_DB` | `${DATA_DIR}/bookpublishers.db` | Path to the book publisher autocomplete DB. Same caveat. |

## Data Storage

All runtime data lives in the bind-mounted `./data` directory:

- `journals.json` — tracked journals
- `settings.json` — UI-configurable settings (lookback days, etc.)
- `cache/` — cached article data per journal (JSON files)
- `journals.db` — journal autocomplete database (~58 MB)
- `bookpublishers.db` — book publisher autocomplete database (~10 MB)

### Database storage

The journal and book-publisher autocomplete databases live in `DATA_DIR` (i.e. your bind-mounted `/data` directory).

On first boot, if these files don't yet exist in `DATA_DIR`, ScholRSS copies the image-baked copies from `/app/journals/` into place. After that, every **Update journal DB** / **Update publisher DB** run writes back to `DATA_DIR`, so rebuilt databases **persist across `docker pull` and container recreation**.

To force a re-migration (for example, after the image ships a fresher baked database and you want to adopt it), stop the container, delete the DB file from your bind mount, and restart:

```bash
docker compose down
rm /path/to/your/data/bookpublishers.db
docker compose up -d
```

## MCP Server

ScholRSS includes an MCP (Model Context Protocol) server so LLMs can query your cached research.

**Tools available:**

| Tool | Description |
|---|---|
| `list_journals` | List tracked journals with article/abstract counts |
| `latest_articles(count, journal?)` | Most recent articles, optionally filtered by journal |
| `search_articles(query, count?)` | Keyword search across titles and abstracts |

**Setup for Claude Code / Claude Desktop:**

```json
{
  "mcpServers": {
    "scholrss": {
      "command": "python3",
      "args": ["/path/to/ScholRSS/mcp_server.py"],
      "env": {
        "SCHOLRSS_DATA_DIR": "/path/to/ScholRSS/data"
      }
    }
  }
}
```

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Web UI |
| `/feed/{issn}` | GET | Atom feed (`?format=rss` for RSS 2.0) |
| `/feed/{issn}/json` | GET | Raw JSON feed data |
| `/opml` | GET | OPML export of all feeds |
| `/api/books/autocomplete?q=` | GET | Publisher autocomplete (FTS5 book publisher database) |
| `/api/books/preview` | POST | Preview candidate works for a book feed config `{publishers, keywords, types, match}` |
| `/api/books/feed` | POST | Persist a book feed definition `{publishers, label, keywords, types, match}` |
| `/api/books/feed/{id}/reannotate` | POST | Clear one book-feed cache and re-fetch to apply latest title/publisher/editor annotations |
| `/api/books/reannotate-all` | POST | Clear all book-feed caches and re-fetch them in the background |
| `/api/autocomplete?q=` | GET | Local journal autocomplete (FTS5) |
| `/api/search/journal?q=` | GET | Online journal search via CrossRef |
| `/api/search/doi?doi=` | GET | Look up journal from a DOI |
| `/api/journal` | POST | Add a journal `{issn, title, publisher}` |
| `/api/journal/bulk` | POST | Bulk import `{issns: ["1234-5678", ...]}` |
| `/api/journal/{issn}` | DELETE | Remove a journal |
| `/api/journal/{issn}/filter` | PUT | Set/clear keyword+author filter `{keywords: [...], authors: [...], match: "any"\|"all"}` |
| `/api/journal/filtered` | POST | Create a new filtered feed variant `{issn, title, publisher, label, keywords, authors, match}` — lets you stack multiple filters on the same ISSN |
| `/api/refresh/{issn}` | POST | Refresh one journal |
| `/api/refresh-all` | POST | Refresh all journals |
| `/api/settings` | GET/PUT | Read/update settings (e.g. `{lookback_days: 365, refresh_hour_utc: 10, refresh_minute_utc: 30}`) |
| `/api/logs` | GET | Get log file tail (`?lines=N`, `?level=ERROR`) |
| `/api/logs` | DELETE | Clear log file |
| `/api/update-journal-db` | POST | Rebuild journal autocomplete database |

## Filtered feeds (for mega-journals / preprint servers)

Mega-journals and preprint servers like **SSRN Electronic Journal** (`1556-5068`) or **arXiv** (`2331-8422`) publish thousands of papers per week. Fetching them unfiltered would flood your reader and waste API calls. Instead, click the **⌕ Filter** button on the journal card and set:

- **Keywords** — comma-separated terms matched against title + abstract (OR by default, switch to AND if you need all)
- **Authors** — comma-separated name fragments matched against author display names (OR)

When a filter is set, ScholRSS switches that journal's fetch path from CrossRef to OpenAlex's `/works` endpoint with server-side filtering:

```
filter=primary_location.source.issn:1556-5068,
       from_publication_date:2025-04-01,
       title_and_abstract.search:privacy|regulation,
       authorships.author.display_name.search:jane+smith
```

Only matching works transit the wire — one request per refresh, no client-side culling. OpenAlex usually returns abstracts inline; anything still missing goes through the normal Semantic Scholar → OpenAlex enrichment fallback. Clearing all fields reverts the journal to the standard CrossRef pipeline.

Two additional options improve coverage for tricky sources:

- **OpenAlex source ID** — some sources (notably SSRN) don't map cleanly from ISSN to the correct OpenAlex source record. If results seem too few, supply the OpenAlex source ID directly (e.g. `S4210172589` for SSRN Electronic Journal — find it at `openalex.org/sources`). When set, the query uses `primary_location.source.id:` instead of `primary_location.source.issn:`.
- **Also search Semantic Scholar** — enables a parallel keyword search via the Semantic Scholar API, which crawls SSRN directly and catches papers that never get DOIs or CrossRef registration. Results are merged and deduplicated by DOI and title. Use the **S2 venue** field (e.g. `SSRN`) to restrict S2 results to a specific venue.

#### Recommended setup for SSRN

SSRN's ISSN (`1556-5068`) resolves to the wrong OpenAlex source by default. For reliable results:

1. Set **OpenAlex source ID** to `S4210172589`
2. Enable **Also search Semantic Scholar**
3. Set **S2 venue** to `SSRN`

This queries both OpenAlex (with the correct source ID) and Semantic Scholar (restricted to SSRN papers), giving the best coverage of SSRN's mix of DOI'd and non-DOI'd uploads.

### Multiple filtered feeds on one ISSN

To track several independent slices of the same mega-journal (e.g. one SSRN feed for "privacy" and another for "AI safety"), open the Add Journal panel and switch to the **Filtered feed** tab. Give each variant a label and its own keywords/authors — every submission creates a separate entry keyed by `<issn>__<slug>` with its own cache, feed URL (`/feed/1556-5068__privacy`, `/feed/1556-5068__ai_safety`, …), and OPML line. The original unfiltered entry keeps working unchanged.

## Book feeds

Books (and book chapters) can be tracked via the new **Books** tab. Define a set of publishers, optional keywords/exclusions, document how they match (ANY/ALL), and preview the candidate works that OpenAlex would return. Saving the configuration persists it in `book_feeds.json`, populates a dedicated cache file (`cache/book__{feed_id}.json`), and makes the stream available via `/feed/book/{feed_id}` (Atom/RSS/JSON) plus the OPML export. A background refresh thread keeps the cache up to date on the same schedule that journals use, and `/api/refresh/book/{feed_id}` lets you trigger an on-demand run. Publisher autocomplete is backed by `bookpublishers.db`, generated with `publisher_merge.py`.

### Bibliographic annotations

Book and book-chapter entries surface bibliographic context in both title and summary:

- Books show `(Publisher)` after the title, plus `Publisher: <name>` in the summary footer.
- Book chapters show `(Publisher, Parent Volume Title)` after the title, plus `In: <volume>` / `Editors: <names>` / `Publisher: <name>` in the summary footer.

Parent-volume title for chapters comes from OpenAlex `primary_location.raw_source_name`, which avoids showing ebook-platform labels like "Elsevier eBooks" as if they were book titles.

Editors come from Crossref's structured `editor` field (queried on the chapter DOI and then on conservative derived parent-book DOI candidates). Many publishers do not provide machine-readable editor metadata, so the editors line is omitted silently when unavailable. Set `BOOK_FETCH_EDITORS=0` to disable editor lookups entirely.

### Recent fixes

**Fixed**

- Book-chapter parent volume titles no longer use ebook-platform names (e.g. "Oxford University Press eBooks"). ScholRSS now prefers OpenAlex `primary_location.raw_source_name` and falls back cautiously.
- Book-chapter editor lookup now uses Crossref editor metadata instead of OpenAlex source-based heuristics, avoiding unrelated-editor matches on large ebook platforms.

**Added**

- `POST /api/books/feed/{id}/reannotate` and `POST /api/books/reannotate-all` to re-fetch cached book feeds and apply corrected annotations without deleting/recreating feeds.
- A UI button to run "Re-annotate all book feeds" from the main header controls.

## Abstract Enrichment Pipeline

For each journal refresh:

1. **CrossRef** — fetch up to 100 recent articles (primary source, freshest metadata)
2. **Semantic Scholar** — single batch POST for all DOIs missing abstracts (very efficient, up to 500 DOIs per request)
3. **OpenAlex** — individual DOI lookups for anything still missing (rate-limited at 150ms per call)

## Rate Limiting

ScholRSS is polite to upstream APIs:
- Semantic Scholar: 1 batch request per journal
- OpenAlex: 150ms between individual DOI lookups
- CrossRef: 500ms delay after fetching works
- 1s delay between journals during bulk refresh
- All requests include `mailto` / API keys for polite pool access

## Lookback and Publication Date Filtering

The **Lookback** setting (default 365 days) controls which articles are fetched and displayed. ScholRSS uses publication date rather than CrossRef's index date to determine relevance:

- CrossRef queries use `from-pub-date` (publication date) instead of `from-index-date` (when the record was added/modified in CrossRef)
- At ingestion time, works published before the lookback window are dropped regardless of source (CrossRef, OpenAlex, or Semantic Scholar)

This prevents decades-old papers that were recently back-indexed or assigned DOIs from flooding your feed.

## Testing

```bash
pip install pytest
pytest tests/ -v                       # all tests
pytest tests/ -v -m "not integration"  # unit tests only (fast, no API calls)
pytest tests/ -v -m integration        # integration tests (hits real APIs)
pytest tests/ -v -m "not integration"  # rerun after adding tests (covers book feed APIs)
```

## Cosmos Setup

ScholRSS works with Cosmos Server's reverse proxy. Either:

- **URL mode:** Add a route in Cosmos pointing to `scholrss:8844`
- **Labels mode:** Uncomment the `cosmos.hostname` / `cosmos.port` labels in `docker-compose.yml`

Set `BASE_URL` to match your external hostname.
