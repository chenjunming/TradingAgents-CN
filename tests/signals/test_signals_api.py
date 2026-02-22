from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.signals import router as signals_router


class _FakeStore:
    def __init__(self):
        self.event = {
            "event_id": "evt_1",
            "user_id": "u1",
            "ticker": "NVDA",
            "market": "US",
            "action_tokens": {"analysis": "tok_1"},
        }

    async def ensure_indexes(self):
        return None

    async def list_events(self, **kwargs):
        return {"items": [{"event_id": "evt_1"}], "total": 1, "limit": 50, "offset": 0}

    async def ack_event(self, user_id: str, event_id: str):
        return event_id == "evt_1"

    async def mute_from_event(self, user_id: str, event_id: str, days: int = 7):
        return event_id == "evt_1"

    async def current_ruleset(self):
        return {"version_hash": "sha256:test"}

    async def get_event(self, user_id: str, event_id: str):
        if user_id == "u1" and event_id == str(self.event.get("event_id")):
            return dict(self.event)
        return None

    async def get_event_by_id(self, event_id: str):
        if event_id == str(self.event.get("event_id")):
            return dict(self.event)
        return None

    async def set_event_analysis_task(self, user_id: str, event_id: str, task_id: str, status: str = "pending"):
        return True


class _FakeAnalysisService:
    def __init__(self):
        self.last_request = None
        self.last_user_id = None

    async def create_analysis_task(self, user_id: str, request):
        self.last_user_id = user_id
        self.last_request = request
        return {"task_id": "task_1", "status": "pending"}

    async def execute_analysis_background(self, task_id: str, user_id: str, request):
        return None


def _build_app():
    app = FastAPI()
    app.include_router(signals_router)
    return app


def test_signals_api_endpoints(monkeypatch):
    fake_store = _FakeStore()
    fake_service = _FakeAnalysisService()
    monkeypatch.setattr("app.routers.signals.store", fake_store)
    monkeypatch.setattr("app.routers.signals.get_simple_analysis_service", lambda: fake_service)

    async def _fake_user():
        return {"id": "u1", "is_admin": False}

    app = _build_app()
    app.dependency_overrides = {}

    from app.routers import signals as signals_mod
    from app.routers.auth_db import get_current_user

    app.dependency_overrides[get_current_user] = _fake_user

    client = TestClient(app)

    r1 = client.get("/api/signals")
    assert r1.status_code == 200

    r2 = client.post("/api/signals/evt_1/ack")
    assert r2.status_code == 200

    r3 = client.post("/api/signals/evt_1/mute", json={"days": 7})
    assert r3.status_code == 200

    r4 = client.get("/api/signals/ruleset/current")
    assert r4.status_code == 200
    assert r4.json()["data"]["version_hash"] == "sha256:test"

    r5 = client.post("/api/signals/evt_1/analysis/run")
    assert r5.status_code == 200
    assert r5.json()["data"]["task_id"] == "task_1"
    assert fake_service.last_request.symbol == "NVDA"
    assert fake_service.last_request.parameters.market_type == "美股"

    r6 = client.get("/api/signals/evt_1/analysis/run", params={"token": "tok_1"})
    assert r6.status_code == 200

    r7 = client.get("/api/signals/evt_1/analysis/run", params={"token": "bad"})
    assert r7.status_code == 403


def test_signals_api_hk_analysis_symbol_normalized(monkeypatch):
    fake_store = _FakeStore()
    fake_store.event = {
        "event_id": "evt_hk",
        "user_id": "u1",
        "ticker": "00883",
        "market": "HK",
        "action_tokens": {"analysis": "tok_hk"},
    }
    fake_service = _FakeAnalysisService()
    monkeypatch.setattr("app.routers.signals.store", fake_store)
    monkeypatch.setattr("app.routers.signals.get_simple_analysis_service", lambda: fake_service)

    async def _fake_user():
        return {"id": "u1", "is_admin": False}

    app = _build_app()
    from app.routers.auth_db import get_current_user

    app.dependency_overrides[get_current_user] = _fake_user
    client = TestClient(app)

    r = client.post("/api/signals/evt_hk/analysis/run")
    assert r.status_code == 200
    assert fake_service.last_request.symbol == "00883.HK"
    assert fake_service.last_request.stock_code == "00883.HK"
    assert fake_service.last_request.parameters.market_type == "港股"
