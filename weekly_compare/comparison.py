"""Same-store, same-weekday, same-hour comparison.

Business rules implemented here:

* **Store identity** — stores are grouped by their Unicode casefold key
  (``stores.code_key``), so spellings such as ``MÜN-01`` and ``mün-01`` are
  one store and the comparison lists exactly one row for it.
* **Scope** — every casefold group that has a current report on *either*
  Monday of the comparison.  A group missing one side still appears (with
  ``—`` in the missing column) so it cannot silently vanish from the report.
* **Pairing** — a group's two reports are paired hour by hour.  An hour counts
  only when it is 报齐 in **both** reports.
* **Zero receipts** — hours are taken from ``report_hours``, which import
  writes for every 报齐 hour (missing receipt rows contribute 0).  No receipt
  rescan.
* **Legacy conflicts** — a legacy database may hold two effective reports for
  the same group and day under different spellings.  When such a group has any
  report on either selected date, the conflicting dates are reported and the
  comparison pauses instead of silently picking one spelling.
* **Consistency** — one SQL statement returns both the per-store rows, the
  all-store total and the conflict rows, executed inside one read snapshot, so
  every figure in a response is guaranteed to be from the same commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import db
from .errors import ImportRejected, StoreConflict
from .services import parse_date, weekday_name

# A single statement -> a single SQLite snapshot for rows, total *and*
# conflicts.  Only report_hours is read: receipt_lines are never rescanned.
_COMPARISON_SQL = """
WITH a AS (
    SELECT s.code_key,
           cr.store_id,
           rv.id AS version_id,
           rv.revision
    FROM current_reports cr
    JOIN stores s ON s.id = cr.store_id
    JOIN report_versions rv ON rv.id = cr.report_version_id
    WHERE cr.business_date = :date_a
),
b AS (
    SELECT s.code_key,
           cr.store_id,
           rv.id AS version_id,
           rv.revision
    FROM current_reports cr
    JOIN stores s ON s.id = cr.store_id
    JOIN report_versions rv ON rv.id = cr.report_version_id
    WHERE cr.business_date = :date_b
),
scope AS (
    SELECT code_key FROM a
    UNION
    SELECT code_key FROM b
),
canonical AS (
    -- One identity row per casefold group: the lowest-id spelling wins, so
    -- the displayed code/name is stable for every group.
    SELECT sc.id AS store_id, sc.code_key, sc.code AS store_code, sc.name AS store_name
    FROM stores sc
    WHERE sc.id IN (SELECT MIN(id) FROM stores GROUP BY code_key)
),
day_dupes AS (
    -- Same-day duplicates across spellings, on the two selected dates only.
    SELECT s.code_key, cr.business_date, COUNT(*) AS n
    FROM current_reports cr
    JOIN stores s ON s.id = cr.store_id
    WHERE cr.business_date IN (:date_a, :date_b)
    GROUP BY s.code_key, cr.business_date
    HAVING COUNT(*) > 1
),
conflicts AS (
    SELECT DISTINCT d.code_key
    FROM day_dupes d
    JOIN scope sc ON sc.code_key = d.code_key
),
conflict_rows AS (
    SELECT 2 AS is_total,
           c.store_id,
           c.store_code,
           c.store_name,
           NULL AS revision_a,
           NULL AS revision_b,
           0 AS comparable_hours,
           0 AS sales_a,
           0 AS sales_b,
           GROUP_CONCAT(d.business_date, '|') AS conflict_dates
    FROM conflicts x
    JOIN canonical c ON c.code_key = x.code_key
    JOIN day_dupes d ON d.code_key = x.code_key
    GROUP BY c.store_id, c.store_code, c.store_name
),
matched AS (
    SELECT ga.code_key,
           ha.hour,
           ha.sales_cents AS sales_a,
           hb.sales_cents AS sales_b
    FROM a ga
    JOIN b gb ON gb.code_key = ga.code_key
    JOIN report_hours ha ON ha.report_version_id = ga.version_id
    JOIN report_hours hb
      ON hb.report_version_id = gb.version_id
     AND hb.hour = ha.hour
),
per_store_matched AS (
    SELECT code_key,
           COUNT(*) AS comparable_hours,
           COALESCE(SUM(sales_a), 0) AS sales_a,
           COALESCE(SUM(sales_b), 0) AS sales_b
    FROM matched
    GROUP BY code_key
),
-- At most one effective row per group per date for a non-conflicting group;
-- pick deterministically so rev1/rev2 stay well-defined.
a_one AS (
    SELECT code_key, store_id, revision,
           ROW_NUMBER() OVER (PARTITION BY code_key ORDER BY store_id) AS rn
    FROM a
),
b_one AS (
    SELECT code_key, store_id, revision,
           ROW_NUMBER() OVER (PARTITION BY code_key ORDER BY store_id) AS rn
    FROM b
),
per_store AS (
    SELECT c.store_id,
           c.store_code,
           c.store_name,
           ao.revision AS revision_a,
           bo.revision AS revision_b,
           COALESCE(pm.comparable_hours, 0) AS comparable_hours,
           COALESCE(pm.sales_a, 0) AS sales_a,
           COALESCE(pm.sales_b, 0) AS sales_b
    FROM scope sc
    JOIN canonical c ON c.code_key = sc.code_key
    LEFT JOIN a_one ao ON ao.code_key = sc.code_key AND ao.rn = 1
    LEFT JOIN b_one bo ON bo.code_key = sc.code_key AND bo.rn = 1
    LEFT JOIN per_store_matched pm ON pm.code_key = sc.code_key
    WHERE sc.code_key NOT IN (SELECT code_key FROM conflicts)
),
data_rows AS (
    SELECT 0 AS is_total, store_id, store_code, store_name,
           revision_a, revision_b, comparable_hours, sales_a, sales_b,
           NULL AS conflict_dates
    FROM per_store
    UNION ALL
    SELECT 1 AS is_total, NULL, NULL, '全店合计',
           NULL, NULL,
           COALESCE(SUM(comparable_hours), 0),
           COALESCE(SUM(sales_a), 0),
           COALESCE(SUM(sales_b), 0),
           NULL
    FROM per_store
)
SELECT is_total, store_id, store_code, store_name,
       revision_a, revision_b, comparable_hours, sales_a, sales_b,
       conflict_dates
FROM data_rows
UNION ALL
SELECT is_total, store_id, store_code, store_name,
       revision_a, revision_b, comparable_hours, sales_a, sales_b,
       conflict_dates
FROM conflict_rows
ORDER BY is_total ASC, store_code ASC
"""


@dataclass(frozen=True)
class ComparisonRow:
    store_id: int
    store_code: str
    store_name: str
    revision_a: int | None
    revision_b: int | None
    comparable_hours: int
    sales_a_cents: int
    sales_b_cents: int

    @property
    def difference_cents(self) -> int:
        return self.sales_b_cents - self.sales_a_cents

    @property
    def has_both_reports(self) -> bool:
        return self.revision_a is not None and self.revision_b is not None


@dataclass(frozen=True)
class ComparisonTotal:
    comparable_hours: int
    sales_a_cents: int
    sales_b_cents: int

    @property
    def difference_cents(self) -> int:
        return self.sales_b_cents - self.sales_a_cents


@dataclass(frozen=True)
class Comparison:
    date_a: date
    date_b: date
    rows: tuple[ComparisonRow, ...]
    total: ComparisonTotal

    @property
    def weekday(self) -> str:
        return weekday_name(self.date_a)

    @property
    def has_rows(self) -> bool:
        return bool(self.rows)

    @property
    def total_has_comparable_hours(self) -> bool:
        return self.total.comparable_hours > 0


def validate_comparison_dates(date_a: str, date_b: str) -> tuple[date, date]:
    """Both dates must be Mondays and must differ; they are returned sorted."""

    day_a = parse_date(date_a, "第一个周一")
    day_b = parse_date(date_b, "第二个周一")
    if day_a.weekday() != 0 or day_b.weekday() != 0:
        raise ImportRejected("同店对比只接受两个周一（所选日期必须都是周一）。")
    if day_a == day_b:
        raise ImportRejected("请选择两个不同的周一。")
    if day_a > day_b:
        day_a, day_b = day_b, day_a
    return day_a, day_b


def compare(database: str, date_a: str, date_b: str) -> Comparison:
    day_a, day_b = validate_comparison_dates(date_a, date_b)

    # One read transaction pins one committed snapshot for the whole statement.
    with db.read_snapshot(database) as conn:
        result_rows = conn.execute(
            _COMPARISON_SQL,
            {"date_a": day_a.isoformat(), "date_b": day_b.isoformat()},
        ).fetchall()

    rows: list[ComparisonRow] = []
    total = ComparisonTotal(0, 0, 0)
    conflicts: list[tuple[str, list[str]]] = []
    for row in result_rows:
        if row["is_total"] == 2:
            dates = sorted((row["conflict_dates"] or "").split("|")) if row["conflict_dates"] else []
            conflicts.append((row["store_code"], dates))
            continue
        if row["is_total"] == 1:
            total = ComparisonTotal(
                comparable_hours=row["comparable_hours"],
                sales_a_cents=row["sales_a"],
                sales_b_cents=row["sales_b"],
            )
            continue
        rows.append(
            ComparisonRow(
                store_id=row["store_id"],
                store_code=row["store_code"],
                store_name=row["store_name"],
                revision_a=row["revision_a"],
                revision_b=row["revision_b"],
                comparable_hours=row["comparable_hours"],
                sales_a_cents=row["sales_a"],
                sales_b_cents=row["sales_b"],
            )
        )

    if conflicts:
        details = [
            f"门店 {code} 在 {('、'.join(dates))} 存在多种编码写法的同日日报。"
            for code, dates in conflicts
        ]
        raise StoreConflict(
            "所选日期中存在同店同日多份日报的冲突，已暂停出表；"
            "请先处理冲突门店的重复日报后再对比。",
            details=details,
        )

    return Comparison(
        date_a=day_a,
        date_b=day_b,
        rows=tuple(rows),
        total=total,
    )
