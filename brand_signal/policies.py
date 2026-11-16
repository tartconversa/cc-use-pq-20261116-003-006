"""跨市场设备服务质量分析：policies模块。"""
import math
from .catalog import iso, instant
from .errors import require
from .store import Store


class Policies:
    def __init__(self, store: Store):
        self.store = store

    def register(self, market: str, effective_at: str, response_hours: float, closure_hours: float, expected_latest: int) -> dict:
        effective = iso(effective_at)
        require(type(response_hours) in (int, float) and math.isfinite(response_hours) and 0 < response_hours <= 168, "invalid_policy", "response target must be positive and at most a week")
        require(type(closure_hours) in (int, float) and math.isfinite(closure_hours) and response_hours <= closure_hours <= 720, "invalid_policy", "closure target must exceed response target")
        require(type(expected_latest) is int and expected_latest >= 0, "invalid_version", "expected latest policy version required")
        with self.store.transaction() as db:
            require(db.execute("SELECT 1 FROM markets WHERE code=?", (market,)).fetchone(), "unknown_market", "market unknown", 404)
            row = db.execute("SELECT * FROM service_policies WHERE market=? ORDER BY version DESC LIMIT 1", (market,)).fetchone()
            latest = row["version"] if row else 0
            require(expected_latest == latest, "stale_policy", "policy version changed", 409)
            if row:
                require(instant(effective) > instant(row["effective_at"]), "overlapping_policy", "policy effective time must increase")
            version = latest + 1
            db.execute("INSERT INTO service_policies VALUES(?,?,?,?,?,?)", (market, version, effective, float(response_hours), float(closure_hours), "active"))
            revision = self.store.bump(db)
            self.store.audit(db, "policy_registered", market, {"version": version, "source_revision": revision})
            return {"market": market, "version": version, "effective_at": effective, "response_hours": response_hours, "closure_hours": closure_hours}

    @staticmethod
    def applicable(db, market: str, opened_at: str) -> dict | None:
        row = db.execute("SELECT * FROM service_policies WHERE market=? AND effective_at<=? AND status='active' ORDER BY effective_at DESC LIMIT 1", (market, opened_at)).fetchone()
        return dict(row) if row else None

    def list(self, market: str) -> list[dict]:
        with self.store.read() as db:
            require(db.execute("SELECT 1 FROM markets WHERE code=?", (market,)).fetchone(), "unknown_market", "market unknown", 404)
            return [dict(row) for row in db.execute("SELECT * FROM service_policies WHERE market=? ORDER BY version", (market,))]
