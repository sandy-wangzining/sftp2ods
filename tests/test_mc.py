# -*- coding: utf-8 -*-
"""mc：凭证、DDL、结构校验、分区覆盖写入、行数校验、SQL 超时。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 只在路径缺失时追加（不插到最前面）：避免把仓库根/tests 目录置于标准库与第三方库
# 之前遮蔽同名模块；按 README 在仓库根运行（或 CI 里 pip install -e .）时，
# 本地包本来就在搜索路径最前（python -m 会把当前目录放在 sys.path[0]）
for _path in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

from _helpers import FakeInstance, FakeTable, OfflineTestCase  # noqa: E402

from sftp2ods import mc as mc_mod  # noqa: E402
from sftp2ods import parse as parse_mod  # noqa: E402


def columns(*pairs):
    result = []
    for index, (name, type_text) in enumerate(pairs):
        result.append(parse_mod.Column(f"Header {index}", name, type_text, parse_mod.kind_of(type_text), comment=""))
    return result


class TestCredentials(OfflineTestCase):
    def test_pick_aksk_variants(self):
        self.assertEqual(mc_mod._pick_aksk({"ak": "A", "sk": "S"}), ("A", "S"))
        self.assertEqual(mc_mod._pick_aksk({"access_key_id": "A", "access_key_secret": "S"}), ("A", "S"))
        self.assertEqual(mc_mod._pick_aksk({}), ("", ""))

    def test_profile_first(self):
        ak, sk, source = mc_mod.load_mc_credentials({"access_key_id": "A", "access_key_secret": "S"}, "作业")
        self.assertEqual((ak, sk), ("A", "S"))
        self.assertIn("作业", source)

    def test_env_when_profile_empty(self):
        # clear=True：不把宿主机上残留的 ALIYUN_*/ALIBABA_* 变量带进用例（结果随环境变化）
        with mock.patch.dict(os.environ, {"ALIYUN_ACCESS_KEY_ID": "EA", "ALIYUN_ACCESS_KEY_SECRET": "ES"}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(mc_mod.Path, "home", return_value=Path(tmp)):
                    ak, sk, source = mc_mod.load_mc_credentials({}, "作业")
        self.assertEqual((ak, sk), ("EA", "ES"))
        self.assertIn("环境变量", source)

    def test_cli_config_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                (home / ".aliyun").mkdir(parents=True)
                (home / ".aliyun" / "config.json").write_text(
                    '{"current": "maxcompute", "profiles": ['
                    '{"name": "maxcompute", "mode": "AK", "access_key_id": "CA", "access_key_secret": "CS"}]}',
                    encoding="utf-8",
                )
                with mock.patch.object(mc_mod.Path, "home", return_value=home):
                    ak, sk, source = mc_mod.load_mc_credentials({}, "作业")
        self.assertEqual((ak, sk), ("CA", "CS"))
        self.assertIn("aliyun CLI", source)

    def test_missing_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(mc_mod.Path, "home", return_value=Path(tmp)):
                    with self.assertRaises(SystemExit) as ctx:
                        mc_mod.load_mc_credentials({}, "作业")
        self.assertIn("AccessKey", str(ctx.exception))


class TestDdl(OfflineTestCase):
    def build(self, **overrides):
        kwargs = dict(
            project="my_project",
            table="ods_demo_di",
            columns=columns(("order_id", "string"), ("amount", "decimal(19,10)")),
            comment="示例表",
            stored_as="",
            lifecycle_days=None,
        )
        kwargs.update(overrides)
        return mc_mod.build_table_ddl(**kwargs)

    def test_basic(self):
        ddl = self.build()
        self.assertIn("create table if not exists my_project.ods_demo_di (", ddl)
        self.assertIn("order_id string", ddl)
        self.assertIn("amount decimal(19,10)", ddl)
        self.assertIn("partitioned by (pt string", ddl)
        self.assertIn("tblproperties ('comment' = '示例表')", ddl)

    def test_comment_escaping(self):
        ddl = self.build(comment="it's ok")
        self.assertIn("it''s ok", ddl)

    def test_stored_as_and_lifecycle(self):
        ddl = self.build(stored_as="orc", lifecycle_days=30)
        self.assertIn("stored as orc", ddl)
        self.assertIn("lifecycle 30", ddl)

    def test_identifier_guard(self):
        for bad in ("bad-name", "a;drop", "1x"):
            with self.assertRaises(SystemExit):
                self.build(table=bad)


class TestVerifySchema(OfflineTestCase):
    def test_ok(self):
        table = FakeTable(columns=[("order_id", "string"), ("amount", "decimal(19,10)")])
        mc_mod.verify_table_schema(table, "t", columns(("order_id", "string"), ("amount", "decimal(19,10)")))

    def test_case_insensitive_names_and_normalized_types(self):
        table = FakeTable(columns=[("ORDER_ID", "STRING"), ("amount", "DECIMAL(19, 10)")])
        mc_mod.verify_table_schema(table, "t", columns(("order_id", "string"), ("amount", "decimal(19,10)")))

    def test_view_rejected(self):
        table = FakeTable(columns=[("order_id", "string")])
        table.is_virtual_view = True
        with self.assertRaises(SystemExit):
            mc_mod.verify_table_schema(table, "t", columns(("order_id", "string")))

    def test_transactional_rejected(self):
        table = FakeTable(columns=[("order_id", "string")])
        table.is_transactional = True
        with self.assertRaises(SystemExit):
            mc_mod.verify_table_schema(table, "t", columns(("order_id", "string")))

    def test_name_order_mismatch(self):
        table = FakeTable(columns=[("amount", "decimal(19,10)"), ("order_id", "string")])
        with self.assertRaises(SystemExit) as ctx:
            mc_mod.verify_table_schema(table, "t", columns(("order_id", "string"), ("amount", "decimal(19,10)")))
        self.assertIn("表结构与配置不一致", str(ctx.exception))

    def test_type_mismatch(self):
        table = FakeTable(columns=[("order_id", "bigint")])
        with self.assertRaises(SystemExit) as ctx:
            mc_mod.verify_table_schema(table, "t", columns(("order_id", "string")))
        self.assertIn("类型", str(ctx.exception))

    def test_partition_mismatch(self):
        table = FakeTable(columns=[("order_id", "string")], partitions=(("ds", "string"),))
        with self.assertRaises(SystemExit):
            mc_mod.verify_table_schema(table, "t", columns(("order_id", "string")))

    def test_ensure_creates_and_verifies(self):
        table = FakeTable(columns=[("order_id", "string")])

        class FakeOdps:
            def __init__(self):
                self.sql = []

            def run_sql(self, sql):
                self.sql.append(sql)
                return FakeInstance()

            def get_table(self, name):
                return table

        o = FakeOdps()
        got = mc_mod.ensure_target_table(o, "my_project", "ods_demo_di", columns(("order_id", "string")))
        self.assertIs(got, table)
        self.assertIn("create table if not exists", o.sql[0])


class TestRunSql(OfflineTestCase):
    def test_success(self):
        o = mock.Mock()
        o.run_sql.return_value = FakeInstance()
        instance = mc_mod.run_sql_with_timeout(o, "select 1", timeout=1)
        self.assertTrue(instance.is_successful())

    def test_terminated_failure_propagates(self):
        class Failing:
            def is_successful(self):
                return False

            def is_terminated(self):
                return True

            def wait_for_success(self, timeout=None):
                raise RuntimeError("sql failed: bad syntax")

        o = mock.Mock()
        o.run_sql.return_value = Failing()
        with self.assertRaises(RuntimeError) as ctx:
            mc_mod.run_sql_with_timeout(o, "select 1", timeout=1)
        self.assertIn("bad syntax", str(ctx.exception))

    def test_timeout_stops_instance(self):
        class Hanging:
            stopped = False

            def is_successful(self):
                return False

            def is_terminated(self):
                return False

            def stop(self):
                Hanging.stopped = True

        o = mock.Mock()
        o.run_sql.return_value = Hanging()
        with self.assertRaises(TimeoutError):
            mc_mod.run_sql_with_timeout(o, "select 1", timeout=0.001)
        self.assertTrue(Hanging.stopped)


class TestWritePartition(OfflineTestCase):
    def factory(self, rows):
        return lambda: (batch for batch in [[row] for row in rows])

    def test_happy_path(self):
        table = FakeTable()
        written = mc_mod.write_partition(table, "ods_demo_di", "20260920", self.factory([["a"], ["b"]]), total=2)
        self.assertEqual(written, 2)
        self.assertEqual(table.deleted, ["pt=20260920"])
        self.assertEqual(table.created, ["pt=20260920"])
        self.assertTrue(table.writers[0].reopen)
        self.assertEqual(table.rows_in("20260920"), [["a"], ["b"]])

    def test_retry_rebuilds_session_and_batches(self):
        table = FakeTable()
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("tunnel down")
            return (batch for batch in [[["a"], ["b"]]])

        written = mc_mod.write_partition(table, "t", "20260920", factory, total=2, retries=3)
        self.assertEqual(written, 2)
        self.assertEqual(calls["n"], 3)  # 第一次校验+失败、第二次校验、第三次写（校验各调一次）
        self.assertEqual(len(table.writers), 1)
        self.assertTrue(table.writers[0].reopen)

    def test_oversized_cell_rejected_before_delete(self):
        table = FakeTable()
        big = "x" * (mc_mod.MAX_CELL_BYTES + 1)
        with self.assertRaises(SystemExit):
            mc_mod.write_partition(table, "t", "20260920", self.factory([[big]]), total=1)
        self.assertEqual(table.deleted, [])

    def test_exhausted_retries_marks_partition_dirty(self):
        table = FakeTable()

        def broken_write(rows):
            raise OSError("tunnel down")

        original_open = table.open_writer

        def open_writer(partition=None, reopen=False):
            writer = original_open(partition=partition, reopen=reopen)
            writer.write = broken_write
            return writer

        table.open_writer = open_writer
        with self.assertRaises(RuntimeError) as ctx:
            mc_mod.write_partition(table, "t", "20260920", self.factory([["a"]]), total=1, retries=2)
        message = str(ctx.exception)
        self.assertIn("分区可能已被清空", message)
        self.assertIn("重跑", message)

    def test_count_mismatch(self):
        table = FakeTable()
        with self.assertRaises(RuntimeError) as ctx:
            mc_mod.write_partition(table, "t", "20260920", self.factory([["a"]]), total=5)
        self.assertIn("写入行数异常", str(ctx.exception))

    def test_retry_after_failed_session_does_not_duplicate(self):
        """第一次写入"写了半截再失败"（会话里残留未提交的块），重试必须开新会话。

        替身按 pyodps 语义建模 reopen：reopen=False 会把失败会话的块与本轮一起提交。
        生产代码漏传 reopen=True 时，本用例会看到分区里出现重复行。
        """
        table = FakeTable()
        original_open = table.open_writer
        calls = {"n": 0}

        def open_writer(partition=None, reopen=False):
            writer = original_open(partition=partition, reopen=reopen)
            calls["n"] += 1
            if calls["n"] == 1:
                real_write = writer.write

                def half_then_fail(rows):
                    real_write(rows)
                    raise OSError("tunnel down after partial write")

                writer.write = half_then_fail
            return writer

        table.open_writer = open_writer
        written = mc_mod.write_partition(table, "t", "20260920", self.factory([["a"], ["b"]]), total=2, retries=3)
        self.assertEqual(written, 2)
        self.assertEqual(table.rows_in("20260920"), [["a"], ["b"]], "重试复用了失败会话，行被提交了两遍")


class TestFakeWriterSessionModel(OfflineTestCase):
    """替身本身的会话语义校准（reopen=True 开新会话 / reopen=False 复用）。"""

    def test_reopen_semantics_match_pyodps(self):
        table = FakeTable()
        with self.assertRaises(OSError):
            with table.open_writer(partition="pt=20260920", reopen=True) as writer:
                writer.write([["stale"]])
                raise OSError("tunnel down")
        with table.open_writer(partition="pt=20260920", reopen=True) as writer:
            writer.write([["fresh"]])
        self.assertEqual(table.rows_in("20260920"), [["fresh"]], "reopen=True 应开新会话、丢弃旧块")

        table2 = FakeTable()
        with self.assertRaises(OSError):
            with table2.open_writer(partition="pt=20260920", reopen=True) as writer:
                writer.write([["stale"]])
                raise OSError("tunnel down")
        with table2.open_writer(partition="pt=20260920", reopen=False) as writer:
            writer.write([["fresh"]])
        self.assertEqual(table2.rows_in("20260920"), [["stale"], ["fresh"]], "reopen=False 应复用失败会话的块")


class TestFakeInstanceStop(OfflineTestCase):
    def test_stop_marks_terminated(self):
        """stop 后 is_terminated 必须为真：否则"stop 后轮询终止"的代码在测试里永不退出
        （单测把 sleep 变成空操作，死循环不会被超时打断）。"""
        instance = FakeInstance()
        self.assertFalse(instance.is_terminated())
        instance.stop()
        self.assertTrue(instance.stopped)
        self.assertTrue(instance.is_terminated())


class TestCellBytes(OfflineTestCase):
    def test_types(self):
        self.assertEqual(mc_mod._cell_bytes(None), 0)
        self.assertEqual(mc_mod._cell_bytes("中文"), 6)
        self.assertEqual(mc_mod._cell_bytes(b"\xff\xfe"), 2)  # bytes 按原始长度算，不走 str() 的 b'...' 形态
        self.assertEqual(mc_mod._cell_bytes(bytearray(b"abc")), 3)
        self.assertEqual(mc_mod._cell_bytes(123), 3)


class TestCountPartition(OfflineTestCase):
    def _odps(self, rows):
        o = mock.Mock()
        o.run_sql.return_value = FakeInstance(rows=rows)
        return o

    def test_named_row(self):
        self.assertEqual(mc_mod.count_partition(self._odps([{"cnt": 7}]), "p", "t", "20260920"), 7)

    def test_tuple_row(self):
        self.assertEqual(mc_mod.count_partition(self._odps([(3,)]), "p", "t", "20260920"), 3)

    def test_no_rows(self):
        self.assertEqual(mc_mod.count_partition(self._odps([]), "p", "t", "20260920"), 0)

    def test_identifier_guard(self):
        with self.assertRaises(SystemExit):
            mc_mod.count_partition(self._odps([]), "p", "t;drop", "20260920")

    def test_partition_value_whitelist(self):
        """分区值拼进 SQL 前必须过白名单（pt 恒为 8 位业务日），注入形态一律拒绝。"""
        for bad in ("2026-09-20", "20260920'; drop table x --", "", "202609201"):
            with self.assertRaises(SystemExit):
                mc_mod.count_partition(self._odps([]), "p", "t", bad)


if __name__ == "__main__":
    unittest.main()
