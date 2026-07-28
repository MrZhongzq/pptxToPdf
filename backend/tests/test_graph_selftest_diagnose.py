from app.services.graph_selftest import (
    STEPS,
    diagnose_graph_error,
    diagnose_token_error,
)


def test_steps_order():
    assert STEPS == ("token", "drive", "upload", "convert", "delete")


def test_token_error_unknown_tenant():
    msg = diagnose_token_error(400, '{"error":"invalid_request","error_description":"AADSTS90002: Tenant not found"}')
    assert "租户" in msg
    assert "AADSTS90002" in msg


def test_token_error_bad_client_id():
    msg = diagnose_token_error(400, '{"error_description":"AADSTS700016: Application not found in directory"}')
    assert "client_id" in msg
    assert "AADSTS700016" in msg


def test_token_error_bad_secret():
    msg = diagnose_token_error(401, '{"error_description":"AADSTS7000215: Invalid client secret provided"}')
    assert "client_secret" in msg
    assert "AADSTS7000215" in msg


def test_token_error_falls_back_to_raw():
    msg = diagnose_token_error(500, "internal server error")
    assert "500" in msg
    assert "internal server error" in msg


def test_drive_404_points_at_site_id():
    msg = diagnose_graph_error("drive", 404, '{"error":{"code":"itemNotFound"}}')
    assert "site_id" in msg


def test_drive_403_points_at_permission():
    msg = diagnose_graph_error("drive", 403, '{"error":{"code":"accessDenied"}}')
    assert "权限" in msg


def test_delete_403_names_the_known_permission_trap():
    msg = diagnose_graph_error("delete", 403, '{"error":{"code":"accessDenied"}}')
    assert "Files.ReadWrite.All" in msg or "Sites.ReadWrite.All" in msg


def test_diagnose_truncates_long_body():
    msg = diagnose_graph_error("upload", 500, "x" * 5000)
    assert len(msg) < 500
