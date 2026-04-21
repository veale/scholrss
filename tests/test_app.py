"""
Test suite for ScholRSS.

Run with: pytest tests/ -v
Run integration tests (hits real APIs): pytest tests/ -v -m integration
Run unit tests only: pytest tests/ -v -m "not integration"
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Set test data dir before importing app
_test_dir = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _test_dir
os.environ["_SCHOLRSS_SCHEDULER_STARTED"] = "1"  # prevent scheduler from starting

import app as scholrss


@pytest.fixture
def client():
    """Flask test client with clean data directory."""
    scholrss.DATA_DIR = Path(_test_dir)
    scholrss.JOURNALS_FILE = scholrss.DATA_DIR / "journals.json"
    scholrss.CACHE_DIR = scholrss.DATA_DIR / "cache"
    scholrss.ensure_dirs()

    # Clean up between tests
    if scholrss.JOURNALS_FILE.exists():
        scholrss.JOURNALS_FILE.unlink()
    for f in scholrss.CACHE_DIR.iterdir():
        f.unlink()

    scholrss.app.config["TESTING"] = True
    with scholrss.app.test_client() as client:
        yield client


@pytest.fixture
def sample_cache(client):
    """Create a sample journal with cached data."""
    journals = {
        "0028-0836": {
            "title": "Nature",
            "publisher": "Springer",
            "added": "2026-01-01T00:00:00+00:00",
        }
    }
    scholrss.save_journals(journals)

    cache = {
        "issn": "0028-0836",
        "journal": journals["0028-0836"],
        "updated": "2026-04-10T12:00:00+00:00",
        "works": [
            {
                "doi": "10.1038/s41586-026-00001-1",
                "title": "Test Article One",
                "authors": ["Alice Smith", "Bob Jones"],
                "date": "2026-04-08T00:00:00+00:00",
                "abstract": "This is a test abstract.",
                "url": "https://doi.org/10.1038/s41586-026-00001-1",
                "source": "crossref+semanticscholar",
            },
            {
                "doi": "10.1038/s41586-026-00002-2",
                "title": "Test Article Two",
                "authors": ["Carol White"],
                "date": "2026-04-07T00:00:00+00:00",
                "abstract": "",
                "url": "https://doi.org/10.1038/s41586-026-00002-2",
                "source": "crossref",
            },
        ],
    }
    cache_path = scholrss.journal_cache_path("0028-0836")
    cache_path.write_text(json.dumps(cache, indent=2))
    return cache


# ── Unit Tests ─────────────────────────────────────────────────────────────


class TestHelpers:
    def test_journal_cache_path(self):
        path = scholrss.journal_cache_path("0028-0836")
        assert "00280836.json" in str(path)

    def test_load_save_journals(self, client):
        journals = scholrss.load_journals()
        assert journals == {}

        scholrss.save_journals({"1234-5678": {"title": "Test"}})
        journals = scholrss.load_journals()
        assert "1234-5678" in journals
        assert journals["1234-5678"]["title"] == "Test"

    def test_reconstruct_abstract(self):
        inv_index = {"Hello": [0], "world": [1], "this": [2], "is": [3], "a": [4], "test": [5]}
        result = scholrss.reconstruct_abstract(inv_index)
        assert result == "Hello world this is a test"

    def test_reconstruct_abstract_empty(self):
        assert scholrss.reconstruct_abstract(None) == ""
        assert scholrss.reconstruct_abstract({}) == ""

    def test_work_after_cutoff(self):
        """Test that _work_after_cutoff correctly filters works by date."""
        from datetime import datetime, timezone

        # Current time as cutoff
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=365)

        # Work within cutoff should be kept
        work_new = {"date": "2026-04-01T00:00:00+00:00"}
        assert scholrss._work_after_cutoff(work_new, cutoff) is True

        # Work outside cutoff should be dropped
        work_old = {"date": "1972-01-01T00:00:00+00:00"}
        assert scholrss._work_after_cutoff(work_old, cutoff) is False

        # Work exactly at cutoff should be kept
        work_at_cutoff = {"date": cutoff.isoformat()}
        assert scholrss._work_after_cutoff(work_at_cutoff, cutoff) is True

        # Work one day before cutoff should be dropped
        work_one_day_before = {"date": (cutoff - timedelta(days=1)).isoformat()}
        assert scholrss._work_after_cutoff(work_one_day_before, cutoff) is False

    def test_work_after_cutoff_parse_failure(self):
        """Test that parse failures keep the work (fail-open)."""
        now = datetime(2026, 4, 18, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=365)

        # Garbage date should be kept (fail-open)
        work_garbage = {"date": "garbage"}
        assert scholrss._work_after_cutoff(work_garbage, cutoff) is True

        # Missing date should be kept
        work_no_date = {"title": "Test"}
        assert scholrss._work_after_cutoff(work_no_date, cutoff) is True


class TestCrossRefQuery:
    def test_crossref_uses_pub_date(self):
        """Test that crossref_latest_works uses from-pub-date and sort=published."""
        with patch("app.requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.json.return_value = {"message": {"items": []}}
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            scholrss.crossref_latest_works("1234-5678", "2025-01-01", rows=50)

            # Check the request was made with correct params
            mock_get.assert_called_once()
            call_args = mock_get.call_args
            params = call_args.kwargs.get("params", {})

            assert "from-pub-date:2025-01-01" in params["filter"]
            assert params["sort"] == "published"
            assert params["order"] == "desc"


class TestUpdateJournalFeedClipping:
    def test_update_journal_feed_clips_old_works(self, client):
        """Test that update_journal_feed drops works older than lookback_days."""
        # Set up a journal
        journals = {
            "1234-5678": {
                "title": "Test Journal",
                "publisher": "Test Publisher",
                "issn": "1234-5678",
            }
        }
        scholrss.save_journals(journals)

        # Mock crossref_latest_works to return a mix of old and new works
        old_date = "1972-01-01T00:00:00+00:00"
        new_date = "2026-04-01T00:00:00+00:00"

        mock_works = [
            {"doi": "10.1234/old", "title": "Old Article", "authors": [],
             "date": old_date, "abstract": "", "url": "", "source": "crossref"},
            {"doi": "10.1234/new", "title": "New Article", "authors": [],
             "date": new_date, "abstract": "", "url": "", "source": "crossref"},
        ]

        with patch("app.crossref_latest_works", return_value=mock_works):
            with patch("app._enrich_missing_abstracts"):
                cache = scholrss.update_journal_feed("1234-5678", journals["1234-5678"])

        # Should only have the new work
        assert len(cache["works"]) == 1
        assert cache["works"][0]["doi"] == "10.1234/new"


class TestRefreshTime:
    def test_seconds_until_next_refresh_time(self):
        """Test _seconds_until_next_refresh_time returns values in valid range."""
        from datetime import datetime, timezone

        # Test with various times
        with patch("app.datetime") as mock_datetime:
            # When it's 10:00 UTC and we want 09:00 UTC, should be ~23 hours
            mock_datetime.now.return_value = datetime(2026, 4, 18, 10, 0, 0, tzinfo=timezone.utc)
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            # Request refresh at 09:00 -> should be ~23 hours
            seconds = scholrss._seconds_until_next_refresh_time(9, 0)
            assert 82800 <= seconds <= 86400  # 23 to 24 hours

            # Request refresh at 11:00 -> should be ~1 hour
            seconds = scholrss._seconds_until_next_refresh_time(11, 0)
            assert 3600 <= seconds <= 7200  # 1 to 2 hours

            # Request refresh at 10:00 -> should be ~24 hours (next day)
            seconds = scholrss._seconds_until_next_refresh_time(10, 0)
            assert 82800 <= seconds <= 86400  # 23 to 24 hours

    def test_seconds_until_next_refresh_time_edge_cases(self):
        """Test edge cases for refresh time calculation."""
        from datetime import datetime, timezone

        with patch("app.datetime") as mock_datetime:
            # Test minute handling
            mock_datetime.now.return_value = datetime(2026, 4, 18, 10, 0, 0, tzinfo=timezone.utc)
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            # Request at 10:30 when it's 10:00 -> should be 30 minutes
            seconds = scholrss._seconds_until_next_refresh_time(10, 30)
            assert 1500 <= seconds <= 3600  # 30 min to 1 hour


class TestSettingsAPI:
    def test_get_settings_includes_refresh_time(self, client):
        """Test that GET /api/settings returns refresh time fields."""
        rv = client.get("/api/settings")
        assert rv.status_code == 200
        data = rv.get_json()
        assert "refresh_hour_utc" in data
        assert "refresh_minute_utc" in data

    def test_put_settings_refresh_hour_validation(self, client):
        """Test validation of refresh_hour_utc."""
        # Invalid hour
        rv = client.put("/api/settings", json={"refresh_hour_utc": 25})
        assert rv.status_code == 400

        # Valid hour
        rv = client.put("/api/settings", json={"refresh_hour_utc": 10, "refresh_minute_utc": 30})
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True

        # Clear the setting
        rv = client.put("/api/settings", json={"refresh_hour_utc": None})
        assert rv.status_code == 200

    def test_put_settings_refresh_minute_validation(self, client):
        """Test validation of refresh_minute_utc."""
        # Invalid minute
        rv = client.put("/api/settings", json={"refresh_hour_utc": 10, "refresh_minute_utc": 60})
        assert rv.status_code == 400

        # Valid minute
        rv = client.put("/api/settings", json={"refresh_hour_utc": 10, "refresh_minute_utc": 45})
        assert rv.status_code == 200


class TestLogsAPI:
    def test_get_logs_no_file(self, client):
        """Test GET /api/logs returns empty when no log file exists."""
        # The test uses a temp directory, so no log file
        rv = client.get("/api/logs")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["lines"] == []
        assert data["size_bytes"] == 0

    def test_get_logs_with_content(self, client):
        """Test GET /api/logs returns log content."""
        # Write some log content
        scholrss.LOG_FILE.write_text("2026-04-18 [INFO] Test log line 1\n2026-04-18 [ERROR] Test error\n")

        rv = client.get("/api/logs")
        assert rv.status_code == 200
        data = rv.get_json()
        assert len(data["lines"]) == 2
        assert "Test log line 1" in data["lines"][0]

    def test_get_logs_filter_by_level(self, client):
        """Test GET /api/logs?level=ERROR filters to error lines."""
        scholrss.LOG_FILE.write_text("2026-04-18 [INFO] Info message\n2026-04-18 [ERROR] Error message\n2026-04-18 [WARNING] Warning message\n")

        rv = client.get("/api/logs?level=ERROR")
        assert rv.status_code == 200
        data = rv.get_json()
        assert len(data["lines"]) == 1
        assert "Error message" in data["lines"][0]

    def test_get_logs_lines_limit(self, client):
        """Test GET /api/logs?lines=N limits the number of lines."""
        # Write 10 lines
        lines = [f"2026-04-18 [INFO] Line {i}\n" for i in range(10)]
        scholrss.LOG_FILE.write_text("".join(lines))

        rv = client.get("/api/logs?lines=5")
        assert rv.status_code == 200
        data = rv.get_json()
        assert len(data["lines"]) == 5

    def test_delete_logs(self, client):
        """Test DELETE /api/logs truncates the file."""
        scholrss.LOG_FILE.write_text("test\n")

        rv = client.delete("/api/logs")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True

        # File should be empty
        assert scholrss.LOG_FILE.read_text() == ""

    def test_rotating_handler_attached(self):
        """Test that the rotating file handler is attached to the logger."""
        handlers = logging.getLogger().handlers
        # Should have at least the file handler (plus basicConfig's stream handler)
        assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers)


class TestRoutes:
    def test_homepage_empty(self, client):
        rv = client.get("/")
        assert rv.status_code == 200
        assert b"ScholRSS" in rv.data
        assert b"No journals tracked yet" in rv.data

    def test_homepage_with_journals(self, client, sample_cache):
        rv = client.get("/")
        assert rv.status_code == 200
        assert b"Nature" in rv.data
        assert b"0028-0836" in rv.data

    def test_feed_not_found(self, client):
        rv = client.get("/feed/9999-9999")
        assert rv.status_code == 404

    def test_feed_atom(self, client, sample_cache):
        rv = client.get("/feed/0028-0836")
        assert rv.status_code == 200
        assert "application/atom+xml" in rv.content_type
        assert b"Nature" in rv.data
        assert b"Test Article One" in rv.data

    def test_feed_rss(self, client, sample_cache):
        rv = client.get("/feed/0028-0836?format=rss")
        assert rv.status_code == 200
        assert "application/rss+xml" in rv.content_type
        assert b"Nature" in rv.data

    def test_feed_json(self, client, sample_cache):
        rv = client.get("/feed/0028-0836/json")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["issn"] == "0028-0836"
        assert len(data["works"]) == 2
        assert data["works"][0]["title"] == "Test Article One"

    def test_feed_json_not_found(self, client):
        rv = client.get("/feed/9999-9999/json")
        assert rv.status_code == 404

    def test_opml_empty(self, client):
        rv = client.get("/opml")
        assert rv.status_code == 200
        assert b"opml" in rv.data

    def test_opml_with_journals(self, client, sample_cache):
        rv = client.get("/opml")
        assert rv.status_code == 200
        assert b"Nature" in rv.data
        assert b"0028-0836" in rv.data


class TestAutocomplete:
    def test_autocomplete_too_short(self, client):
        rv = client.get("/api/autocomplete?q=a")
        assert rv.status_code == 200
        assert rv.get_json() == []

    def test_autocomplete_empty(self, client):
        rv = client.get("/api/autocomplete?q=")
        assert rv.status_code == 200
        assert rv.get_json() == []

    def test_autocomplete_no_db(self, client):
        """Returns empty when DB doesn't exist (graceful fallback)."""
        import app as scholrss
        orig = scholrss.JOURNALS_DB
        scholrss.JOURNALS_DB = Path("/nonexistent/journals.db")
        rv = client.get("/api/autocomplete?q=nature")
        assert rv.status_code == 200
        assert rv.get_json() == []
        scholrss.JOURNALS_DB = orig


class TestAPIRoutes:
    def test_search_empty_query(self, client):
        rv = client.get("/api/search/journal?q=")
        assert rv.status_code == 200
        assert rv.get_json() == []

    def test_doi_search_no_doi(self, client):
        rv = client.get("/api/search/doi?doi=")
        assert rv.status_code == 400

    def test_add_journal_no_issn(self, client):
        rv = client.post("/api/journal",
                         json={"issn": "", "title": "Test"})
        assert rv.status_code == 400

    def test_add_journal(self, client):
        with patch.object(scholrss.threading.Thread, "start"):
            rv = client.post("/api/journal",
                             json={"issn": "1234-5678", "title": "Test Journal", "publisher": "Test"})
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["issn"] == "1234-5678"

        # Verify saved
        journals = scholrss.load_journals()
        assert "1234-5678" in journals
        assert journals["1234-5678"]["title"] == "Test Journal"

    def test_delete_journal(self, client, sample_cache):
        rv = client.delete("/api/journal/0028-0836")
        assert rv.status_code == 200
        assert rv.get_json()["ok"] is True

        # Verify removed
        journals = scholrss.load_journals()
        assert "0028-0836" not in journals
        assert not scholrss.journal_cache_path("0028-0836").exists()

    def test_delete_nonexistent_journal(self, client):
        rv = client.delete("/api/journal/9999-9999")
        assert rv.status_code == 200  # idempotent

    def test_refresh_not_found(self, client):
        rv = client.post("/api/refresh/9999-9999")
        assert rv.status_code == 404

    def test_refresh_journal(self, client, sample_cache):
        with patch.object(scholrss.threading.Thread, "start"):
            rv = client.post("/api/refresh/0028-0836")
        assert rv.status_code == 200
        assert rv.get_json()["ok"] is True

    def test_refresh_all(self, client):
        with patch.object(scholrss.threading.Thread, "start"):
            rv = client.post("/api/refresh-all")
        assert rv.status_code == 200
        assert rv.get_json()["ok"] is True

    def test_bulk_import_empty(self, client):
        rv = client.post("/api/journal/bulk", json={"issns": []})
        assert rv.status_code == 400

    def test_bulk_import(self, client):
        with patch.object(scholrss.threading.Thread, "start"):
            rv = client.post("/api/journal/bulk",
                             json={"issns": ["0028-0836", "1932-6203"]})
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert "2" in data["message"]


class TestFeedGeneration:
    def test_feed_has_required_fields(self, client, sample_cache):
        fg = scholrss.generate_feed("0028-0836")
        assert fg is not None
        xml = fg.atom_str(pretty=True).decode()
        assert "Test Article One" in xml
        assert "Test Article Two" in xml
        assert "Alice Smith" in xml
        assert "This is a test abstract" in xml
        assert "10.1038/s41586-026-00001-1" in xml

    def test_feed_caps_at_50_entries(self, client):
        """Feed should cap at 50 entries even if cache has more."""
        journals = {"0000-0000": {"title": "Big Journal", "publisher": "Test"}}
        scholrss.save_journals(journals)

        works = []
        for i in range(75):
            works.append({
                "doi": f"10.1234/test-{i:04d}",
                "title": f"Article {i}",
                "authors": ["Author"],
                "date": "2026-04-01T00:00:00+00:00",
                "abstract": f"Abstract {i}",
                "url": f"https://doi.org/10.1234/test-{i:04d}",
                "source": "crossref",
            })

        cache = {
            "issn": "0000-0000",
            "journal": journals["0000-0000"],
            "updated": "2026-04-10T12:00:00+00:00",
            "works": works,
        }
        scholrss.journal_cache_path("0000-0000").write_text(json.dumps(cache))

        fg = scholrss.generate_feed("0000-0000")
        xml = fg.atom_str(pretty=True).decode()
        # Should have 50 entries, not 75
        assert xml.count("<entry>") == 50

    def test_feed_returns_none_for_missing_cache(self):
        assert scholrss.generate_feed("9999-9999") is None


class TestBookFeeds:
    def test_books_preview_accepts_keywords_only(self, client):
        with patch("app.openalex_book_works", return_value=[]):
            rv = client.post("/api/books/preview", json={"keywords": ["ai"]})
        assert rv.status_code == 200

    def test_books_preview_rejects_empty(self, client):
        rv = client.post("/api/books/preview", json={})
        assert rv.status_code == 400

    def test_books_preview_success(self, client):
        mock_works = [{"doi": "10.1234/book", "title": "Book One", "authors": ["Author"],
                       "date": "2026-04-15T00:00:00+00:00", "abstract": "Test", "url": "https://doi.org/10.1234/book",
                       "source": "openalex"}]
        with patch("app.openalex_book_works", return_value=mock_works) as mock_query:
            payload = {
                "publishers": [{"id": "P123", "name": "Example Publisher"}],
                "keywords": ["ai"],
            }
            rv = client.post("/api/books/preview", json=payload)
        assert rv.status_code == 200
        assert rv.get_json()["works"] == mock_works
        mock_query.assert_called_once()

    def test_books_feed_creation(self, client):
        payload = {
            "label": "AI Books",
            "publishers": [{"id": "P123", "name": "Example Publisher"}],
            "keywords": ["ai"]
        }
        rv = client.post("/api/books/feed", json=payload)
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        feeds = scholrss.load_book_feeds()
        assert data["feed_id"] in feeds
        assert feeds[data["feed_id"]]["label"] == "AI Books"

    def test_refresh_book_feed(self, client):
        feed_id = "ai_books"
        scholrss.save_book_feeds({
            feed_id: {
                "publishers": [{"id": "P123", "name": "Example Publisher"}],
                "label": "AI Books",
                "keywords": ["ai"],
            }
        })
        with patch.object(scholrss.threading.Thread, "start"):
            rv = client.post(f"/api/refresh/book/{feed_id}")
        assert rv.status_code == 200
        assert rv.get_json()["feed_id"] == feed_id

    def test_refresh_book_feed_missing(self, client):
        rv = client.post("/api/refresh/book/does_not_exist")
        assert rv.status_code == 404

    def test_delete_book_feed(self, client):
        feed_id = "delete_books"
        scholrss.save_book_feeds({
            feed_id: {
                "publishers": [{"id": "P123", "name": "Example Publisher"}],
                "label": "Delete Books",
                "keywords": ["ai"],
            }
        })
        cache = {
            "publishers": [{"id": "P123", "name": "Example Publisher"}],
            "works": [{
                "doi": "10.1234/book",
                "title": "Book Title",
                "authors": ["Author"],
                "date": "2026-04-15T00:00:00+00:00",
                "abstract": "Test abstract",
                "url": "https://doi.org/10.1234/book",
                "source": "openalex",
            }],
        }
        scholrss.book_feed_cache_path(feed_id).write_text(json.dumps(cache))
        rv = client.delete(f"/api/books/feed/{feed_id}")
        assert rv.status_code == 200
        assert not scholrss.book_feed_cache_path(feed_id).exists()
        assert feed_id not in scholrss.load_book_feeds()

    def test_opml_includes_book_feed(self, client):
        feed_id = "opml_books"
        scholrss.save_book_feeds({
            feed_id: {
                "publishers": [{"id": "P123", "name": "OPML Publisher"}],
                "label": "OPML Books",
                "keywords": ["ai"],
            }
        })
        rv = client.get("/opml")
        assert rv.status_code == 200
        assert b"OPML Books" in rv.data
        assert feed_id.encode() in rv.data

    def test_generate_book_feed(self, client):
        feed_id = "generated_books"
        cache = {
            "label": "Generated Books",
            "publishers": [{"id": "P123", "name": "Example Publisher"}],
            "keywords": ["ai"],
            "works": [{
                "doi": "10.1234/book",
                "title": "Beautiful Book",
                "authors": ["Author Name"],
                "date": "2026-04-15T00:00:00+00:00",
                "abstract": "Test abstract",
                "url": "https://doi.org/10.1234/book",
                "source": "openalex",
            }],
            "updated": "2026-04-20T00:00:00+00:00",
        }
        scholrss.book_feed_cache_path(feed_id).write_text(json.dumps(cache))
        fg = scholrss.generate_book_feed(feed_id)
        assert fg is not None
        xml = fg.atom_str(pretty=True).decode()
        assert "Generated Books" in xml
        assert "Beautiful Book" in xml

    def test_book_query_builds_filter_with_publisher_lineage(self):
        with patch("app.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"results": []}
            scholrss.openalex_book_works({
                "publisher_ids": ["P123", "P456"],
                "types": ["book", "book-chapter"],
                "keywords": ["ethics"],
                "keywords_match": "any",
                "from_date": "2025-01-01",
            }, limit=10)
            called_filter = mock_get.call_args.kwargs["params"]["filter"]
            assert "primary_location.source.publisher_lineage:P123|P456" in called_filter
            assert "type:book|book-chapter" in called_filter
            assert "title_and_abstract.search:ethics" in called_filter
            assert "from_publication_date:2025-01-01" in called_filter

    def test_book_query_all_match_produces_separate_clauses(self):
        with patch("app.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"results": []}
            scholrss.openalex_book_works({
                "publisher_ids": ["P123"],
                "keywords": ["ethics", "fairness"],
                "keywords_match": "all",
            }, limit=10)
            called_filter = mock_get.call_args.kwargs["params"]["filter"]
            assert "title_and_abstract.search:ethics" in called_filter
            assert "title_and_abstract.search:fairness" in called_filter
            assert "ethics|fairness" not in called_filter

    def test_book_feed_id_slugifies_label(self, client):
        import re
        payload = {
            "label": "AI & Ethics: A Reader",
            "publishers": [{"id": "P123", "name": "Routledge"}],
            "keywords": ["ai"],
        }
        rv = client.post("/api/books/feed", json=payload)
        assert rv.status_code == 200
        feed_id = rv.get_json()["feed_id"]
        assert " " not in feed_id
        assert re.match(r"^[a-z0-9_]+$", feed_id)

    def test_book_feed_slug_collision(self, client):
        payload = {
            "label": "History Books",
            "publishers": [{"id": "P123", "name": "Routledge"}],
        }
        r1 = client.post("/api/books/feed", json=payload)
        r2 = client.post("/api/books/feed", json=payload)
        assert r1.get_json()["feed_id"] != r2.get_json()["feed_id"]
        assert r2.get_json()["feed_id"].endswith("_2")

    def test_publisher_name_preserved_on_save(self, client):
        payload = {
            "label": "Test",
            "publishers": [{"id": "P4310319965", "name": "Routledge"}],
        }
        rv = client.post("/api/books/feed", json=payload)
        feed_id = rv.get_json()["feed_id"]
        saved = scholrss.load_book_feeds()[feed_id]
        assert saved["publishers"][0]["name"] == "Routledge"

    def test_keywords_match_all_reaches_filter(self, client):
        payload = {
            "label": "AND test",
            "publishers": [{"id": "P123", "name": "Test Pub"}],
            "keywords": ["a", "b"],
            "keywords_match": "all",
        }
        rv = client.post("/api/books/feed", json=payload)
        feed_id = rv.get_json()["feed_id"]
        feeds = scholrss.load_book_feeds()
        with patch("app.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"results": []}
            scholrss.update_book_feed(feed_id, feeds[feed_id])
        called_filter = mock_get.call_args.kwargs["params"]["filter"]
        assert called_filter.count("title_and_abstract.search:") == 2

    @pytest.mark.integration
    def test_book_query_live_openalex_publisher_lineage(self):
        works = scholrss.openalex_book_works({
            "publisher_ids": ["P4310320990"],
            "types": ["book"],
            "from_date": "2024-01-01",
        }, limit=5)
        assert len(works) > 0
        assert all(w.get("title") for w in works)


class TestDbMigration:
    def test_migration_copies_bundled_when_data_dir_empty(self, tmp_path, monkeypatch):
        bundled = tmp_path / "bundled.db"
        bundled.write_bytes(b"fake-sqlite-content")
        target = tmp_path / "data" / "bundled.db"
        monkeypatch.setattr(scholrss, "_BUNDLED_PUBLISHERS_DB", bundled)
        monkeypatch.setattr(scholrss, "BOOK_PUBLISHERS_DB", target)
        monkeypatch.setattr(scholrss, "_BUNDLED_JOURNALS_DB", tmp_path / "nope.db")
        monkeypatch.setattr(scholrss, "JOURNALS_DB", tmp_path / "nope_target.db")
        monkeypatch.setattr(scholrss, "DATA_DIR", tmp_path)

        scholrss._migrate_dbs_to_data_dir()

        assert target.exists()
        assert target.read_bytes() == b"fake-sqlite-content"

    def test_migration_never_overwrites_existing(self, tmp_path, monkeypatch):
        bundled = tmp_path / "bundled.db"
        bundled.write_bytes(b"new-version")
        target = tmp_path / "data" / "bundled.db"
        target.parent.mkdir()
        target.write_bytes(b"user-version")
        monkeypatch.setattr(scholrss, "_BUNDLED_PUBLISHERS_DB", bundled)
        monkeypatch.setattr(scholrss, "BOOK_PUBLISHERS_DB", target)
        monkeypatch.setattr(scholrss, "_BUNDLED_JOURNALS_DB", tmp_path / "nope.db")
        monkeypatch.setattr(scholrss, "JOURNALS_DB", tmp_path / "nope_target.db")
        monkeypatch.setattr(scholrss, "DATA_DIR", tmp_path)

        scholrss._migrate_dbs_to_data_dir()

        assert target.read_bytes() == b"user-version"


class TestSemanticScholar:
    def test_batch_abstracts_empty(self):
        result = scholrss.semantic_scholar_batch_abstracts([])
        assert result == {}

    def test_batch_abstracts_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "abstract": "Test abstract text",
                "externalIds": {"DOI": "10.1234/test-001"},
            },
            None,  # some papers may not be found
            {
                "abstract": None,
                "externalIds": {"DOI": "10.1234/test-002"},
            },
        ]
        with patch("app.requests.post", return_value=mock_response):
            result = scholrss.semantic_scholar_batch_abstracts(
                ["10.1234/test-001", "10.1234/test-002"]
            )
        assert "10.1234/test-001" in result
        assert result["10.1234/test-001"] == "Test abstract text"
        assert "10.1234/test-002" not in result  # no abstract

    def test_batch_abstracts_rate_limited(self):
        mock_response = MagicMock()
        mock_response.status_code = 429
        with patch("app.requests.post", return_value=mock_response):
            result = scholrss.semantic_scholar_batch_abstracts(["10.1234/test"])
        assert result == {}


class TestOpenAlexEnrich:
    def test_enrich_abstract_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "abstract_inverted_index": {"Hello": [0], "world": [1]}
        }
        with patch("app.requests.get", return_value=mock_response):
            result = scholrss.openalex_enrich_abstract("10.1234/test")
        assert result == "Hello world"

    def test_enrich_abstract_not_found(self):
        mock_response = MagicMock()
        mock_response.status_code = 404
        with patch("app.requests.get", return_value=mock_response):
            result = scholrss.openalex_enrich_abstract("10.1234/nonexistent")
        assert result == ""

    def test_enrich_abstract_no_abstract(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"abstract_inverted_index": None}
        with patch("app.requests.get", return_value=mock_response):
            result = scholrss.openalex_enrich_abstract("10.1234/test")
        assert result == ""


# ── Integration Tests (hit real APIs) ──────────────────────────────────────


@pytest.mark.integration
class TestCrossRefIntegration:
    def test_search_journal(self):
        results = scholrss.crossref_search_journal("nature")
        assert len(results) > 0
        assert any("issn" in r for r in results)

    def test_journal_from_doi(self):
        result = scholrss.crossref_journal_from_doi("10.1038/s41586-024-07386-0")
        assert result is not None
        assert result["title"]
        assert len(result["issn"]) > 0

    def test_latest_works(self):
        works = scholrss.crossref_latest_works("0028-0836", "2026-03-01")
        assert len(works) > 0
        work = works[0]
        assert "doi" in work
        assert "title" in work
        assert "authors" in work
        assert "date" in work
        assert "url" in work
        assert "source" in work
        assert work["source"] == "crossref"


@pytest.mark.integration
class TestSemanticScholarIntegration:
    def test_batch_abstracts(self):
        # Use well-known DOIs that should have abstracts
        dois = [
            "10.1038/s41586-024-07386-0",
            "10.1371/journal.pone.0000000",  # likely not found
        ]
        result = scholrss.semantic_scholar_batch_abstracts(dois)
        # At least one should come back (the Nature one)
        assert isinstance(result, dict)


@pytest.mark.integration
class TestOpenAlexIntegration:
    def test_enrich_abstract(self):
        # PLOS ONE articles typically have abstracts in OpenAlex
        result = scholrss.openalex_enrich_abstract("10.1371/journal.pone.0309800")
        # May or may not have abstract, but should not error
        assert isinstance(result, str)


@pytest.mark.integration
class TestEndToEndIntegration:
    def test_full_flow(self, client):
        """Test the complete add -> refresh -> feed flow."""
        # Search for a journal
        rv = client.get("/api/search/journal?q=PLOS+ONE")
        assert rv.status_code == 200
        results = rv.get_json()
        assert len(results) > 0

        # Add journal
        rv = client.post("/api/journal",
                         json={"issn": "1932-6203", "title": "PLOS ONE", "publisher": "PLOS"})
        assert rv.status_code == 200
        assert rv.get_json()["ok"]

        # Wait briefly for background thread, then manually update
        import time
        time.sleep(1)

        # Manually update (synchronous)
        journals = scholrss.load_journals()
        scholrss.update_journal_feed("1932-6203", journals["1932-6203"])

        # Check feed
        rv = client.get("/feed/1932-6203")
        assert rv.status_code == 200
        assert b"PLOS ONE" in rv.data

        # Check JSON
        rv = client.get("/feed/1932-6203/json")
        assert rv.status_code == 200
        data = rv.get_json()
        assert len(data["works"]) > 0
        assert data["journal"]["title"] == "PLOS ONE"

        # Check some works have abstracts
        with_abstract = sum(1 for w in data["works"] if w.get("abstract"))
        assert with_abstract > 0, "Expected at least some abstracts"

        # Check OPML includes the journal
        rv = client.get("/opml")
        assert rv.status_code == 200
        assert b"1932-6203" in rv.data

        # Delete journal
        rv = client.delete("/api/journal/1932-6203")
        assert rv.status_code == 200

        # Feed should be gone
        rv = client.get("/feed/1932-6203")
        assert rv.status_code == 404
