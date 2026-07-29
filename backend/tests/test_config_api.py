"""Task 10 追加：只读容量端点，供前端在选 Graph 引擎时做上传前的启发式预判。

裁决记录（协调者）：
- 只读，只吐容量相关数字：max_file_size / graph_max_shards /
  graph_max_shard_bytes / graph_max_merge_bytes。
- 绝对不吐任何凭证/配置状态（如 Graph 是否已配置）——那是四期管理面的
  范畴，也是信息泄露面，这轮不碰。
- 值必须直接来自 app.config.settings 单例，不许在端点里抄一份常量——
  第一个测试用 monkeypatch 改 settings 属性，如果端点抄了常量而不是读
  settings，这个测试会红。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 同 test_tasks_api.py 的 client fixture：Base/engine 延迟到函数体内导入，
    # 因为 conftest.py 的 _isolate_app_db autouse fixture 重定向了
    # app.db.engine，模块顶层 import 会绕过这个重定向。
    from app.db import Base, engine

    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    settings.ensure_dirs()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_capacity_reflects_current_settings(client, monkeypatch):
    """值必须现读 settings，不是端点里抄的常量副本。"""
    monkeypatch.setattr(settings, "max_file_size", 999)
    monkeypatch.setattr(settings, "graph_max_shards", 7)
    monkeypatch.setattr(settings, "graph_max_shard_bytes", 1234)
    monkeypatch.setattr(settings, "graph_max_merge_bytes", 5678)

    resp = await client.get("/api/config/capacity")

    assert resp.status_code == 200
    assert resp.json() == {
        "max_file_size": 999,
        "graph_max_shards": 7,
        "graph_max_shard_bytes": 1234,
        "graph_max_merge_bytes": 5678,
    }


async def test_capacity_uses_default_settings_values(client):
    """不 monkeypatch 时，端点吐的是当前进程里的真实默认值——
    与硬编码在测试里的期望数字脱钩，防止两边各自漂移互相掩盖。"""
    resp = await client.get("/api/config/capacity")

    assert resp.status_code == 200
    body = resp.json()
    assert body["max_file_size"] == settings.max_file_size
    assert body["graph_max_shards"] == settings.graph_max_shards
    assert body["graph_max_shard_bytes"] == settings.graph_max_shard_bytes
    assert body["graph_max_merge_bytes"] == settings.graph_max_merge_bytes


async def test_capacity_does_not_leak_credential_or_extra_fields(client):
    """不吐 secret_key 等凭证/配置状态，也不多吐任何字段——
    响应形状必须恰好是这四个容量数字。"""
    resp = await client.get("/api/config/capacity")

    body = resp.json()
    assert set(body.keys()) == {
        "max_file_size",
        "graph_max_shards",
        "graph_max_shard_bytes",
        "graph_max_merge_bytes",
    }
    assert "secret_key" not in body
