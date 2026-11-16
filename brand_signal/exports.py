"""跨市场设备服务质量分析：exports模块。"""
import hashlib
import json
import uuid
from .errors import require
from .publication import Publication
from .store import Store, now


class Exports:
    def __init__(self, store: Store):
        self.store = store
        self.publication = Publication(store)

    def create(self, market: str, version: int, format: str) -> dict:
        require(format in {"csv", "json"}, "invalid_format", "export format unknown")
        release = self.publication.read(market, version)
        if format == "csv":
            payload = self.publication.export_csv(market, version)
        else:
            payload = json.dumps(release, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(payload.encode()).hexdigest()
        identifier = uuid.uuid4().hex
        with self.store.transaction() as db:
            require(db.execute("SELECT 1 FROM publications WHERE market=? AND version=?", (market, version)).fetchone(), "unknown_publication", "publication unknown", 404)
            timestamp = now()
            db.execute("INSERT INTO export_jobs VALUES(?,?,?,?,?,?,?,?,?)", (identifier, market, version, format, "completed", checksum, payload, timestamp, timestamp))
            self.store.audit(db, "export_created", identifier, {"market": market, "version": version, "format": format, "sha256": checksum})
            return {"id": identifier, "market": market, "version": version, "format": format, "status": "completed", "sha256": checksum, "bytes": len(payload.encode())}

    def read(self, identifier: str) -> dict:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM export_jobs WHERE id=?", (identifier,)).fetchone()
            require(row is not None, "unknown_export", "export unknown", 404)
            result = dict(row)
            result["sha256"] = result.pop("digest")
            result["verified"] = hashlib.sha256(result["payload"].encode()).hexdigest() == result["sha256"]
            return result

    def list(self, market: str) -> list[dict]:
        with self.store.read() as db:
            require(db.execute("SELECT 1 FROM markets WHERE code=?", (market,)).fetchone(), "unknown_market", "market unknown", 404)
            return [{"id": row["id"], "market": row["market"], "version": row["version"], "format": row["format"], "status": row["status"], "sha256": row["digest"], "created_at": row["created_at"]} for row in db.execute("SELECT * FROM export_jobs WHERE market=? ORDER BY created_at,id", (market,))]
