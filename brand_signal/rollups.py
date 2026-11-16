"""跨市场设备服务质量分析：rollups模块。"""
import json
from collections import defaultdict
from .errors import require
from .publication import Publication
from .store import Store


SUM_FIELDS = ("installed_devices", "active_devices", "exposure_hours", "ticket_opened", "ticket_closed", "ticket_open_at_end", "critical_opened", "acknowledged", "response_hours_total", "telemetry_events", "devices_reporting", "run_hours", "downtime_hours")


class Rollups:
    def __init__(self, store: Store):
        self.store = store
        self.publication = Publication(store)

    def build(self, market: str, version: int) -> dict:
        release = self.publication.read(market, version)
        by_day = defaultdict(lambda: {key: 0 for key in SUM_FIELDS})
        product_count = defaultdict(int)
        for row in release["rows"]:
            totals = by_day[row["day"]]
            for key in SUM_FIELDS:
                totals[key] += row[key]
            product_count[row["day"]] += 1
        items = []
        for day in sorted(by_day):
            totals = by_day[day]
            observed = totals["run_hours"] + totals["downtime_hours"]
            items.append({"market": market, "version": version, "day": day, "products": product_count[day], **totals, "ticket_rate_per_100_devices": 100 * totals["ticket_opened"] / totals["active_devices"] if totals["active_devices"] else None, "coverage": totals["devices_reporting"] / totals["active_devices"] if totals["active_devices"] else None, "downtime_share": totals["downtime_hours"] / observed if observed else None})
        with self.store.transaction() as db:
            require(db.execute("SELECT 1 FROM publications WHERE market=? AND version=?", (market, version)).fetchone(), "unknown_publication", "publication unknown", 404)
            existing = db.execute("SELECT count(*) FROM market_rollups WHERE market=? AND version=?", (market, version)).fetchone()[0]
            require(existing in (0, len(items)), "partial_rollup", "materialization is incomplete", 409)
            if existing:
                stored = [json.loads(r[0]) for r in db.execute("SELECT payload FROM market_rollups WHERE market=? AND version=? ORDER BY day", (market, version))]
                require(stored == items, "rollup_conflict", "materialized totals disagree with release", 409)
                return {"market": market, "version": version, "days": len(items), "replay": True}
            for item in items:
                db.execute("INSERT INTO market_rollups VALUES(?,?,?,?)", (market, version, item["day"], json.dumps(item, sort_keys=True)))
            self.store.audit(db, "rollup_built", market, {"version": version, "days": len(items)})
            return {"market": market, "version": version, "days": len(items), "replay": False}

    def read(self, market: str, version: int) -> list[dict]:
        self.publication.read(market, version)
        with self.store.read() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM market_rollups WHERE market=? AND version=? ORDER BY day", (market, version))]
