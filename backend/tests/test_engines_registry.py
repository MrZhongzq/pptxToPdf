"""get_engine 的注册表与凭证注入测试。

Graph 引擎需要凭证才能构造；凭证读取被从 GraphEngine.convert() 移到了这里
（Task 8 的设计改动，见 task-8-report.md）：GraphEngine 本身不再碰数据库，
base.py 的"引擎不得访问数据库"约束才名副其实。这里验证的正是那条移动之后
的接线——`get_engine("graph", session=...)` 必须真的调用 load_credentials
并把结果注入构造函数，未配置时必须在碰 HTTP 之前就报错。
"""
import pytest

import app.services.engines as engines_module
from app.config import settings
from app.errors import GraphNotConfigured
from app.services.engines import get_engine
from app.services.engines.graph import GraphEngine
from app.services.graph_credentials import save_credentials

SECRET_KEY = "8I3F3CqPwlEsmMDLbEIVSXd8oXlmqkOMWFnDPbNXKvA="


def test_get_engine_graph_raises_graph_not_configured_without_session(monkeypatch):
    """没有 session 就没法读凭证——必须是清楚的调用方错误，不能是
    访问 None.tenant_id 之类看不出原因的 AttributeError。"""
    with pytest.raises(ValueError, match="session"):
        get_engine("graph")


def test_get_engine_graph_raises_graph_not_configured_when_no_credentials_saved(
    session, monkeypatch
):
    """凭证未配置时，get_engine 必须在碰 HTTP（甚至在构造 GraphEngine）之前
    就抛 GraphNotConfigured——这条路径完全不涉及网络。"""
    monkeypatch.setattr(settings, "secret_key", SECRET_KEY)

    with pytest.raises(GraphNotConfigured):
        get_engine("graph", session=session)


def test_get_engine_graph_injects_loaded_credentials(session, monkeypatch):
    """凭证注入真的发生：get_engine 读到的凭证内容必须原样传进
    GraphEngine 的构造函数，而不是引擎自己再去开一次会话读。"""
    monkeypatch.setattr(settings, "secret_key", SECRET_KEY)
    save_credentials(
        session,
        tenant_id="tid",
        client_id="cid",
        client_secret="secret",
        site_id="site-1",
        drive_path="staging",
    )

    engine = get_engine("graph", session=session)

    assert isinstance(engine, GraphEngine)
    assert engine.credentials.tenant_id == "tid"
    assert engine.credentials.client_id == "cid"
    assert engine.credentials.client_secret == "secret"
    assert engine.credentials.site_id == "site-1"
    assert engine.credentials.drive_path == "staging"


def test_get_engine_constructs_registered_graph_subclass_not_base_class(
    session, monkeypatch
):
    """m4 复审指出的缺陷：`issubclass(cls, GraphEngine)` 判断对了要不要走
    凭证注入这条分支，但如果分支体硬编码 `return GraphEngine(...)`（而不是
    `return cls(...)`），注册的子类会被静默替换成基类实例——特判命中了，
    构造的却是错误的类型。这里注册一个 GraphEngine 子类，断言 get_engine
    返回的真的是这个子类的实例（不是被基类偷换），且凭证确实注入到位。
    """
    monkeypatch.setattr(settings, "secret_key", SECRET_KEY)
    save_credentials(
        session,
        tenant_id="tid",
        client_id="cid",
        client_secret="secret",
        site_id="site-1",
        drive_path="staging",
    )

    class _CustomGraphEngine(GraphEngine):
        pass

    monkeypatch.setitem(engines_module._ENGINES, "graph", _CustomGraphEngine)

    engine = get_engine("graph", session=session)

    assert type(engine) is _CustomGraphEngine
    assert isinstance(engine, _CustomGraphEngine)
    assert engine.credentials.tenant_id == "tid"


def test_get_engine_returns_placeholder():
    assert get_engine("placeholder").name == "placeholder"


def test_get_engine_unknown_raises():
    with pytest.raises(KeyError):
        get_engine("nonexistent-engine")


def test_get_engine_non_graph_ignores_session_kwarg(session):
    """非 graph 引擎不需要凭证，传不传 session 都不该报错或碰凭证表。"""
    assert get_engine("libreoffice", session=session).name == "libreoffice"
