"""跨市场设备服务质量分析：catalog模块。"""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .errors import DomainError, require
from .store import Store


def instant(value: str) -> datetime:
    require(isinstance(value, str) and bool(value), "invalid_time", "timestamp required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainError("invalid_time", "ISO timestamp required") from exc
    require(result.tzinfo is not None, "invalid_time", "timezone offset required")
    return result.astimezone(timezone.utc)


def iso(value: str) -> str:
    return instant(value).isoformat()


def market_day(timestamp: str, zone: str) -> str:
    return instant(timestamp).astimezone(ZoneInfo(zone)).date().isoformat()


class Catalog:
    def __init__(self, store: Store):
        self.store = store

    def add_market(self, code: str, zone: str) -> dict:
        require(isinstance(code, str) and 2 <= len(code) <= 24 and code.replace("-", "").isalnum(), "invalid_market", "market code required")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
            raise DomainError("invalid_timezone", "IANA timezone required") from exc
        with self.store.transaction() as db:
            require(not db.execute("SELECT 1 FROM markets WHERE code=?", (code,)).fetchone(), "market_exists", "market already exists", 409)
            db.execute("INSERT INTO markets VALUES(?,?,?)", (code, zone, self.store_now()))
            self.store.bump(db)
            self.store.audit(db, "market_created", code, {"timezone": zone})
        return {"code": code, "timezone": zone}

    @staticmethod
    def store_now():
        from .store import now
        return now()

    def add_product(self, code: str, name: str) -> dict:
        require(isinstance(code, str) and 2 <= len(code) <= 48, "invalid_product", "product code required")
        require(isinstance(name, str) and 1 <= len(name.strip()) <= 120, "invalid_product", "product name required")
        with self.store.transaction() as db:
            require(not db.execute("SELECT 1 FROM products WHERE code=?", (code,)).fetchone(), "product_exists", "product already exists", 409)
            db.execute("INSERT INTO products VALUES(?,?,?)", (code, name.strip(), self.store_now()))
            self.store.bump(db)
            self.store.audit(db, "product_created", code, {"name": name.strip()})
        return {"code": code, "name": name.strip()}

    def install(self, device_id: str, market: str, product: str, installed_at: str) -> dict:
        require(isinstance(device_id, str) and 1 <= len(device_id) <= 80, "invalid_device", "device id required")
        installed = iso(installed_at)
        with self.store.transaction() as db:
            require(db.execute("SELECT 1 FROM markets WHERE code=?", (market,)).fetchone(), "unknown_market", "market unknown", 404)
            require(db.execute("SELECT 1 FROM products WHERE code=?", (product,)).fetchone(), "unknown_product", "product unknown", 404)
            require(not db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone(), "device_exists", "device already exists", 409)
            db.execute("INSERT INTO devices(id,market,product,installed_at) VALUES(?,?,?,?)", (device_id, market, product, installed))
            rev = self.store.bump(db)
            self.store.audit(db, "device_installed", device_id, {"market": market, "product": product, "source_revision": rev})
        return {"id": device_id, "market": market, "product": product, "installed_at": installed, "revision": 1}

    def retire(self, device_id: str, retired_at: str, expected_revision: int) -> dict:
        retired = iso(retired_at)
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            require(row is not None, "unknown_device", "device unknown", 404)
            require(row["revision"] == expected_revision, "stale_device", "device revision changed", 409)
            require(row["retired_at"] is None, "already_retired", "device already retired", 409)
            require(instant(retired) > instant(row["installed_at"]), "invalid_lifecycle", "retirement must follow installation")
            db.execute("UPDATE devices SET retired_at=?,revision=revision+1 WHERE id=?", (retired, device_id))
            rev = self.store.bump(db)
            self.store.audit(db, "device_retired", device_id, {"at": retired, "source_revision": rev})
        return {"id": device_id, "retired_at": retired, "revision": expected_revision + 1}

    @staticmethod
    def device(db, device_id: str, at: str | None = None):
        row = db.execute("SELECT d.*,m.timezone FROM devices d JOIN markets m ON m.code=d.market WHERE d.id=?", (device_id,)).fetchone()
        require(row is not None, "unknown_device", "device unknown", 404)
        if at is not None:
            moment = instant(at)
            require(moment >= instant(row["installed_at"]), "outside_lifecycle", "event precedes installation")
            require(row["retired_at"] is None or moment < instant(row["retired_at"]), "outside_lifecycle", "event after retirement")
        return row

    def list_devices(self, market: str | None = None) -> list[dict]:
        with self.store.read() as db:
            query = "SELECT * FROM devices"
            rows = db.execute(query + (" WHERE market=?" if market else "") + " ORDER BY id", ((market,) if market else ())).fetchall()
            return [dict(row) for row in rows]

    def market(self, code: str) -> dict:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM markets WHERE code=?", (code,)).fetchone()
            require(row is not None, "unknown_market", "market unknown", 404)
            return dict(row)

    @staticmethod
    def local_bounds(day: str, zone: str) -> tuple[datetime, datetime]:
        try:
            date = datetime.fromisoformat(day)
            require(date.date().isoformat() == day, "invalid_day", "date must be YYYY-MM-DD")
        except ValueError as exc:
            raise DomainError("invalid_day", "date must be YYYY-MM-DD") from exc
        start = date.replace(tzinfo=ZoneInfo(zone))
        end = (date + timedelta(days=1)).replace(tzinfo=ZoneInfo(zone))
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
