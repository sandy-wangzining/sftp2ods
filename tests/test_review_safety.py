"""原子发布与通知时序回归。"""

import tempfile
from decimal import Decimal
from unittest import mock

from _helpers import FakeOdps, FakeTable, OfflineTestCase, csv_bytes
from test_cli import HEADERS, World

from sftp2ods import mc


class TestNotificationTiming(OfflineTestCase):
    def test_dry_run_logs_extra_columns_without_success_notification(self):
        data = csv_bytes(HEADERS + ["Note"], [["o1", "1.50", "hello"]])
        with tempfile.TemporaryDirectory() as tmp, World(tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920", dry_run=True), 0)
            self.assertEqual([c for c in world.notify_calls if "新增列" in c["title"]], [])
            self.assertEqual(world.table.written, {})
            self.assertTrue(any("新增列" in line for line in world.access_logs))

    def test_failed_write_does_not_send_ingested_notification(self):
        data = csv_bytes(HEADERS + ["Note"], [["o1", "1.50", "hello"]])
        with tempfile.TemporaryDirectory() as tmp, World(tmp, files={"/data/report_20260920.csv": data}) as world:
            with mock.patch.object(mc, "write_partition", side_effect=RuntimeError("Tunnel unavailable")):
                self.assertEqual(world.sync(bizdate="20260920"), 1)
            self.assertEqual([c for c in world.notify_calls if "新增列" in c["title"]], [])

    def test_failed_final_verification_does_not_notify_ingested_data(self):
        data = csv_bytes(HEADERS + ["Note"], [["o1", "1.50", "hello"]])
        with tempfile.TemporaryDirectory() as tmp, World(tmp, files={"/data/report_20260920.csv": data}) as world:
            real_count = mc.count_partition

            def count(o, project, name, pt, **kwargs):
                return 999 if name == world.odps.table_name else real_count(o, project, name, pt, **kwargs)

            with mock.patch.object(mc, "count_partition", side_effect=count):
                self.assertEqual(world.sync(bizdate="20260920"), 1)
            self.assertTrue(world.table.rows_in("20260920"))
            self.assertEqual([c for c in world.notify_calls if "新增列" in c["title"]], [])

    def test_notification_observes_published_and_verified_partition(self):
        data = csv_bytes(HEADERS + ["Note"], [["o1", "1.50", "hello"]])
        with tempfile.TemporaryDirectory() as tmp, World(tmp, files={"/data/report_20260920.csv": data}) as world:
            capture = world._capture_notify

            def notify(*args, **kwargs):
                self.assertEqual(world.table.rows_in("20260920")[0][0], "o1")
                self.assertTrue(world.ledger())
                return capture(*args, **kwargs)

            with mock.patch("sftp2ods.cli.notify", side_effect=notify):
                self.assertEqual(world.sync(bizdate="20260920"), 0)
            self.assertEqual(len(world.notify_calls), 1)


class TestAtomicPublication(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.table = FakeTable(columns=[("value", "string")])
        self.odps = FakeOdps(self.table, "t")
        self.table.written["pt=20260920"] = [["old"]]
        self.records = [["new1"], ["new2"]]
        self.expected = [["new1"], ["new2"]]

    def write(self, **kwargs):
        return mc.write_partition(
            self.odps, self.table, "p", "t", "20260920", lambda: iter([self.records]), total=2, retries=2, **kwargs
        )

    def assert_old_target(self):
        self.assertEqual(self.table.written["pt=20260920"], [["old"]])
        self.assertEqual(self.table.deleted, [])

    def test_tunnel_failure_preserves_old_target(self):
        original = self.odps.get_table

        def get_table(name):
            table = original(name)
            if "tmp_" in name:
                table.open_writer = mock.Mock(side_effect=RuntimeError("Tunnel failed"))
            return table

        with mock.patch.object(self.odps, "get_table", side_effect=get_table):
            with self.assertRaises(RuntimeError):
                self.write()
        self.assert_old_target()
        self.assertFalse(any(sql.startswith("insert overwrite") for sql in self.odps.sql))

    def test_staging_count_mismatch_cannot_publish(self):
        with mock.patch.object(mc, "count_partition", return_value=0):
            with self.assertRaises(RuntimeError):
                self.write()
        self.assert_old_target()
        self.assertFalse(any(sql.startswith("insert overwrite") for sql in self.odps.sql))

    def test_lost_publication_response_reuses_verified_stage(self):
        original = self.odps.run_sql
        publications = []

        def run_sql(sql):
            result = original(sql)
            if sql.startswith("insert overwrite"):
                publications.append(sql)
                if len(publications) == 1:
                    result.id = "completed-publish"
                    result.is_successful = mock.Mock(side_effect=TimeoutError("response lost after commit"))
                    result.is_terminated = mock.Mock(return_value=True)
                    result.stop = mock.Mock()
            return result

        with mock.patch.object(self.odps, "run_sql", side_effect=run_sql):
            self.assertEqual(self.write(), 2)
        self.assertEqual(len(publications), 2)
        self.assertEqual(self.table.written["pt=20260920"], self.expected)
        self.assertEqual(len(self.odps.stage.writers), 1)
        self.assertEqual(self.table.deleted, [])

    def test_failed_publication_keeps_verified_stage_for_retry(self):
        original = self.odps.run_sql

        def run_sql(sql):
            if sql.startswith("insert overwrite"):
                result = mock.Mock()
                result.id = "rejected-publish"
                result.is_successful.return_value = False
                result.is_terminated.return_value = True
                result.wait_for_success.side_effect = RuntimeError("publish rejected")
                return result
            return original(sql)

        with mock.patch.object(self.odps, "run_sql", side_effect=run_sql):
            with self.assertRaisesRegex(RuntimeError, "暂存表"):
                self.write()
        self.assert_old_target()
        self.assertEqual(len(self.odps.stage.writers), 1)
        self.assertTrue(self.odps.stages)

    def test_unknown_publication_blocks_retry_and_a_later_writer(self):
        from sftp2ods import _common

        original = self.odps.run_sql
        instance = mock.Mock()
        instance.id = "pending-publish"
        instance.is_successful.side_effect = ConnectionError("poll failed")
        instance.is_terminated.return_value = False
        instance.stop.side_effect = ConnectionError("cancel failed")
        publications = []

        def run_sql(sql):
            if sql.startswith("insert overwrite"):
                publications.append(sql)
                return instance
            return original(sql)

        self.odps.get_instance = mock.Mock(return_value=instance)
        with mock.patch.object(self.odps, "run_sql", side_effect=run_sql):
            with self.assertRaises(_common.UnresolvedSqlError):
                self.write()
            with self.assertRaises(_common.UnresolvedSqlError):
                self.write()
        self.assertEqual(len(publications), 1)
        self.assert_old_target()
        instance.is_terminated.return_value = True
        self.assertEqual(self.write(), 2)
        self.assertEqual(self.table.written["pt=20260920"], self.expected)

    def test_cleanup_failure_does_not_fail_published_partition(self):
        original = self.odps.run_sql

        def run_sql(sql):
            if sql.startswith("drop table"):
                raise RuntimeError("cleanup failed")
            return original(sql)

        with mock.patch.object(self.odps, "run_sql", side_effect=run_sql):
            self.assertEqual(self.write(), 2)
        self.assertEqual(self.table.written["pt=20260920"], self.expected)
        ddl = next(sql for sql in self.odps.sql if sql.startswith("create table"))
        self.assertIn("'lifecycle.deletemeta'='true'", ddl)
        self.assertIn("lifecycle 1", ddl)

    def test_interrupt_preserves_old_target(self):
        original = self.odps.get_table

        def get_table(name):
            table = original(name)
            if "tmp_" in name:
                table.open_writer = mock.Mock(side_effect=KeyboardInterrupt())
            return table

        with mock.patch.object(self.odps, "get_table", side_effect=get_table):
            with self.assertRaises(KeyboardInterrupt):
                self.write()
        self.assert_old_target()

    def test_publication_preserves_all_configured_columns_in_order(self):
        table = FakeTable(columns=[("id", "string"), ("amount", "decimal(18,2)"), ("note", "string")])
        odps = FakeOdps(table, "t")
        row = ["a", Decimal("1234567890123456.01"), None]
        self.assertEqual(mc.write_partition(odps, table, "p", "t", "20260920", lambda: iter([[row]]), total=1), 1)
        self.assertEqual(table.rows_in("20260920"), [row])
        sql = next(sql for sql in odps.sql if sql.startswith("insert overwrite"))
        self.assertIn("select id, amount, note from ", sql)
        self.assertNotIn("select *", sql)
