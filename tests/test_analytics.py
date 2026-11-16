from brand_signal.alerts import Alerts
from brand_signal.annotations import Annotations
from brand_signal.exports import Exports
from brand_signal.investigations import Investigations
from brand_signal.reconciliation import Reconciliation
from brand_signal.errors import DomainError
from support import Scenario


class AnalyticsTests(Scenario):
    def test_local_day_ticket_and_meter(self):
        self.submit_ticket()
        self.submit_meter()
        row = self.analytics.live("ES", "2026-10-21", "2026-10-21")["rows"][0]
        self.assertEqual(row["ticket_opened"], 1)
        self.assertEqual(row["telemetry_events"], 1)
        self.assertEqual(row["devices_reporting"], 1)

    def test_missing_measurement_has_null_rate(self):
        row = self.analytics.live("ES", "2026-10-21", "2026-10-21")["rows"][0]
        self.assertIsNone(row["downtime_share"])
        self.assertEqual(row["telemetry_coverage"], 0)

    def test_ticket_rate_uses_active_device_count(self):
        self.submit_ticket()
        row = self.analytics.live("ES", "2026-10-21", "2026-10-21")["rows"][0]
        self.assertEqual(row["tickets_per_100_devices"], 100)

    def test_partial_day_installation_affects_exposure(self):
        self.catalog.install("device-2", "ES", "dryer", "2026-10-21T10:00:00Z")
        row = self.analytics.live("ES", "2026-10-21", "2026-10-21")["rows"][0]
        self.assertEqual(row["active_devices"], 2)
        self.assertGreater(row["exposure_hours"], 24)
        self.assertLess(row["exposure_hours"], 48)

    def test_retired_device_not_active_later(self):
        self.catalog.retire("device-1", "2026-10-21T00:00:00Z", 1)
        row = self.analytics.live("ES", "2026-10-22", "2026-10-22")["rows"][0]
        self.assertEqual(row["active_devices"], 0)
        self.assertIsNone(row["tickets_per_100_devices"])

    def test_fleet_summary_uses_device_days(self):
        self.submit_ticket()
        report = self.analytics.fleet_summary("ES", "2026-10-21", "2026-10-22")
        product = report["products"][0]
        self.assertEqual(product["device_days"], 2)
        self.assertEqual(product["tickets"], 1)
        self.assertEqual(product["ticket_rate_per_100_device_days"], 50)

    def test_device_timeline_joins_sources(self):
        self.submit_ticket()
        self.submit_meter()
        day = self.analytics.device_timeline("device-1", "2026-10-21", "2026-10-21")["days"][0]
        self.assertEqual(day["tickets_opened"], ["ticket-1"])
        self.assertEqual(day["telemetry"][0]["event_id"], "meter-1")

    def test_ticket_close_updates_daily_count(self):
        self.submit_ticket()
        self.service.transition("ticket-1", "acknowledge", "2026-10-21T00:00:00Z", 1)
        self.service.transition("ticket-1", "resolve", "2026-10-21T01:00:00Z", 2)
        self.service.transition("ticket-1", "close", "2026-10-21T02:00:00Z", 3)
        row = self.analytics.live("ES", "2026-10-21", "2026-10-21")["rows"][0]
        self.assertEqual(row["ticket_closed"], 1)
        self.assertEqual(row["ticket_open_at_end"], 0)

    def test_quality_tracks_partial_rejection(self):
        self.intake.submit("tickets", "mixed", [self.ticket(), self.ticket(key="bad", identifier="bad", device="missing")])
        quality = self.analytics.quality("ES")
        self.assertEqual(quality["sources"]["tickets"]["accepted"], 1)
        self.assertEqual(quality["sources"]["tickets"]["rejected"], 1)

    def test_reconciliation_finds_ticket_without_meter(self):
        self.submit_ticket()
        report = Reconciliation(self.store).inspect("ES", "2026-10-21", "2026-10-21")
        self.assertIn("ticket_without_meter", [r["kind"] for r in report["issues"]])

    def test_saved_reconciliation_stays_frozen(self):
        reconcile = Reconciliation(self.store)
        before = reconcile.inspect("ES", "2026-10-21", "2026-10-21")
        saved = reconcile.save("ES", "2026-10-21", "2026-10-21", before["source_revision"])
        self.submit_ticket()
        self.assertEqual(reconcile.read(saved["id"])["issue_count"], before["issue_count"])

    def test_alerts_derive_from_reconciliation(self):
        self.submit_ticket()
        revision = self.analytics.live("ES", "2026-10-21", "2026-10-21")["source_revision"]
        alerts = Alerts(self.store).evaluate("ES", "2026-10-21", "2026-10-21", revision)
        self.assertTrue(alerts["alerts"])
        self.assertEqual(len(Alerts(self.store).list("ES")), len(alerts["alerts"]))

    def test_investigation_tracks_alert_and_cas(self):
        self.submit_ticket()
        revision = self.analytics.live("ES", "2026-10-21", "2026-10-21")["source_revision"]
        alert = Alerts(self.store).evaluate("ES", "2026-10-21", "2026-10-21", revision)["alerts"][0]
        workflow = Investigations(self.store)
        opened = workflow.open(alert["id"], "analyst")
        assigned = workflow.transition(opened["id"], "assign", 1, "reviewer", "triage")
        self.assertEqual(assigned["state"], "assigned")
        with self.assertRaises(DomainError) as error:
            workflow.transition(opened["id"], "resolve", 1, "reviewer", "done")
        self.assertEqual(error.exception.code, "stale_investigation")

    def test_annotation_revision_and_filter(self):
        notes = Annotations(self.store)
        created = notes.create("ES", "device-1", "2026-10-21", "service", "synthetic review")
        changed = notes.replace(created["id"], "confirmed", 1)
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(notes.list("ES", "2026-10-21")[0]["text"], "confirmed")

    def test_export_job_digest_and_version(self):
        self.release()
        exports = Exports(self.store)
        created = exports.create("ES", 1, "json")
        self.assertTrue(exports.read(created["id"])["verified"])
        self.assertEqual(exports.list("ES")[0]["version"], 1)
