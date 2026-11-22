"""Validated inputs, current-report queries and the atomic import."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import db
from .errors import ImportRejected, NotFound, RevisionConflict, StoreConflict
from .receipts import parse_complete_hours, parse_receipts_csv

_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_STORE_CODE_RE = re.compile(r"^[^\s,，;；/\\]{1,40}$")
_MAX_NAME_LEN = 80
_MAX_REVISION = 9_999_999
# Total of the 报齐 hours for one day.  Receipt parsing already bounds each
# hour's total; this bounds their sum so the INTEGER column can never overflow.
MAX_TOTAL_CENTS = 2**63 - 1


def store_code_key(code: str) -> str:
    """Identity key for a store code.

    Store identity is defined by Unicode ``casefold`` rather than SQLite's
    ``COLLATE NOCASE`` (which only folds ASCII).  ``MÜN-01`` and ``mün-01``
    therefore resolve to the same store, and so do every other Unicode case
    variant (e.g. ``ß``/``ss``).  The original spelling is always preserved in
    ``stores.code``; only lookups and grouping use this key.
    """

    return code.strip().casefold()


def weekday_name(value: date) -> str:
    return _WEEKDAY_NAMES[value.weekday()]


# ---------------------------------------------------------------------------
# Input validation (all of it runs before a write transaction is opened)
# ---------------------------------------------------------------------------


def validate_store_code(raw: str) -> str:
    code = (raw or "").strip()
    if not code:
        raise ImportRejected("请填写门店编码。")
    if not _STORE_CODE_RE.fullmatch(code):
        raise ImportRejected(
            "门店编码不能为空、不能含空白或 , ; / \\ 等分隔符，最长 40 个字符。"
        )
    return code


def validate_store_name(raw: str, *, required: bool) -> str:
    name = (raw or "").strip()
    if not name:
        if required:
            raise ImportRejected("首次导入该门店时请填写门店名称。")
        return ""
    if len(name) > _MAX_NAME_LEN:
        raise ImportRejected(f"门店名称最长 {_MAX_NAME_LEN} 个字符。")
    if any(ch in name for ch in "\r\n\t"):
        raise ImportRejected("门店名称不能包含换行或制表符。")
    return name


def parse_date(raw: str, field_label: str = "业务日期") -> date:
    text = (raw or "").strip()
    if not text:
        raise ImportRejected(f"请填写{field_label}。")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ImportRejected(f"{field_label}格式应为 YYYY-MM-DD。") from exc
    return parsed


def parse_revision(raw: str | int) -> int:
    text = str(raw).strip() if raw is not None else ""
    if not re.fullmatch(r"\d{1,7}", text):
        raise ImportRejected("修订号必须是 1–9999999 的整数。")
    revision = int(text)
    if revision < 1:
        raise ImportRejected("修订号必须大于等于 1。")
    return revision


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def display_timestamp(value: str) -> str:
    """Render an ISO UTC timestamp in the server's local timezone."""

    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value or "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Store:
    id: int
    code: str
    name: str


@dataclass(frozen=True)
class CurrentReport:
    store_id: int
    store_code: str
    store_name: str
    business_date: str
    revision: int
    complete_hour_count: int
    total_sales_cents: int
    receipt_line_count: int
    updated_at: str

    @property
    def weekday(self) -> str:
        return weekday_name(date.fromisoformat(self.business_date))


_REPORT_SELECT = """
    SELECT s.id AS store_id, s.code AS store_code, s.name AS store_name,
           cr.business_date, rv.revision, rv.complete_hour_count,
           rv.total_sales_cents, rv.created_at, rv.receipt_line_count
    FROM current_reports cr
    JOIN stores s ON s.id = cr.store_id
    JOIN report_versions rv ON rv.id = cr.report_version_id
"""


def _report_from_row(row: sqlite3.Row) -> CurrentReport:
    return CurrentReport(
        store_id=row["store_id"],
        store_code=row["store_code"],
        store_name=row["store_name"],
        business_date=row["business_date"],
        revision=row["revision"],
        complete_hour_count=row["complete_hour_count"],
        total_sales_cents=row["total_sales_cents"],
        receipt_line_count=row["receipt_line_count"],
        updated_at=row["created_at"],
    )


def _select_report_by_code(
    conn: sqlite3.Connection, store_code: str, business_date: str
) -> sqlite3.Row | None:
    """Find one effective report for a store spelling on a day.

    An exact spelling match wins (so a legacy database that keeps one report
    per spelling still serves each spelling its own original row).  If there
    is no exact row we fall back to the store's casefold group, ordered by the
    canonical (lowest-id) spelling — this is what lets any spelling find the
    store's report when the group's reports live on non-overlapping dates.
    """

    key = store_code_key(store_code)
    row = conn.execute(
        _REPORT_SELECT
        + " WHERE s.code = ? COLLATE BINARY AND cr.business_date = ?",
        (store_code.strip(), business_date),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        _REPORT_SELECT
        + " WHERE s.code_key = ? AND cr.business_date = ? ORDER BY s.id ASC LIMIT 1",
        (key, business_date),
    ).fetchone()


def list_stores(database: str | Path) -> list[Store]:
    """One row per logical store (casefold group), canonical spelling first."""

    conn = db.connect(database)
    try:
        rows = conn.execute(
            """
            SELECT id, code, name FROM stores
            WHERE id IN (SELECT MIN(id) FROM stores GROUP BY code_key)
            ORDER BY code
            """
        ).fetchall()
    finally:
        conn.close()
    return [Store(id=r["id"], code=r["code"], name=r["name"]) for r in rows]


def get_store(database: str | Path, store_id: int) -> Store:
    conn = db.connect(database)
    try:
        row = conn.execute(
            "SELECT id, code, name FROM stores WHERE id = ?", (store_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise NotFound("门店不存在。")
    return Store(id=row["id"], code=row["code"], name=row["name"])


def get_current_report(
    database: str | Path, store_code: str, business_date: str
) -> CurrentReport | None:
    conn = db.connect(database)
    try:
        row = _select_report_by_code(conn, store_code, business_date)
    finally:
        conn.close()
    return _report_from_row(row) if row is not None else None


def list_current_reports(
    database: str | Path,
    *,
    business_date: str | None = None,
    code_query: str | None = None,
    limit: int = 50,
) -> list[CurrentReport]:
    """Current-report listing from one read snapshot."""

    clauses: list[str] = []
    params: list[object] = []
    if business_date:
        clauses.append("cr.business_date = ?")
        params.append(business_date)
    if code_query:
        clauses.append("s.code LIKE ? ESCAPE '\\'")
        escaped = code_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 500)))

    with db.read_snapshot(database) as conn:
        rows = conn.execute(
            _REPORT_SELECT
            + where
            + " ORDER BY cr.business_date DESC, s.code ASC LIMIT ?",
            params,
        ).fetchall()
    return [_report_from_row(r) for r in rows]


def get_report_with_hours(
    database: str | Path, store_code: str, business_date: str
) -> tuple[CurrentReport, list[tuple[int, int]]] | None:
    """Fetch a report's header and its hourly rows from one snapshot.

    Two SELECTs inside one read transaction: the detail page can never display a
    header from one revision next to hours from another.
    """

    with db.read_snapshot(database) as conn:
        row = _select_report_by_code(conn, store_code, business_date)
        if row is None:
            return None
        report = _report_from_row(row)
        hour_rows = conn.execute(
            """
            SELECT rh.hour, rh.sales_cents
            FROM current_reports cr
            JOIN report_hours rh ON rh.report_version_id = cr.report_version_id
            WHERE cr.store_id = ? AND cr.business_date = ?
            ORDER BY rh.hour
            """,
            (report.store_id, report.business_date),
        ).fetchall()

    return report, [(r["hour"], r["sales_cents"]) for r in hour_rows]


# ---------------------------------------------------------------------------
# Import / correction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportResult:
    store: Store
    business_date: str
    revision: int
    replaced_revision: int | None
    complete_hours: frozenset[int]
    total_sales_cents: int
    receipt_line_count: int


def _frozen_store_error(
    group_rows: list[sqlite3.Row], duplicate_dates: list[str]
) -> StoreConflict:
    """Build the rejection for a legacy store frozen by a same-day duplicate."""

    codes = "、".join(row["code"] for row in group_rows)
    return StoreConflict(
        f"门店（{codes}）在 {'、'.join(duplicate_dates)} "
        "存在多种编码写法的同日日报，该门店已被冻结："
        "新的报送一律拒收，原有日报保持不变。",
        details=[
            "请联系数据管理员保留其中一份日报后再试；"
            "同店对比在包含该门店日报的日期上也会暂停出表。"
        ],
    )


def import_daily_report(
    database: str | Path,
    *,
    store_code: str,
    store_name: str = "",
    business_date: str,
    revision: str | int,
    complete_hours: str,
    receipts_csv: bytes,
) -> ImportResult:
    """Validate and atomically replace one store-day's current report.

    1. Every input (CSV included) is validated before any transaction opens.
    2. One ``BEGIN IMMEDIATE`` transaction resolves the store through its
       Unicode casefold key, enforces the revision rule against *every*
       spelling of the same store, inserts an immutable new version, its
       per-hour summary and the raw receipt lines, then flips
       ``current_reports`` to the new version.
    3. Any error rolls the whole thing back: the old report, its hours and all
       comparison results stay exactly as they were.

    A correction is therefore a *new* revision — older receipt lines are never
    read or rewritten.  Because ``BEGIN IMMEDIATE`` serialises writers, two
    concurrent imports of the same store-day are judged against the same rule:
    exactly one can win, and the loser's revision is rejected against the
    winner's already-committed value.
    """

    code = validate_store_code(store_code)
    name = validate_store_name(store_name, required=False)
    day = parse_date(business_date)
    day_text = day.isoformat()
    revision_number = parse_revision(revision)
    hours = parse_complete_hours(complete_hours)
    parsed = parse_receipts_csv(receipts_csv)
    total_sales = sum(parsed.hourly_totals.get(hour, 0) for hour in hours)
    if abs(total_sales) > MAX_TOTAL_CENTS:
        raise ImportRejected("销售额合计超出允许范围。")
    receipt_count = len(parsed.lines)
    now = _now_iso()
    key = store_code_key(code)

    with db.write_transaction(database) as conn:
        # Resolve the whole casefold group.  New stores have one row; legacy
        # databases may hold several spellings of the same physical store.
        group_rows = conn.execute(
            "SELECT id, code, code_key, name FROM stores WHERE code_key = ? ORDER BY id",
            (key,),
        ).fetchall()

        if not group_rows:
            if not name:
                raise ImportRejected("首次导入该门店时请填写门店名称。")
            cursor = conn.execute(
                "INSERT INTO stores (code, code_key, name, created_at) VALUES (?, ?, ?, ?)",
                (code, key, name, now),
            )
            target_id = cursor.lastrowid
            store = Store(id=target_id, code=code, name=name)
        else:
            # Same store under any spelling lands on the canonical (lowest-id)
            # row, so a store never accumulates a second spelling going forward.
            canonical = group_rows[0]
            target_id = canonical["id"]

            # Legacy conflict: two effective reports for one day under
            # different spellings.  The store is frozen — reject every new
            # submission and keep every original row untouched.
            duplicates = conn.execute(
                """
                SELECT business_date, COUNT(*) AS n
                FROM current_reports
                WHERE store_id IN (
                    SELECT id FROM stores WHERE code_key = ?
                )
                GROUP BY business_date
                HAVING n > 1
                ORDER BY business_date
                """,
                (key,),
            ).fetchall()
            if duplicates:
                raise _frozen_store_error(
                    group_rows, [row["business_date"] for row in duplicates]
                )

            store = Store(
                id=target_id, code=canonical["code"], name=canonical["name"]
            )
            if name and name != canonical["name"]:
                conn.execute("UPDATE stores SET name = ? WHERE id = ?", (name, target_id))
                store = Store(id=target_id, code=store.code, name=name)

        # Revision rule across the whole casefold group.  A normal group has a
        # single store row, so this is the store-day's current revision; for a
        # legacy multi-spelling group it is the highest effective revision of
        # any spelling on that day.
        current = conn.execute(
            """
            SELECT MAX(rv.revision) AS revision
            FROM current_reports cr
            JOIN stores gs ON gs.id = cr.store_id
            JOIN report_versions rv ON rv.id = cr.report_version_id
            WHERE gs.code_key = ? AND cr.business_date = ?
            """,
            (key, day_text),
        ).fetchone()

        replaced_revision = None
        if current is not None and current["revision"] is not None:
            current_revision = current["revision"]
            if revision_number <= current_revision:
                raise RevisionConflict(
                    f"{store.code} {day_text} 当前修订号为 {current_revision}，"
                    f"只接受更高的修订号（收到的为 {revision_number}）。"
                )
            replaced_revision = current_revision

        try:
            cursor = conn.execute(
                """
                INSERT INTO report_versions
                    (store_id, business_date, revision, complete_hour_count,
                     total_sales_cents, receipt_line_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_id,
                    day_text,
                    revision_number,
                    len(hours),
                    total_sales,
                    receipt_count,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Last line of defence for concurrent imports.
            raise RevisionConflict(
                f"{store.code} {day_text} 修订号 {revision_number} 已存在，请使用更高修订号。"
            ) from exc
        version_id = cursor.lastrowid

        conn.executemany(
            "INSERT INTO report_hours (report_version_id, hour, sales_cents) VALUES (?, ?, ?)",
            [
                (version_id, hour, parsed.hourly_totals.get(hour, 0))
                for hour in sorted(hours)
            ],
        )
        conn.executemany(
            "INSERT INTO receipt_lines (report_version_id, hour, amount_cents) VALUES (?, ?, ?)",
            [(version_id, hour, amount) for hour, amount in parsed.lines],
        )
        conn.execute(
            """
            INSERT INTO current_reports (store_id, business_date, report_version_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (store_id, business_date) DO UPDATE SET
                report_version_id = excluded.report_version_id,
                updated_at = excluded.updated_at
            """,
            (target_id, day_text, version_id, now),
        )

    return ImportResult(
        store=store,
        business_date=day_text,
        revision=revision_number,
        replaced_revision=replaced_revision,
        complete_hours=hours,
        total_sales_cents=total_sales,
        receipt_line_count=receipt_count,
    )
