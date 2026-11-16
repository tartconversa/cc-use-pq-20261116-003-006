"""跨市场设备服务质量分析：contracts模块。"""
import json
from .catalog import iso, instant
from .errors import DomainError, require
from .store import Store, now


DEFAULT_REQUIRED = {
    "tickets": ["event_key", "id", "device_id", "opened_at"],
    "telemetry": ["event_key", "id", "device_id", "observed_at", "run_hours", "downtime_hours"],
}


class Contracts:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _validate_definition(source: str, required: list[str], aliases: dict[str, str]) -> None:
        require(source in DEFAULT_REQUIRED, "invalid_source", "source unknown")
        require(isinstance(required, list) and len(required) == len(set(required)), "invalid_contract", "required fields must be distinct")
        require(all(isinstance(value, str) and value for value in required), "invalid_contract", "required fields must be names")
        require(set(DEFAULT_REQUIRED[source]).issubset(required), "invalid_contract", "identity and measurement fields required")
        require(isinstance(aliases, dict), "invalid_contract", "aliases must be object")
        require(all(isinstance(key, str) and isinstance(value, str) and key and value for key, value in aliases.items()), "invalid_contract", "aliases must map names")
        require(not set(aliases).intersection(required), "invalid_contract", "alias must not shadow canonical field")
        require(set(aliases.values()).issubset(required), "invalid_contract", "alias targets must be required fields")
        require(len(set(aliases.values())) == len(aliases), "invalid_contract", "aliases must have distinct targets")

    def register(self, source: str, effective_at: str, required: list[str], aliases: dict[str, str], expected_latest: int) -> dict:
        self._validate_definition(source, required, aliases)
        timestamp = iso(effective_at)
        require(type(expected_latest) is int and expected_latest >= 0, "invalid_version", "expected latest version required")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM source_contracts WHERE source=? ORDER BY version DESC LIMIT 1", (source,)).fetchone()
            latest = row["version"] if row else 0
            require(latest == expected_latest, "stale_contract", "source contract changed", 409)
            if row:
                require(instant(timestamp) > instant(row["effective_at"]), "overlapping_contract", "new contract must start later")
            version = latest + 1
            db.execute("INSERT INTO source_contracts VALUES(?,?,?,?,?,?)", (source, version, timestamp, json.dumps(required), json.dumps(aliases, sort_keys=True), "active"))
            revision = self.store.bump(db)
            self.store.audit(db, "contract_registered", source, {"version": version, "effective_at": timestamp, "source_revision": revision})
            return {"source": source, "version": version, "effective_at": timestamp, "required": required, "aliases": aliases}

    @staticmethod
    def _select(db, source: str, timestamp: str):
        row = db.execute("SELECT * FROM source_contracts WHERE source=? AND effective_at<=? AND status='active' ORDER BY effective_at DESC LIMIT 1", (source, timestamp)).fetchone()
        return row

    def normalize(self, db, source: str, value: dict) -> tuple[dict, int]:
        require(source in DEFAULT_REQUIRED, "invalid_source", "source unknown")
        require(isinstance(value, dict), "invalid_row", "row object required")
        time_field = "opened_at" if source == "tickets" else "observed_at"
        raw_time = value.get(time_field)
        if raw_time is None:
            rows = db.execute("SELECT aliases_json FROM source_contracts WHERE source=? ORDER BY version DESC", (source,)).fetchall()
            for row in rows:
                for alias, target in json.loads(row["aliases_json"]).items():
                    if target == time_field and alias in value:
                        raw_time = value[alias]
                        break
                if raw_time is not None:
                    break
        timestamp = iso(raw_time)
        contract = self._select(db, source, timestamp)
        if contract is None:
            aliases = {}
            required = DEFAULT_REQUIRED[source]
            version = 0
        else:
            aliases = json.loads(contract["aliases_json"])
            required = json.loads(contract["required_json"])
            version = contract["version"]
        normalized = dict(value)
        for alias, target in aliases.items():
            if alias in normalized:
                require(target not in normalized, "ambiguous_field", "alias and canonical field both present")
                normalized[target] = normalized.pop(alias)
        missing = [field for field in required if field not in normalized and not (field == "device_id" and "external_device_id" in normalized)]
        require(not missing, "missing_field", "required fields missing: " + ",".join(missing))
        return normalized, version

    def list(self, source: str) -> list[dict]:
        require(source in DEFAULT_REQUIRED, "invalid_source", "source unknown")
        with self.store.read() as db:
            rows = db.execute("SELECT * FROM source_contracts WHERE source=? ORDER BY version", (source,)).fetchall()
            return [{**dict(row), "required": json.loads(row["required_json"]), "aliases": json.loads(row["aliases_json"])} for row in rows]

    def trace(self, source: str, event_key: str) -> dict:
        require(source in DEFAULT_REQUIRED, "invalid_source", "source unknown")
        with self.store.read() as db:
            event = db.execute("SELECT * FROM source_events WHERE source=? AND event_key=?", (source, event_key)).fetchone()
            require(event is not None, "unknown_event", "event unknown", 404)
            batch_row = db.execute("SELECT r.*,b.request_key,b.created_at FROM batch_rows r JOIN batches b ON b.id=r.batch_id WHERE b.source=? AND r.event_key=? AND r.status='accepted' ORDER BY b.created_at LIMIT 1", (source, event_key)).fetchone()
            require(batch_row is not None, "unknown_event", "accepted event missing", 404)
            return {"source": source, "event_key": event_key, "record_id": event["record_id"], "digest": event["digest"], "request_key": batch_row["request_key"], "batch_id": batch_row["batch_id"], "row_no": batch_row["row_no"], "payload": json.loads(batch_row["payload"]), "accepted_at": batch_row["created_at"]}
