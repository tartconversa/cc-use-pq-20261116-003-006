import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from brand_signal.api import create_app


class ApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client = TestClient(create_app(str(Path(directory.name) / "http.sqlite3")))

    def seed(self):
        self.assertEqual(self.client.post("/markets", json={"code": "ES", "timezone": "Europe/Madrid"}).status_code, 200)
        self.assertEqual(self.client.post("/products", json={"code": "dryer", "name": "Synthetic Dryer"}).status_code, 200)
        self.assertEqual(self.client.post("/devices", json={"id": "d1", "market": "ES", "product": "dryer", "installed_at": "2026-10-01T00:00:00Z"}).status_code, 200)

    def test_http_business_journey(self):
        self.seed()
        upload = self.client.post("/ingest/tickets", json={"request_key": "r1", "rows": [{"event_key": "e1", "id": "t1", "device_id": "d1", "opened_at": "2026-10-20T22:30:00Z", "severity": "high"}]})
        self.assertEqual(upload.status_code, 200)
        self.assertEqual(upload.json()["accepted"], 1)
        revision = self.client.get("/analytics/ES", params={"from_day": "2026-10-21", "to_day": "2026-10-21"}).json()["source_revision"]
        candidate = self.client.post("/candidates", json={"market": "ES", "from_day": "2026-10-21", "to_day": "2026-10-21", "expected_head": 0}).json()
        self.assertEqual(candidate["source_revision"], revision)
        published = self.client.post(f"/candidates/{candidate['id']}/publish", json={"expected_head": 0, "expected_source_revision": revision})
        self.assertEqual(published.status_code, 200)
        frozen = self.client.get("/publications/ES/1").json()
        self.assertEqual(frozen["rows"][0]["ticket_opened"], 1)
        self.assertIn("ticket_opened", self.client.get("/publications/ES/1/csv").text)

    def test_domain_error_has_code(self):
        response = self.client.post("/markets", json={"code": "ES", "timezone": "Unreal/Zone"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_timezone")

    def test_schema_error_is_visible(self):
        response = self.client.post("/products", json={"code": "dryer"})
        self.assertEqual(response.status_code, 422)

    def test_missing_publication_is_not_live_fallback(self):
        self.seed()
        response = self.client.get("/publications/ES/1")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "unknown_publication")

    def test_http_source_replay(self):
        self.seed()
        body = {"request_key": "stable", "rows": [{"event_key": "e1", "id": "m1", "device_id": "d1", "observed_at": "2026-10-21T00:00:00Z", "run_hours": 12, "downtime_hours": 1}]}
        first = self.client.post("/ingest/telemetry", json=body)
        second = self.client.post("/ingest/telemetry", json=body)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(second.json()["replay"])
        self.assertEqual(first.json()["id"], second.json()["id"])

    def test_http_ticket_revision_conflict(self):
        self.seed()
        self.client.post("/ingest/tickets", json={"request_key": "r1", "rows": [{"event_key": "e1", "id": "t1", "device_id": "d1", "opened_at": "2026-10-20T22:30:00Z"}]})
        response = self.client.post("/tickets/t1/transitions", json={"action": "acknowledge", "at": "2026-10-21T00:00:00Z", "expected_revision": 1})
        self.assertEqual(response.status_code, 200)
        stale = self.client.post("/tickets/t1/transitions", json={"action": "resolve", "at": "2026-10-21T01:00:00Z", "expected_revision": 1})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"], "stale_ticket")

    def test_http_settlement_late_data_workflow(self):
        self.seed()
        day = "2026-10-21"
        opened = self.client.post("/settlements/windows", json={"market": "ES", "day": day})
        self.assertEqual(opened.status_code, 200)
        window_id = opened.json()["id"]
        # 关窗后补传计量
        closed = self.client.post(f"/settlements/windows/{window_id}/close", json={"expected_revision": 1})
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(closed.json()["status"], "closed")
        upload = self.client.post("/ingest/telemetry", json={"request_key": "late-1", "rows": [
            {"event_key": "m-late", "id": "m1", "device_id": "d1", "observed_at": "2026-10-21T08:00:00Z", "run_hours": 12, "downtime_hours": 2}
        ]})
        self.assertEqual(upload.status_code, 200)
        self.assertTrue(upload.json()["rows"][0]["is_late"])
        pending = self.client.get("/settlements/ES/pending")
        self.assertEqual(len(pending.json()["pending"]), 1)
        impact = self.client.get(f"/settlements/windows/{window_id}/impact")
        self.assertEqual(impact.status_code, 200)
        self.assertEqual(impact.json()["late_by_source"], {"telemetry": 1})
        # 重开窗口
        reopen = self.client.post(f"/settlements/windows/{window_id}/reopen", json={"expected_revision": 2, "reason": "review"})
        self.assertEqual(reopen.status_code, 200)
        self.assertEqual(reopen.json()["status"], "reopened")
        # 错误修订号被 CAS 拒绝
        stale = self.client.post(f"/settlements/windows/{window_id}/close", json={"expected_revision": 2})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"], "stale_window")
        # 发布修订版本后迟到事件纳入
        self.client.post(f"/settlements/windows/{window_id}/close", json={"expected_revision": 3})
        candidate = self.client.post("/candidates", json={"market": "ES", "from_day": day, "to_day": day, "expected_head": 0})
        published = self.client.post(f"/candidates/{candidate.json()['id']}/publish", json={"expected_head": 0, "expected_source_revision": candidate.json()["source_revision"]})
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()["late_included"], 1)
        self.assertEqual(self.client.get("/settlements/ES/pending").json()["pending"], [])
        late = self.client.get(f"/settlements/windows/{window_id}/late")
        self.assertEqual(late.json()["late_arrivals"][0]["status"], "included")
