# -*- coding: utf-8 -*-
"""mc：凭证、DDL、结构校验、分区覆盖写入、行数校验、SQL 超时。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# 只在路径缺失时追加（不插到最前面）：避免把仓库根/tests 目录置于标准库与第三方库
# 之前遮蔽同名模块；按 README 在仓库根运行（或 CI 里 pip install -e .）时，
# 本地包本来就在搜索路径最前（python -m 会把当前目录放在 sys.path[0]）
for _path in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

from _helpers import FakeInstance, FakeOdps, FakeTable, OfflineTestCase  # noqa: E402

from sftp2ods import mc as mc_mod  # noqa: E402
from sftp2ods import parse as parse_mod  # noqa: E402

# 基类 OfflineTestCase 会把 time.sleep 打成空操作（加速测试）；要制造"慢提交"的用例
# 得用这个真身（模块导入时保存，那时还没被 patch）
_REAL_SLEEP = time.sleep


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

    def test_partial_job_aksk_is_config_error(self):
        """AK/SK 只填一半（典型：键名拼错）必须报错、不回退：回退到 env/本机 CLI 会用
        另一个身份写库（审计/计费全错）——即使环境变量里有完整凭证也不许用。"""
        with mock.patch.dict(os.environ, {"ALIYUN_ACCESS_KEY_ID": "EA", "ALIYUN_ACCESS_KEY_SECRET": "ES"}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                mc_mod.load_mc_credentials({"access_key_id": "half"}, "作业文件")
        message = str(ctx.exception)
        self.assertIn("只填了一半", message)
        self.assertIn("access_key_secret", message)
        self.assertNotIn("half", message)  # 半对里的值也不回显

    def test_profile_first(self):
        ak, sk, source = mc_mod.load_mc_credentials({"access_key_id": "A", "access_key_secret": "S"}, "作业")
        self.assertEqual((ak, sk), ("A", "S"))
        self.assertIn("作业", source)

    def test_non_mapping_profile_is_config_error(self):
        """maxcompute 误写成字符串/数组时不能裸 AttributeError，也不能静默回退到其它凭证源。"""
        with self.assertRaises(SystemExit) as ctx:
            mc_mod.load_mc_credentials("aliyun_cli", "作业文件")
        self.assertIn("maxcompute", str(ctx.exception))

    def test_non_mapping_profile_message_hides_content(self):
        """maxcompute 块写错时的报错只报类型、不回显内容（那里可能有 AK/SK 明文）。"""
        with self.assertRaises(SystemExit) as ctx:
            mc_mod.load_mc_credentials("access_key_secret=SEKRET123", "作业文件")
        self.assertNotIn("SEKRET123", str(ctx.exception))

    def test_fullwidth_partition_digits_rejected(self):
        """全角/阿拉伯-印度数字不是业务日：白名单按 ASCII 匹配，否则会写进畸形分区
        （下游按 pt='20260920' 取数得 0 行，而调度显示成功）。"""
        for bad in ("２０２６０９２０", "٢٠٢٦٠٩٢٠"):
            with self.assertRaises(SystemExit):
                mc_mod._partition_literal(bad)

    def test_env_when_profile_empty(self):
        # clear=True：不把宿主机上残留的 ALIYUN_*/ALIBABA_* 变量带进用例（结果随环境变化）
        with mock.patch.dict(os.environ, {"ALIYUN_ACCESS_KEY_ID": "EA", "ALIYUN_ACCESS_KEY_SECRET": "ES"}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(mc_mod.Path, "home", return_value=Path(tmp)):
                    ak, sk, source = mc_mod.load_mc_credentials({}, "作业")
        self.assertEqual((ak, sk), ("EA", "ES"))
        self.assertIn("环境变量", source)

    def test_explicit_cli_profile_does_not_fall_back(self):
        """显式指定 --mc-profile 时找不到/没 AK 必须报错：静默回退到其它 profile
        等于用另一个身份写库。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                (home / ".aliyun").mkdir(parents=True)
                (home / ".aliyun" / "config.json").write_text(
                    '{"current": "other", "profiles": ['
                    '{"name": "other", "mode": "AK", "access_key_id": "OA", "access_key_secret": "OS"}]}',
                    encoding="utf-8",
                )
                with mock.patch.object(mc_mod.Path, "home", return_value=home):
                    with self.assertRaises(SystemExit) as ctx:
                        mc_mod.load_mc_credentials({}, "作业", cli_profile="typo_profile")
                self.assertIn("不会自动回退", str(ctx.exception))

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

    def test_lifecycle_zero_is_rejected_not_silently_dropped(self):
        """lifecycle_days=0（被理解成"永不过期"）必须显式报错：真值判断静默省略会让表按
        项目默认生命周期回收数据。"""
        with self.assertRaises(SystemExit) as ctx:
            self.build(lifecycle_days=0)
        self.assertIn("lifecycle", str(ctx.exception))

    def test_comment_escaping(self):
        ddl = self.build(comment="it's ok")
        self.assertIn("it''s ok", ddl)

    def test_comment_backslash_escaped(self):
        r"""反斜杠是 DDL 串的转义符：以 \ 结尾的注释会吃掉收尾引号、破坏后续语句。"""
        ddl = self.build(comment="尾" + chr(92))
        self.assertIn("尾" + chr(92) * 2 + "'", ddl)

    def test_stored_as_and_lifecycle(self):
        ddl = self.build(stored_as="orc", lifecycle_days=30)
        self.assertIn("stored as orc", ddl)
        self.assertIn("lifecycle 30", ddl)
        # LIFECYCLE 必须排在 TBLPROPERTIES 之后（官方 PK 表语法与 api2ods 的顺序）
        self.assertLess(ddl.index("tblproperties"), ddl.index("lifecycle 30"))

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

    def test_ensure_uses_qualified_table_name(self):
        """取表必须带 project 前缀：连接默认 project 与 target.project 不同时，
        裸名会拿到另一个 project 的同名表（校验错对象、Tunnel 写错库）。"""
        table = FakeTable(columns=[("order_id", "string")])
        names = []

        class RecordingOdps(FakeOdps):
            def get_table(self, name):
                names.append(name)
                return super().get_table(name)

        o = RecordingOdps(table, "ods_demo_di")
        mc_mod.ensure_target_table(o, "my_project", "ods_demo_di", columns(("order_id", "string")))
        self.assertEqual(names, ["my_project.ods_demo_di"])

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

    def test_slow_submit_counts_toward_timeout(self):
        """超时预算从提交前开始：提交本身耗时超过 timeout 时立即 stop+超时抛错，
        而不是再拿一整份新预算去轮询。"""

        class SlowSubmit:
            def __init__(self):
                self.stopped = False

            def is_successful(self):
                return False

            def is_terminated(self):
                return False

            def stop(self):
                self.stopped = True

        inst = SlowSubmit()
        o = mock.Mock()

        def slow_run_sql(_sql):
            time.sleep(0.05)
            return inst

        o.run_sql.side_effect = slow_run_sql
        started = time.monotonic()
        # 基类把 time.sleep 置空以加速测试，本用例要制造"提交耗时"必须恢复真实 sleep
        # （否则 elapsed 恒 ~0，断言退化成恒真、抓不到预算重置）
        with mock.patch.object(time, "sleep", _REAL_SLEEP):
            with self.assertRaises(TimeoutError):
                mc_mod.run_sql_with_timeout(o, "select 1", timeout=0.01)
        elapsed = time.monotonic() - started
        self.assertTrue(inst.stopped)
        # 提交耗时(0.05s)必须计入预算：预算若被重置，最快也要多睡一个轮询周期(1s)才超时
        self.assertLess(elapsed, 0.5)

    def test_polling_error_still_stops_instance(self):
        """轮询中任何异常（网络抖动等）都要先 stop 云端实例再抛出：只覆盖超时分支
        会把昂贵 SQL 留在云端跑（重跑还会与它并发操作同一分区）。"""

        class Exploding:
            def __init__(self):
                self.stopped = False

            def is_successful(self):
                raise RuntimeError("network flake")

            def stop(self):
                self.stopped = True

        inst = Exploding()
        o = mock.Mock()
        o.run_sql.return_value = inst
        with self.assertRaises(RuntimeError):
            mc_mod.run_sql_with_timeout(o, "select 1", timeout=1)
        self.assertTrue(inst.stopped)

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
        written = mc_mod.write_partition(
            FakeOdps(table), table, "demo_project", "ods_demo_di", "20260920", self.factory([["a"], ["b"]]), total=2
        )
        self.assertEqual(written, 2)
        self.assertEqual(table.deleted, ["pt=20260920"])
        self.assertEqual(table.created, ["pt=20260920"])
        # if_exists/if_not_exists 必须是 True：先删再填的幂等性靠它们（漏传会在重跑时报错）
        self.assertEqual(table.delete_if_exists, [True])
        self.assertEqual(table.create_if_not_exists, [True])
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

        written = mc_mod.write_partition(FakeOdps(table), table, "p", "t", "20260920", factory, total=2, retries=3)
        self.assertEqual(written, 2)
        self.assertEqual(calls["n"], 3)  # 第一次校验+失败、第二次校验、第三次写（校验各调一次）
        self.assertEqual(len(table.writers), 1)
        self.assertTrue(table.writers[0].reopen)

    def test_cell_size_check_scan_once_across_retries(self):
        """校验成功后重试不再全量重扫源数据（记忆化）：写入失败一次时工厂只被多叫一次。"""
        table = FakeTable()
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            if calls["n"] == 2:  # 第一次写（第 2 次读）抖一下
                raise OSError("tunnel down")
            return (batch for batch in [[["a"], ["b"]]])

        written = mc_mod.write_partition(FakeOdps(table), table, "p", "t", "20260920", factory, total=2, retries=3)
        self.assertEqual(written, 2)
        # 1=校验、2=首次写入(失败)、3=重试写入；校验没有在重试时重扫
        self.assertEqual(calls["n"], 3)

    def test_oversized_cell_rejected_before_delete(self):
        table = FakeTable()
        big = "x" * (mc_mod.MAX_CELL_BYTES + 1)
        with self.assertRaises(SystemExit):
            mc_mod.write_partition(FakeOdps(table), table, "p", "t", "20260920", self.factory([[big]]), total=1)
        self.assertEqual(table.deleted, [])  # 超限校验失败发生在任何 DDL 之前
        self.assertEqual(table.writers, [])  # 连写入会话都不该打开

    def test_partition_value_whitelist_before_delete(self):
        """与 count_partition 同一套白名单：非法分区值不能先拼进 delete 的 DDL（可能删错分区）。"""
        table = FakeTable()
        for bad in ("2026-09-20", "20260920'; drop table x --", ""):
            with self.assertRaises(SystemExit):
                mc_mod.write_partition(FakeOdps(table), table, "p", "t", bad, self.factory([["a"]]), total=1)
        self.assertEqual(table.deleted, [])  # 校验失败发生在任何 DDL 之前
        self.assertEqual(table.writers, [])  # 连写入会话都不该打开

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
            mc_mod.write_partition(
                FakeOdps(table), table, "p", "t", "20260920", self.factory([["a"]]), total=1, retries=2
            )
        message = str(ctx.exception)
        self.assertIn("分区可能已被清空", message)
        self.assertIn("重跑", message)

    def test_count_mismatch(self):
        table = FakeTable()
        with self.assertRaises(RuntimeError) as ctx:
            mc_mod.write_partition(FakeOdps(table), table, "p", "t", "20260920", self.factory([["a"]]), total=5)
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
        written = mc_mod.write_partition(
            FakeOdps(table), table, "p", "t", "20260920", self.factory([["a"], ["b"]]), total=2, retries=3
        )
        self.assertEqual(written, 2)
        self.assertEqual(table.rows_in("20260920"), [["a"], ["b"]], "重试复用了失败会话，行被提交了两遍")

    def test_partition_ops_use_timeout_protected_ddl(self):
        """分区增删必须走带超时的 DDL（run_sql），不再调用 pyodps 的 delete/create_partition。"""

        class TableWithoutPyodpsApi(FakeTable):
            def delete_partition(self, spec, if_exists=False):
                raise AssertionError("分区删除不该再走 pyodps API（同步、不受 --sql-timeout 保护）")

            def create_partition(self, spec, if_not_exists=False):
                raise AssertionError("分区创建不该再走 pyodps API（同步、不受 --sql-timeout 保护）")

        table = TableWithoutPyodpsApi()
        o = FakeOdps(table)
        written = mc_mod.write_partition(o, table, "p", "t", "20260920", self.factory([["a"]]), total=1)
        self.assertEqual(written, 1)
        sqls = "\n".join(o.sql)
        self.assertIn("drop if exists partition (pt='20260920')", sqls)
        self.assertIn("add if not exists partition (pt='20260920')", sqls)

    def test_drop_and_add_partition_sql_and_timeout(self):
        """删/建分区的 SQL 形态与 timeout 透传（README 承诺分区增删受 --sql-timeout 保护）。"""
        with mock.patch.object(mc_mod, "run_sql_with_timeout") as run:
            mc_mod.drop_partition(mock.Mock(), "p", "t", "pt=20260920", timeout=7)
            mc_mod.add_partition(mock.Mock(), "p", "t", "pt='20260920'", timeout=7)
        sqls = [call.args[1] for call in run.call_args_list]
        self.assertEqual(sqls[0], "alter table p.t drop if exists partition (pt='20260920')")
        self.assertEqual(sqls[1], "alter table p.t add if not exists partition (pt='20260920')")
        self.assertTrue(all(call.kwargs["timeout"] == 7 for call in run.call_args_list))

    def test_sql_spec_normalizes_quotes_and_rejects_bad_input(self):
        self.assertEqual(mc_mod._sql_spec("pt=20260920"), "pt='20260920'")
        self.assertEqual(mc_mod._sql_spec("pt='20260920'"), "pt='20260920'")
        self.assertEqual(mc_mod._sql_spec('pt="20260920"'), "pt='20260920'")
        # 分区值与 count/write 同一套白名单：非 8 位业务日、带注入尾巴的都不拼进 DDL
        for bad in ("pt=", "=20260920", "pt;drop=20260920", "pt=2026-09-20", "pt=20260920'; drop table x --"):
            with self.assertRaises(SystemExit):
                mc_mod._sql_spec(bad)


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

    def test_terminated_but_failed_raises(self):
        """实例终止但未成功：不能只靠 wait_for_success 抛错，显式判定后再返回。"""

        class TerminatedFailed(FakeInstance):
            def is_successful(self):
                return False

            def is_terminated(self):
                return True

            def wait_for_success(self, timeout=None):
                return self  # 模拟"没抛错"的实现差异

        with self.assertRaises(RuntimeError) as ctx:
            mc_mod.count_partition(mock.Mock(run_sql=lambda _sql: TerminatedFailed([])), "p", "t", "20260920")
        self.assertIn("未成功", str(ctx.exception))

    def test_no_rows_is_an_error_not_zero(self):
        """count(*) 必然返回一行：读不到行说明 SQL 没真正执行/reader 异常，
        返回 0 会把「没读到结果」伪装成「分区 0 行」（写前保护会据此误判）。"""
        with self.assertRaises(RuntimeError) as ctx:
            mc_mod.count_partition(self._odps([]), "p", "t", "20260920")
        self.assertIn("未返回行", str(ctx.exception))

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
