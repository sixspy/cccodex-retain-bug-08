import os

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app

REFERENCE_DATE = "2026-11-17"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """每个用例独立库 + 固定基准日 + 默认处置策略，避免用例互相污染。"""
    db = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("RETAIN_REFERENCE_DATE", REFERENCE_DATE)
    monkeypatch.setenv("RETAIN_GRACE_DISPOSITION", "reject")
    monkeypatch.setenv("RETAIN_GRACE_DAYS", "3")
    init_db(str(db))
    yield db


@pytest.fixture()
def client(_isolated_env):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def authed(client):
    r = client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return client
