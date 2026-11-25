"""End-to-end check: crawl the deliberately broken fixture site and assert the audits catch the planted problems.
Run from the project folder:  python -m pytest tests -q     (or: python -m tests.test_suite)"""
import asyncio
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SEO_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from app import analysis as an, pipeline, reports, store  # noqa: E402
from tests.fixture_server import H  # noqa: E402


def _serve(port):
    H.port = port
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_full_pipeline():
    srv = _serve(8791)
    try:
        result, site, issues = asyncio.run(pipeline.run_full(
            "http://127.0.0.1:8791/", {"max_pages": 60, "crawl_sitemap": True}, label="test"))
    finally:
        srv.shutdown()
    ids = {i.id for i in issues}
    expected = {"broken_links", "redirect_chain", "redirect_loop", "dup_content", "dup_title", "hreflang_no_return", "hreflang_bad_code",
                "sd_errors", "orphan", "noindex", "sitemap_noindex", "sitemap_redirect", "a11y_input_no_label", "blocked_robots",
                "broken_resources", "url_uppercase_characters_in_path", "spelling", "canonicalised"}
    assert expected <= ids, f"missing: {expected - ids}"
    loaded = store.load_crawl(result["crawl_id"])
    assert loaded and len(loaded[0].pages) == len(site.pages)
    for name in reports.REPORTS:
        cols, rows = reports.get(loaded[0], name)
        assert all(len(r) == len(cols) for r in rows), name
    assert len(an.generate_sitemaps(site)) == 1
    assert store.compare_crawls(result["crawl_id"], result["crawl_id"])["summary"]["changed_urls"] == 0


if __name__ == "__main__":
    test_full_pipeline()
    print("ok")
