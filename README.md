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
| `UPDATE_INTERVAL_HOURS` | `24` | Hours between automatic feed refreshes |
| `LOOKBACK_DAYS` | `365` | Default lookback window (overridden by UI setting) |
| `MAX_ARTICLES` | `100` | Max articles fetched/cached per journal (1–1000; overridden by UI setting) |
| `DATA_DIR` | `/data` | Where journals config and cache are stored |

## Data Storage

All runtime data lives in the bind-mounted `./data` directory:

- `journals.json` — tracked journals
- `settings.json` — UI-configurable settings (lookback days, etc.)
- `cache/` — cached article data per journal (JSON files)

The journal autocomplete database (`journals/journals.db`, ~58MB) is baked into the Docker image. Use the **Update DB** button in the UI to rebuild it from online sources.

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
| `/api/settings` | GET/PUT | Read/update settings (e.g. `{lookback_days: 365}`) |
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
       authorships.author.display_name.search:lilian+edwards
```

Only matching works transit the wire — one request per refresh, no client-side culling. OpenAlex usually returns abstracts inline; anything still missing goes through the normal Semantic Scholar → OpenAlex enrichment fallback. Clearing all fields reverts the journal to the standard CrossRef pipeline.

### Multiple filtered feeds on one ISSN

To track several independent slices of the same mega-journal (e.g. one SSRN feed for "privacy" and another for "AI safety"), open the Add Journal panel and switch to the **Filtered feed** tab. Give each variant a label and its own keywords/authors — every submission creates a separate entry keyed by `<issn>__<slug>` with its own cache, feed URL (`/feed/1556-5068__privacy`, `/feed/1556-5068__ai_safety`, …), and OPML line. The original unfiltered entry keeps working unchanged.

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

## Testing

```bash
pip install pytest
pytest tests/ -v                       # all tests
pytest tests/ -v -m "not integration"  # unit tests only (fast, no API calls)
pytest tests/ -v -m integration        # integration tests (hits real APIs)
```

## Cosmos Setup

ScholRSS works with Cosmos Server's reverse proxy. Either:

- **URL mode:** Add a route in Cosmos pointing to `scholrss:8844`
- **Labels mode:** Uncomment the `cosmos.hostname` / `cosmos.port` labels in `docker-compose.yml`

Set `BASE_URL` to match your external hostname.
