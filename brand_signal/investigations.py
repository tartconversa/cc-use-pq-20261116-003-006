"""跨市场设备服务质量分析：investigations模块。"""
import uuid
from .errors import require
from .store import Store, now


TRANSITIONS = {"open": {"assign": "assigned", "dismiss": "dismissed"}, "assigned": {"resolve": "resolved", "escalate": "escalated", "reassign": "assigned"}, "escalated": {"resolve": "resolved", "reassign": "assigned"}, "resolved": {"reopen": "open"}, "dismissed": {"reopen": "open"}}


class Investigations:
    def __init__(self, store: Store):
        self.store = store

    def open(self, alert_id: str, owner: str) -> dict:
        require(isinstance(owner, str) and 1 <= len(owner.strip()) <= 80, "invalid_owner", "owner required")
        identifier = uuid.uuid4().hex
        with self.store.transaction() as db:
            alert = db.execute("SELECT * FROM quality_alerts WHERE id=?", (alert_id,)).fetchone()
            require(alert is not None, "unknown_alert", "alert unknown", 404)
            require(not db.execute("SELECT 1 FROM investigations WHERE alert_id=? AND state NOT IN ('dismissed','resolved')", (alert_id,)).fetchone(), "active_investigation", "alert already being investigated", 409)
            timestamp = now()
            db.execute("INSERT INTO investigations VALUES(?,?,?,?,?,?,?)", (identifier, alert["market"], alert_id, "open", owner.strip(), timestamp, 1))
            db.execute("INSERT INTO investigation_history VALUES(?,?,?,?,?,?)", (identifier, 1, "open", timestamp, owner.strip(), ""))
            self.store.audit(db, "investigation_opened", identifier, {"alert": alert_id, "owner": owner.strip()})
            return {"id": identifier, "alert_id": alert_id, "market": alert["market"], "state": "open", "owner": owner.strip(), "revision": 1}

    def transition(self, identifier: str, action: str, expected_revision: int, owner: str, note: str) -> dict:
        require(isinstance(owner, str) and 1 <= len(owner.strip()) <= 80, "invalid_owner", "owner required")
        require(isinstance(note, str) and len(note) <= 2000, "invalid_note", "note too long")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM investigations WHERE id=?", (identifier,)).fetchone()
            require(row is not None, "unknown_investigation", "investigation unknown", 404)
            require(row["revision"] == expected_revision, "stale_investigation", "investigation changed", 409)
            target = TRANSITIONS[row["state"]].get(action)
            require(target is not None, "invalid_transition", "investigation action unavailable", 409)
            if action in {"resolve", "dismiss"}:
                require(bool(note.strip()), "reason_required", "resolution reason required")
            revision = expected_revision + 1
            timestamp = now()
            db.execute("UPDATE investigations SET state=?,owner=?,revision=? WHERE id=?", (target, owner.strip(), revision, identifier))
            db.execute("INSERT INTO investigation_history VALUES(?,?,?,?,?,?)", (identifier, revision, action, timestamp, owner.strip(), note.strip()))
            self.store.audit(db, "investigation_transition", identifier, {"from": row["state"], "to": target, "owner": owner.strip()})
            return {"id": identifier, "state": target, "owner": owner.strip(), "revision": revision}

    def get(self, identifier: str) -> dict:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM investigations WHERE id=?", (identifier,)).fetchone()
            require(row is not None, "unknown_investigation", "investigation unknown", 404)
            history = db.execute("SELECT * FROM investigation_history WHERE investigation_id=? ORDER BY revision", (identifier,)).fetchall()
            return {**dict(row), "history": [dict(item) for item in history]}

    def list(self, market: str, state: str | None = None) -> list[dict]:
        with self.store.read() as db:
            require(db.execute("SELECT 1 FROM markets WHERE code=?", (market,)).fetchone(), "unknown_market", "market unknown", 404)
            query = "SELECT * FROM investigations WHERE market=?"
            params = [market]
            if state is not None:
                require(state in TRANSITIONS, "invalid_state", "investigation state unknown")
                query += " AND state=?"
                params.append(state)
            return [dict(row) for row in db.execute(query + " ORDER BY opened_at,id", params)]
