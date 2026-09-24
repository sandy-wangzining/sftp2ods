# -*- coding: utf-8 -*-
"""state（台账）与 notify（飞书告警）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import OfflineTestCase  # noqa: E402

from sftp2ods import notify as notify_mod  # noqa: E402
from sftp2ods import state as state_mod  # noqa: E402


class TestState(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_load_missing(self):
        self.assertEqual(state_mod.load_state(self.tmp / "nope.json"), {})

    def test_roundtrip(self):
        path = self.tmp / "state.json"
        state = {"a.csv": {"table": "t", "pt": "20260920", "size": 3, "rows": 1}}
        state_mod.save_state(path, state)
        self.assertEqual(state_mod.load_state(path), state)
        self.assertFalse((self.tmp / "state.json.tmp").exists())

    def test_save_creates_parent_dir(self):
        path = self.tmp / "deep" / "dir" / ".uploaded.json"
        state_mod.save_state(path, {"a": {"table": "t", "pt": "1", "size": 1}})
        self.assertTrue(path.is_file())

    def test_corrupt(self):
        path = self.tmp / "state.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(SystemExit):
            state_mod.load_state(path)

    def test_non_object(self):
        path = self.tmp / "state.json"
        path.write_text("[1]", encoding="utf-8")
        with self.assertRaises(SystemExit):
            state_mod.load_state(path)

    def test_record_of_matches_any_key(self):
        state = {"20260920/a.csv": {"table": "t", "pt": "20260920", "size": 3, "rows": 1}}
        self.assertTrue(state_mod.record_of(state, ("a.csv", "20260920/a.csv"), 3, "t", "20260920"))
        self.assertFalse(state_mod.record_of(state, ("a.csv",), 3, "t", "20260920"))
        self.assertFalse(state_mod.record_of(state, ("20260920/a.csv",), 4, "t", "20260920"))
        self.assertFalse(state_mod.record_of(state, ("20260920/a.csv",), 3, "other", "20260920"))

    def test_record_of_project_guard(self):
        """换过目标项目（表名相同）时不能拿另一个项目的上传记录跳过。"""
        state = {"a.csv": {"project": "prod", "table": "t", "pt": "20260920", "size": 3, "rows": 1}}
        self.assertTrue(state_mod.record_of(state, ("a.csv",), 3, "t", "20260920", project="prod"))
        self.assertFalse(state_mod.record_of(state, ("a.csv",), 3, "t", "20260920", project="dev"))
        # 旧脚本台账没有 project 字段：兼容放行
        legacy = {"a.csv": {"table": "t", "pt": "20260920", "size": 3, "rows": 1}}
        self.assertTrue(state_mod.record_of(legacy, ("a.csv",), 3, "t", "20260920", project="dev"))

    def test_local_ready(self):
        path = self.tmp / "f.csv"
        path.write_bytes(b"123")
        self.assertTrue(state_mod.local_ready(path, 3))
        self.assertFalse(state_mod.local_ready(path, 4))
        self.assertFalse(state_mod.local_ready(self.tmp / "nope", 0))


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"code": 0}

    def json(self):
        return self._payload


class FakeRequests:
    def __init__(self, response=None, exc=None):
        self.response = response or FakeResponse()
        self.exc = exc
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.response


class TestNotify(OfflineTestCase):
    def test_disabled(self):
        fake = FakeRequests()
        with mock.patch.object(notify_mod, "requests", fake):
            self.assertFalse(notify_mod.notify("https://hook", "t", ["x"], enabled=False))
        self.assertEqual(fake.calls, [])

    def test_no_webhook(self):
        fake = FakeRequests()
        with mock.patch.object(notify_mod, "requests", fake):
            self.assertFalse(notify_mod.notify("", "t", ["x"]))
        self.assertEqual(fake.calls, [])

    def test_success_payload(self):
        fake = FakeRequests()
        with mock.patch.object(notify_mod, "requests", fake):
            self.assertTrue(notify_mod.notify("https://open.feishu.cn/x", "标题", ["一行"], "脚注"))
        self.assertEqual(len(fake.calls), 1)
        card = fake.calls[0]["json"]
        self.assertEqual(card["msg_type"], "interactive")
        self.assertEqual(card["card"]["header"]["title"]["content"], "标题")
        self.assertEqual(card["card"]["elements"][0]["text"]["content"], "一行")

    def test_api_error_returns_false(self):
        fake = FakeRequests(response=FakeResponse(status_code=400, payload={"code": 19001}))
        with mock.patch.object(notify_mod, "requests", fake):
            self.assertFalse(notify_mod.notify("https://hook", "t", ["x"]))

    def test_exception_swallowed(self):
        fake = FakeRequests(exc=OSError("network down"))
        with mock.patch.object(notify_mod, "requests", fake):
            self.assertFalse(notify_mod.notify("https://hook", "t", ["x"]))

    def test_missing_requests_module(self):
        with mock.patch.object(notify_mod, "requests", None):
            self.assertFalse(notify_mod.notify("https://hook", "t", ["x"]))


class TestStateFileShape(OfflineTestCase):
    def test_matches_legacy_ledger_shape(self):
        """旧脚本台账的字段（table/pt/size/rows）必须能被读出来并在跳过判定里命中。"""
        legacy = {
            "settlement_report_20260920.csv": {
                "table": "ods_clink_settlement_details_di",
                "pt": "20260920",
                "size": 123,
                "rows": 4,
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".uploaded.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            state = state_mod.load_state(path)
            self.assertTrue(
                state_mod.record_of(
                    state, ("settlement_report_20260920.csv",), 123, "ods_clink_settlement_details_di", "20260920"
                )
            )


if __name__ == "__main__":
    unittest.main()
