# -*- coding: utf-8 -*-
"""utils：布尔/标识符校验、脱敏、重试、运行锁。"""

from __future__ import annotations

import errno
import io
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

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

    def test_inline_header_with_space_after_separator(self):
        """行中部 `X-Api-Key: sk-xxx`（冒号后带空格）：_HEADER_RE 只认行首，
        _QUERY_RE 必须兜住——否则 requests 异常里的这种形态会把密钥明文写进日志。"""
        out = utils.redact("boom [X-Api-Key: sk-abcdef123456] end")
        self.assertNotIn("sk-abcdef123456", out)
        self.assertIn("X-Api-Key: ***", out)

    def test_inline_header_with_space_no_false_positive_on_ordinary_text(self):
        """普通文本 "note: hello" 不能因为「冒号+空格」被误伤（键名不敏感就原样保留）。"""
        self.assertEqual(utils.redact("note: hello there"), "note: hello there")

    def test_webhook(self):
        text = utils.redact("post https://open.feishu.cn/open-apis/bot/v2/hook/abc-def-123 failed")
        self.assertIn("/hook/***", text)
        self.assertNotIn("abc-def-123", text)

    def test_url_password(self):
        self.assertIn("user:***@", utils.redact("ssh://user:pass123@host:22"))

    def test_json_numeric_secret_is_masked(self):
        """{"password": 12345} 这类不带引号的数字值也要遮（JSON 规则只吃字符串值）。"""
        out = utils.redact('{"password": 12345, "page": 2}')
        self.assertNotIn("12345", out)
        self.assertIn("***", out)

    def test_plain_text_untouched(self):
        self.assertEqual(utils.redact("hello world"), "hello world")

    def test_spaced_sensitive_value_masked_to_eol(self):
        """口令短语含空格（password=my secret）不能被第一个词截断：敏感键的值遮到行尾。"""
        out = utils.redact("login failed: password=my secret and more")
        self.assertNotIn("secret", out)
        self.assertNotIn("more", out)
        # 已遮罩的文本不重复吞（*** 开头的值跳过本规则）
        self.assertEqual(utils.redact("Invalid token: *** / ***"), "Invalid token: *** / ***")
        # 未闭合引号（日志截断）：KV/JSON 要收尾引号、常规 QUERY 不吃引号，必须走
        # 这条兜底，否则三套规则全绕过、明文泄露
        out = utils.redact('password="abc123456')
        self.assertNotIn("abc123456", out)
        out = utils.redact("password='abc123456")
        self.assertNotIn("abc123456", out)
        # 「带引号的键」+ 不带引号的值：KV/JSON 都不收，必须走 SPACE 兜底
        out = utils.redact('"password": my secret')
        self.assertNotIn("secret", out)

    def test_url_userinfo_password_with_at_sign(self):
        """userinfo 口令含 @（proxy 场景）要按最后一个 @ 切分：余段不能明文留下。"""
        out = utils.redact("HTTPS_PROXY=https://user:p@ss@proxy:8080")
        self.assertNotIn("p@ss", out)
        self.assertNotIn("ss@proxy", out)
        self.assertIn("user:***@", out)

    def test_quoted_value_after_key(self):
        """!r 插值/repr 形态（access_token='t-xxx'，行中）必须遮：query 规则的值部分不吃引号。"""
        out = utils.redact("拉取失败 access_token='t-g1045abc123456' url=https://x")
        self.assertNotIn("t-g1045abc123456", out)
        self.assertIn("access_token='***'", out)
        out = utils.redact('fail: code=1, token: "t-g1045abc123456"')
        self.assertNotIn("t-g1045abc123456", out)
        # 键名不敏感、值里再嵌 k=v 的也要递归兜住
        out = utils.redact("note: 'access_token=abc123456'")
        self.assertNotIn("abc123456", out)

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

    def test_nested_query_does_not_recursion_error(self):
        """嵌套 key=value 段用迭代脱敏，不能按段数递归到 RecursionError。"""
        nested = "x=" * 2000 + "token=supersecret"
        out = utils.redact("note=" + nested)
        self.assertNotIn("supersecret", out)
        self.assertIn("token=***", out)

    def test_percent_encoded_nesting_is_bounded(self):
        """多层 %25 编码嵌套（解码一层才露出下一层）不能把脱敏打成 RecursionError。

        实测 1200 层嵌套（输入约 1.4MB）在无深度上限时会抛 RecursionError——而脱敏恰好
        跑在"打印失败原因"的必经路径上。这里用小规模嵌套验证管道接通 + 直接验证上限分支
        （大规模构造本身是 O(n²) 的，不适合放进套件）。
        """
        from urllib.parse import quote

        text = "token=secret123456"
        for i in range(30):
            text = f"k{i}=" + quote(text, safe="")
        out = utils.redact(text)
        self.assertIsInstance(out, str)
        self.assertIn("***", out)
        # 深度上限：到上限按"宁可多脱敏"整段遮掉
        self.assertEqual(utils.redact("x", _depth=utils._MAX_REDACT_DEPTH), "***")

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

    def test_single_string_values_are_not_split_into_chars(self):
        """values 传成单个字符串时按"一个密钥"处理：set() 拆成单字符会让值级脱敏静默失效。"""
        out = utils.redact_secrets("topsecret-pw", "password is topsecret-pw")
        self.assertNotIn("topsecret-pw", out)
        self.assertIn("***", out)

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

    def test_redact_secrets_masks_aggressively_encoded_form(self):
        """部分编码器把 "-" 这类字符也编码成 %2D：该形态（旧注释里的例子）同样要遮。"""
        out = utils.redact_secrets(["t-abc123"], "url?data=t%2Dabc123 end")
        self.assertNotIn("t%2Dabc123", out)
        self.assertIn("***", out)

    def test_redact_secrets_masks_aggressively_encoded_non_ascii(self):
        """含中文的口令：激进编码变体按字节编码（%E5%AF%86，而不是 Latin-1 的 å…）。"""
        secret = "p@ss-密码"
        encoded = "p%40ss%2D%E5%AF%86%E7%A0%81"
        out = utils.redact_secrets([secret], f"url?data={encoded} end")
        self.assertNotIn(encoded, out)
        self.assertIn("***", out)

    def test_redact_secrets_tolerates_non_str_values(self):
        """数字密钥能遮；None/bool 跳过（str 化会把文本里的 None/True 误替成 ***）。"""
        out = utils.redact_secrets([12345, None, True], "count=None flag=True id=12345")
        self.assertIn("id=***", out)
        self.assertIn("count=None", out)
        self.assertIn("flag=True", out)


class TestRetry(OfflineTestCase):
    def test_deterministic_error_message_is_redacted(self):
        """确定性错误分支的报错同样过值级脱敏（KeyError 回显的键里可能带凭证值）。"""

        def broken():
            raise KeyError("token=SECRET-abc123")

        with self.assertRaises(RuntimeError) as ctx:
            utils.retry_call(broken, attempts=1, desc="x", secrets=["SECRET-abc123"])
        self.assertNotIn("SECRET-abc123", str(ctx.exception))

    def test_deterministic_error_is_not_retried(self):
        """TypeError/KeyError 这类编程错误重试多少次都一样：直接报错，不白等退避。"""
        calls = {"n": 0}

        def broken():
            calls["n"] += 1
            raise TypeError("字段名写错")

        started = time.perf_counter()
        with self.assertRaises(RuntimeError) as ctx:
            utils.retry_call(broken, attempts=5, base_delay=30, desc="x")
        self.assertEqual(calls["n"], 1)  # 只试了一次
        self.assertLess(time.perf_counter() - started, 1.0)  # 没有真的睡 30s
        self.assertIn("确定性错误", str(ctx.exception))

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

    def test_redact_secrets_accepts_bare_scalar_values(self):
        """values 直接传裸标量（数字/字符串，没有列表壳）也不能炸：与单字符串同口径。"""
        out = utils.redact_secrets(123456, "charge failed id=123456")
        self.assertNotIn("123456", out)

    def test_first_backoff_respects_max_delay(self):
        """base_delay 配得比 max_delay 大时首次退避也不能超上限（原来只有后续退避夹取）。"""
        slept = []

        def always_fail():
            raise RuntimeError("boom")

        with mock.patch.object(utils.time, "sleep", side_effect=slept.append):
            with self.assertRaises(RuntimeError):
                utils.retry_call(always_fail, attempts=2, base_delay=100, max_delay=3, desc="测试")
        self.assertEqual(slept, [3])

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


class TestTryLockErrno(OfflineTestCase):
    def _fcntl_mod(self):
        mod = mock.Mock()
        mod.LOCK_EX = 2
        mod.LOCK_NB = 4
        mod.LOCK_UN = 8
        return mod

    def test_busy_errnos_return_false(self):
        fake = self._fcntl_mod()
        fh = mock.Mock()
        for code in (errno.EAGAIN, errno.EACCES, errno.EDEADLK):
            fake.flock.side_effect = OSError(code, "busy")
            with mock.patch.object(utils, "fcntl", fake):
                self.assertFalse(utils._try_lock(fh), msg=code)

    def test_unsupported_lock_fails_closed_by_default(self):
        """文件系统不支持文件锁时默认拒绝执行（fail-closed）：无锁继续会让两个实例并发写
        同一作业/表、台账丢更新；显式 SFTP2ODS_ALLOW_NO_LOCK=1 才接受无互斥风险继续。"""
        fake = self._fcntl_mod()
        fake.flock.side_effect = OSError(errno.ENOLCK, "no lock")
        with mock.patch.object(utils, "fcntl", fake), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                utils._try_lock(mock.Mock())
        self.assertIn("ALLOW_NO_LOCK", str(ctx.exception))
        logged = []
        with (
            mock.patch.object(utils, "fcntl", fake),
            mock.patch.dict(os.environ, {"SFTP2ODS_ALLOW_NO_LOCK": "1"}, clear=True),
            mock.patch.object(utils, "log_once", logged.append),
        ):
            self.assertTrue(utils._try_lock(mock.Mock()))
        self.assertTrue(any("无互斥风险" in str(line) for line in logged), logged)

    def test_run_lock_closes_fh_when_try_lock_raises_systemexit(self):
        """_try_lock 抛 SystemExit（文件系统不支持锁且未放行）时 __enter__ 也要关句柄：
        with 不会为抛出的 __enter__ 调 __exit__，不关就按次泄漏 fd（与 interprocess_lock 同口径）。"""
        with tempfile.TemporaryDirectory() as tmp:
            lock = utils.RunLock(Path(tmp) / "x.lock")
            with mock.patch.object(utils, "_try_lock", side_effect=SystemExit("no lock support")):
                with self.assertRaises(SystemExit):
                    lock.__enter__()
            self.assertIsNone(lock.fh)

    def test_other_oserror_raises(self):
        fake = self._fcntl_mod()
        fake.flock.side_effect = OSError(errno.EBADF, "bad fd")
        with mock.patch.object(utils, "fcntl", fake):
            with self.assertRaises(OSError) as ctx:
                utils._try_lock(mock.Mock())
        self.assertEqual(ctx.exception.errno, errno.EBADF)


class TestLogSink(OfflineTestCase):
    def test_write_failure_warns_stderr_and_drops_sink(self):
        class Boom:
            def write(self, *_args, **_kwargs):
                raise OSError("disk full")

            def flush(self):
                return None

        boom = Boom()
        utils.add_log_sink(boom)
        try:
            buf = io.StringIO()
            with mock.patch.object(sys, "stderr", buf):
                utils.log("hello")
                first = buf.getvalue()
                self.assertIn("日志文件", first)
                utils.log("again")
                self.assertEqual(buf.getvalue(), first)
        finally:
            utils.remove_log_sink(boom)

    def test_broken_stdout_does_not_break_business(self):
        """stdout 断管（BrokenPipeError，如 `| head` 提前退出）时 log() 自身不能抛异常。"""
        with mock.patch("builtins.print", side_effect=BrokenPipeError("closed")):
            utils.log("业务还在跑")  # 不抛即通过

    def test_broken_sink_is_closed_when_dropped(self):
        """写失败的 sink 被摘掉时必须顺手关闭：只从列表移除的话句柄会挂到进程退出。"""

        class Boom:
            def __init__(self):
                self.closed = False

            def write(self, *_args, **_kwargs):
                raise OSError("disk full")

            def flush(self):
                return None

            def close(self):
                self.closed = True

        boom = Boom()
        utils.add_log_sink(boom)
        try:
            with mock.patch.object(sys, "stderr", io.StringIO()):
                utils.log("hello")
            # 断言放在 remove_log_sink 之前：后者自己也会 close，放在后面就测不出"摘掉时顺手关"
            self.assertTrue(boom.closed)
        finally:
            # 断言失败也要摘掉坏 sink：否则它会挂在模块级列表里污染后续用例
            utils.remove_log_sink(boom)


if __name__ == "__main__":
    unittest.main()
