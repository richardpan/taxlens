"""Privacy regression test: the live web bundle must not reference any
external hosts. TaxLens's value proposition is local-first; a CDN load
leaks IP + browser fingerprint on every page open even when no tax data
is sent. This test fails CI if someone adds back an external <script>,
<link>, or <img> reference.
"""
from pathlib import Path

from fastapi.testclient import TestClient


WEB = Path(__file__).resolve().parents[1] / "src" / "taxlens" / "web"
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")


def test_index_has_no_external_urls():
    """Catch http:// and https:// references in the served HTML."""
    for needle in ("http://", "https://"):
        assert needle not in INDEX_HTML, (
            f"Found '{needle}' in the served index.html — every external "
            "reference leaks the user's IP to the third party. Vendor the "
            "asset into src/taxlens/web/vendor/ instead."
        )


def test_vendored_assets_are_present():
    """The two vendored CDNs must exist on disk; otherwise the dashboard
    would silently render with no styling or charts."""
    assert (WEB / "vendor" / "tailwind.css").is_file()
    assert (WEB / "vendor" / "chart.umd.min.js").is_file()
    # Sanity-check file size (catches an empty/truncated download). Tailwind
    # is now a tree-shaken pre-built stylesheet (~17 KB) instead of the
    # 440 KB runtime tailwind.js, so the floor is much lower.
    assert (WEB / "vendor" / "tailwind.css").stat().st_size > 5_000
    assert (WEB / "vendor" / "chart.umd.min.js").stat().st_size > 50_000


def test_index_response_has_strict_csp():
    """The served HTML response must set a Content-Security-Policy that
    blocks outbound network calls (connect-src 'self')."""
    from taxlens.api import app
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "connect-src 'self'" in csp


def test_vendor_assets_served_at_static_paths():
    """The vendored assets must be reachable through the /static mount used
    by index.html's <script>/<link> tags."""
    from taxlens.api import app
    client = TestClient(app)
    # Tailwind CSS is small (~17 KB tree-shaken); Chart.js is large.
    cases = [
        ("/static/vendor/tailwind.css", 5_000),
        ("/static/vendor/chart.umd.min.js", 50_000),
    ]
    for path, min_bytes in cases:
        r = client.get(path)
        assert r.status_code == 200, f"{path} did not serve (status {r.status_code})"
        assert len(r.content) > min_bytes
