"""跨市场设备服务质量分析：mappings模块。"""
from .catalog import Catalog, iso, instant
from .errors import require
from .store import Store


class Mappings:
    def __init__(self, store: Store):
        self.store = store

    def register(self, source: str, external_id: str, device_id: str, valid_from: str, valid_to: str | None = None) -> dict:
        require(source in {"tickets", "telemetry"}, "invalid_source", "source unknown")
        require(isinstance(external_id, str) and 1 <= len(external_id) <= 100, "invalid_external_id", "external id required")
        start = iso(valid_from)
        stop = iso(valid_to) if valid_to else None
        require(stop is None or instant(stop) > instant(start), "invalid_interval", "mapping end must follow start")
        with self.store.transaction() as db:
            Catalog.device(db, device_id)
            existing = db.execute("SELECT * FROM source_mappings WHERE source=? AND external_id=?", (source, external_id)).fetchall()
            for row in existing:
                old_start = instant(row["valid_from"])
                old_stop = instant(row["valid_to"]) if row["valid_to"] else None
                require(not (old_stop is None or instant(start) < old_stop) or (stop is not None and instant(stop) <= old_start), "mapping_overlap", "external id already mapped in interval", 409)
            db.execute("INSERT INTO source_mappings VALUES(?,?,?,?,?)", (source, external_id, device_id, start, stop))
            revision = self.store.bump(db)
            self.store.audit(db, "mapping_registered", external_id, {"source": source, "device": device_id, "source_revision": revision})
            return {"source": source, "external_id": external_id, "device_id": device_id, "valid_from": start, "valid_to": stop}

    @staticmethod
    def resolve(db, source: str, external_id: str, at: str) -> str:
        timestamp = iso(at)
        rows = db.execute("SELECT device_id FROM source_mappings WHERE source=? AND external_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)", (source, external_id, timestamp, timestamp)).fetchall()
        require(len(rows) == 1, "unmapped_device", "external equipment id has no unique active mapping")
        return rows[0]["device_id"]

    def list(self, source: str, external_id: str | None = None) -> list[dict]:
        require(source in {"tickets", "telemetry"}, "invalid_source", "source unknown")
        with self.store.read() as db:
            if external_id is None:
                rows = db.execute("SELECT * FROM source_mappings WHERE source=? ORDER BY external_id,valid_from", (source,))
            else:
                rows = db.execute("SELECT * FROM source_mappings WHERE source=? AND external_id=? ORDER BY valid_from", (source, external_id))
            return [dict(row) for row in rows]
