from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _connection_record) -> None:
    """api 与 worker 两个容器共享同一个 SQLite 文件，必须开 WAL。

    WAL 允许一写多读并发；busy_timeout 让偶发的写锁竞争自动重试
    而不是立刻抛 database is locked。仅对 SQLite 生效。
    """
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _ensure_columns() -> None:
    """给已存在的表补齐 ORM 里新增的列。

    `create_all` 建新表，但**不给已存在的表加列**——四期部署时踩过：
    `Task.shard_total` 是三期加的字段，升级已有部署时那一列根本没被创建，
    只能现场手工 `ALTER TABLE`。这个函数把那次手工操作固化下来。

    刻意只做「加列」一件事：改类型、删列、重命名在 SQLite 上本来就要靠
    重建表来实现，真遇到时应该正面引入 Alembic，而不是把这个工具越描越
    黑。加列是这个项目唯一实际发生过的 schema 变更类型，30 行覆盖 100%。

    新列必须是 nullable 或带默认值——SQLite 的 ADD COLUMN 不接受没有默认
    值的 NOT NULL 列。这不是限制，是提醒：给已有行准备好取值本来就是加列
    时该想清楚的事。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all 刚建的新表，列是齐的
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} "
                ddl += column.type.compile(engine.dialect)
                if column.default is not None and column.default.is_scalar:
                    ddl += f" DEFAULT {column.default.arg!r}"
                elif not column.nullable:
                    raise RuntimeError(
                        f"{table.name}.{column.name} 是 NOT NULL 且无默认值，"
                        "SQLite 无法为已有行补上——给它加默认值或改成 nullable"
                    )
                conn.execute(text(ddl))


def init_db() -> None:
    from app import models  # noqa: F401  确保模型已注册到 Base.metadata

    Base.metadata.create_all(engine)
    _ensure_columns()


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
