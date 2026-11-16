import unittest
from brand_signal.catalog import Catalog, instant, iso, market_day
from brand_signal.errors import DomainError
from support import Scenario


class CatalogTests(Scenario):
    def test_market_and_product_are_unique(self):
        with self.assertRaises(DomainError) as error:
            self.catalog.add_market("ES", "Europe/Madrid")
        self.assertEqual(error.exception.code, "market_exists")
        with self.assertRaises(DomainError) as error:
            self.catalog.add_product("dryer", "Another")
        self.assertEqual(error.exception.code, "product_exists")

    def test_invalid_timezone_is_rejected(self):
        with self.assertRaises(DomainError) as error:
            self.catalog.add_market("XX", "Solar/Unknown")
        self.assertEqual(error.exception.code, "invalid_timezone")

    def test_install_requires_known_market(self):
        with self.assertRaises(DomainError) as error:
            self.catalog.install("d2", "ZZ", "dryer", "2026-10-01T00:00:00Z")
        self.assertEqual(error.exception.code, "unknown_market")

    def test_install_requires_known_product(self):
        with self.assertRaises(DomainError) as error:
            self.catalog.install("d2", "ES", "missing", "2026-10-01T00:00:00Z")
        self.assertEqual(error.exception.code, "unknown_product")

    def test_install_duplicate_device(self):
        with self.assertRaises(DomainError) as error:
            self.catalog.install("device-1", "ES", "dryer", "2026-10-01T00:00:00Z")
        self.assertEqual(error.exception.code, "device_exists")

    def test_retirement_cas_and_lifecycle(self):
        result = self.catalog.retire("device-1", "2026-10-22T00:00:00Z", 1)
        self.assertEqual(result["revision"], 2)
        with self.assertRaises(DomainError) as error:
            self.catalog.retire("device-1", "2026-10-23T00:00:00Z", 1)
        self.assertEqual(error.exception.code, "stale_device")
        with self.assertRaises(DomainError) as error:
            self.catalog.retire("device-1", "2026-10-23T00:00:00Z", 2)
        self.assertEqual(error.exception.code, "already_retired")

    def test_retirement_must_follow_install(self):
        with self.assertRaises(DomainError) as error:
            self.catalog.retire("device-1", "2026-09-30T00:00:00Z", 1)
        self.assertEqual(error.exception.code, "invalid_lifecycle")

    def test_device_lifecycle_bounds_affect_intake(self):
        self.catalog.retire("device-1", "2026-10-22T00:00:00Z", 1)
        first = self.submit_meter(at="2026-09-30T23:59:59Z")
        self.assertEqual(first["rows"][0]["reason"], "outside_lifecycle")
        second = self.intake.submit("telemetry", "after-retire", [self.meter(at="2026-10-22T00:00:00Z")])
        self.assertEqual(second["rows"][0]["reason"], "outside_lifecycle")

    def test_local_day_uses_market_zone(self):
        self.assertEqual(market_day("2026-10-20T22:30:00Z", "Europe/Madrid"), "2026-10-21")
        self.assertEqual(market_day("2026-10-20T21:30:00Z", "Europe/Madrid"), "2026-10-20")

    def test_instant_requires_explicit_offset(self):
        with self.assertRaises(DomainError) as error:
            instant("2026-10-21T00:00:00")
        self.assertEqual(error.exception.code, "invalid_time")
        self.assertEqual(iso("2026-10-21T02:00:00+02:00"), "2026-10-21T00:00:00+00:00")

    def test_catalog_revision_changes_source_revision(self):
        before = self.analytics.live("ES", "2026-10-21", "2026-10-21")["source_revision"]
        self.catalog.install("device-2", "ES", "dryer", "2026-10-01T00:00:00Z")
        after = self.analytics.live("ES", "2026-10-21", "2026-10-21")["source_revision"]
        self.assertEqual(after, before + 1)

    def test_device_listing_is_scoped(self):
        self.catalog.add_market("US", "America/New_York")
        self.catalog.install("device-us", "US", "dryer", "2026-10-01T00:00:00Z")
        self.assertEqual([r["id"] for r in self.catalog.list_devices("ES")], ["device-1"])
        self.assertEqual(len(self.catalog.list_devices()), 2)
