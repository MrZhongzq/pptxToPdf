import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base


@pytest.fixture(autouse=True)
def _isolate_app_db(tmp_path, monkeypatch):
    """防止测试污染开发者本地真实数据库。

    app/db.py 在模块 import 时就用 settings.database_url（默认
    "sqlite:///./pptx2pdf.db"）构造好了模块级全局 engine/SessionLocal。
    各测试文件里的 `monkeypatch.setattr(settings, "storage_root", ...)`
    只改文件存储路径，改不到这个早已绑定好的 engine——于是
    test_uploads_api.py / test_tasks_api.py / test_e2e_large_upload.py 里
    `Base.metadata.drop_all(engine)` / `create_all(engine)` 实际操作的是
    开发者机器上真实的 backend/pptx2pdf.db。

    这里把 app.db.engine / app.db.SessionLocal 重定向到 tmp_path 下的独立
    sqlite 文件；同时把 app.services.pipeline 里已经在模块级
    `from app.db import SessionLocal` 导入过的那份引用也同步替换，否则
    后台任务（run_task）仍然会用回旧的 SessionLocal 连到真实库。

    autouse + 函数级作用域：pytest 保证同一 scope 下 autouse fixture 先于
    显式请求的 fixture（如各测试文件里的 `client`）执行，所以这里的重定向
    总能赶在任何测试代码触碰 engine 之前生效。
    """
    import app.db as db_module
    import app.services.pipeline as pipeline_module

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'isolated.db'}",
        connect_args={"check_same_thread": False},
    )
    test_session_local = sessionmaker(
        bind=test_engine, autoflush=False, expire_on_commit=False
    )

    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(db_module, "SessionLocal", test_session_local)
    monkeypatch.setattr(pipeline_module, "SessionLocal", test_session_local)

    yield

    test_engine.dispose()


@pytest.fixture(autouse=True)
def _force_placeholder_engine(monkeypatch):
    """本机没有 LibreOffice。一期测试验证的是上传协议与状态机，
    不是转换质量——把引擎选择固定回占位引擎，让这些测试继续有效。
    真实转换在测试机上验证，见计划的完成判据。"""
    monkeypatch.setattr(
        "app.services.pipeline.select_engine", lambda meta: "placeholder"
    )


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    s = maker()
    yield s
    s.close()


@pytest.fixture
def storage_root(tmp_path):
    root = tmp_path / "storage"
    for sub in ("uploads", "originals", "outputs"):
        (root / sub).mkdir(parents=True)
    return root
