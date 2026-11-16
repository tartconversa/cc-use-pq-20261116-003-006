from brand_signal.errors import DomainError
from brand_signal.settlements import Settlements
from support import Scenario


DAY = "2026-10-21"


class SettlementTests(Scenario):
    def setUp(self):
        super().setUp()
        self.settlements = Settlements(self.store)
        self.window = self.settlements.open_window("ES", DAY)

    def close(self):
        return self.settlements.close_window(self.window["id"], self.window["revision"])

    def test_window_lifecycle_open_close_reopen(self):
        self.assertEqual(self.window["status"], "open")
        self.assertEqual(self.window["revision"], 1)
        closed = self.close()
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["revision"], 2)
        self.assertIn("settled_issues", closed)
        reopened = self.settlements.reopen_window(self.window["id"], 2, "late meter expected")
        self.assertEqual(reopened["status"], "reopened")
        self.assertEqual(reopened["revision"], 3)
        again = self.settlements.close_window(self.window["id"], 3)
        self.assertEqual(again["status"], "closed")
        self.assertEqual(again["revision"], 4)

    def test_window_unique_per_market_day(self):
        with self.assertRaises(DomainError) as error:
            self.settlements.open_window("ES", DAY)
        self.assertEqual(error.exception.code, "window_exists")

    def test_close_requires_current_revision(self):
        with self.assertRaises(DomainError) as error:
            self.settlements.close_window(self.window["id"], 99)
        self.assertEqual(error.exception.code, "stale_window")

    def test_only_closed_window_can_reopen(self):
        with self.assertRaises(DomainError) as error:
            self.settlements.reopen_window(self.window["id"], 1)
        self.assertEqual(error.exception.code, "window_not_closed")

    def test_event_before_close_is_not_late(self):
        result = self.submit_meter()
        self.assertFalse(result["rows"][0]["is_late"])
        self.assertEqual(result["late"], 0)

    def test_event_after_close_is_registered_late(self):
        self.close()
        result = self.submit_meter()
        self.assertEqual(result["accepted"], 1)
        self.assertTrue(result["rows"][0]["is_late"])
        self.assertEqual(result["late"], 1)
        pending = self.settlements.pending("ES")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event_key"], "meter-key")
        self.assertEqual(pending[0]["status"], "pending")

    def test_late_flag_uses_market_local_day_not_utc_calendar(self):
        # UTC 21:30 在 Madrid(UTC+2) 仍是当天 23:30，归属 2026-10-21 的窗口
        self.close()
        result = self.submit_ticket(at="2026-10-21T21:30:00Z")
        self.assertTrue(result["rows"][0]["is_late"])
        # UTC 22:30 在 Madrid 已是次日 00:30，本地日 2026-10-22 无窗口，不算迟到
        next_day = self.submit_ticket(key="next-day", identifier="ticket-2", at="2026-10-21T22:30:00Z")
        self.assertFalse(next_day["rows"][0]["is_late"])

    def test_duplicate_upload_does_not_double_register_late(self):
        self.close()
        first = self.submit_meter()
        replay = self.submit_meter()
        self.assertTrue(first["rows"][0]["is_late"])
        self.assertTrue(replay["replay"])
        self.assertEqual(len(self.settlements.pending("ES")), 1)
        listing = self.settlements.list_late(self.window["id"])
        self.assertEqual(len(listing["late_arrivals"]), 1)

    def test_different_request_same_event_key_still_single_late_record(self):
        self.close()
        self.submit_meter()
        other = self.intake.submit("telemetry", "other-request", [self.meter(identifier="different-id")])
        self.assertEqual(other["rows"][0]["reason"], "event_conflict")
        self.assertEqual(len(self.settlements.pending("ES")), 1)

    def test_preview_flags_late_without_writing(self):
        self.close()
        preview = self.intake.preview("telemetry", [self.meter()])
        self.assertEqual(preview["ready"], 1)
        self.assertEqual(preview["late"], 1)
        self.assertTrue(preview["rows"][0]["is_late"])
        self.assertEqual(self.settlements.pending("ES"), [])

    def test_reopened_window_still_captures_late_events(self):
        self.close()
        self.settlements.reopen_window(self.window["id"], 2)
        result = self.submit_meter()
        self.assertTrue(result["rows"][0]["is_late"])
        self.assertEqual(len(self.settlements.pending("ES")), 1)

    def test_late_event_changes_live_coverage_immediately_but_waits_for_revision(self):
        # 先发布一版：设备当天无计量，覆盖率为 null
        self.release()
        self.close()
        self.submit_meter(run=12, down=2)
        live = self.analytics.live("ES", DAY, DAY)["rows"][0]
        self.assertEqual(live["telemetry_events"], 1)
        self.assertEqual(live["telemetry_coverage"], 1.0)
        # 已发布版本保持不变（有活跃设备但零上报，覆盖率按分析语义为 0.0）
        frozen = self.publication.read("ES", 1)["rows"][0]
        self.assertEqual(frozen["telemetry_events"], 0)
        self.assertEqual(frozen["telemetry_coverage"], 0.0)
        pending = self.settlements.pending("ES")
        self.assertEqual(pending[0]["status"], "pending")

    def test_impact_reports_coverage_downtime_and_alert_differences(self):
        # 结算时存在工单但没有计量 -> ticket_without_meter 告警
        self.submit_ticket()
        # 先发布基线版本，使 impact 能给出 before/after 对照
        self.release()
        self.close()
        # 迟到的计量事件：补覆盖率、停机占比，并消解缺计量告警
        self.submit_meter(run=12, down=2)
        report = self.settlements.impact(self.window["id"])
        self.assertEqual(report["late_by_source"], {"telemetry": 1})
        changes = {(c["day"], c["product"]): c["fields"] for c in report["metric_changes"]}
        fields = changes[(DAY, "dryer")]
        self.assertEqual(fields["telemetry_events"], {"before": 0, "after": 1})
        self.assertEqual(fields["telemetry_coverage"], {"before": 0.0, "after": 1.0})
        self.assertEqual(fields["downtime_share"]["before"], None)
        self.assertAlmostEqual(fields["downtime_share"]["after"], 2 / 14)
        kinds = {(issue["kind"], issue.get("device")) for issue in report["alert_changes"]["resolved"]}
        self.assertIn(("ticket_without_meter", "device-1"), kinds)
        self.assertEqual(report["alert_changes"]["new"], [])

    def test_impact_detects_new_alerts_from_late_data(self):
        self.close()
        # 迟到计量带停机但没有工单 -> downtime_without_ticket 新告警
        self.submit_meter(run=10, down=3)
        report = self.settlements.impact(self.window["id"])
        kinds = {(issue["kind"], issue.get("device")) for issue in report["alert_changes"]["new"]}
        self.assertIn(("downtime_without_ticket", "device-1"), kinds)

    def test_include_in_new_revision_marks_pending_included(self):
        self.release()
        self.close()
        self.submit_meter(run=12, down=2)
        candidate = self.publication.prepare("ES", DAY, DAY, 1)
        published = self.publication.publish(candidate["id"], 1, candidate["source_revision"])
        self.assertEqual(published["late_included"], 1)
        self.assertEqual(self.settlements.pending("ES"), [])
        detail = self.settlements.list_late(self.window["id"])
        self.assertEqual(detail["late_arrivals"][0]["status"], "included")
        # 新版本包含迟到数据
        self.assertEqual(self.publication.read("ES", 2)["rows"][0]["telemetry_events"], 1)

    def test_old_version_remains_reproducible_after_late_inclusion(self):
        self.release()
        before = self.publication.read("ES", 1)
        self.close()
        self.submit_meter(run=12, down=2)
        candidate = self.publication.prepare("ES", DAY, DAY, 1)
        self.publication.publish(candidate["id"], 1, candidate["source_revision"])
        self.assertEqual(self.publication.read("ES", 1), before)

    def test_late_event_included_once_even_if_seen_by_two_candidate_ranges(self):
        self.release()
        self.close()
        self.submit_meter(run=12, down=2)
        candidate = self.publication.prepare("ES", DAY, DAY, 1)
        self.publication.publish(candidate["id"], 1, candidate["source_revision"])
        # 再来一个覆盖同一天的候选并发布为 v3：纳入表不得重复计数
        follow = self.publication.prepare("ES", DAY, DAY, 2)
        result = self.publication.publish(follow["id"], 2, follow["source_revision"])
        self.assertEqual(result["late_included"], 1)
        with self.store.read() as db:
            count = db.execute(
                "SELECT count(*) FROM late_publication_inclusions WHERE market='ES'"
            ).fetchone()[0]
        # 同一迟到事件在两个版本各一条，但同版本内只有一条
        self.assertEqual(count, 2)
        listing = self.settlements.list_late(self.window["id"])
        self.assertEqual(len(listing["late_arrivals"]), 1)

    def test_concurrent_close_with_stale_revision_is_rejected(self):
        first = self.settlements.close_window(self.window["id"], 1)
        self.assertEqual(first["status"], "closed")
        with self.assertRaises(DomainError) as error:
            self.settlements.close_window(self.window["id"], 1)
        self.assertEqual(error.exception.code, "stale_window")

    def test_list_and_filter_windows(self):
        self.close()
        self.settlements.open_window("ES", "2026-10-22")
        closed = self.settlements.list_windows("ES", status="closed")
        self.assertEqual([w["day"] for w in closed], [DAY])
        all_windows = self.settlements.list_windows("ES")
        self.assertEqual(len(all_windows), 2)
        self.assertEqual(closed[0]["late_counts"], {})

    def test_close_snapshots_head_version_for_baseline(self):
        self.release()
        closed = self.close()
        self.assertEqual(closed["head_version"], 1)

    def test_impact_without_baseline_publication_uses_live_values(self):
        # 没有任何发布版本时关窗：baseline_head_version 为 None，差异相对空基线
        self.close()
        self.submit_meter(run=10, down=2)
        report = self.settlements.impact(self.window["id"])
        self.assertIsNone(report["baseline_head_version"])
        fields = report["metric_changes"][0]["fields"]
        self.assertEqual(fields["telemetry_events"]["before"], None)
        self.assertEqual(fields["telemetry_events"]["after"], 1)

    def test_multiple_late_events_accumulate_and_include_together(self):
        self.release()
        self.close()
        self.submit_meter(key="m1", identifier="obs-1", run=8, down=1)
        self.submit_ticket(key="t-late", identifier="case-late", at="2026-10-21T12:00:00Z")
        pending = self.settlements.pending("ES")
        self.assertEqual([item["source"] for item in pending], ["telemetry", "tickets"])
        candidate = self.publication.prepare("ES", DAY, DAY, 1)
        published = self.publication.publish(candidate["id"], 1, candidate["source_revision"])
        self.assertEqual(published["late_included"], 2)
        self.assertEqual(self.settlements.pending("ES"), [])
        detail = self.settlements.list_late(self.window["id"])
        self.assertEqual({row["status"] for row in detail["late_arrivals"]}, {"included"})
