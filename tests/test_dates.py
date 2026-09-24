# -*- coding: utf-8 -*-
"""dates：日期解析、业务日环境变量、预期最新、缺文件核对、处理范围规划。"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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

    def test_rejects_bad(self):
        with self.assertRaises(SystemExit):
            dates.norm_date("202609", "--bizdate")


class TestEnvBizdate(OfflineTestCase):
    def test_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(dates.env_bizdate())

    def test_valid(self):
        with mock.patch.dict(os.environ, {"bizdate": "2026-09-20"}):
            self.assertEqual(dates.env_bizdate().isoformat(), "2026-09-20")

    def test_invalid_strict(self):
        with mock.patch.dict(os.environ, {"bizdate": "oops"}):
            with self.assertRaises(SystemExit):
                dates.env_bizdate()

    def test_invalid_non_strict(self):
        with mock.patch.dict(os.environ, {"SKYNET_BIZDATE": "oops"}):
            self.assertIsNone(dates.env_bizdate(strict=False))


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


class TestFindMissing(OfflineTestCase):
    def test_internal_gap(self):
        have = ["20260901", "20260902", "20260904"]
        self.assertEqual(dates.find_missing(have, "20260901", "20260904"), ["20260903"])

    def test_tail_missing(self):
        self.assertEqual(dates.find_missing(["20260901"], "20260901", "20260903"), ["20260902", "20260903"])


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


if __name__ == "__main__":
    unittest.main()
