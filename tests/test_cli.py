# -*- coding: utf-8 -*-
"""cli：主流程（同步/跳过/补数/dry-run/缺文件/错误路径/退出码/运行锁）。"""

from __future__ import annotations

import json
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

from _helpers import (  # noqa: E402
    FakeOdps,
    FakeSftp,
    FakeTable,
    OfflineTestCase,
    add_dir,
    add_file,
    connect_to,
    csv_bytes,
    make_args,
    minimal_job,
)

from sftp2ods import cli as cli_mod  # noqa: E402
from sftp2ods import config  # noqa: E402
from sftp2ods import mc as mc_mod  # noqa: E402
from sftp2ods import parse as parse_mod  # noqa: E402
from sftp2ods import sftp as sftp_mod  # noqa: E402
from sftp2ods import state as state_mod  # noqa: E402
from sftp2ods.utils import FatalSourceError, RunLock, collect_secret_values, redact_secrets  # noqa: E402

HEADERS = ["Order ID", "Settlement amount"]

# mc.write_partition 的调用顺序：第 1 次 batches_factory() 是写前单格大小检查，
# 第 2 次才是真正写库（见 mc.write_partition._do）。改实现时同步改这里。
WRITE_PHASE_CALL_INDEX = 2


def report(rows) -> bytes:
    return csv_bytes(HEADERS, rows)


class World:
    """一个离线世界：假 SFTP + 假 ODPS + 捕获飞书通知。"""

    def __init__(self, tmp, job=None, files=None, fake=None, connect=None, download_dir=None):
        self.tmp = Path(tmp)
        self.job = config.normalize_job(job or minimal_job())
        if download_dir:
            self.job["source"]["download_dir"] = str(download_dir)
        self.job_path = self.tmp / "jobs" / "demo.json"
        self.job_path.parent.mkdir(parents=True, exist_ok=True)
        self.job_path.write_text(json.dumps(self.job, ensure_ascii=False, indent=2), encoding="utf-8")
        self.fake = fake or FakeSftp()
        for remote, data in (files or {}).items():
            add_file(self.fake, remote, data)
        self.table = FakeTable(columns=[(c["name"], c["type"]) for c in self.job["parse"]["columns"]])
        self.odps = FakeOdps(self.table, self.job["target"]["table"])
        self.notify_calls = []
        self.access_logs = []
        self._patches = [
            mock.patch.object(sftp_mod.SftpSource, "_connect", connect or connect_to(self.fake)),
            mock.patch.object(mc_mod, "connect_odps", lambda *a, **k: self.odps),
            mock.patch.object(cli_mod, "notify", self._capture_notify),
        ]

    def _capture_notify(self, webhook, title, lines, footer="", enabled=True, timeout=15):
        self.notify_calls.append(
            {"webhook": webhook, "title": title, "lines": list(lines), "footer": footer, "enabled": enabled}
        )
        return True

    def __enter__(self):
        started = []
        try:
            for patcher in self._patches:
                patcher.start()
                started.append(patcher)
            self.access_logs.clear()
            self._log_patch = mock.patch.object(cli_mod, "log", self.access_logs.append)
            self._log_patch.start()
            started.append(self._log_patch)
        except BaseException:
            # 中途失败要把已启动的 patcher 全部回滚：否则全局 mock 留在原地，
            # 后续用例对着被 mock 的世界跑，故障点还会指向无关的地方
            for patcher in reversed(started):
                patcher.stop()
            raise
        return self

    def __exit__(self, *exc_info):
        self._log_patch.stop()
        for patcher in reversed(self._patches):
            patcher.stop()

    @property
    def download_dir(self) -> Path:
        return config.resolve_download_dir(self.job, self.job_path)

    def ledger(self) -> dict:
        return state_mod.load_state(self.download_dir / state_mod.STATE_FILE_NAME)

    def sync(self, bizdate="", **kwargs):
        return cli_mod.run_sync(self.job, {}, make_args(**kwargs), self.job_path, bizdate=bizdate)

    def check(self, bizdate="", **kwargs):
        return cli_mod.run_check(self.job, {}, make_args(**kwargs), self.job_path, bizdate=bizdate)


class CliTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


class TestWorldHarness(CliTestCase):
    def test_enter_rolls_back_started_patches_on_failure(self):
        """__enter__ 中途失败时回滚已启动的 patcher，不能把全局 mock 留在原地污染后续用例。"""
        world = World(self.tmp)
        first, second = mock.MagicMock(), mock.MagicMock()
        second.start.side_effect = RuntimeError("boom")
        world._patches = [first, second]
        with self.assertRaises(RuntimeError):
            with world:
                pass
        first.stop.assert_called_once_with()
        second.stop.assert_not_called()


class TestBackfillEmptyRemote(CliTestCase):
    def test_empty_remote_with_range_reports_cleanly(self):
        """远端一个文件都没有 + 显式区间：干净报"区间没有匹配文件"，不能 min() 空序列崩溃。"""
        with World(self.tmp) as world:
            rc = world.sync(start_date="2026-09-01", end_date="2026-09-02")
        self.assertEqual(rc, 1)
        self.assertTrue(any("没有任何匹配文件" in line for line in world.access_logs), world.access_logs[-3:])


class TestSyncHappyPath(CliTestCase):
    def test_write_skip_and_force(self):
        data = report([["o1", "1.50"], ["o2", "2.50"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            rows = world.table.rows_in("20260920")
            self.assertEqual([r[0] for r in rows], ["o1", "o2"])
            ledger = world.ledger()
            self.assertEqual(ledger["report_20260920.csv"]["pt"], "20260920")
            self.assertEqual(ledger["report_20260920.csv"]["size"], len(data))

            deletes = len(world.table.deleted)
            downloads = len(world.fake.downloaded)
            self.assertEqual(world.sync(bizdate="20260920"), 0)  # 台账命中，跳过
            self.assertEqual(len(world.table.deleted), deletes)
            self.assertEqual(len(world.fake.downloaded), downloads)

            self.assertEqual(world.sync(bizdate="20260920", force=True), 0)  # 强制重写
            self.assertEqual(len(world.table.deleted), deletes + 1)
            self.assertEqual(len(world.fake.downloaded), downloads)  # 本地文件在，不重下

    def test_multi_file_same_date_merged(self):
        job = minimal_job(
            source={"root": "/data", "layout": "flat", "file_regex": "report_(?P<date>\\d{8})(?:_\\d)?\\.csv"}
        )
        world = World(
            self.tmp,
            job=job,
            files={
                "/data/report_20260920.csv": report([["o1", "1.00"]]),
                "/data/report_20260920_2.csv": report([["o2", "2.00"]]),
            },
        )
        with world:
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            self.assertEqual(len(world.table.rows_in("20260920")), 2)
            self.assertEqual(len(world.ledger()), 2)

    def test_dry_run_touches_nothing(self):
        data = report([["o1", "1.50"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920", dry_run=True), 0)
            self.assertEqual(world.table.deleted, [])
            self.assertFalse((world.download_dir / state_mod.STATE_FILE_NAME).exists())
            self.assertTrue(any("dry-run 完成" in line for line in world.access_logs))


class TestSyncRanges(CliTestCase):
    def test_start_end_range(self):
        files = {
            "/data/report_20260920.csv": report([["o1", "1.00"]]),
            "/data/report_20260921.csv": report([["o2", "2.00"]]),
            "/data/report_20260922.csv": report([["o3", "3.00"]]),
        }
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(start_date="2026-09-20", end_date="2026-09-21"), 0)
            self.assertIn("pt=20260920", world.table.deleted)
            self.assertIn("pt=20260921", world.table.deleted)
            self.assertNotIn("pt=20260922", world.table.deleted)

    def test_explicit_empty_bizdate_is_arg_error(self):
        """显式传空 --bizdate（调度脚本 `--bizdate "$pt"` 且 $pt 未定义）不能按"未指定"
        静默处理：按参数问题报错退出（退出码 2），而不是回退成"处理全部日期"。"""
        for empty in ("", "   "):
            msg = cli_mod._check_cli_args(make_args(bizdate=empty))
            self.assertTrue(msg, "空 bizdate 必须返回错误文案")
            self.assertIn("日期", msg)

    def test_bizdate_with_range_is_rejected(self):
        # 参数互斥在命令行阶段就拦下：退出码 2（参数问题），不进远端流程
        with World(self.tmp) as world:
            rc = cli_mod.main(["--job", str(world.job_path), "--bizdate", "20260920", "--start-date", "2026-09-19"])
            self.assertEqual(rc, 2)
            self.assertTrue(any("互斥" in str(line) for line in world.access_logs))

    def test_start_after_end_is_rejected(self):
        with World(self.tmp) as world:
            rc = cli_mod.main(["--job", str(world.job_path), "--start-date", "2026-09-21", "--end-date", "2026-09-20"])
            self.assertEqual(rc, 2)
            self.assertTrue(any("不能早于" in str(line) for line in world.access_logs))

    def test_malformed_date_arg_returns_2(self):
        """日期写法不对（--start-date 2026-9-1）是参数问题：退出码 2，不是数据问题（1）。"""
        with World(self.tmp) as world:
            rc = cli_mod.main(["--job", str(world.job_path), "--start-date", "2026-9-1"])
            self.assertEqual(rc, 2)
            self.assertTrue(any("start-date" in str(line) for line in world.access_logs))

    def test_range_without_overlap_fails(self):
        """补数区间与远端完全没有交集时必须失败：以前会静默 rc=0，和"补数成功"长得一样。"""
        files = {"/data/report_20260920.csv": report([["o1", "1.00"]])}
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(start_date="2026-01-01", end_date="2026-01-31"), 1)
            self.assertTrue(any("没有交集" in str(line) for line in world.access_logs))
            self.assertEqual(world.table.deleted, [])

    def test_start_date_after_expected_latest_fails(self):
        files = {"/data/report_20260920.csv": report([["o1", "1.00"]])}
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(start_date="2099-01-01"), 1)


class TestSyncRangeNoOverlap(CliTestCase):
    """显式补数区间在远端没有任何匹配文件 → 失败（1.4.1），不能静默 rc=0 像"补数成功"。"""

    def test_range_outside_remote_fails_without_writing(self):
        # 远端只有 20200101，补数区间 [2020-01-02, 2020-01-03] 完全在数据范围之外
        files = {"/data/report_20200101.csv": report([["o1", "1.00"]])}
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(start_date="2020-01-02", end_date="2020-01-03"), 1)
            self.assertEqual(world.table.deleted, [], "失败时不应有任何删分区动作")
            self.assertEqual(world.table.written, {}, "失败时不应写入任何分区")
            self.assertTrue(any("补数区间" in str(line) for line in world.access_logs))

    def test_range_partial_overlap_writes_intersection(self):
        files = {
            "/data/report_20260920.csv": report([["o1", "1.00"]]),
            "/data/report_20260922.csv": report([["o2", "2.00"]]),
        }
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(start_date="2026-09-20", end_date="2026-09-22"), 0)
            self.assertEqual(sorted(world.table.deleted), ["pt=20260920", "pt=20260922"])

    def test_range_all_uploaded_does_not_false_fail(self):
        """区间内文件都在、只是台账已上传：proc_dates 仍非空，第二次运行不能误判失败。"""
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(start_date="2026-09-20", end_date="2026-09-20"), 0)
            deletes = len(world.table.deleted)
            self.assertEqual(world.sync(start_date="2026-09-20", end_date="2026-09-20"), 0)
            self.assertEqual(len(world.table.deleted), deletes, "已上传的区间被误判成失败或重写")

    def test_range_outside_remote_force_succeeds(self):
        files = {"/data/report_20200101.csv": report([["o1", "1.00"]])}
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(start_date="2020-01-02", end_date="2020-01-03", force=True), 0)
            self.assertEqual(world.table.deleted, [])

    def test_full_run_without_range_still_warns(self):
        files = {
            "/data/report_20260920.csv": report([["o1", "1.00"]]),
            "/data/report_20260922.csv": report([["o2", "2.00"]]),
        }
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(), 0)  # 不传区间：缺 20260921 仍只告警、照常同步
            self.assertTrue(any("缺少文件" in str(line) for line in world.access_logs))


class TestSyncGuards(CliTestCase):
    def test_missing_single_bizdate_fails_and_alerts(self):
        # ①显式点名单日（--bizdate）而该日无文件：必须失败（rc=1），不能静默 rc=0 让调度以为成功。
        #   原用例（本方法的前身 test_missing_file_warns_and_continues）断言 rc=0，是 1.2.0 旧语义；
        #   单日边界已改为失败（见 CHANGELOG 1.4.0），故此处改为断言新行为。
        files = {"/data/report_20260920.csv": report([["o1", "1.00"]])}
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(bizdate="20260921"), 1)
            self.assertEqual(world.table.deleted, [])
            joined = "\n".join(str(line) for line in world.access_logs)
            self.assertIn("20260921", joined)
            self.assertEqual(len(world.notify_calls), 1)
            self.assertIn("20260921", world.notify_calls[0]["title"])

    def test_missing_single_bizdate_force_still_continues(self):
        # --force 是"我知道这天可能没数"的显式放行：单日缺失也不再失败（rc=0，仅告警）。
        files = {"/data/report_20260920.csv": report([["o1", "1.00"]])}
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(bizdate="20260921", force=True), 0)
            self.assertEqual(world.table.deleted, [])
            self.assertTrue(any("缺少文件" in str(line) for line in world.access_logs))

    def test_missing_without_bizdate_still_warns(self):
        # ②不传业务日（处理远端全部日期）时缺某天：仍只告警不失败（回归，防改坏 1.2.0 原设计）。
        files = {
            "/data/report_20260920.csv": report([["o1", "1.00"]]),
            "/data/report_20260922.csv": report([["o2", "2.00"]]),
        }
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(), 0)
            self.assertEqual(len(world.notify_calls), 1)
            self.assertIn("缺失", world.notify_calls[0]["title"])
            self.assertIn("20260921", "\n".join(world.notify_calls[0]["lines"]))

    def test_missing_in_range_still_warns(self):
        # ③补数区间内缺某天：仍只告警、照常同步区间内已有文件（rc=0）。
        files = {
            "/data/report_20260920.csv": report([["o1", "1.00"]]),
            "/data/report_20260922.csv": report([["o2", "2.00"]]),
        }
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(start_date="2026-09-20", end_date="2026-09-22"), 0)
            self.assertEqual(sorted(world.table.deleted), ["pt=20260920", "pt=20260922"])
            self.assertEqual(len(world.notify_calls), 1)
            self.assertIn("20260921", "\n".join(world.notify_calls[0]["lines"]))

    def test_missing_check_false_with_single_bizdate_still_warns(self):
        # missing.check=false 时不做核对：即使单日无文件也保持原行为（只提示、rc=0）。
        job = minimal_job()
        job["missing"] = {"check": False}
        with World(self.tmp, job=job, files={"/data/report_20260920.csv": report([["o1", "1.00"]])}) as world:
            self.assertEqual(world.sync(bizdate="20260921"), 0)
            self.assertTrue(any("没有要处理的日期" in str(line) for line in world.access_logs))

    def test_missing_file_writes_existing_dates_anyway(self):
        # 缺 20260921，但 20 与 22 的文件要照常写入（下游任务不受影响）
        files = {
            "/data/report_20260920.csv": report([["o1", "1.00"]]),
            "/data/report_20260922.csv": report([["o2", "2.00"]]),
        }
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(), 0)
            self.assertEqual(sorted(world.table.deleted), ["pt=20260920", "pt=20260922"])
            self.assertEqual(len(world.notify_calls), 1)
            self.assertIn("缺失", world.notify_calls[0]["title"])
            self.assertIn("20260921", "\n".join(world.notify_calls[0]["lines"]))

    def test_empty_remote_alerts_and_aborts(self):
        with World(self.tmp) as world:
            self.assertEqual(world.sync(), 1)
            self.assertEqual(len(world.notify_calls), 1)
            self.assertIn("为空", world.notify_calls[0]["title"])

    def test_no_notify_flag(self):
        with World(self.tmp) as world:
            self.assertEqual(world.sync(no_notify=True), 1)
            self.assertEqual(len(world.notify_calls), 1)
            self.assertFalse(world.notify_calls[0]["enabled"])

    def test_parse_error_before_write(self):
        data = report([["o1", "not-a-number"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 1)
            self.assertEqual(world.table.deleted, [])
            self.assertTrue(any("解析失败" in str(line) for line in world.access_logs))

    def test_footer_mismatch_before_write(self):
        job = minimal_job()
        job["parse"]["footer"] = {"sum": ["settlement_amount"]}
        data = csv_bytes(HEADERS, [["o1", "1.00"], ["", "9.99"]])
        with World(self.tmp, job=job, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 1)
            self.assertEqual(world.table.deleted, [])
            self.assertTrue(any("合计" in str(line) for line in world.access_logs))

    def test_zero_rows_writes_empty_partition_by_default(self):
        data = csv_bytes(HEADERS, [])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            self.assertEqual(world.table.rows_in("20260920"), [])
            self.assertIn("pt=20260920", world.table.deleted)

    def test_zero_rows_rejected_when_allow_empty_false(self):
        job = minimal_job()
        job["target"]["allow_empty"] = False
        data = csv_bytes(HEADERS, [])
        with World(self.tmp, job=job, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 1)
            self.assertEqual(world.table.deleted, [])

    def test_zero_rows_does_not_wipe_existing_partition(self):
        """源文件被截断成只剩表头时，不能把已有分区的数据静默清空（先删再填不可逆）。"""
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            self.assertEqual(len(world.table.rows_in("20260920")), 1)
            # 源方把文件改坏成"只剩表头"（大小变了 → 台账不命中，会重跑）
            empty = csv_bytes(HEADERS, [])
            world.fake.replace_file("/data/report_20260920.csv", empty)
            self.assertEqual(world.sync(bizdate="20260920"), 1)
            self.assertEqual(len(world.table.rows_in("20260920")), 1, "已有数据被清空了")
            self.assertTrue(any("为避免清空已有数据" in str(line) for line in world.access_logs))

    def test_zero_rows_force_rewrites_partition(self):
        """确认源方真的改成零行时，--force 是显式放行开关。"""
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            empty = csv_bytes(HEADERS, [])
            world.fake.replace_file("/data/report_20260920.csv", empty)
            self.assertEqual(world.sync(bizdate="20260920", force=True), 0)
            self.assertEqual(world.table.rows_in("20260920"), [])

    def test_count_mismatch_detected(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            with mock.patch.object(mc_mod, "count_partition", return_value=0):
                self.assertEqual(world.sync(bizdate="20260920"), 1)
            self.assertTrue(any("写后校验不一致" in str(line) for line in world.access_logs))

    def test_project_change_in_ledger_forces_rewrite(self):
        """台账里的 project 与本次不一致（如 dev→prod）→ 不能跳过，必须重写。"""
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            ledger_path = world.download_dir / state_mod.STATE_FILE_NAME
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            # setdefault + 断言键存在：台账键口径变化时失败信息是"没有该键"，而不是裸 KeyError
            self.assertIn("report_20260920.csv", ledger)
            ledger.setdefault("report_20260920.csv", {})["project"] = "other_project"
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            deletes = len(world.table.deleted)
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            self.assertEqual(len(world.table.deleted), deletes + 1)

    def test_corrupted_local_file_is_redownloaded(self):
        """本地文件被改坏但大小没变（台账 md5 对不上）时必须重新下载：

        原来下载准备阶段按"仅比大小"复用它——损坏数据被重新解析上传、台账 md5 还被覆盖，
        且永远不会从远端重下。"""
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            entry = json.loads((world.download_dir / state_mod.STATE_FILE_NAME).read_text(encoding="utf-8"))
            self.assertIn("report_20260920.csv", entry)
            local = world.download_dir / "report_20260920.csv"
            self.assertTrue(local.is_file())
            local.write_bytes(b"x" * local.stat().st_size)  # 大小不变、内容损坏
            downloads = len(world.fake.downloaded)
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            # 修复后：md5 对不上 → 重新下载
            self.assertEqual(len(world.fake.downloaded), downloads + 1)

    def test_ledger_save_failure_fails_run(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            with mock.patch.object(state_mod, "save_state", side_effect=OSError("disk full")):
                self.assertEqual(world.sync(bizdate="20260920"), 1)
            self.assertTrue(any("台账写入失败" in str(line) for line in world.access_logs))

    def test_no_dates_matched_warns(self):
        job = minimal_job()
        job["missing"] = {"check": False}
        with World(self.tmp, job=job, files={"/data/report_20260920.csv": report([["o1", "1.00"]])}) as world:
            self.assertEqual(world.sync(bizdate="20260921"), 0)
            self.assertTrue(any("没有要处理的日期" in str(line) for line in world.access_logs))

    def test_download_error_reports_redacted(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            world.fake.fail_downloads = 99  # 下载始终失败（重试也会耗尽）
            self.assertEqual(world.sync(bizdate="20260920"), 1)
            joined = "\n".join(str(line) for line in world.access_logs)
            self.assertIn("下载", joined)
            self.assertNotIn("secret-pw", joined)


class TestSyncNewColumns(CliTestCase):
    """源文件表头新增列：不报错、照常入库，发飞书提醒（人工决定是否加列）。"""

    def test_extra_columns_ingest_and_notify(self):
        data = csv_bytes(HEADERS + ["Note"], [["o1", "1.50", "hello"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            self.assertEqual([r[0] for r in world.table.rows_in("20260920")], ["o1"])
            alerts = [c for c in world.notify_calls if "新增列" in c["title"]]
            self.assertEqual(len(alerts), 1)
            joined = "\n".join(alerts[0]["lines"])
            self.assertIn("Note", joined)
            self.assertIn("report_20260920.csv", joined)
            self.assertTrue(any("新增列" in str(line) for line in world.access_logs))

    def test_extra_columns_notify_once_per_column_set(self):
        job = minimal_job(
            source={"root": "/data", "layout": "flat", "file_regex": "report_(?P<date>\\d{8})(?:_\\d)?\\.csv"}
        )
        files = {
            "/data/report_20260920.csv": csv_bytes(HEADERS + ["Note"], [["o1", "1.00", "x"]]),
            "/data/report_20260920_2.csv": csv_bytes(HEADERS + ["Note"], [["o2", "2.00", "y"]]),
        }
        with World(self.tmp, job=job, files=files) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            self.assertEqual(len(world.table.rows_in("20260920")), 2)
            alerts = [c for c in world.notify_calls if "新增列" in c["title"]]
            self.assertEqual(len(alerts), 1, "同一批新列名只应提醒一次")

    def test_extra_columns_no_notify_flag(self):
        data = csv_bytes(HEADERS + ["Note"], [["o1", "1.50", "hello"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.sync(bizdate="20260920", no_notify=True), 0)
            self.assertEqual(len(world.table.rows_in("20260920")), 1)
            alerts = [c for c in world.notify_calls if "新增列" in c["title"]]
            self.assertEqual(len(alerts), 1)
            self.assertFalse(alerts[0]["enabled"], "--no-notify 时应为关闭状态")


class TestSyncLedgerCompat(CliTestCase):
    def test_date_dir_legacy_flat_file_and_ledger_skips(self):
        job = minimal_job()
        job["source"] = {
            "root": "settlements",
            "layout": "date_dir",
            "date_dir_regex": "(?P<date>\\d{8})",
            "file_regex": "detail_(?P<date>\\d{8})_USD\\.csv",
        }
        data = report([["o1", "1.00"]])
        fake = FakeSftp()
        add_dir(fake, "settlements/20260920")
        add_file(fake, "settlements/20260920/detail_20260920_USD.csv", data)
        with World(self.tmp, job=job, fake=fake) as world:
            world.download_dir.mkdir(parents=True, exist_ok=True)
            # 旧脚本口径：文件平铺 + 台账键是文件名
            (world.download_dir / "detail_20260920_USD.csv").write_bytes(data)
            state_mod.save_state(
                world.download_dir / state_mod.STATE_FILE_NAME,
                {"detail_20260920_USD.csv": {"table": "ods_demo_di", "pt": "20260920", "size": len(data), "rows": 1}},
            )
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            self.assertEqual(world.table.deleted, [])
            self.assertEqual(world.fake.downloaded, [])

    def test_date_dir_new_layout_writes_nested_ledger_key(self):
        job = minimal_job()
        job["source"] = {
            "root": "settlements",
            "layout": "date_dir",
            "date_dir_regex": "(?P<date>\\d{8})",
            "file_regex": "detail_(?P<date>\\d{8})_USD\\.csv",
        }
        data = report([["o1", "1.00"]])
        fake = FakeSftp()
        add_dir(fake, "settlements/20260920")
        add_file(fake, "settlements/20260920/detail_20260920_USD.csv", data)
        with World(self.tmp, job=job, fake=fake) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            self.assertIn("20260920/detail_20260920_USD.csv", world.ledger())
            self.assertTrue((world.download_dir / "20260920" / "detail_20260920_USD.csv").is_file())

    def test_date_dir_legacy_flat_file_not_reused_for_other_date(self):
        """旧目录里的平铺同名文件属于别的日期时，不能被当成本日已下载的文件写进库。

        "回退按文件名找文件"只允许用于判定已上传（台账证明那个键属于这一天）；
        下载/写库阶段必须认 日期/文件名 的当前口径，否则大小恰好相同的旧文件会被静默写成当天数据。
        """
        job = minimal_job()
        job["source"] = {
            "root": "settlements",
            "layout": "date_dir",
            "date_dir_regex": "(?P<date>\\d{8})",
            "file_regex": "a\\.csv",
        }
        data_19 = report([["old-19", "9.99"]])
        data_20 = report([["new-20", "9.99"]])
        self.assertEqual(len(data_19), len(data_20), "两个文件必须等长，才构成「大小一致」的迷惑条件")
        fake = FakeSftp()
        add_dir(fake, "settlements/20260919")
        add_file(fake, "settlements/20260919/a.csv", data_19)
        add_dir(fake, "settlements/20260920")
        add_file(fake, "settlements/20260920/a.csv", data_20)
        with World(self.tmp, job=job, fake=fake) as world:
            world.download_dir.mkdir(parents=True, exist_ok=True)
            # 旧脚本遗留：平铺下载目录里躺着 20260919 的文件（台账里没有 20260920 的记录）
            (world.download_dir / "a.csv").write_bytes(data_19)
            self.assertEqual(world.sync(bizdate="20260920"), 0)
            rows = world.table.rows_in("20260920")
            self.assertEqual([r[0] for r in rows], ["new-20"], "别的日期的本地文件被当成当天数据写进了库")
            self.assertEqual(world.fake.downloaded, ["settlements/20260920/a.csv"], "本日文件应重新下载")


class TestSyncConnectionErrors(CliTestCase):
    def test_fatal_auth_error_redacted(self):
        def boom(self):
            raise FatalSourceError("Authentication failed [password=secret-pw]")

        with World(self.tmp, connect=boom) as world:
            self.assertEqual(world.sync(bizdate="20260920"), 1)
            joined = "\n".join(str(line) for line in world.access_logs)
            self.assertIn("列远端文件失败", joined)
            self.assertNotIn("secret-pw", joined)
            self.assertIn("***", joined)


class TestSyncInterrupt(CliTestCase):
    """同步主路径的 Ctrl+C 契约（README 承诺中断 → 退出码 130）。

    此前只有向导（--init）路径验证过 130，run_sync 主路径没有任何用例；api2ods / feishu2ods
    都逐码验证过 130，sftp2ods 是唯一缺口。这里用 mock 离线打断，不连真实 SFTP/MaxCompute。
    """

    def test_keyboard_interrupt_during_download_returns_130_and_writes_nothing(self):
        """下载阶段被 Ctrl+C（最常见的中断点，发生在写库之前）：rc=130 且没有任何写库动作。

        130 的核心语义是"中断不能写坏分区"——下载/解析都在写库前，所以此刻必须完全没碰库。
        """
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:

            def boom(self, item, local_path):
                raise KeyboardInterrupt

            with mock.patch.object(sftp_mod.SftpSource, "download", boom):
                rc = cli_mod.main(["--job", str(world.job_path), "--bizdate", "20260920"])
            self.assertEqual(rc, 130)
            self.assertEqual(world.table.deleted, [], "中断前不应有任何删分区动作")
            self.assertEqual(world.table.written, {}, "中断前不应写入任何分区")
            self.assertFalse((world.download_dir / state_mod.STATE_FILE_NAME).exists(), "中断不应落台账")
            self.assertTrue(any("已中断" in str(line) for line in world.access_logs))

    def test_keyboard_interrupt_during_parse_returns_130_and_writes_nothing(self):
        """下载完、解析阶段被 Ctrl+C：仍在写库之前，rc=130 且没有任何写库动作。"""
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:

            def boom(self, path, stats=None):
                raise KeyboardInterrupt

            with mock.patch.object(parse_mod.ParseSpec, "iter_rows", boom):
                rc = cli_mod.main(["--job", str(world.job_path), "--bizdate", "20260920"])
            self.assertEqual(rc, 130)
            self.assertEqual(world.table.deleted, [])
            self.assertEqual(world.table.written, {})

    def test_keyboard_interrupt_during_write_returns_130(self):
        """写入阶段（先删再填）被 Ctrl+C：rc=130；此刻分区已被删、可能留下空/半截分区。

        这是现有真实行为——README「行为与保护」第 10 条：写入阶段中断的分区可能不完整，
        重跑同一命令会从头覆盖、幂等自愈。本用例只把真实行为钉死，不改变实现。
        """
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            calls = {"n": 0}
            real_iter_batches = parse_mod.iter_batches

            def counting(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] >= WRITE_PHASE_CALL_INDEX:
                    raise KeyboardInterrupt
                return real_iter_batches(*args, **kwargs)

            with mock.patch.object(parse_mod, "iter_batches", counting):
                rc = cli_mod.main(["--job", str(world.job_path), "--bizdate", "20260920"])
            self.assertEqual(rc, 130)
            self.assertIn("pt=20260920", world.table.deleted, "中断前已执行删分区（先删再填不可逆）")


class TestCheck(CliTestCase):
    def test_check_ok(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.check(), 0)
            self.assertTrue(any("检查通过" in line for line in world.access_logs))

    def test_check_table_missing_still_ok(self):
        with World(self.tmp) as world:
            with mock.patch.object(world.odps, "exist_table", return_value=False):
                self.assertEqual(world.check(), 0)
            self.assertTrue(any("表不存在" in line for line in world.access_logs))

    def test_check_remote_newer_than_expected(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20990101.csv": data}) as world:
            self.assertEqual(world.check(), 0)
            self.assertTrue(any("晚于预期最新" in line for line in world.access_logs))

    def test_check_with_bizdate_only_checks_that_day(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(world.check(bizdate="20260920"), 0)
            self.assertTrue(any("的远端文件存在" in line for line in world.access_logs))
            self.assertEqual(world.check(bizdate="20260919"), 0)
            self.assertTrue(any("缺文件核对未通过" in line for line in world.access_logs))

    def test_check_schema_mismatch_fails(self):
        with World(self.tmp) as world:
            world.table.table_schema.columns[0].type = "bigint"
            self.assertEqual(world.check(), 1)


class TestMainEntry(CliTestCase):
    def test_no_job_returns_2(self):
        self.assertEqual(cli_mod.main([]), 2)

    def test_negative_sql_timeout_returns_2(self):
        self.assertEqual(cli_mod.main(["--job", "whatever.json", "--sql-timeout", "-1"]), 2)

    def test_empty_systemexit_message_is_not_treated_as_pass(self):
        """参数校验里捕获到 SystemExit()（空消息）时不能返回空串被当成"校验通过"。"""
        args = cli_mod.build_parser().parse_args(["--job", "x.json", "--bizdate", "20260101"])
        with mock.patch.object(cli_mod, "parse_day_arg", side_effect=SystemExit()):
            problem = cli_mod._check_cli_args(args)
        self.assertTrue(problem)

    def test_bad_log_file_returns_2(self):
        """--log-file 指向目录属"参数问题"：按退出码约定报 2（还没做过任何远端操作），
        不能混进 1（运行失败）让调度按数据故障告警。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli_mod.main(["--job", "whatever.json", "--log-file", tmp]), 2)

    def test_happy_path(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            rc = cli_mod.main(["--job", str(world.job_path), "--bizdate", "20260920"])
            self.assertEqual(rc, 0)
            self.assertEqual(len(world.table.rows_in("20260920")), 1)

    def test_bad_bizdate_returns_2(self):
        # --bizdate 写法不对属于"命令行参数问题"：退出码 2，调度不需要按数据故障告警
        with World(self.tmp) as world:
            self.assertEqual(cli_mod.main(["--job", str(world.job_path), "--bizdate", "oops"]), 2)

    def test_bad_job_json_returns_1(self):
        path = self.tmp / "broken.json"
        path.write_text("{oops", encoding="utf-8")
        self.assertEqual(cli_mod.main(["--job", str(path)]), 1)

    def test_init_errors_map_to_exit_codes(self):
        """--init 分支与其它分支同口径：配置/文件错 → 1（记日志），Ctrl+C → 130，不留裸 traceback。"""
        from sftp2ods import init_wizard

        with mock.patch.object(init_wizard, "run_init", side_effect=SystemExit("--init-out 指向的是目录")):
            self.assertEqual(cli_mod.main(["--init"]), 1)
        with mock.patch.object(init_wizard, "run_init", side_effect=KeyboardInterrupt):
            self.assertEqual(cli_mod.main(["--init"]), 130)

    def test_init_unexpected_error_is_logged_not_bare_traceback(self):
        """向导内部未预期异常：记一笔真实错误（不是"已取消"）并按 1 退出，不抛裸 traceback。"""
        from sftp2ods import init_wizard

        with mock.patch.object(init_wizard, "run_init", side_effect=ValueError("内部解析出错")):
            self.assertEqual(cli_mod.main(["--init"]), 1)

    def test_check_flag(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            self.assertEqual(cli_mod.main(["--job", str(world.job_path), "--check"]), 0)

    def test_lock_conflict_returns_1(self):
        with World(self.tmp) as world:
            lock_path = cli_mod._lock_path(world.job_path.resolve())
            with RunLock(lock_path):
                rc = cli_mod.main(["--job", str(world.job_path), "--bizdate", "20260920"])
            self.assertEqual(rc, 1)

    def test_env_bizdate_used(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            with mock.patch.dict("os.environ", {"bizdate": "20260920"}, clear=False):
                rc = cli_mod.main(["--job", str(world.job_path)])
            self.assertEqual(rc, 0)

    def test_env_bizdate_missing_day_returns_1(self):
        # 调度（DataWorks）用环境变量 bizdate 点名某天、而该天无文件：与 --bizdate 同样失败（rc=1）。
        # 这正是 F6 要防的"静默缺数"场景——调度看到 rc=0 会以为 pt=<bizdate> 已产出，其实没有。
        files = {"/data/report_20260920.csv": report([["o1", "1.00"]])}
        with World(self.tmp, files=files) as world:
            with mock.patch.dict("os.environ", {"bizdate": "20260921"}, clear=False):
                rc = cli_mod.main(["--job", str(world.job_path)])
            self.assertEqual(rc, 1)

    def test_env_bizdate_invalid_returns_1(self):
        with World(self.tmp) as world:
            with mock.patch.dict("os.environ", {"bizdate": "oops"}, clear=False):
                self.assertEqual(cli_mod.main(["--job", str(world.job_path)]), 1)

    def test_explicit_range_wins_over_env_bizdate(self):
        """调度环境里 bizdate 总存在；显式补数区间必须优先，不能被"单日 vs 区间"互斥拦下。"""
        files = {
            "/data/report_20260920.csv": report([["o1", "1.00"]]),
            "/data/report_20260921.csv": report([["o2", "2.00"]]),
        }
        with World(self.tmp, files=files) as world:
            with mock.patch.dict("os.environ", {"bizdate": "20260922"}, clear=False):
                rc = cli_mod.main(
                    ["--job", str(world.job_path), "--start-date", "2026-09-20", "--end-date", "2026-09-21"]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(sorted(world.table.deleted), ["pt=20260920", "pt=20260921"])


class TestSyncLedgerKeyMismatch(CliTestCase):
    def test_ledger_rows_track_local_file_not_remote_name(self):
        """台账键的 basename 与远端文件名不同口径时，行数按本地落地文件记录，不能再 KeyError。

        原实现：行数按 path.name 累积、却按 item.name 取值——两把键不一致时数据已入库、
        台账写不进去，异常还会穿透 run_sync 变成裸 traceback。
        """
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            item = sftp_mod.RemoteFile(
                date="20260920",
                name="report_20260920.csv",
                size=len(data),
                remote="/data/report_20260920.csv",
                ledger_key="20260920/renamed.csv",
            )
            with mock.patch.object(sftp_mod.SftpSource, "list_files", lambda self: {"20260920": [item]}):
                self.assertEqual(world.sync(bizdate="20260920"), 0)
            self.assertEqual(world.ledger()["20260920/renamed.csv"]["rows"], 1)


class TestLifecycleDays(CliTestCase):
    def test_nan_and_infinity_rejected(self):
        # json.load 默认接受 NaN/Infinity 字面量：原来 float(raw) != int(raw) 会对 NaN 抛
        # 未捕获的 ValueError（裸 traceback），现在统一给 SystemExit
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(SystemExit):
                cli_mod._lifecycle_days({"lifecycle_days": bad})

    def test_positive_int_values(self):
        self.assertEqual(cli_mod._lifecycle_days({"lifecycle_days": 30}), 30)
        self.assertEqual(cli_mod._lifecycle_days({"lifecycle_days": 30.0}), 30)
        self.assertIsNone(cli_mod._lifecycle_days({}))
        for bad in (True, 0, -1, 2.5, "30"):
            with self.assertRaises(SystemExit):
                cli_mod._lifecycle_days({"lifecycle_days": bad})


class TestRedactionHelpers(CliTestCase):
    def test_redact_job_masks_configured_secrets(self):
        job = minimal_job()
        job["notify"] = {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/abc12345"}
        text = f"failed: {job['sftp']['auth']['password']} and hook abc12345"
        out = redact_secrets(collect_secret_values(job), text)
        self.assertNotIn("secret-pw", out)
        self.assertNotIn("abc12345", out)

    def test_redact_job_includes_config_secrets(self):
        """异常文本里回显的 --config 明文也要遮：凭证常只写 --config（作业文件里是
        ${secrets.xxx}），SDK 报错回显的 AK/密码来自 config 那份，只收作业文件的密钥值会漏遮。"""
        job = minimal_job()
        config = {"maxcompute": {"access_key_secret": "cfgs-ec-ret-abcdef12"}}
        out = cli_mod._redact_job(job, "boom: cfgs-ec-ret-abcdef12", config)
        self.assertNotIn("cfgs-ec-ret-abcdef12", out)
        self.assertIn("***", out)

    def test_prepare_stage_error_masks_config_secrets(self):
        """凭证只写在 --config 的 secrets 里时，准备阶段的报错也不能回显明文。

        作业文件里只有 ${secrets.tbl} 时，只按 job_raw 收密钥值会漏遮（把配置里的值
        原样打进日志/--log-file）；这里断言两份配置一起参与值级脱敏。
        """
        job = minimal_job(target={"project": "demo_project", "table": "${secrets.tbl}"})
        config_path = self.tmp / "config.json"
        config_path.write_text(json.dumps({"secrets": {"tbl": "my table"}}), encoding="utf-8")
        with World(self.tmp, job=job) as world:
            rc = cli_mod.main(["--job", str(world.job_path), "--config", str(config_path), "--check"])
            joined = "\n".join(str(line) for line in world.access_logs)
        self.assertEqual(rc, 1)
        self.assertNotIn("my table", joined)
        self.assertIn("***", joined)


class TestPromptSecret(CliTestCase):
    def test_getpass_success(self):
        with mock.patch.object(cli_mod.getpass, "getpass", return_value="hidden"):
            self.assertEqual(cli_mod.prompt_secret(), "hidden")

    def test_fallback_warns_that_input_echoes(self):
        logs = []
        with mock.patch.object(cli_mod.getpass, "getpass", side_effect=EOFError()):
            with mock.patch.object(cli_mod, "log", logs.append):
                with mock.patch("builtins.input", return_value="echoed"):
                    self.assertEqual(cli_mod.prompt_secret(), "echoed")
        self.assertTrue(any("明文回显" in str(line) for line in logs), logs)

    def test_unexpected_errors_are_not_swallowed(self):
        with mock.patch.object(cli_mod.getpass, "getpass", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                cli_mod.prompt_secret()


class TestLockDirOverride(CliTestCase):
    """SFTP2ODS_LOCK_DIR：把锁钉在固定目录，避免不同身份/TMPDIR 下互斥静默失效。"""

    def test_env_override_pins_location(self):
        pinned = self.tmp / "shared-locks"
        with mock.patch.dict(os.environ, {"SFTP2ODS_LOCK_DIR": str(pinned)}):
            path = cli_mod._lock_path(self.tmp / "jobs" / "demo.json")
            again = cli_mod._lock_path(self.tmp / "jobs" / "demo.json")
        self.assertEqual(path.parent, pinned)
        self.assertEqual(path, again)
        self.assertTrue(path.name.endswith(".lock"))

    def test_env_override_unusable_fails_loudly(self):
        """显式指定的锁目录不可用要立刻报错（静默换目录就等于互斥失效）。"""
        with (
            mock.patch.dict(os.environ, {"SFTP2ODS_LOCK_DIR": str(self.tmp / "ro" / "locks")}),
            mock.patch.object(Path, "mkdir", side_effect=OSError("只读")),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli_mod._lock_path(self.tmp / "jobs" / "demo.json")
        self.assertIn("SFTP2ODS_LOCK_DIR", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
