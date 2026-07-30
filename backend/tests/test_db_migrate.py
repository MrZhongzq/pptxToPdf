"""最小列迁移器：给已存在的表补齐 ORM 里新增的列。

这个东西保证的是「升级已有部署不炸」，而四期在这上面栽过一次——
Task.shard_total 是三期加的字段，升级时那一列根本没被创建，真机上
现场手工 ALTER TABLE 才救回来。所以这里的每条断言都要真的建一张
「缺列的旧表」，而不是在全新库上跑一遍看不报错就算过。
"""

import pytest
from sqlalchemy import Column, Integer, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """造一个「旧版」数据库：表在，但少几列。"""
    import app.db as db_module

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        # 故意只建两列——真实的旧库就是这样：主键在，后加的字段不在
        conn.execute(text("CREATE TABLE widgets (widget_id VARCHAR(36) PRIMARY KEY, name VARCHAR(64))"))
        conn.execute(text("INSERT INTO widgets VALUES ('w1', '既有数据')"))
    monkeypatch.setattr(db_module, "engine", eng)
    yield eng
    eng.dispose()


def _schema_with(*extra_columns):
    """造一份 ORM 元数据，widgets 表带上给定的额外列。"""

    class Base(DeclarativeBase):
        pass

    from sqlalchemy import Table

    # 每次调用都是一个全新的 DeclarativeBase 子类，自带独立的 metadata，
    # 所以同名表不会撞车，也不需要清空。
    Table(
        "widgets",
        Base.metadata,
        Column("widget_id", String(36), primary_key=True),
        Column("name", String(64)),
        *extra_columns,
    )
    return Base


def test_adds_missing_nullable_column(legacy_db, monkeypatch):
    """最典型的场景：新版本加了一个 nullable 列。"""
    import app.db as db_module

    monkeypatch.setattr(db_module, "Base", _schema_with(Column("note", String(128), nullable=True)))
    db_module._ensure_columns()

    cols = {c["name"] for c in inspect(legacy_db).get_columns("widgets")}
    assert "note" in cols


def test_existing_rows_survive(legacy_db, monkeypatch):
    """加列不能丢数据——这正是四期真机上不敢删库重建的理由。"""
    import app.db as db_module

    monkeypatch.setattr(db_module, "Base", _schema_with(Column("note", String(128), nullable=True)))
    db_module._ensure_columns()

    with legacy_db.begin() as conn:
        rows = list(conn.execute(text("SELECT widget_id, name, note FROM widgets")))
    assert rows == [("w1", "既有数据", None)]


def test_is_idempotent(legacy_db, monkeypatch):
    """跑两遍不能报 duplicate column——每次启动都会调它。"""
    import app.db as db_module

    monkeypatch.setattr(db_module, "Base", _schema_with(Column("note", String(128), nullable=True)))
    db_module._ensure_columns()
    db_module._ensure_columns()  # 不抛就算过

    cols = [c["name"] for c in inspect(legacy_db).get_columns("widgets")]
    assert cols.count("note") == 1


def test_adds_column_with_scalar_default(legacy_db, monkeypatch):
    """带标量默认值的列，已有行要拿到那个默认值而不是 NULL。"""
    import app.db as db_module

    monkeypatch.setattr(
        db_module,
        "Base",
        _schema_with(Column("kind", String(16), nullable=False, default="basic")),
    )
    db_module._ensure_columns()

    with legacy_db.begin() as conn:
        assert list(conn.execute(text("SELECT kind FROM widgets"))) == [("basic",)]


def test_refuses_not_null_column_without_default(legacy_db, monkeypatch):
    """NOT NULL 且无默认值：SQLite 根本没法给已有行补值。

    宁可在启动时炸出一条说清原因的错，也不能让 ALTER TABLE 抛一句
    「Cannot add a NOT NULL column with default value NULL」让人去猜。
    """
    import app.db as db_module

    monkeypatch.setattr(
        db_module, "Base", _schema_with(Column("required", Integer, nullable=False))
    )
    with pytest.raises(RuntimeError, match="NOT NULL 且无默认值"):
        db_module._ensure_columns()


def test_leaves_unrelated_tables_alone(legacy_db, monkeypatch):
    """ORM 里有而库里没有的表，交给 create_all，迁移器不该碰。"""
    import app.db as db_module
    from sqlalchemy import Table

    base = _schema_with(Column("note", String(128), nullable=True))
    Table("brand_new", base.metadata, Column("id", String(36), primary_key=True))
    monkeypatch.setattr(db_module, "Base", base)

    db_module._ensure_columns()  # 不该因为 brand_new 不存在而抛

    assert "brand_new" not in set(inspect(legacy_db).get_table_names())
