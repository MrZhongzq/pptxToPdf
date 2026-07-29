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


def test_token_error_real_tenant_format_response_is_not_misdiagnosed_as_unknown_tenant():
    """真机实测：假租户名触发的是 AADSTS900023（tenant_id 不是合法 DNS
    名/域名格式），不是 AADSTS90002（租户查不到）。裸子串匹配下
    "AADSTS90002" in body 对这段真实响应也会是 True，从而被误判成
    "租户不存在"——这条测试用真机抓到的原始响应体，专门堵住这个误判。"""
    body = (
        '{"error":"invalid_request","error_description":"AADSTS900023: '
        "Specified tenant identifier 'fake-tenant' is neither a valid DNS "
        'name, nor a valid external domain."}'
    )
    msg = diagnose_token_error(400, body)
    assert "AADSTS900023" in msg
    assert "DNS" in msg or "格式" in msg
    assert "AADSTS90002）" not in msg
    assert "租户不存在" not in msg


def test_token_error_unknown_tenant_not_confused_with_invalid_format_code():
    """AADSTS90002（无 900023 后缀）本身仍要正确诊断为"租户不存在"，
    不能因为新增了 900023 的分支就被抢先匹配或漏判。"""
    msg = diagnose_token_error(400, '{"error_description":"AADSTS90002: Tenant not found"}')
    assert "租户不存在" in msg
    assert "AADSTS90002）" in msg
    assert "AADSTS900023" not in msg


def test_token_error_secret_expired():
    """AADSTS7000222 = client secret 已过期（需要去 Azure 门户续），
    与 AADSTS7000215（secret 抄错了）修复动作不同，诊断文案也要不同。"""
    msg = diagnose_token_error(
        401,
        '{"error_description":"AADSTS7000222: The provided client secret '
        'keys for app are expired."}',
    )
    assert "AADSTS7000222" in msg
    assert "过期" in msg


def test_token_error_bad_secret_not_confused_with_expired_secret():
    msg = diagnose_token_error(401, '{"error_description":"AADSTS7000215: Invalid client secret provided"}')
    assert "AADSTS7000215）" in msg
    assert "AADSTS7000222" not in msg


def test_token_error_code_boundary_no_prefix_false_match():
    """裸子串匹配的核心缺陷：AADSTS90002 是 AADSTS900023 的前缀。构造一个
    只在数字上多一位、格式仍类似真实响应的 body，确认不会被前缀命中。"""
    msg = diagnose_token_error(400, '{"error_description":"AADSTS900029: some future error code"}')
    assert "AADSTS90002）" not in msg
    assert "AADSTS900023" not in msg
    # 未知码应当落到兜底分支，而不是被误判成任何一条已知诊断
    assert "HTTP 400" in msg
