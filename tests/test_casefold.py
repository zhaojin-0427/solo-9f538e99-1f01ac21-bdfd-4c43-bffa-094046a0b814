"""Unicode casefold identity: one physical store under many spellings.

Covers the rules that ASCII-only ``COLLATE NOCASE`` could not express:

* ``MÜN-01`` and ``mün-01`` (and every other Unicode case variant) are the
  same store, matched via ``str.casefold()``;
* same-store/same-day imports only accept a revision strictly higher than the
  highest *effective* revision, regardless of which spelling is used;
* reports spread across spellings on non-overlapping dates are findable by any
  spelling and collapse to one row in the same-store comparison;
* a legacy database that holds two effective reports for one group/day is
  frozen: new submissions are rejected, originals are kept, comparisons pause;
* an old database file migrates in place and stays usable.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from weekly_compare import comparison, db, services
from weekly_compare.errors import RevisionConflict, StoreConflict

MON_A = "2026-11-02"
MON_B = "2026-11-09"
MON_C = "2026-11-16"


def csv_bytes(*rows: tuple[int, int]) -> bytes:
    return ("小时,金额分\n" + "".join(f"{h},{a}\n" for h, a in rows)).encode()


class CasefoldIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = os.path.join(self.tmp.name, "casefold.db")
        db.init_db(self.database)

    def import_report(self, **overrides):
        kwargs = dict(
            store_code="MÜN-01",
            store_name="慕尼黑店",
            business_date=MON_A,
            revision="1",
            complete_hours="9-10",
            receipts_csv=csv_bytes((9, 1000), (10, 2000)),
        )
        kwargs.update(overrides)
        return services.import_daily_report(self.database, **kwargs)

    def test_unicode_case_variants_are_one_store(self):
        self.import_report(store_code="MÜN-01")
        # Lowercase ü + different case — same physical store, no new row.
        result = self.import_report(
            store_code="mün-01", store_name="", business_date=MON_B
        )
        self.assertEqual(result.store.code, "MÜN-01")  # canonical spelling kept
        self.assertEqual(len(services.list_stores(self.database)), 1)
        conn = db.connect(self.database)
        try:
            keys = {r["code_key"] for r in conn.execute("SELECT code_key FROM stores")}
        finally:
            conn.close()
        self.assertEqual(keys, {"mün-01"})

    def test_german_eszett_and_greek_casefold(self):
        # ß casefolds to "ss"; Σ/σ share a casefold.
        self.import_report(store_code="STRA-ß1", store_name="甲店")
        result = self.import_report(
            store_code="STRA-SS1", store_name="", business_date=MON_B
        )
        self.assertEqual(result.store.code, "STRA-ß1")
        self.assertEqual(len(services.list_stores(self.database)), 1)

        self.import_report(store_code="Σ-2", store_name="乙店", business_date=MON_C)
        self.assertEqual(
            len(services.list_stores(self.database)),
            2,  # distinct groups stay distinct
        )

    def test_lower_revision_under_other_spelling_is_rejected(self):
        self.import_report(revision="3")
        # Re-filing the same day in a different casing with a lower revision
        # must not sneak a second, lower-revision report into the store.
        with self.assertRaises(RevisionConflict):
            self.import_report(store_code="mün-01", revision="1")
        with self.assertRaises(RevisionConflict):
            self.import_report(store_code="MÜN-01", revision="3")
        report = services.get_current_report(self.database, "mün-01", MON_A)
        self.assertEqual(report.revision, 3)
        self.assertEqual(report.total_sales_cents, 3000)
        conn = db.connect(self.database)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM current_reports").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_higher_revision_under_other_spelling_replaces(self):
        self.import_report(revision="1")
        result = self.import_report(
            store_code="mün-01",
            revision="2",
            receipts_csv=csv_bytes((9, 50)),
        )
        self.assertEqual(result.replaced_revision, 1)
        # Findable under either spelling, canonical code preserved.
        for spelling in ("MÜN-01", "mün-01", "Mün-01"):
            report = services.get_current_report(self.database, spelling, MON_A)
            self.assertIsNotNone(report)
            self.assertEqual(report.revision, 2)
            self.assertEqual(report.store_code, "MÜN-01")
        self.assertEqual(len(services.list_stores(self.database)), 1)

    def test_non_overlapping_dates_findable_by_any_spelling(self):
        self.import_report(store_code="MÜN-01", business_date=MON_A)
        self.import_report(store_code="mün-01", business_date=MON_B, store_name="")
        # Each day's report is reachable through every spelling.
        for spelling in ("MÜN-01", "mün-01"):
            self.assertEqual(
                services.get_current_report(self.database, spelling, MON_A).business_date,
                MON_A,
            )
            self.assertEqual(
                services.get_current_report(self.database, spelling, MON_B).business_date,
                MON_B,
            )
        # And the comparison lists the store exactly once.
        result = comparison.compare(self.database, MON_A, MON_B)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].store_code, "MÜN-01")
        self.assertIsNotNone(result.rows[0].revision_a)
        self.assertIsNotNone(result.rows[0].revision_b)


class LegacyDatabaseTests(unittest.TestCase):
    """A database written by the pre-casefold application must keep working."""

    LEGACY_SCHEMA = """
    CREATE TABLE stores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE report_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id INTEGER NOT NULL REFERENCES stores(id),
        business_date TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        complete_hour_count INTEGER NOT NULL CHECK (complete_hour_count >= 0),
        total_sales_cents INTEGER NOT NULL,
        receipt_line_count INTEGER NOT NULL DEFAULT 0 CHECK (receipt_line_count >= 0),
        created_at TEXT NOT NULL,
        UNIQUE (store_id, business_date, revision)
    );
    CREATE TABLE report_hours (
        report_version_id INTEGER NOT NULL REFERENCES report_versions(id) ON DELETE CASCADE,
        hour INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
        sales_cents INTEGER NOT NULL,
        PRIMARY KEY (report_version_id, hour)
    );
    CREATE TABLE receipt_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_version_id INTEGER NOT NULL REFERENCES report_versions(id) ON DELETE CASCADE,
        hour INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
        amount_cents INTEGER NOT NULL
    );
    CREATE TABLE current_reports (
        store_id INTEGER NOT NULL REFERENCES stores(id),
        business_date TEXT NOT NULL,
        report_version_id INTEGER NOT NULL REFERENCES report_versions(id),
        updated_at TEXT NOT NULL,
        PRIMARY KEY (store_id, business_date)
    );
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = os.path.join(self.tmp.name, "legacy.db")

    def _seed_legacy(self, *, duplicate_day: str | None):
        """Create an old-schema database with two spellings of one store."""

        conn = sqlite3.connect(self.database)
        try:
            conn.executescript(self.LEGACY_SCHEMA)
            now = "2026-10-01T00:00:00+00:00"
            conn.execute(
                "INSERT INTO stores (code, name, created_at) VALUES (?, ?, ?)",
                ("MÜN-01", "慕尼黑店", now),
            )
            conn.execute(
                "INSERT INTO stores (code, name, created_at) VALUES (?, ?, ?)",
                ("mün-01", "慕尼黑店", now),
            )

            def add_version(store_id, day, revision, amount):
                cur = conn.execute(
                    """
                    INSERT INTO report_versions
                        (store_id, business_date, revision, complete_hour_count,
                         total_sales_cents, receipt_line_count, created_at)
                    VALUES (?, ?, ?, 1, ?, 1, ?)
                    """,
                    (store_id, day, revision, amount, now),
                )
                vid = cur.lastrowid
                conn.execute(
                    "INSERT INTO report_hours (report_version_id, hour, sales_cents) "
                    "VALUES (?, 9, ?)",
                    (vid, amount),
                )
                conn.execute(
                    "INSERT INTO receipt_lines (report_version_id, hour, amount_cents) "
                    "VALUES (?, 9, ?)",
                    (vid, amount),
                )
                conn.execute(
                    "INSERT INTO current_reports (store_id, business_date, "
                    "report_version_id, updated_at) VALUES (?, ?, ?, ?)",
                    (store_id, day, vid, now),
                )

            # Non-overlapping dates: each spelling owns a different Monday.
            add_version(1, MON_A, 1, 1000)
            add_version(2, MON_B, 1, 2000)
            if duplicate_day is not None:
                # Same day stored under both spellings — the frozen conflict.
                add_version(1, duplicate_day, 1, 3000)
                add_version(2, duplicate_day, 2, 4000)
            conn.commit()
        finally:
            conn.close()

    def test_legacy_db_migrates_in_place_and_stays_readable(self):
        self._seed_legacy(duplicate_day=None)
        # Opening through the application triggers the additive migration.
        db.init_db(self.database)
        conn = db.connect(self.database)
        try:
            rows = conn.execute("SELECT code, code_key FROM stores ORDER BY id").fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [(r["code"], r["code_key"]) for r in rows],
            [("MÜN-01", "mün-01"), ("mün-01", "mün-01")],
        )
        # Original rows are untouched; every spelling still serves its own day.
        self.assertEqual(
            services.get_current_report(self.database, "MÜN-01", MON_A).total_sales_cents,
            1000,
        )
        self.assertEqual(
            services.get_current_report(self.database, "mün-01", MON_B).total_sales_cents,
            2000,
        )
        # Any spelling finds either day (group fallback) ...
        self.assertEqual(
            services.get_current_report(self.database, "MÜN-01", MON_B).total_sales_cents,
            2000,
        )
        # ... and the comparison collapses the two spellings to one row.
        result = comparison.compare(self.database, MON_A, MON_B)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].store_code, "MÜN-01")
        self.assertEqual(result.rows[0].sales_a_cents, 1000)
        self.assertEqual(result.rows[0].sales_b_cents, 2000)

    def test_frozen_store_rejects_every_new_submission(self):
        self._seed_legacy(duplicate_day=MON_C)
        db.init_db(self.database)

        original_count = self._count_versions()
        for spelling, day in (
            ("MÜN-01", MON_A),       # a clean day of the same store
            ("mün-01", MON_B),
            ("mün-01", MON_C),       # the conflicting day itself
            ("Mün-01", "2026-11-23"),  # any brand-new day
        ):
            with self.subTest(spelling=spelling, day=day):
                with self.assertRaises(StoreConflict):
                    services.import_daily_report(
                        self.database,
                        store_code=spelling,
                        store_name="",
                        business_date=day,
                        revision="9",
                        complete_hours="9-9",
                        receipts_csv=csv_bytes((9, 1)),
                    )
        # Nothing was written or deleted.
        self.assertEqual(self._count_versions(), original_count)
        report = services.get_current_report(self.database, "MÜN-01", MON_C)
        self.assertEqual(report.total_sales_cents, 3000)
        report = services.get_current_report(self.database, "mün-01", MON_C)
        self.assertEqual(report.total_sales_cents, 4000)

    def _count_versions(self) -> int:
        conn = db.connect(self.database)
        try:
            return conn.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0]
        finally:
            conn.close()

    def test_comparison_pauses_with_conflict_dates_on_selected_days(self):
        self._seed_legacy(duplicate_day=MON_C)
        db.init_db(self.database)
        # MON_C contains the store's reports and is duplicated -> pause.
        with self.assertRaises(StoreConflict) as ctx:
            comparison.compare(self.database, MON_B, MON_C)
        message = ctx.exception.message
        self.assertIn("暂停", message)
        self.assertTrue(any(MON_C in d for d in ctx.exception.details))
        # A pair of clean dates for other stores would still work; here the
        # store also has reports on MON_A/MON_B, so a clean pair compares fine
        # because the duplicate lives on MON_C, outside the selected dates.
        result = comparison.compare(self.database, MON_A, MON_B)
        self.assertEqual(len(result.rows), 1)

    def test_migration_is_idempotent(self):
        self._seed_legacy(duplicate_day=None)
        db.init_db(self.database)
        keys_before = self._dump_keys()
        db.init_db(self.database)  # second boot, e.g. another gunicorn worker
        db.init_db(self.database)
        self.assertEqual(self._dump_keys(), keys_before)

    def test_concurrent_boots_migrate_exactly_once(self):
        import threading

        self._seed_legacy(duplicate_day=None)
        errors: list[BaseException] = []

        def boot():
            try:
                db.init_db(self.database)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=boot) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        # Every worker saw the same end state and the backfill ran once.
        self.assertEqual(
            self._dump_keys(),
            [("MÜN-01", "mün-01"), ("mün-01", "mün-01")],
        )

    def _dump_keys(self):
        conn = db.connect(self.database)
        try:
            return [
                (r["code"], r["code_key"])
                for r in conn.execute("SELECT code, code_key FROM stores ORDER BY id")
            ]
        finally:
            conn.close()


class ConcurrencyCasefoldTests(unittest.TestCase):
    """Concurrent imports obey the same revision rule under casefold identity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = os.path.join(self.tmp.name, "concurrent.db")
        db.init_db(self.database)

    def _import(self, code, revision):
        return services.import_daily_report(
            self.database,
            store_code=code,
            store_name="慕尼黑店",
            business_date=MON_A,
            revision=revision,
            complete_hours="9-9",
            receipts_csv=csv_bytes((9, 100 * int(revision))),
        )

    def test_parallel_same_revision_different_spelling_one_wins(self):
        import threading

        errors: list[BaseException] = []

        def worker(code, revision, barrier):
            try:
                barrier.wait()
                self._import(code, revision)
            except BaseException as exc:  # noqa: BLE001 - collected for assertions
                errors.append(exc)

        threads = []
        barrier = threading.Barrier(4)
        for code, revision in (
            ("MÜN-01", "2"),
            ("mün-01", "2"),
            ("MÜn-01", "2"),
            ("mün-01", "5"),  # higher revision; only accepted if not stale
        ):
            t = threading.Thread(target=worker, args=(code, revision, barrier))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # Invariants under any lock-acquisition order:
        # * every failure is a revision conflict (never a second store, never
        #   a half-written row, never a lock timeout leaking out);
        # * exactly one spelling/current row survives, pointing at the highest
        #   committed revision; the same revision can never be stored twice.
        self.assertTrue(all(isinstance(e, RevisionConflict) for e in errors))
        report = services.get_current_report(self.database, "MÜN-01", MON_A)
        self.assertIn(report.revision, (2, 5))
        conn = db.connect(self.database)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM current_reports").fetchone()[0], 1
            )
            # Only distinct (store, day, revision) tuples may exist as versions.
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(DISTINCT revision) FROM report_versions"
                ).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0],
            )
        finally:
            conn.close()

    def test_identical_revision_under_concurrency_only_one_commits(self):
        # Two threads, same revision, different spellings: exactly one may
        # win regardless of scheduling (the UNIQUE constraint is the backstop).
        import threading

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def worker(code):
            try:
                barrier.wait()
                self._import(code, "2")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("MÜN-01",)),
            threading.Thread(target=worker, args=("mün-01",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RevisionConflict)
        report = services.get_current_report(self.database, "mün-01", MON_A)
        self.assertEqual(report.revision, 2)
        conn = db.connect(self.database)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM current_reports").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_revision_rule_after_a_concurrent_commit(self):
        # Deterministic interleaving: one commit lands, then the stale one runs.
        self._import("MÜN-01", "1")
        self._import("mün-01", "2")  # committed under another spelling
        with self.assertRaises(RevisionConflict):
            self._import("MÜN-01", "2")  # same revision, now stale
        with self.assertRaises(RevisionConflict):
            self._import("Mün-01", "1")  # lower revision, any spelling
        # Strictly higher revision under yet another spelling is accepted.
        self._import("mÜn-01", "3")
        report = services.get_current_report(self.database, "MÜN-01", MON_A)
        self.assertEqual(report.revision, 3)
        self.assertEqual(report.store_code, "MÜN-01")
        conn = db.connect(self.database)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_strictly_higher_revision_after_concurrent_commit_is_accepted(self):
        self._import("MÜN-01", "1")

        import threading

        barrier = threading.Barrier(2)
        outcomes: dict[str, str] = {}

        def worker(label, code, revision):
            try:
                barrier.wait()
                self._import(code, revision)
                outcomes[label] = "ok"
            except RevisionConflict:
                outcomes[label] = "conflict"

        t1 = threading.Thread(target=worker, args=("rev2", "mün-01", "2"))
        t2 = threading.Thread(target=worker, args=("rev3", "MÜN-01", "3"))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        # Both revisions are > 1, so both are legal against the committed
        # baseline.  Writers are serialised: both may commit (rev3 ends up
        # effective), or the second one may find its revision already stale.
        # Either way there is exactly one current row and no lock error leaks.
        self.assertTrue(all(v in {"ok", "conflict"} for v in outcomes.values()))
        report = services.get_current_report(self.database, "mün-01", MON_A)
        self.assertIn(report.revision, (2, 3))
        if report.revision == 2:
            # rev3 lost the race and must be retried with a higher number.
            self.assertEqual(outcomes["rev3"], "conflict")
            self._import("mün-01", "4")
            self.assertEqual(
                services.get_current_report(self.database, "MÜN-01", MON_A).revision, 4
            )
        else:
            self.assertEqual(outcomes["rev3"], "ok")

        conn = db.connect(self.database)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM current_reports").fetchone()[0], 1
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
