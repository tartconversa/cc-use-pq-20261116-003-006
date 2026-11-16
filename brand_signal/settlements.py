"""跨市场设备服务质量分析：settlements模块。

市场级日结算窗口与迟到数据管理。窗口按市场+本地日唯一：
  open（已开窗口，数据正常流入）→ closed（已结算，新事件登记为迟到）
  closed → reopened（重开核对，新事件同样登记为迟到）→ closed。
迟到事件只登记一次，纳入某个发布版本后状态变为 included；同一事件
在同一版本中的纳入由 late_publication_inclusions 唯一约束兜底。
"""
import json
import uuid
from .analytics import days_between
from .errors import require
from .reconciliation import Reconciliation
from .store import Store, now


WINDOW_STATES = {"open", "closed", "reopened"}
METRIC_FIELDS = (
    "installed_devices", "active_devices", "exposure_hours", "ticket_opened",
    "ticket_closed", "ticket_open_at_end", "critical_opened", "acknowledged",
    "response_hours_total", "telemetry_events", "devices_reporting", "run_hours",
    "downtime_hours", "telemetry_coverage", "tickets_per_100_devices", "downtime_share",
)


def _window_payload(row, db) -> dict:
    payload = dict(row)
    payload["settled_issues"] = json.loads(payload.pop("settled_issues_json") or "[]")
    counts = db.execute(
        "SELECT status,count(*) AS n FROM late_arrivals WHERE window_id=? GROUP BY status",
        (row["id"],),
    ).fetchall()
    payload["late_counts"] = {item["status"]: item["n"] for item in counts}
    payload["late_total"] = sum(item["n"] for item in counts)
    return payload


class Settlements:
    def __init__(self, store: Store):
        self.store = store
        self.reconciliation = Reconciliation(store)

    # ---------- 窗口生命周期 ----------

    def open_window(self, market: str, day: str) -> dict:
        list(days_between(day, day))
        with self.store.transaction() as db:
            require(db.execute("SELECT 1 FROM markets WHERE code=?", (market,)).fetchone(), "unknown_market", "market unknown", 404)
            existing = db.execute("SELECT * FROM settlement_windows WHERE market=? AND day=?", (market, day)).fetchone()
            require(existing is None, "window_exists", "settlement window already exists", 409)
            window_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO settlement_windows(id,market,day,status,revision,opened_at) VALUES(?,?,?,?,?,?)",
                (window_id, market, day, "open", 1, now()),
            )
            self.store.audit(db, "window_opened", window_id, {"market": market, "day": day})
            return _window_payload(db.execute("SELECT * FROM settlement_windows WHERE id=?", (window_id,)).fetchone(), db)

    def close_window(self, window_id: str, expected_revision: int) -> dict:
        require(type(expected_revision) is int and expected_revision > 0, "invalid_revision", "integer window revision required")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM settlement_windows WHERE id=?", (window_id,)).fetchone()
            require(row is not None, "unknown_window", "settlement window unknown", 404)
            require(row["revision"] == expected_revision, "stale_window", "window revision changed", 409)
            require(row["status"] in {"open", "reopened"}, "window_not_open", "window unavailable for close", 409)
            zone = db.execute("SELECT timezone FROM markets WHERE code=?", (row["market"],)).fetchone()[0]
            metrics = self.reconciliation.analytics.calculate(db, row["market"], row["day"], row["day"])
            issues = self.reconciliation._issues(db, row["market"], zone, {row["day"]}, metrics)
            head = db.execute("SELECT version FROM publication_heads WHERE market=?", (row["market"],)).fetchone()
            head_version = head[0] if head else None
            timestamp = now()
            db.execute(
                "UPDATE settlement_windows SET status='closed',revision=revision+1,closed_at=?,last_closed_at=?,head_version=?,settled_issues_json=? WHERE id=?",
                (timestamp, timestamp, head_version, json.dumps(issues, sort_keys=True, ensure_ascii=False), window_id),
            )
            self.store.audit(db, "window_closed", window_id, {"market": row["market"], "day": row["day"], "head_version": head_version, "issues": len(issues)})
            return _window_payload(db.execute("SELECT * FROM settlement_windows WHERE id=?", (window_id,)).fetchone(), db)

    def reopen_window(self, window_id: str, expected_revision: int, reason: str = "") -> dict:
        require(type(expected_revision) is int and expected_revision > 0, "invalid_revision", "integer window revision required")
        require(isinstance(reason, str) and len(reason) <= 1000, "invalid_note", "reason too long")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM settlement_windows WHERE id=?", (window_id,)).fetchone()
            require(row is not None, "unknown_window", "settlement window unknown", 404)
            require(row["revision"] == expected_revision, "stale_window", "window revision changed", 409)
            require(row["status"] == "closed", "window_not_closed", "only a closed window can be reopened", 409)
            timestamp = now()
            db.execute(
                "UPDATE settlement_windows SET status='reopened',revision=revision+1,reopened_at=? WHERE id=?",
                (timestamp, window_id),
            )
            self.store.audit(db, "window_reopened", window_id, {"market": row["market"], "day": row["day"], "reason": reason})
            return _window_payload(db.execute("SELECT * FROM settlement_windows WHERE id=?", (window_id,)).fetchone(), db)

    def get(self, window_id: str) -> dict:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM settlement_windows WHERE id=?", (window_id,)).fetchone()
            require(row is not None, "unknown_window", "settlement window unknown", 404)
            return _window_payload(row, db)

    def list_windows(self, market: str, day: str | None = None, status: str | None = None) -> list[dict]:
        if status is not None:
            require(status in WINDOW_STATES, "invalid_window_state", "window state unknown")
        with self.store.read() as db:
            require(db.execute("SELECT 1 FROM markets WHERE code=?", (market,)).fetchone(), "unknown_market", "market unknown", 404)
            query = "SELECT * FROM settlement_windows WHERE market=?"
            parameters = [market]
            if day:
                query += " AND day=?"
                parameters.append(day)
            if status:
                query += " AND status=?"
                parameters.append(status)
            rows = db.execute(query + " ORDER BY day", parameters).fetchall()
            return [_window_payload(row, db) for row in rows]

    # ---------- 迟到登记（由接收流程在同一事务内调用） ----------

    def register_late(self, db, *, window, source: str, event_key: str, record_id: str, batch_id: str,
                      device_id: str, source_revision: int) -> bool:
        if db.execute("SELECT 1 FROM late_arrivals WHERE source=? AND event_key=?", (source, event_key)).fetchone():
            # 重复上传：事件已登记，不产生第二条待处理记录
            return True
        late_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO late_arrivals(id,window_id,source,event_key,record_id,batch_id,day,device_id,status,source_revision,received_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (late_id, window["id"], source, event_key, record_id, batch_id, window["day"], device_id,
             "pending", source_revision, now()),
        )
        self.store.audit(db, "late_registered", late_id, {
            "window": window["id"], "source": source, "event_key": event_key,
            "record_id": record_id, "day": window["day"], "batch": batch_id,
        })
        return True

    def is_late_event(self, db, source: str, event_key: str) -> bool:
        return db.execute("SELECT 1 FROM late_arrivals WHERE source=? AND event_key=?", (source, event_key)).fetchone() is not None

    def list_late(self, window_id: str, status: str | None = None) -> dict:
        require(status in (None, "pending", "included"), "invalid_late_status", "late status unknown")
        with self.store.read() as db:
            window = db.execute("SELECT * FROM settlement_windows WHERE id=?", (window_id,)).fetchone()
            require(window is not None, "unknown_window", "settlement window unknown", 404)
            query = "SELECT * FROM late_arrivals WHERE window_id=?"
            parameters = [window_id]
            if status:
                query += " AND status=?"
                parameters.append(status)
            rows = db.execute(query + " ORDER BY received_at,id", parameters).fetchall()
            return {"window": _window_payload(window, db), "late_arrivals": [dict(row) for row in rows]}

    def pending(self, market: str) -> list[dict]:
        with self.store.read() as db:
            require(db.execute("SELECT 1 FROM markets WHERE code=?", (market,)).fetchone(), "unknown_market", "market unknown", 404)
            rows = db.execute(
                "SELECT l.* FROM late_arrivals l JOIN settlement_windows w ON w.id=l.window_id "
                "WHERE w.market=? AND l.status='pending' ORDER BY l.day,l.received_at,l.id",
                (market,),
            ).fetchall()
            return [dict(row) for row in rows]

    # ---------- 影响核对 ----------

    def impact(self, window_id: str) -> dict:
        with self.store.read() as db:
            window = db.execute("SELECT * FROM settlement_windows WHERE id=?", (window_id,)).fetchone()
            require(window is not None, "unknown_window", "settlement window unknown", 404)
            market, day = window["market"], window["day"]
            late_rows = db.execute("SELECT * FROM late_arrivals WHERE window_id=? ORDER BY received_at,id", (window_id,)).fetchall()
            analytics = self.reconciliation.analytics
            live_rows = {(r["day"], r["product"]): r for r in analytics.calculate(db, market, day, day)}
            baseline_rows = {}
            baseline_snapshot = None
            if window["head_version"] is not None:
                published = db.execute(
                    "SELECT payload FROM publication_rows WHERE market=? AND version=?",
                    (market, window["head_version"]),
                ).fetchall()
                baseline_rows = {(day, json.loads(r[0])["product"]): json.loads(r[0]) for r in published}
            metric_changes = []
            for key in sorted(baseline_rows.keys() | live_rows.keys()):
                before, after = baseline_rows.get(key), live_rows.get(key)
                fields = {}
                for field in METRIC_FIELDS:
                    old_value = before.get(field) if before else None
                    new_value = after.get(field) if after else None
                    if old_value != new_value and (old_value or new_value):
                        fields[field] = {"before": old_value, "after": new_value}
                if fields:
                    metric_changes.append({"day": key[0], "product": key[1], "fields": fields})
            zone = db.execute("SELECT timezone FROM markets WHERE code=?", (market,)).fetchone()[0]
            current_issues = self.reconciliation._issues(db, market, zone, {day}, list(live_rows.values()))
            if window["settled_issues_json"] is not None:
                baseline_snapshot = json.loads(window["settled_issues_json"])
            elif baseline_rows:
                baseline_snapshot = self.reconciliation._issues(db, market, zone, {day}, list(baseline_rows.values()))
            baseline_issues = baseline_snapshot or []
            before_keys = {Reconciliation.issue_key(item): item for item in baseline_issues}
            after_keys = {Reconciliation.issue_key(item): item for item in current_issues}
            new_issues = [after_keys[key] for key in sorted(after_keys.keys() - before_keys.keys())]
            resolved_issues = [before_keys[key] for key in sorted(before_keys.keys() - after_keys.keys())]
            by_source = {}
            for row in late_rows:
                by_source.setdefault(row["source"], 0)
                by_source[row["source"]] += 1
            return {
                "window": _window_payload(window, db),
                "source_revision": self.store.revision(db),
                "baseline_head_version": window["head_version"],
                "late_arrivals": [dict(row) for row in late_rows],
                "late_by_source": by_source,
                "metric_changes": metric_changes,
                "alert_changes": {"new": new_issues, "resolved": resolved_issues},
            }

    # ---------- 纳入发布版本（由 Publication 在同一事务内调用） ----------

    def include_in_publication(self, db, market: str, version: int, candidate) -> int:
        days = set(days_between(candidate["from_day"], candidate["to_day"]))
        rows = db.execute(
            "SELECT l.* FROM late_arrivals l JOIN settlement_windows w ON w.id=l.window_id "
            "WHERE w.market=? AND l.day IN (%s)" % ",".join("?" for _ in days),
            [market, *sorted(days)],
        ).fetchall()
        for row in rows:
            # 显式存在性检查：重放或重复发布时同一 (版本, 事件) 绝不插入第二次
            already = db.execute(
                "SELECT 1 FROM late_publication_inclusions WHERE market=? AND version=? AND late_id=?",
                (market, version, row["id"]),
            ).fetchone()
            if not already:
                db.execute(
                    "INSERT INTO late_publication_inclusions VALUES(?,?,?,?)",
                    (market, version, row["id"], now()),
                )
            db.execute("UPDATE late_arrivals SET status='included' WHERE id=? AND status='pending'", (row["id"],))
        if rows:
            self.store.audit(db, "late_included", market, {"version": version, "events": len(rows)})
        return len(rows)
