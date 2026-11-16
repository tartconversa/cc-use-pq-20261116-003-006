from brand_signal.errors import DomainError
from brand_signal.policies import Policies
from brand_signal.reviews import Reviews
from support import Scenario


class ServiceTests(Scenario):
    def test_ticket_sequence_and_history(self):
        self.submit_ticket()
        self.service.transition("ticket-1", "acknowledge", "2026-10-21T00:00:00Z", 1, "received")
        self.service.transition("ticket-1", "resolve", "2026-10-21T01:00:00Z", 2, "fixed")
        self.service.transition("ticket-1", "close", "2026-10-21T02:00:00Z", 3, "customer confirmed")
        ticket = self.service.ticket("ticket-1")
        self.assertEqual(ticket["state"], "closed")
        self.assertEqual([r["action"] for r in ticket["history"]], ["open", "acknowledge", "resolve", "close"])

    def test_stale_ticket_transition(self):
        self.submit_ticket()
        self.service.transition("ticket-1", "acknowledge", "2026-10-21T00:00:00Z", 1)
        with self.assertRaises(DomainError) as error:
            self.service.transition("ticket-1", "resolve", "2026-10-21T01:00:00Z", 1)
        self.assertEqual(error.exception.code, "stale_ticket")

    def test_invalid_state_transition(self):
        self.submit_ticket()
        with self.assertRaises(DomainError) as error:
            self.service.transition("ticket-1", "close", "2026-10-21T00:00:00Z", 1)
        self.assertEqual(error.exception.code, "invalid_transition")

    def test_out_of_order_transition(self):
        self.submit_ticket()
        with self.assertRaises(DomainError) as error:
            self.service.transition("ticket-1", "acknowledge", "2026-10-20T20:00:00Z", 1)
        self.assertEqual(error.exception.code, "out_of_order")

    def test_close_can_reopen(self):
        self.submit_ticket()
        self.service.transition("ticket-1", "acknowledge", "2026-10-21T00:00:00Z", 1)
        self.service.transition("ticket-1", "resolve", "2026-10-21T01:00:00Z", 2)
        self.service.transition("ticket-1", "close", "2026-10-21T02:00:00Z", 3)
        result = self.service.transition("ticket-1", "reopen", "2026-10-21T03:00:00Z", 4)
        self.assertIsNone(result["closed_at"])
        self.assertEqual(result["state"], "open")

    def test_cancelled_ticket_is_terminal(self):
        self.submit_ticket()
        self.service.transition("ticket-1", "cancel", "2026-10-21T00:00:00Z", 1)
        with self.assertRaises(DomainError) as error:
            self.service.transition("ticket-1", "reopen", "2026-10-21T01:00:00Z", 2)
        self.assertEqual(error.exception.code, "invalid_transition")

    def test_policy_applies_by_open_time(self):
        policies = Policies(self.store)
        policies.register("ES", "2026-10-01T00:00:00Z", 4, 48, 0)
        self.submit_ticket()
        self.service.transition("ticket-1", "acknowledge", "2026-10-21T00:30:00Z", 1)
        self.service.transition("ticket-1", "resolve", "2026-10-21T01:00:00Z", 2)
        self.service.transition("ticket-1", "close", "2026-10-21T02:00:00Z", 3)
        report = Reviews(self.store).inspect("ES", "2026-10-21", "2026-10-21")
        self.assertEqual(report["totals"]["response_met"], 1)
        self.assertEqual(report["totals"]["closure_met"], 1)
        self.assertEqual(report["rows"][0]["policy_version"], 1)

    def test_policy_revisions_do_not_rewrite_ticket_open(self):
        policies = Policies(self.store)
        policies.register("ES", "2026-10-01T00:00:00Z", 1, 12, 0)
        self.submit_ticket()
        policies.register("ES", "2026-10-22T00:00:00Z", 2, 24, 1)
        report = Reviews(self.store).inspect("ES", "2026-10-21", "2026-10-21")
        self.assertEqual(report["rows"][0]["response_target"], 1)

    def test_policy_cas_conflict(self):
        policies = Policies(self.store)
        policies.register("ES", "2026-10-01T00:00:00Z", 2, 24, 0)
        with self.assertRaises(DomainError) as error:
            policies.register("ES", "2026-10-22T00:00:00Z", 2, 24, 0)
        self.assertEqual(error.exception.code, "stale_policy")

    def test_review_saved_is_immutable_after_late_action(self):
        self.submit_ticket()
        review = Reviews(self.store)
        revision = review.inspect("ES", "2026-10-21", "2026-10-21")["source_revision"]
        saved = review.save("ES", "2026-10-21", "2026-10-21", revision)
        self.service.transition("ticket-1", "acknowledge", "2026-10-21T00:30:00Z", 1)
        self.assertEqual(review.read(saved["id"])["totals"]["responded"], 0)
        self.assertEqual(review.inspect("ES", "2026-10-21", "2026-10-21")["totals"]["responded"], 1)

    def test_review_save_requires_current_revision(self):
        self.submit_ticket()
        with self.assertRaises(DomainError) as error:
            Reviews(self.store).save("ES", "2026-10-21", "2026-10-21", 0)
        self.assertEqual(error.exception.code, "stale_source")
