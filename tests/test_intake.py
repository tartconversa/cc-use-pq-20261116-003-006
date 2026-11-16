from brand_signal.errors import DomainError
from brand_signal.contracts import Contracts
from brand_signal.mappings import Mappings
from support import Scenario


class IntakeTests(Scenario):
    def test_accept_and_reject_are_isolated(self):
        result = self.intake.submit("tickets", "mixed", [self.ticket(), self.ticket(key="bad", identifier="bad", device="absent")])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["rows"][1]["reason"], "unknown_device")
        self.assertEqual(self.service.ticket("ticket-1")["state"], "open")

    def test_same_request_replays_without_new_revision(self):
        rows = [self.ticket()]
        first = self.intake.submit("tickets", "stable", rows)
        revision = self.analytics.live("ES", "2026-10-21", "2026-10-21")["source_revision"]
        second = self.intake.submit("tickets", "stable", rows)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["replay"])
        self.assertEqual(revision, self.analytics.live("ES", "2026-10-21", "2026-10-21")["source_revision"])

    def test_request_key_payload_conflict(self):
        self.intake.submit("tickets", "stable", [self.ticket()])
        with self.assertRaises(DomainError) as error:
            self.intake.submit("tickets", "stable", [self.ticket(severity="critical")])
        self.assertEqual(error.exception.code, "request_conflict")

    def test_event_key_conflict_across_requests(self):
        self.intake.submit("tickets", "one", [self.ticket()])
        second = self.intake.submit("tickets", "two", [self.ticket(identifier="other")])
        self.assertEqual(second["rows"][0]["reason"], "event_conflict")

    def test_duplicate_event_key_inside_batch(self):
        rows = [self.ticket(), self.ticket(identifier="other")]
        result = self.intake.submit("tickets", "dup", rows)
        self.assertEqual(result["rows"][1]["reason"], "duplicate_in_batch")

    def test_preview_does_not_change_source(self):
        before = self.analytics.live("ES", "2026-10-21", "2026-10-21")["source_revision"]
        preview = self.intake.preview("tickets", [self.ticket(), self.ticket(key="other", identifier="other", device="bad")])
        self.assertEqual(preview["ready"], 1)
        self.assertEqual(before, self.analytics.live("ES", "2026-10-21", "2026-10-21")["source_revision"])

    def test_ticket_id_conflict_is_distinct_from_event_key(self):
        self.intake.submit("tickets", "one", [self.ticket()])
        result = self.intake.submit("tickets", "two", [self.ticket(key="new-key")])
        self.assertEqual(result["rows"][0]["reason"], "ticket_exists")

    def test_bad_meter_does_not_rollback_good_meter(self):
        result = self.intake.submit("telemetry", "meters", [self.meter(), self.meter(key="bad", identifier="bad", run=25)])
        self.assertEqual([r["status"] for r in result["rows"]], ["accepted", "rejected"])
        self.assertEqual(len(self.telemetry.list_for_device("device-1")), 1)

    def test_meter_sum_cannot_exceed_day(self):
        result = self.submit_meter(run=22, down=3)
        self.assertEqual(result["rows"][0]["reason"], "invalid_hours")

    def test_boolean_hours_rejected(self):
        result = self.submit_meter(run=True)
        self.assertEqual(result["rows"][0]["reason"], "invalid_hours")

    def test_batch_replay_retains_row_results(self):
        rows = [self.ticket(), {"event_key": "missing", "id": "ticket-2", "device_id": "absent", "opened_at": "2026-10-21T00:00:00Z"}]
        original = self.intake.submit("tickets", "key", rows)
        replay = self.intake.submit("tickets", "key", rows)
        self.assertEqual(original["rows"], replay["rows"])
        self.assertEqual(self.intake.batch(original["id"])["rejected"], 1)

    def test_contract_alias_normalizes_before_business_validation(self):
        contracts = Contracts(self.store)
        contracts.register("tickets", "2026-10-01T00:00:00Z", ["event_key", "id", "device_id", "opened_at"], {"case_time": "opened_at"}, 0)
        row = self.ticket()
        row["case_time"] = row.pop("opened_at")
        result = self.intake.submit("tickets", "alias", [row])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(contracts.trace("tickets", "event-1")["record_id"], "ticket-1")

    def test_ambiguous_alias_rejected_per_row(self):
        Contracts(self.store).register("tickets", "2026-10-01T00:00:00Z", ["event_key", "id", "device_id", "opened_at"], {"case_time": "opened_at"}, 0)
        row = self.ticket()
        row["case_time"] = row["opened_at"]
        result = self.intake.submit("tickets", "ambiguous", [row])
        self.assertEqual(result["rows"][0]["reason"], "ambiguous_field")

    def test_contract_cas(self):
        contracts = Contracts(self.store)
        contracts.register("tickets", "2026-10-01T00:00:00Z", ["event_key", "id", "device_id", "opened_at"], {}, 0)
        with self.assertRaises(DomainError) as error:
            contracts.register("tickets", "2026-10-10T00:00:00Z", ["event_key", "id", "device_id", "opened_at"], {}, 0)
        self.assertEqual(error.exception.code, "stale_contract")

    def test_trace_preserves_original_payload(self):
        result = self.submit_ticket()
        trace = Contracts(self.store).trace("tickets", "event-1")
        self.assertEqual(trace["batch_id"], result["id"])
        self.assertEqual(trace["payload"]["severity"], "normal")

    def test_external_device_mapping_resolves_for_meter(self):
        mappings = Mappings(self.store)
        mappings.register("telemetry", "partner-serial", "device-1", "2026-10-01T00:00:00Z")
        row = self.meter()
        del row["device_id"]
        row["external_device_id"] = "partner-serial"
        accepted = self.intake.submit("telemetry", "mapped", [row])
        self.assertEqual(accepted["accepted"], 1)
        self.assertEqual(self.telemetry.list_for_device("device-1")[0]["id"], "meter-1")

    def test_unmapped_external_device_is_row_rejection(self):
        row = self.meter()
        del row["device_id"]
        row["external_device_id"] = "unknown-partner"
        result = self.intake.submit("telemetry", "unmapped", [row])
        self.assertEqual(result["rows"][0]["reason"], "unmapped_device")

    def test_external_and_canonical_device_are_ambiguous(self):
        row = self.meter()
        row["external_device_id"] = "partner-serial"
        result = self.intake.submit("telemetry", "ambiguous-id", [row])
        self.assertEqual(result["rows"][0]["reason"], "ambiguous_device")

    def test_mapping_overlap_is_rejected(self):
        mappings = Mappings(self.store)
        mappings.register("telemetry", "partner-serial", "device-1", "2026-10-01T00:00:00Z")
        with self.assertRaises(DomainError) as error:
            mappings.register("telemetry", "partner-serial", "device-1", "2026-10-20T00:00:00Z")
        self.assertEqual(error.exception.code, "mapping_overlap")

    def test_mapping_version_follows_event_time(self):
        mappings = Mappings(self.store)
        mappings.register("telemetry", "partner-serial", "device-1", "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")
        mappings.register("telemetry", "partner-serial", "device-1", "2026-10-20T00:00:00Z")
        self.assertEqual(len(mappings.list("telemetry", "partner-serial")), 2)
        row = self.meter()
        del row["device_id"]
        row["external_device_id"] = "partner-serial"
        self.assertEqual(self.intake.submit("telemetry", "mapped-late", [row])["accepted"], 1)
