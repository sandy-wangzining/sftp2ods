# -*- coding: utf-8 -*-
"""cli：主流程（同步/跳过/补数/dry-run/缺文件/错误路径/退出码/运行锁）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
from sftp2ods import sftp as sftp_mod  # noqa: E402
from sftp2ods import state as state_mod  # noqa: E402
from sftp2ods.utils import FatalSourceError, RunLock, collect_secret_values, redact_secrets  # noqa: E402

HEADERS = ["Order ID", "Settlement amount"]


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
            mock.patch.object(
                sftp_mod.SftpSource, "_connect", connect or connect_to(self.fake)
            ),
            mock.patch.object(mc_mod, "connect_odps", lambda *a, **k: self.odps),
            mock.patch.object(cli_mod, "notify", self._capture_notify),
        ]

    def _capture_notify(self, webhook, title, lines, footer="", enabled=True, timeout=15):
        self.notify_calls.append(
            {"webhook": webhook, "title": title, "lines": list(lines), "footer": footer, "enabled": enabled}
        )
        return True

    def __enter__(self):
        for patcher in self._patches:
            patcher.start()
        self.access_logs.clear()
        self._log_patch = mock.patch.object(cli_mod, "log", self.access_logs.append)
        self._log_patch.start()
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

    def check(self, **kwargs):
        return cli_mod.run_check(self.job, {}, make_args(**kwargs), self.job_path)


class CliTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


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
        job = minimal_job(source={"root": "/data", "layout": "flat", "file_regex": "report_(?P<date>\\d{8})(?:_\\d)?\\.csv"})
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

    def test_bizdate_with_range_is_rejected(self):
        with World(self.tmp) as world:
            rc = cli_mod.main(["--job", str(world.job_path), "--bizdate", "20260920", "--start-date", "2026-09-19"])
            self.assertEqual(rc, 1)
            self.assertTrue(any("互斥" in str(line) for line in world.access_logs))


class TestSyncGuards(CliTestCase):
    def test_missing_file_alerts_and_aborts(self):
        files = {"/data/report_20260920.csv": report([["o1", "1.00"]])}
        with World(self.tmp, files=files) as world:
            self.assertEqual(world.sync(bizdate="20260921"), 1)
            self.assertEqual(world.table.deleted, [])
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

    def test_count_mismatch_detected(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            with mock.patch.object(mc_mod, "count_partition", return_value=0):
                self.assertEqual(world.sync(bizdate="20260920"), 1)
            self.assertTrue(any("写后校验不一致" in str(line) for line in world.access_logs))

    def test_download_error_reports_redacted(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            world.fake.fail_downloads = 99  # 下载始终失败（重试也会耗尽）
            self.assertEqual(world.sync(bizdate="20260920"), 1)
            joined = "\n".join(str(line) for line in world.access_logs)
            self.assertIn("下载", joined)
            self.assertNotIn("secret-pw", joined)


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

    def test_check_schema_mismatch_fails(self):
        with World(self.tmp) as world:
            world.table.table_schema.columns[0].type = "bigint"
            self.assertEqual(world.check(), 1)


class TestMainEntry(CliTestCase):
    def test_no_job_returns_2(self):
        self.assertEqual(cli_mod.main([]), 2)

    def test_happy_path(self):
        data = report([["o1", "1.00"]])
        with World(self.tmp, files={"/data/report_20260920.csv": data}) as world:
            rc = cli_mod.main(["--job", str(world.job_path), "--bizdate", "20260920"])
            self.assertEqual(rc, 0)
            self.assertEqual(len(world.table.rows_in("20260920")), 1)

    def test_bad_bizdate_returns_1(self):
        with World(self.tmp) as world:
            self.assertEqual(cli_mod.main(["--job", str(world.job_path), "--bizdate", "oops"]), 1)

    def test_bad_job_json_returns_1(self):
        path = self.tmp / "broken.json"
        path.write_text("{oops", encoding="utf-8")
        self.assertEqual(cli_mod.main(["--job", str(path)]), 1)

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

    def test_env_bizdate_invalid_returns_1(self):
        with World(self.tmp) as world:
            with mock.patch.dict("os.environ", {"bizdate": "oops"}, clear=False):
                self.assertEqual(cli_mod.main(["--job", str(world.job_path)]), 1)


class TestRedactionHelpers(CliTestCase):
    def test_redact_job_masks_configured_secrets(self):
        job = minimal_job()
        job["notify"] = {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/abc12345"}
        text = f"failed: {job['sftp']['auth']['password']} and hook abc12345"
        out = redact_secrets(collect_secret_values(job), text)
        self.assertNotIn("secret-pw", out)
        self.assertNotIn("abc12345", out)


if __name__ == "__main__":
    unittest.main()
