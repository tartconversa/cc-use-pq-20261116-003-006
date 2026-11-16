"""跨市场设备服务质量分析：comparison模块。"""
from collections import defaultdict
from .errors import require
from .publication import Publication
from .store import Store


class Comparison:
    def __init__(self, store: Store):
        self.store = store
        self.publication = Publication(store)

    def compare(self, selections: list[dict], product: str, from_day: str, to_day: str) -> dict:
        require(isinstance(selections, list) and 2 <= len(selections) <= 20, "invalid_selection", "select 2..20 market versions")
        require(isinstance(product, str) and product, "invalid_product", "product required")
        markets = [item.get("market") for item in selections if isinstance(item, dict)]
        require(len(markets) == len(selections) and len(set(markets)) == len(markets), "invalid_selection", "markets must be distinct")
        results = []
        for selected in selections:
            require(type(selected.get("version")) is int and selected["version"] > 0, "invalid_selection", "positive version required")
            release = self.publication.read(selected["market"], selected["version"])
            rows = [row for row in release["rows"] if row["product"] == product and from_day <= row["day"] <= to_day]
            require(rows, "missing_product", "selection has no product rows")
            totals = defaultdict(float)
            reporting_days = 0
            for row in rows:
                for field in ("active_devices", "exposure_hours", "ticket_opened", "ticket_closed", "critical_opened", "telemetry_events", "devices_reporting", "run_hours", "downtime_hours"):
                    totals[field] += row[field]
                reporting_days += bool(row["devices_reporting"])
            results.append({"market": selected["market"], "version": selected["version"], "source_revision": release["source_revision"], "days": len(rows), "reporting_days": reporting_days, "ticket_rate_per_100_device_days": 100 * totals["ticket_opened"] / totals["active_devices"] if totals["active_devices"] else None, "downtime_share": totals["downtime_hours"] / (totals["run_hours"] + totals["downtime_hours"]) if totals["run_hours"] + totals["downtime_hours"] else None, "totals": dict(totals)})
        return {"product": product, "from_day": from_day, "to_day": to_day, "markets": results}
