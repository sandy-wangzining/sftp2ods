# -*- coding: utf-8 -*-
"""utils：布尔/标识符校验、脱敏、重试、运行锁。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import OfflineTestCase  # noqa: E402

from sftp2ods import utils  # noqa: E402


class TestAsBool(OfflineTestCase):
    def test_json_bool(self):
        self.assertTrue(utils.as_bool(True, False))
        self.assertFalse(utils.as_bool(False, True))

    def test_strings(self):
        self.assertTrue(utils.as_bool("true", False))
        self.assertTrue(utils.as_bool(" YES ", False))
        self.assertFalse(utils.as_bool("off", True))
        self.assertFalse(utils.as_bool("0", True))

    def test_defaults(self):
        self.assertTrue(utils.as_bool(None, True))
        self.assertFalse(utils.as_bool("", False))

    def test_typo_raises(self):
        with self.assertRaises(SystemExit) as ctx:
            utils.as_bool("flase", False, field="target.allow_empty")
        self.assertIn("target.allow_empty", str(ctx.exception))


class TestIdentifier(OfflineTestCase):
    def test_valid(self):
        self.assertEqual(utils.require_identifier("ods_demo_di", "target.table"), "ods_demo_di")

    def test_invalid(self):
        for bad in ("1abc", "a-b", "a b", "a;b", "", "表"):
            with self.assertRaises(SystemExit):
                utils.require_identifier(bad, "target.table")


class TestRedact(OfflineTestCase):
    def test_query_token(self):
        self.assertIn("token=***", utils.redact("GET https://x.com/a?token=abcd1234&page=1"))

    def test_json_secret(self):
        text = utils.redact('{"secret_key": "abcd1234", "page": 2}')
        self.assertIn('"secret_key": "***"', text)
        self.assertIn('"page": 2', text)

    def test_bearer(self):
        out = utils.redact("Authorization: Bearer sk-abcdef123456")
        self.assertNotIn("sk-abcdef123456", out)
        self.assertIn("***", out)

    def test_header_line(self):
        self.assertIn("X-Api-Key: ***", utils.redact("X-Api-Key: my-key-value\nnext line"))

    def test_webhook(self):
        text = utils.redact("post https://open.feishu.cn/open-apis/bot/v2/hook/abc-def-123 failed")
        self.assertIn("/hook/***", text)
        self.assertNotIn("abc-def-123", text)

    def test_url_password(self):
        self.assertIn("user:***@", utils.redact("ssh://user:pass123@host:22"))

    def test_plain_text_untouched(self):
        self.assertEqual(utils.redact("hello world"), "hello world")


class TestSecretValues(OfflineTestCase):
    def _job(self):
        return {
            "secrets": {"sftp_password": "topsecret-pw", "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/hookvalue"},
            "sftp": {"host": "h", "username": "u", "auth": {"type": "password", "password": "topsecret-pw"}},
            "maxcompute": {"project": "p", "access_key_id": "AKID12345", "access_key_secret": "SKVALUE999"},
            "notify": {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/hookvalue"},
        }

    def test_collects_expected_values(self):
        values = utils.collect_secret_values(self._job())
        for expected in ("topsecret-pw", "AKID12345", "SKVALUE999", "hookvalue"):
            self.assertIn(expected, values)

    def test_redact_secrets_by_value(self):
        job = self._job()
        text = f"Authentication failed for user with password {job['sftp']['auth']['password']}"
        out = utils.redact_secrets(utils.collect_secret_values(job), text)
        self.assertNotIn("topsecret-pw", out)
        self.assertIn("***", out)

    def test_short_values_are_not_replaced(self):
        out = utils.redact_secrets(["ab"], "ab is a common substring")
        self.assertIn("ab is", out)


class TestRetry(OfflineTestCase):
    def test_succeeds_after_failures(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            return "ok"

        self.assertEqual(utils.retry_call(flaky, attempts=3, base_delay=0, desc="测试"), "ok")
        self.assertEqual(calls["n"], 3)

    def test_exhausted_message(self):
        def always_fail():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError) as ctx:
            utils.retry_call(always_fail, attempts=2, base_delay=0, desc="测试")
        self.assertIn("重试 1 次仍失败", str(ctx.exception))

    def test_fatal_not_retried(self):
        calls = {"n": 0}

        def fatal():
            calls["n"] += 1
            raise utils.FatalSourceError("auth failed")

        with self.assertRaises(utils.FatalSourceError):
            utils.retry_call(fatal, attempts=5, base_delay=0, desc="测试")
        self.assertEqual(calls["n"], 1)


class TestRunLock(OfflineTestCase):
    def test_second_acquire_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.lock"
            with utils.RunLock(path):
                with self.assertRaises(SystemExit):
                    utils.RunLock(path).__enter__()

    def test_ready_after_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.lock"
            with utils.RunLock(path):
                pass
            with utils.RunLock(path) as lock:
                self.assertTrue(lock.path.is_file())


if __name__ == "__main__":
    unittest.main()
