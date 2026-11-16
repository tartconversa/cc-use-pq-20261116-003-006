from brand_signal.errors import DomainError
from brand_signal.rollups import Rollups
from brand_signal.sampling import Sampling
from brand_signal.lineage import Lineage
from brand_signal.comparison import Comparison
from support import Scenario


class PublicationTests(Scenario):
    def test_candidate_freezes_rows(self):
        self.submit_ticket()
        candidate = self.publication.prepare("ES", "2026-10-21", "2026-10-21", 0)
        snapshot = self.publication.candidate(candidate["id"])
        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["rows"][0]["ticket_opened"], 1)

    def test_late_source_invalidates_candidate(self):
        candidate = self.publication.prepare("ES", "2026-10-21", "2026-10-21", 0)
        self.submit_ticket()
        self.assertFalse(self.publication.candidate(candidate["id"])["ready"])
        with self.assertRaises(DomainError) as error:
            self.publication.publish(candidate["id"], 0, candidate["source_revision"])
        self.assertEqual(error.exception.code, "stale_source")

    def test_head_cas_rejects_second_candidate(self):
        first = self.publication.prepare("ES", "2026-10-21", "2026-10-21", 0)
        second = self.publication.prepare("ES", "2026-10-21", "2026-10-21", 0)
        self.publication.publish(first["id"], 0, first["source_revision"])
        with self.assertRaises(DomainError) as error:
            self.publication.publish(second["id"], 0, second["source_revision"])
        self.assertEqual(error.exception.code, "stale_head")

    def test_published_candidate_cannot_publish_twice(self):
        candidate = self.publication.prepare("ES", "2026-10-21", "2026-10-21", 0)
        self.publication.publish(candidate["id"], 0, candidate["source_revision"])
        with self.assertRaises(DomainError) as error:
            self.publication.publish(candidate["id"], 0, candidate["source_revision"])
        self.assertEqual(error.exception.code, "candidate_not_prepared")

    def test_discarded_candidate_cannot_publish(self):
        candidate = self.publication.prepare("ES", "2026-10-21", "2026-10-21", 0)
        self.publication.discard(candidate["id"])
        with self.assertRaises(DomainError) as error:
            self.publication.publish(candidate["id"], 0, candidate["source_revision"])
        self.assertEqual(error.exception.code, "candidate_not_prepared")

    def test_older_version_unchanged_by_late_meter(self):
        self.submit_meter()
        self.release()
        before = self.publication.read("ES", 1)
        self.submit_meter(key="m2", identifier="m2", run=4, down=1)
        self.release(expected_head=1)
        self.assertEqual(self.publication.read("ES", 1), before)
        changes = self.publication.diff("ES", 1, 2)["changes"]
        self.assertEqual(changes[0]["before"]["telemetry_events"], 1)
        self.assertEqual(changes[0]["after"]["telemetry_events"], 2)

    def test_csv_reads_frozen_version(self):
        self.submit_ticket()
        self.release()
        content = self.publication.export_csv("ES", 1)
        self.assertIn("ticket_opened", content)
        self.assertIn("2026-10-21", content)

    def test_rollup_materialization_replays(self):
        self.submit_ticket()
        self.release()
        rollups = Rollups(self.store)
        first = rollups.build("ES", 1)
        second = rollups.build("ES", 1)
        self.assertFalse(first["replay"])
        self.assertTrue(second["replay"])
        self.assertEqual(rollups.read("ES", 1)[0]["ticket_opened"], 1)

    def test_sample_is_deterministic(self):
        self.release()
        sampler = Sampling(self.store)
        first = sampler.select("ES", 1, 1, "audit-seed")
        second = sampler.select("ES", 1, 1, "audit-seed")
        self.assertEqual(first, second)
        self.assertEqual(first["samples"][0]["device_count"], 1)

    def test_lineage_traces_source_batch(self):
        batch = self.submit_ticket()
        self.release()
        explanation = Lineage(self.store).explain("ES", 1, "2026-10-21", "dryer")
        self.assertEqual(explanation["source_rows"][0]["origin_batch"], batch["id"])

    def test_cross_market_comparison_requires_distinct_markets(self):
        self.release()
        with self.assertRaises(DomainError) as error:
            Comparison(self.store).compare([{"market": "ES", "version": 1}, {"market": "ES", "version": 1}], "dryer", "2026-10-21", "2026-10-21")
        self.assertEqual(error.exception.code, "invalid_selection")

    def test_source_revision_does_not_change_on_publication(self):
        before = self.analytics.live("ES", "2026-10-21", "2026-10-21")["source_revision"]
        self.release()
        after = self.analytics.live("ES", "2026-10-21", "2026-10-21")["source_revision"]
        self.assertEqual(before, after)
