"""跨市场设备服务质量分析：reviews模块。"""
import json
import uuid
from collections import defaultdict
from .analytics import days_between
from .catalog import instant, market_day
from .errors import require
from .policies import Policies
from .store import Store, now


class Reviews:
    def __init__(self, store: Store):
        self.store = store

    def inspect(self, market: str, from_day: str, to_day: str) -> dict:
        days = set(days_between(from_day, to_day))
        with self.store.read() as db:
            zone = db.execute("SELECT timezone FROM markets WHERE code=?", (market,)).fetchone()
            require(zone is not None, "unknown_market", "market unknown", 404)
            cases = db.execute("SELECT t.*,d.product FROM tickets t JOIN devices d ON d.id=t.device_id WHERE d.market=? ORDER BY t.opened_at,t.id", (market,)).fetchall()
            history = defaultdict(list)
            for row in db.execute("SELECT h.* FROM ticket_history h JOIN tickets t ON t.id=h.ticket_id JOIN devices d ON d.id=t.device_id WHERE d.market=? ORDER BY h.ticket_id,h.revision", (market,)):
                history[row["ticket_id"]].append(row)
            rows = []
            for ticket in cases:
                day = market_day(ticket["opened_at"], zone[0])
                if day not in days:
                    continue
                policy = Policies.applicable(db, market, ticket["opened_at"])
                actions = history[ticket["id"]]
                first_response = next((r["event_at"] for r in actions if r["action"] == "acknowledge"), None)
                first_close = next((r["event_at"] for r in actions if r["action"] == "close"), None)
                response_elapsed = (instant(first_response) - instant(ticket["opened_at"])).total_seconds() / 3600 if first_response else None
                closure_elapsed = (instant(first_close) - instant(ticket["opened_at"])).total_seconds() / 3600 if first_close else None
                rows.append({"ticket_id": ticket["id"], "device_id": ticket["device_id"], "product": ticket["product"], "day": day, "severity": ticket["severity"], "policy_version": policy["version"] if policy else None, "response_hours": round(response_elapsed, 6) if response_elapsed is not None else None, "closure_hours": round(closure_elapsed, 6) if closure_elapsed is not None else None, "response_target": policy["response_hours"] if policy else None, "closure_target": policy["closure_hours"] if policy else None, "response_met": response_elapsed <= policy["response_hours"] if policy and response_elapsed is not None else None, "closure_met": closure_elapsed <= policy["closure_hours"] if policy and closure_elapsed is not None else None})
            totals = {"tickets": len(rows), "with_policy": sum(row["policy_version"] is not None for row in rows), "responded": sum(row["response_hours"] is not None for row in rows), "closed": sum(row["closure_hours"] is not None for row in rows), "response_met": sum(row["response_met"] is True for row in rows), "closure_met": sum(row["closure_met"] is True for row in rows)}
            return {"market": market, "from_day": from_day, "to_day": to_day, "source_revision": self.store.revision(db), "totals": totals, "rows": rows}

    def save(self, market: str, from_day: str, to_day: str, expected_source_revision: int) -> dict:
        report = self.inspect(market, from_day, to_day)
        require(report["source_revision"] == expected_source_revision, "stale_source", "source revision changed", 409)
        identifier = uuid.uuid4().hex
        with self.store.transaction() as db:
            require(self.store.revision(db) == expected_source_revision, "stale_source", "source revision changed", 409)
            db.execute("INSERT INTO service_reviews VALUES(?,?,?,?,?,?,?)", (identifier, market, from_day, to_day, expected_source_revision, json.dumps(report, sort_keys=True), now()))
            self.store.audit(db, "service_review_saved", identifier, {"market": market, "source_revision": expected_source_revision})
        return {"id": identifier, **report}

    def read(self, identifier: str) -> dict:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM service_reviews WHERE id=?", (identifier,)).fetchone()
            require(row is not None, "unknown_review", "service review unknown", 404)
            return {"id": identifier, "created_at": row["created_at"], **json.loads(row["payload"])}
