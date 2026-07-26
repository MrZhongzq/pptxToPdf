import pytest

from app.config import settings
from app.errors import GraphNotConfigured
from app.services.graph_credentials import (
    GraphCredentialData,
    load_credentials,
    save_credentials,
)

KEY = "8I3F3CqPwlEsmMDLbEIVSXd8oXlmqkOMWFnDPbNXKvA="


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", KEY)


def test_roundtrip(session, with_key):
    save_credentials(
        session,
        tenant_id="tid",
        client_id="cid",
        client_secret="s3cr3t",
        site_id="sid",
        drive_path="staging",
    )
    got = load_credentials(session)
    assert got == GraphCredentialData(
        tenant_id="tid",
        client_id="cid",
        client_secret="s3cr3t",
        site_id="sid",
        drive_path="staging",
    )


def test_secret_is_not_stored_in_plaintext(session, with_key):
    from app.models import GraphCredential

    save_credentials(
        session,
        tenant_id="tid",
        client_id="cid",
        client_secret="s3cr3t",
        site_id="sid",
        drive_path="staging",
    )
    row = session.get(GraphCredential, 1)
    assert "s3cr3t" not in row.client_secret_encrypted
    assert row.client_secret_encrypted != "s3cr3t"


def test_save_overwrites_single_row(session, with_key):
    from app.models import GraphCredential

    save_credentials(session, tenant_id="a", client_id="a", client_secret="a",
                     site_id="a", drive_path="a")
    save_credentials(session, tenant_id="b", client_id="b", client_secret="b",
                     site_id="b", drive_path="b")
    assert session.query(GraphCredential).count() == 1
    assert load_credentials(session).tenant_id == "b"


def test_load_without_record_raises(session, with_key):
    with pytest.raises(GraphNotConfigured) as exc:
        load_credentials(session)
    assert exc.value.code == "GRAPH_NOT_CONFIGURED"


def test_missing_key_raises(session, monkeypatch):
    monkeypatch.setattr(settings, "secret_key", None)
    with pytest.raises(GraphNotConfigured) as exc:
        load_credentials(session)
    assert "PPTX2PDF_SECRET_KEY" in exc.value.message


def test_corrupted_ciphertext_raises(session, with_key):
    from app.models import GraphCredential

    save_credentials(session, tenant_id="t", client_id="c", client_secret="s",
                     site_id="s", drive_path="d")
    row = session.get(GraphCredential, 1)
    row.client_secret_encrypted = "not-a-valid-fernet-token"
    session.commit()

    # 密文损坏必须报 GraphNotConfigured 而不是让裸 InvalidToken 穿透——
    # 后者不是 AppError，会退化成不带错误码的 500。
    with pytest.raises(GraphNotConfigured):
        load_credentials(session)


def test_wrong_key_raises(session, with_key, monkeypatch):
    save_credentials(session, tenant_id="t", client_id="c", client_secret="s",
                     site_id="s", drive_path="d")
    monkeypatch.setattr(
        settings, "secret_key", "Zt7VQKLBB3sfxaMxxLh6EFRmbUlq7wPCM0hEXeYqQ4Y="
    )
    with pytest.raises(GraphNotConfigured):
        load_credentials(session)
