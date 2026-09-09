import pytest

from nvplan.db.session import SessionLocal, get_engine, init_db, seed_categories


@pytest.fixture()
def engine():
    eng = get_engine("sqlite:///:memory:")
    init_db(eng)
    return eng


@pytest.fixture()
def session(engine):
    with SessionLocal(bind=engine) as s:
        seed_categories(s)
        yield s
