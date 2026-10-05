# -*- coding: utf-8 -*-
"""dates：日期解析、业务日环境变量、预期最新、缺文件核对、处理范围规划。"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# 只在路径缺失时追加（不插到最前面）：避免把仓库根/tests 目录置于标准库与第三方库
# 之前遮蔽同名模块；按 README 在仓库根运行（或 CI 里 pip install -e .）时，
# 本地包本来就在搜索路径最前（python -m 会把当前目录放在 sys.path[0]）
for _path in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

from _helpers import OfflineTestCase  # noqa: E402

from sftp2ods import dates  # noqa: E402


class TestParseDay(OfflineTestCase):
    def test_compact_and_iso(self):
        self.assertEqual(dates.parse_day_arg("20260920").isoformat(), "2026-09-20")
        self.assertEqual(dates.parse_day_arg("2026-09-20").isoformat(), "2026-09-20")

    def test_invalid_forms(self):
        for bad in ("2026-W38-1", "2026092", "20261301", "20260230", "", " 20260920-"):
            with self.assertRaises(SystemExit):
                dates.parse_day_arg(bad)


class TestNormDate(OfflineTestCase):
    def test_normalizes(self):
        self.assertEqual(dates.norm_date("2026-09-20", "--bizdate"), "20260920")
        self.assertEqual(dates.norm_date("2026/09/20", "--bizdate"), "20260920")
        self.assertEqual(dates.norm_date("20260920", "--bizdate"), "20260920")

    def test_rejects_bad(self):
        # 分隔符位置不对的写法不能被"删掉所有 - 和 /"静默归一化（会落到错误的 pt 上）
        for bad in ("202609", "20-2609-21", "2026-0/921", "2026092-1", "2026-0921"):
            with self.assertRaises(SystemExit):
                dates.norm_date(bad, "--bizdate")


class TestEnvBizdate(OfflineTestCase):
    def test_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(dates.env_bizdate())

    def test_valid(self):
        with mock.patch.dict(os.environ, {"bizdate": "2026-09-20"}, clear=True):
            self.assertEqual(dates.env_bizdate().isoformat(), "2026-09-20")

    def test_invalid_strict(self):
        with mock.patch.dict(os.environ, {"bizdate": "oops"}, clear=True):
            with self.assertRaises(SystemExit):
                dates.env_bizdate()

    def test_invalid_non_strict(self):
        with mock.patch.dict(os.environ, {"SKYNET_BIZDATE": "oops"}, clear=True):
            self.assertIsNone(dates.env_bizdate(strict=False))

    def test_empty_env_is_error_not_unset(self):
        """变量存在但值为空（export bizdate=$1 且 $1 为空）不能当成"未设置"静默回退处理
        全部日期：strict 报错；非 strict（只读体检）告警后按未设置继续。"""
        with mock.patch.dict(os.environ, {"bizdate": ""}, clear=True):
            with self.assertRaises(SystemExit):
                dates.env_bizdate()
            self.assertIsNone(dates.env_bizdate(strict=False))
        with mock.patch.dict(os.environ, {"SKYNET_BIZDATE": "   "}, clear=True):
            with self.assertRaises(SystemExit):
                dates.env_bizdate()

    def test_blank_bizdate_does_not_mask_valid_skynet(self):
        """bizdate 是空白（空白串在 or 链里是真值）时不能把后面有效的 SKYNET_BIZDATE
        一起吞掉——按"第一个非空白值"取。"""
        with mock.patch.dict(os.environ, {"bizdate": "   ", "SKYNET_BIZDATE": "20260920"}, clear=True):
            self.assertEqual(dates.env_bizdate().isoformat(), "2026-09-20")
        with mock.patch.dict(os.environ, {"bizdate": "", "SKYNET_BIZDATE": "20260920"}, clear=True):
            self.assertEqual(dates.env_bizdate().isoformat(), "2026-09-20")

    def test_norm_date_rejects_fullwidth_digits(self):
        """全角数字不能被"归一化"成全角字符串（字符串比较会把真实日期整段滤掉）：
        re.ASCII 后按参数错直接报错。"""
        with self.assertRaises(SystemExit):
            dates.norm_date("２０２６０９２０", "--bizdate")
        with self.assertRaises(SystemExit):
            dates.norm_date("2026０９20", "--start-date")

    def test_fullwidth_digits_are_not_business_days(self):
        r"""全角数字（中文输入法常见）不是合法业务日：白名单按 ASCII 匹配（Unicode 的 \d 会放行）。"""
        with self.assertRaises(SystemExit):
            dates.parse_day_arg("２０２６０９２０")


class TestGrace(OfflineTestCase):
    def test_parse(self):
        self.assertEqual(dates.parse_grace("02:30"), (2, 30))
        self.assertEqual(dates.parse_grace(""), (0, 0))

    def test_invalid(self):
        for bad in ("2:3:0", "25:00", "aa:bb", "2:60"):
            with self.assertRaises(SystemExit):
                dates.parse_grace(bad)


class TestExpectedLatest(OfflineTestCase):
    def test_utc_yesterday(self):
        tz = dates.load_zone("UTC")
        now = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(dates.expected_latest(tz, "", now=now), "20260923")

    def test_grace_before(self):
        tz = dates.load_zone("UTC")
        now = datetime(2026, 9, 24, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(dates.expected_latest(tz, "02:30", now=now), "20260922")

    def test_grace_after(self):
        tz = dates.load_zone("UTC")
        now = datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(dates.expected_latest(tz, "02:30", now=now), "20260923")

    def test_naive_now_is_interpreted_in_given_tz_not_host_tz(self):
        """朴素 datetime 不能走 astimezone()（那会按**本机时区**解释，换台机器结果就变）。"""
        tz = dates.load_zone("UTC")
        naive = datetime(2026, 9, 24, 3, 0)  # 无 tzinfo：应按 UTC 解释 → 3:00 ≥ 02:30 → 23 日
        self.assertEqual(dates.expected_latest(tz, "02:30", now=naive), "20260923")
        naive_early = datetime(2026, 9, 24, 1, 0)  # 1:00 < 02:30 → 再退一天
        self.assertEqual(dates.expected_latest(tz, "02:30", now=naive_early), "20260922")


class TestFindMissing(OfflineTestCase):
    def test_internal_gap(self):
        have = ["20260901", "20260902", "20260904"]
        self.assertEqual(dates.find_missing(have, "20260901", "20260904"), ["20260903"])

    def test_tail_missing(self):
        self.assertEqual(dates.find_missing(["20260901"], "20260901", "20260903"), ["20260902", "20260903"])

    def test_malformed_range_is_config_error(self):
        """区间写错给配置错，不是裸 ValueError（库调用方可能绕过 CLI 校验）。"""
        for start, end in (("2026-09-01", "20260903"), ("20260901", "20260903x")):
            with self.assertRaises(SystemExit) as err:
                dates.find_missing(["20260901"], start, end)
            self.assertIn("yyyyMMdd", str(err.exception))


class TestPlanDates(OfflineTestCase):
    ALL = ["20260901", "20260902", "20260903"]

    def test_bizdate_single_day(self):
        missing, proc, r_start, r_end = dates.plan_dates(self.ALL, bizdate="20260902", expected="20260903")
        self.assertEqual(missing, [])
        self.assertEqual(proc, ["20260902"])
        self.assertEqual((r_start, r_end), ("20260902", "20260902"))

    def test_bizdate_missing(self):
        missing, proc, _, _ = dates.plan_dates(self.ALL, bizdate="20260904", expected="20260903")
        self.assertEqual(missing, ["20260904"])
        self.assertEqual(proc, [])

    def test_start_clamped_to_remote_min(self):
        missing, proc, r_start, r_end = dates.plan_dates(self.ALL, start="20260801", expected="20260903")
        self.assertEqual(missing, [])
        self.assertEqual(r_start, "20260901")
        self.assertEqual(proc, self.ALL)

    def test_end_overrides_expected(self):
        missing, proc, _, r_end = dates.plan_dates(self.ALL, end="20260902", expected="20260930")
        self.assertEqual(r_end, "20260902")
        self.assertEqual(missing, [])
        self.assertEqual(proc, ["20260901", "20260902"])

    def test_missing_tail_reported(self):
        missing, _, _, r_end = dates.plan_dates(["20260901"], expected="20260903")
        self.assertEqual(missing, ["20260902", "20260903"])
        self.assertEqual(r_end, "20260903")

    def test_check_disabled(self):
        missing, proc, r_start, r_end = dates.plan_dates(["20260901"], expected="20260903", check_missing=False)
        self.assertEqual(missing, [])
        self.assertEqual(proc, ["20260901"])
        self.assertEqual(r_end, "20260901")

    def test_empty_remote(self):
        self.assertEqual(dates.plan_dates([], expected="20260903"), ([], [], "", ""))

    def test_start_only_processes_from_start(self):
        missing, proc, _, r_end = dates.plan_dates(self.ALL, start="20260902", expected="20260903")
        self.assertEqual(missing, [])
        self.assertEqual(proc, ["20260902", "20260903"])

    def test_remote_min_after_expected(self):
        """远端最早日期晚于预期最新（新接入的源）：核对区间为空，不算缺文件。"""
        missing, proc, r_start, r_end = dates.plan_dates(["20260924"], expected="20260923")
        self.assertEqual(missing, [])
        self.assertEqual(proc, ["20260924"])
        self.assertEqual((r_start, r_end), ("20260924", "20260923"))


if __name__ == "__main__":
    unittest.main()
