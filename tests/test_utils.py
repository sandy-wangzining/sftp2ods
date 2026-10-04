# -*- coding: utf-8 -*-
"""utils：布尔/标识符校验、脱敏、重试、运行锁。"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

# 只在路径缺失时追加（不插到最前面）：避免把仓库根/tests 目录置于标准库与第三方库
# 之前遮蔽同名模块；按 README 在仓库根运行（或 CI 里 pip install -e .）时，
# 本地包本来就在搜索路径最前（python -m 会把当前目录放在 sys.path[0]）
for _path in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

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

    def test_non_string_rejected(self):
        # None/True 经 str() 会变成 "None"/"True" 这种"合法"标识符，必须直接拒绝
        for bad in (None, True, False, 123):
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

    def test_webhook_id_without_scheme(self):
        """requests 的异常消息里 webhook 只有路径（没有 scheme），裸 hook id 也必须遮掉。"""
        text = "Max retries exceeded with url: /open-apis/bot/v2/hook/9f8e7d6c-5b4a-3210 (Caused by ...)"
        out = utils.redact(text)
        self.assertNotIn("9f8e7d6c-5b4a-3210", out)
        self.assertIn("/hook/***", out)

    def test_backslash_run_is_fast(self):
        """反斜杠串不能触发 _JSON_RE 的指数回溯（修复前 36 个反斜杠要 19 秒）。

        用"规模翻倍、耗时不应超线性暴涨"的相对判据（地板 0.5s 兜底计时噪声）：
        共享 CI 上固定 1 秒的绝对阈值会偶发假失败，而回溯爆炸的耗时是指数级、
        任何合理判据都拦得住。
        """

        def elapsed(count: int) -> float:
            text = "'a':'" + "\\" * count + "xy"
            started = time.perf_counter()
            out = utils.redact(text)
            self.assertEqual(out, text)
            return time.perf_counter() - started

        small, big = elapsed(20), elapsed(40)
        self.assertLess(big, max(small * 8, 0.5), f"反斜杠串耗时 {small:.3f}s → {big:.3f}s，疑似回溯爆炸")

    def test_long_token_like_text_is_fast(self):
        """超长的小写字母数字串不能把 _URL_AUTH_RE 拖成 O(n²)（修复前 20KB 要 10 秒以上）。

        文本里必须同时出现 "://" 与 "@"（否则预判直接跳过该正则，测不到它）；
        规模放大 4 倍后耗时不应接近 16 倍。O(n²) 的下界判据同样用地板值兜住计时噪声。
        """

        def elapsed(kb: int) -> float:
            n = kb * 1024
            text = "a" * n + "://" + "b" * n + "@"
            started = time.perf_counter()
            out = utils.redact(text)
            self.assertEqual(out, text)  # 没有可脱敏的 userinfo，只测扫描/回溯的成本
            return time.perf_counter() - started

        small, big = elapsed(4), elapsed(16)
        self.assertLess(big, max(small * 8, 0.5), f"脱敏耗时 {small:.3f}s → {big:.3f}s，正则疑似退化成 O(n²)")


class TestSecretValues(OfflineTestCase):
    def _job(self):
        return {
            "secrets": {
                "sftp_password": "topsecret-pw",
                "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/hookvalue",
            },
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

    def test_redact_secrets_masks_url_encoded_forms(self):
        """凭证以 URL 编码形态落进自由文本时也要遮：明文 / quote / quote_plus 三种形态一起替。

        paramiko 等把凭证塞进自由文本的报错里没有可识别的键名，形态规则挡不住；
        只替明文时，编码后的凭证（如 token 里的空格/斜杠被转义）会原样进日志。
        """
        secret = "SECRET value/with+plus"
        encoded = urllib.parse.quote(secret, safe="")
        encoded_plus = urllib.parse.quote_plus(secret)
        # 三种形态确实互不相同，用例才有意义
        self.assertNotIn(secret, (encoded, encoded_plus))
        self.assertNotEqual(encoded, encoded_plus)
        text = f"plain={secret} quote={encoded} plus={encoded_plus}"
        out = utils.redact_secrets([secret], text)
        self.assertNotIn(secret, out)
        self.assertNotIn(encoded, out)
        self.assertNotIn(encoded_plus, out)
        self.assertEqual(out.count("***"), 3)


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

    def test_attempts_must_be_positive(self):
        # attempts<=0 时循环体一次都不执行，最终会报出"重试 -1 次仍失败：None"这种无信息的错
        with self.assertRaises(ValueError):
            utils.retry_call(lambda: None, attempts=0, base_delay=0, desc="测试")

    def test_secret_values_redacted_in_final_error(self):
        secret = "SECRETVALUE-12345"

        def fail():
            raise RuntimeError(f"auth failed for {secret}")

        with self.assertRaises(RuntimeError) as ctx:
            utils.retry_call(fail, attempts=1, base_delay=0, desc="测试", secrets=[secret])
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))


class TestRunLock(OfflineTestCase):
    def test_lock_path_is_redirected_to_temp_in_tests(self):
        """单测把运行锁落点重定向到临时目录：跑测试不会在仓库 .run-locks/ 里
        无限累积锁文件（临时作业路径每次哈希都不同）。生产行为不变。

        断言可观察行为（锁文件落在系统临时目录下、能正常加锁），不依赖 cli 内部的
        ROOT 常量名——常量改名/改惰性求值不该让用例失败。
        """
        from sftp2ods import cli as cli_mod

        lock = cli_mod._lock_path(Path("jobs/demo.json"))
        self.assertTrue(lock.is_relative_to(Path(tempfile.gettempdir())))
        with utils.RunLock(lock):
            self.assertTrue(lock.is_file())

    def test_second_acquire_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.lock"
            with utils.RunLock(path):
                with self.assertRaises(SystemExit):
                    # 第二次进入失败时的清理路径与生产一致（不手工调 __enter__ 泄漏锁对象）
                    with utils.RunLock(path):
                        pass

    def test_ready_after_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.lock"
            with utils.RunLock(path):
                pass
            with utils.RunLock(path) as lock:
                self.assertTrue(lock.path.is_file())


if __name__ == "__main__":
    unittest.main()
