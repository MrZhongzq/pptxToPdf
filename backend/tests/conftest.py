import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base


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
