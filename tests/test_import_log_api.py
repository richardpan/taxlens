"""Test the import-log API endpoints (listing + fetching)."""
from pathlib import Path

from fastapi.testclient import TestClient


def test_list_endpoint_returns_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("TAXLENS_LOGS_DIR", str(tmp_path))
    # Drop a couple of synthetic log files
    (tmp_path / "import-20260101-120000-foo.log").write_text("test1", encoding="utf-8")
    (tmp_path / "import-20260102-120000-bar.log").write_text("test2", encoding="utf-8")
    # Also drop a non-log file that must NOT be listed
    (tmp_path / "random.txt").write_text("nope", encoding="utf-8")

    # Reload the api module so it picks up the env override at startup.
    import importlib
    import taxlens.api as api_mod
    importlib.reload(api_mod)

    client = TestClient(api_mod.app)
    r = client.get("/api/import-logs")
    assert r.status_code == 200
    names = [item["name"] for item in r.json()]
    assert "import-20260101-120000-foo.log" in names
    assert "import-20260102-120000-bar.log" in names
    assert "random.txt" not in names
    # Sorted descending by name (newest-first)
    assert names.index("import-20260102-120000-bar.log") < names.index("import-20260101-120000-foo.log")


def test_get_endpoint_returns_log_body(tmp_path, monkeypatch):
    monkeypatch.setenv("TAXLENS_LOGS_DIR", str(tmp_path))
    (tmp_path / "import-20260101-120000-foo.log").write_text("hello world", encoding="utf-8")

    import importlib
    import taxlens.api as api_mod
    importlib.reload(api_mod)
    client = TestClient(api_mod.app)

    r = client.get("/api/import-logs/import-20260101-120000-foo.log")
    assert r.status_code == 200
    assert r.text == "hello world"
    assert "text/plain" in r.headers.get("content-type", "")


def test_get_endpoint_rejects_path_traversal(tmp_path, monkeypatch):
    """Hardening: filename query must not escape the logs directory."""
    monkeypatch.setenv("TAXLENS_LOGS_DIR", str(tmp_path))
    import importlib
    import taxlens.api as api_mod
    importlib.reload(api_mod)
    client = TestClient(api_mod.app)

    # All of these must be rejected with 4xx, NOT serve arbitrary host files.
    for bad in [
        "import-..%2F..%2Fetc%2Fpasswd",  # url-encoded ../
        "import-_..%5C..%5Cwindows%5Cwin.ini",  # url-encoded ..\
    ]:
        r = client.get(f"/api/import-logs/{bad}")
        # Either rejected by our guard (400) or simply not found (404) — both safe.
        assert r.status_code in (400, 404), f"{bad} -> {r.status_code}"

    # Bare prefix-stripping attempt — name doesn't start with "import-"
    r = client.get("/api/import-logs/etc_passwd")
    assert r.status_code == 400


def test_get_endpoint_returns_404_for_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TAXLENS_LOGS_DIR", str(tmp_path))
    import importlib
    import taxlens.api as api_mod
    importlib.reload(api_mod)
    client = TestClient(api_mod.app)
    r = client.get("/api/import-logs/import-99999999-000000-nope.log")
    assert r.status_code == 404
