"""跨市场设备服务质量分析：annotations模块。"""
import uuid
from .analytics import days_between
from .catalog import Catalog
from .errors import require
from .store import Store, now


CATEGORIES = {"service", "meter", "installation", "context"}


class Annotations:
    def __init__(self, store: Store):
        self.store = store

    def create(self, market: str, device_id: str, day: str, category: str, text: str) -> dict:
        list(days_between(day, day))
        require(category in CATEGORIES, "invalid_category", "annotation category unknown")
        require(isinstance(text, str) and 1 <= len(text.strip()) <= 2000, "invalid_annotation", "annotation text required")
        identifier = uuid.uuid4().hex
        with self.store.transaction() as db:
            device = Catalog.device(db, device_id)
            require(device["market"] == market, "wrong_market", "device belongs to another market")
            created = now()
            db.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?)", (identifier, market, device_id, day, category, text.strip(), 1, created))
            self.store.audit(db, "annotation_created", identifier, {"device": device_id, "day": day})
            return {"id": identifier, "market": market, "device_id": device_id, "day": day, "category": category, "text": text.strip(), "revision": 1, "created_at": created}

    def replace(self, identifier: str, text: str, expected_revision: int) -> dict:
        require(isinstance(text, str) and 1 <= len(text.strip()) <= 2000, "invalid_annotation", "annotation text required")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM annotations WHERE id=?", (identifier,)).fetchone()
            require(row is not None, "unknown_annotation", "annotation unknown", 404)
            require(row["revision"] == expected_revision, "stale_annotation", "annotation revision changed", 409)
            db.execute("UPDATE annotations SET text=?,revision=revision+1 WHERE id=?", (text.strip(), identifier))
            self.store.audit(db, "annotation_replaced", identifier, {"old_revision": expected_revision, "old_text": row["text"], "new_text": text.strip()})
            return {**dict(row), "text": text.strip(), "revision": expected_revision + 1}

    def list(self, market: str, day: str | None = None, device_id: str | None = None) -> list[dict]:
        with self.store.read() as db:
            require(db.execute("SELECT 1 FROM markets WHERE code=?", (market,)).fetchone(), "unknown_market", "market unknown", 404)
            query = "SELECT * FROM annotations WHERE market=?"
            params = [market]
            if day:
                list(days_between(day, day))
                query += " AND day=?"
                params.append(day)
            if device_id:
                query += " AND device_id=?"
                params.append(device_id)
            return [dict(row) for row in db.execute(query + " ORDER BY day,device_id,id", params)]
