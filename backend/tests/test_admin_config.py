from app.config import settings
from app.errors import (
    AdminBadPassword,
    AdminNotConfigured,
    AdminUnauthorized,
    GraphSelftestFailed,
)


def test_admin_error_codes_and_statuses():
    assert AdminNotConfigured.code == "ADMIN_NOT_CONFIGURED"
    assert AdminNotConfigured.http_status == 503
    assert AdminUnauthorized.code == "ADMIN_UNAUTHORIZED"
    assert AdminUnauthorized.http_status == 401
    assert AdminBadPassword.code == "ADMIN_BAD_PASSWORD"
    assert AdminBadPassword.http_status == 401
    assert GraphSelftestFailed.code == "GRAPH_SELFTEST_FAILED"
    assert GraphSelftestFailed.http_status == 422


def test_admin_settings_defaults():
    assert settings.admin_password_hash is None
    assert settings.admin_cookie_secure is False
    assert settings.admin_session_days == 3


def test_admin_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("PPTX2PDF_ADMIN_PASSWORD_HASH", "scrypt$aa$bb")
    monkeypatch.setenv("PPTX2PDF_ADMIN_COOKIE_SECURE", "true")
    monkeypatch.setenv("PPTX2PDF_ADMIN_SESSION_DAYS", "7")
    from app.config import Settings

    fresh = Settings()
    assert fresh.admin_password_hash == "scrypt$aa$bb"
    assert fresh.admin_cookie_secure is True
    assert fresh.admin_session_days == 7
