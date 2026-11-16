"""跨市场设备服务质量分析：alerts模块。"""
import json
import uuid
from .catalog import market_day
from .errors import require
from .reconciliation import Reconciliation
from .store import Store, now


class Alerts:
    def __init__(self, store: Store):
        self.store = store
        self.reconciliation = Reconciliation(store)

    def evaluate(self, market: str, from_day: str, to_day: str, expected_source_revision: int) -> dict:
        report = self.reconciliation.inspect(market, from_day, to_day)
        require(report["source_revision"] == expected_source_revision, "stale_source", "source revision changed", 409)
        alerts = []
        for issue in report["issues"]:
            severity = "high" if issue["kind"] in {"ticket_without_meter", "case_load_exceeds_fleet"} else "medium"
            alerts.append({"source": "reconciliation", "day": issue["day"], "kind": issue["kind"], "severity": severity, "payload": issue})
        with self.store.transaction() as db:
            require(self.store.revision(db) == expected_source_revision, "stale_source", "source revision changed", 409)
            for entry in alerts:
                identifier = uuid.uuid4().hex
                db.execute("INSERT INTO quality_alerts VALUES(?,?,?,?,?,?,?,?)", (identifier, market, entry["source"], entry["day"], entry["kind"], entry["severity"], json.dumps(entry["payload"], sort_keys=True), now()))
                entry["id"] = identifier
            self.store.audit(db, "alerts_evaluated", market, {"count": len(alerts), "source_revision": expected_source_revision})
        return {"market": market, "source_revision": expected_source_revision, "alerts": alerts}

    def list(self, market: str, kind: str | None = None) -> list[dict]:
        with self.store.read() as db:
            require(db.execute("SELECT 1 FROM markets WHERE code=?", (market,)).fetchone(), "unknown_market", "market unknown", 404)
            query = "SELECT * FROM quality_alerts WHERE market=?"
            parameters = [market]
            if kind:
                query += " AND kind=?"
                parameters.append(kind)
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in db.execute(query + " ORDER BY day,id", parameters)]
