"""
Tests for env-configured CORS origins (app.main.create_app / ScannerConfig).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.scanner.config import config as scanner_config


@pytest.fixture(autouse=True)
def restore_cors_config():
    original = scanner_config.cors_allowed_origins
    yield
    scanner_config.cors_allowed_origins = original


def test_default_origins_are_not_wildcard():
    assert "*" not in scanner_config.cors_allowed_origins_list


def test_default_includes_common_dev_ports():
    origins = scanner_config.cors_allowed_origins_list
    assert "http://localhost:3000" in origins


def test_allowed_origin_gets_cors_header():
    from app.main import create_app
    app = create_app()
    client = TestClient(app)

    resp = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_disallowed_origin_does_not_get_cors_header():
    from app.main import create_app
    app = create_app()
    client = TestClient(app)

    resp = client.get("/health", headers={"Origin": "http://evil.example.com"})
    assert "access-control-allow-origin" not in resp.headers


def test_configured_origin_list_is_respected():
    scanner_config.cors_allowed_origins = "https://app.example.com"
    from app.main import create_app
    app = create_app()
    client = TestClient(app)

    resp = client.get("/health", headers={"Origin": "https://app.example.com"})
    assert resp.headers.get("access-control-allow-origin") == "https://app.example.com"

    resp2 = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert "access-control-allow-origin" not in resp2.headers


def test_parses_comma_separated_origins_with_whitespace():
    scanner_config.cors_allowed_origins = " https://a.com , https://b.com "
    assert scanner_config.cors_allowed_origins_list == ["https://a.com", "https://b.com"]
