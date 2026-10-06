# -*- coding: utf-8 -*-
"""init_wizard：脚本化问答生成配置（不连 SFTP、不连数仓）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

# 只在路径缺失时追加（不插到最前面）：避免把仓库根/tests 目录置于标准库与第三方库
# 之前遮蔽同名模块；按 README 在仓库根运行（或 CI 里 pip install -e .）时，
# 本地包本来就在搜索路径最前（python -m 会把当前目录放在 sys.path[0]）
for _path in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

from _helpers import FakeSftp, OfflineTestCase, add_file, connect_to, csv_bytes  # noqa: E402

from sftp2ods import (
    config,  # noqa: E402
    init_wizard,  # noqa: E402
)
from sftp2ods import sftp as sftp_mod  # noqa: E402
from sftp2ods.utils import FatalSourceError  # noqa: E402


class ScriptedAsk:
    """按"提示词片段"顺序回答；顺序即问答顺序，匹配到的规则会被消费。"""

    def __init__(self, script):
        self.script = list(script)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        # 在副本上枚举：边遍历边 pop 只因为紧跟着 return 才安全，日后想改成"未命中继续扫"
        # 就会下标错位/漏答
        for index, (fragment, value) in enumerate(list(self.script)):
            if fragment in prompt:
                self.script.pop(index)
                return str(value)
        raise AssertionError(f"没有匹配的预设回答：{prompt!r}")


class WizardTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.csv_path = self.tmp / "sample.csv"
        self.csv_path.write_bytes(
            csv_bytes(["Order ID", "Settlement amount", "Created time"], [["o1", "1.00", "2026-09-20 00:00:00"]])
        )
        self.out_path = self.tmp / "job.json"

    def base_script(self, sample_choice="2"):
        script = [
            ("作业名", "demo_sftp"),
            ("主机名", "sftp.example.com"),
            ("端口", "2200"),
            ("登录名", "user1"),
            ("请选择编号", "1"),  # 认证 = 密码
            ("密码", "pw123"),
            ("远端目录", "/statements"),
            ("请选择编号", "1"),  # 布局 = 平铺
            ("文件名正则", "report_(?P<date>\\d{8})\\.csv"),
            ("请选择编号", sample_choice),
        ]
        if sample_choice == "2":
            script.append(("本地 CSV", str(self.csv_path)))
        return script

    def common_tail(self):
        return [
            ("金额", "2"),
            ("整数", ""),
            ("合计行", "n"),
            ("关键字段", "order_id"),
            ("缺文件核对", "y"),
            ("时区", "UTC"),
            ("几点前跑", "02:30"),
            ("webhook", "https://open.feishu.cn/open-apis/bot/v2/hook/zzz"),
            ("项目名", "my_project"),
            ("目标表名", "ods_demo_di"),
            ("AccessKeyId", "AK123"),
            ("AccessKeySecret", "SK456"),
            ("endpoint", ""),
            ("一句话描述", ""),
        ]

    def run_wizard(self, script, out_path=None):
        ask = ScriptedAsk(script)
        # 同一个 ScriptedAsk 兼作密钥入口：密钥类问题现在走 ask_secret（默认 getpass 不回显），
        # 脚本化测试把两者指向同一个按问答题顺序取答案的对象即可
        rc = init_wizard.run_init(
            out_path=str(out_path or self.out_path), ask=ask, ask_secret=ask, echo=lambda *_a, **_k: None
        )
        return rc, ask

    def load_job(self):
        raw = json.loads(self.out_path.read_text(encoding="utf-8"))
        job, _config = config.render_job(raw, {}, date(2026, 9, 23))
        job = config.normalize_job(job)
        config.validate_job(job)
        return job


class TestWizardHappyPath(WizardTestCase):
    def test_generates_valid_job(self):
        rc, _ = self.run_wizard(self.base_script() + self.common_tail())
        self.assertEqual(rc, 0)
        self.assertTrue(self.out_path.is_file())
        job = self.load_job()
        self.assertEqual(job["job"], "demo_sftp")
        self.assertEqual(job["sftp"]["host"], "sftp.example.com")
        self.assertEqual(job["sftp"]["port"], 2200)
        self.assertEqual(job["sftp"]["auth"]["password"], "pw123")
        self.assertEqual(job["source"]["root"], "/statements")
        self.assertEqual(job["source"]["layout"], "flat")
        self.assertEqual(job["source"]["file_regex"], "report_(?P<date>\\d{8})\\.csv")
        names = [(col["name"], col["type"]) for col in job["parse"]["columns"]]
        self.assertEqual(
            names,
            [("order_id", "string"), ("settlement_amount", "decimal(19,10)"), ("created_time", "string")],
        )
        self.assertEqual(job["parse"]["skip_if_empty"], ["order_id"])
        self.assertEqual(job["missing"]["timezone"], "UTC")
        self.assertEqual(job["missing"]["grace"], "02:30")
        self.assertEqual(job["notify"]["webhook"], "https://open.feishu.cn/open-apis/bot/v2/hook/zzz")
        self.assertEqual(job["target"]["table"], "ods_demo_di")
        self.assertEqual(job["maxcompute"]["project"], "my_project")

    def test_footer_without_amount_column_is_not_written(self):
        """选了合计行核对但没有金额（decimal）列：不写空 footer、只提示未启用
        （空列表与"没配"等价，留着会让用户以为配了核对）。"""
        tail = []
        for question, answer in self.common_tail():
            if question == "金额":
                answer = ""  # 没有金额列
            elif question == "整数":
                answer = "2"  # 第 2 列按整数处理
            elif question == "合计行":
                answer = "y"  # 仍选择做合计核对
            tail.append((question, answer))
        rc, _ = self.run_wizard(self.base_script() + tail)
        self.assertEqual(rc, 0)
        raw = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertNotIn("footer", raw["parse"])

    def test_no_sample_placeholder(self):
        script = self.base_script(sample_choice="3")
        # 占位表头名 + 没有 skip 问题，直接接尾部规则
        tail = [
            ("第一列表头", "Order ID"),
            ("合计行", "n"),
            ("缺文件核对", "n"),
            ("webhook", ""),
            ("项目名", "my_project"),
            ("目标表名", ""),
            ("AccessKeyId", "AK"),
            ("AccessKeySecret", "SK"),
            ("endpoint", ""),
            ("一句话描述", ""),
        ]
        rc, _ = self.run_wizard(script + tail)
        self.assertEqual(rc, 0)
        job = self.load_job()
        self.assertEqual(job["parse"]["columns"], [{"header": "Order ID", "name": "order_id", "type": "string"}])
        self.assertFalse(job["missing"]["check"])
        self.assertEqual(job["target"]["table"], "ods_demo_sftp_di")  # 空答案 → 默认表名


class TestWizardFailurePaths(WizardTestCase):
    def test_empty_host_cancels(self):
        script = [("作业名", "x"), ("主机名", ""), ("主机名", ""), ("主机名", "")]
        rc, _ = self.run_wizard(script)
        self.assertEqual(rc, 1)
        self.assertFalse(self.out_path.exists())

    def test_eof_cancels(self):
        class EofAsk:
            def __call__(self, prompt=""):
                raise EOFError()

        rc = init_wizard.run_init(
            out_path=str(self.out_path), ask=EofAsk(), ask_secret=EofAsk(), echo=lambda *_a, **_k: None
        )
        self.assertEqual(rc, 1)

    def test_keyboard_interrupt_returns_130(self):
        """Ctrl+C 按 README 的退出码约定报 130（与同步流程一致），不再混进"配置错"的 1。"""

        class InterruptAsk:
            def __call__(self, prompt=""):
                raise KeyboardInterrupt()

        rc = init_wizard.run_init(
            out_path=str(self.out_path), ask=InterruptAsk(), ask_secret=InterruptAsk(), echo=lambda *_a, **_k: None
        )
        self.assertEqual(rc, 130)
        self.assertFalse(self.out_path.exists())

    def test_keyboard_interrupt_after_replace_reports_written(self):
        """os.replace 已完成后才 Ctrl+C（如收尾 chmod 阶段）不能再说"未生成任何文件"：
        含明文密钥的配置已经在磁盘上，该报"文件已写全"（与内层收尾提示自相矛盾会误导操作者）。"""
        script = self.base_script() + self.common_tail()
        asks = ScriptedAsk(script)
        echoes: list = []
        fake_os = mock.Mock(wraps=init_wizard.os)
        fake_os.name = "posix"  # chmod 收紧分支只在 posix 执行（Windows 上本来就不走）
        fake_os.chmod.side_effect = KeyboardInterrupt
        with mock.patch.object(init_wizard, "os", fake_os):
            rc = init_wizard.run_init(
                out_path=str(self.out_path),
                ask=asks,
                ask_secret=asks,
                echo=lambda *a: echoes.append(" ".join(str(x) for x in a)),
            )
        self.assertEqual(rc, 130)
        self.assertTrue(self.out_path.is_file())
        joined = "\n".join(echoes)
        self.assertIn("已写全", joined)
        self.assertNotIn("未生成任何文件", joined)

    def test_empty_password_three_times_cancels(self):
        """密码连续三次为空要取消并返回 1：原来是无上限 while，输入源持续给空白行
        （管道/自动应答）时会一直刷屏不退出。"""
        script = [
            ("作业名", "x"),
            ("主机名", "h"),
            ("端口", "22"),
            ("登录名", "u"),
            ("请选择编号", "1"),  # 认证 = 密码
            ("密码", ""),
            ("密码", ""),
            ("密码", ""),
        ]
        rc, _ = self.run_wizard(script)
        self.assertEqual(rc, 1)
        self.assertFalse(self.out_path.exists())

    def test_out_path_is_directory(self):
        with self.assertRaises(SystemExit):
            self.run_wizard(self.base_script() + self.common_tail(), out_path=self.tmp)

    def test_relative_workdir_path_is_not_double_joined(self):
        """workdir 传相对路径且 --init-out 留空时，默认输出只拼一次 root：
        原来会对已含 root 的相对路径再拼一次，静默生成到 build/out/build/out/jobs/ 下。"""
        script = self.base_script() + self.common_tail()
        asks = ScriptedAsk(script)
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rc = init_wizard.run_init(
                    workdir=Path("build/out"), ask=asks, ask_secret=asks, echo=lambda *_a, **_k: None
                )
            finally:
                os.chdir(cwd)  # Windows 上必须先把 CWD 移出 tmp，否则 TemporaryDirectory 清理失败
            self.assertEqual(rc, 0)
            self.assertTrue((Path(tmp) / "build" / "out" / "jobs" / "demo_sftp.json").is_file())
            self.assertFalse((Path(tmp) / "build" / "out" / "build").exists())

    def test_empty_aksk_warns_before_success(self):
        """AK/SK 留空时仍可生成（凭证可以只放 --config），但必须明确提示作业文件里
        没有可用凭证——不能静默报"已生成成功"，让用户到 --check 才发现跑不起来。"""
        script = [
            (frag, "" if frag in ("AccessKeyId", "AccessKeySecret") else val)
            for frag, val in self.base_script() + self.common_tail()
        ]
        echoes: list = []
        ask = ScriptedAsk(script)
        rc = init_wizard.run_init(
            out_path=str(self.out_path),
            ask=ask,
            ask_secret=ask,
            echo=lambda *a: echoes.append(" ".join(str(x) for x in a)),
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(echoes)
        self.assertIn("AccessKeyId/AccessKeySecret 未填全", joined)

    def test_blank_webhook_and_sk_are_treated_as_unset(self):
        """webhook/sk 的纯空白输入不能算"已配置"：webhook 是 URL、AK/SK 是固定格式凭据，
        空白只可能是误输入——否则生成一份必然发不出告警/必然认证失败的作业却报成功。"""
        script = [
            (frag, "   " if frag == "webhook" else (" " if frag == "AccessKeySecret" else val))
            for frag, val in self.base_script() + self.common_tail()
        ]
        echoes: list = []
        ask = ScriptedAsk(script)
        rc = init_wizard.run_init(
            out_path=str(self.out_path),
            ask=ask,
            ask_secret=ask,
            echo=lambda *a: echoes.append(" ".join(str(x) for x in a)),
        )
        self.assertEqual(rc, 0)
        raw = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertNotIn("notify", raw)
        self.assertEqual(raw["maxcompute"]["access_key_secret"], "")
        self.assertTrue(any("AccessKeyId/AccessKeySecret 未填全" in e for e in echoes), echoes)

    def test_blank_passphrase_is_treated_as_unset(self):
        """私钥口令纯空白 = 没填：不写 passphrase 字段（与密码分支的 strip 判空同口径）。"""
        script = [item for item in self.base_script() + self.common_tail() if item[0] != "密码"]
        idx = next(i for i, item in enumerate(script) if item[0] == "请选择编号")
        script[idx] = ("请选择编号", "2")  # 认证 = 私钥
        script[idx + 1 : idx + 1] = [("私钥文件路径", "~/.ssh/id_rsa"), ("私钥口令", "   ")]
        rc, _ = self.run_wizard(script)
        self.assertEqual(rc, 0)
        raw = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["sftp"]["auth"]["type"], "key")
        self.assertNotIn("passphrase", raw["sftp"]["auth"])


class TestWizardSecretInput(WizardTestCase):
    """密钥类输入必须走不回显入口（原来走 input()，密码/口令/webhook 明文回显在终端）。"""

    def test_secret_prompts_go_through_unhidden_entry(self):
        plain, secret = [], []
        choices = iter(["1", "1", "3"])  # 认证=密码、布局=平铺、样本来源=暂时没有

        def ask(prompt=""):
            plain.append(prompt)
            if "请选择编号" in prompt:
                return next(choices, "")
            for fragment, value in (
                ("主机名", "sftp.example.com"),
                ("登录名", "user1"),
                ("远端目录", "/data"),
                ("文件名正则", "report_(?P<date>\\d{8})\\.csv"),
                ("合计行", "n"),
                ("缺文件核对", "n"),
            ):
                if fragment in prompt:
                    return value
            return ""

        def ask_secret(prompt=""):
            secret.append(prompt)
            return "hidden-value"

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            rc = init_wizard.run_init(out_path=str(path), ask=ask, ask_secret=ask_secret, echo=lambda *_a, **_k: None)
            job = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        # 密码 / webhook / AccessKeySecret 都走不回显入口
        self.assertTrue(any("密码" in p for p in secret))
        self.assertTrue(any("webhook" in p for p in secret))
        self.assertTrue(any("AccessKeySecret" in p for p in secret))
        # 普通问题不会落到密钥入口
        self.assertFalse(any("主机名" in p for p in secret))
        self.assertFalse(any("AccessKeySecret" in p for p in plain))
        self.assertEqual(job["sftp"]["auth"]["password"], "hidden-value")
        self.assertEqual(job["maxcompute"]["access_key_secret"], "hidden-value")

    def test_default_secret_entry_uses_getpass(self):
        """不注入 ask_secret 时，默认入口是 getpass（不回显）。"""
        with mock.patch.object(init_wizard.getpass, "getpass", return_value="hidden") as gp:
            self.assertEqual(init_wizard._default_ask_secret("密码："), "hidden")
        gp.assert_called_once_with("密码：")

    def test_bad_regex_generates_but_validation_flags(self):
        # 向导本身不校验正则语法（--check/正式跑时才校验），生成的文件仍可被 load 到再报错
        script = self.base_script() + self.common_tail()
        # 把正则答案改成一个没有 date 捕获组的写法
        for index, (fragment, value) in enumerate(script):
            if fragment == "文件名正则":
                script[index] = (fragment, "report_\\d{8}\\.csv")
        rc, _ = self.run_wizard(script)
        self.assertEqual(rc, 0)
        raw = json.loads(self.out_path.read_text(encoding="utf-8"))
        with self.assertRaises(SystemExit):
            config.validate_job(config.normalize_job(raw))


class TestWizardSampleErrors(WizardTestCase):
    def test_local_sample_missing_file_retries(self):
        """读本地样本失败（不存在/无权限等 OSError）要提示后重试，不能直接终止向导。"""
        asks = iter([str(self.tmp / "no_such_file.csv"), str(self.csv_path)])
        echoes: list = []
        # next(asks, "")：问答次数一旦增加会得到明确的"读取失败"而非裸 StopIteration
        headers = init_wizard._read_local_sample(lambda p="": next(asks, ""), echoes.append)
        self.assertEqual(headers, ["Order ID", "Settlement amount", "Created time"])
        self.assertTrue(any("读取失败" in str(line) for line in echoes), echoes)

    def test_remote_connect_unexpected_error_propagates(self):
        """代码缺陷（TypeError 等）不能降级成"连接失败"：否则会生成一份列定义完全错误的配置
        却提示成功。"""
        with mock.patch.object(sftp_mod.SftpSource, "__init__", side_effect=TypeError("bug")):
            with self.assertRaises(TypeError):
                init_wizard._read_remote_sample(lambda p="": p, lambda *_a, **_k: None, {}, {})

    def test_remote_sample_bad_header_returns_none_like_local(self):
        """远端样本读表头失败（编码/格式异常）与本地样本同口径：提示后返回 None，
        让 _collect_sample_headers 的三次重选机制生效，而不是把整个向导打挂。"""
        fake = FakeSftp()
        add_file(fake, "/statements/report_20260920.csv", csv_bytes(["Order ID"], [["o1"]]))
        sftp_cfg = {"host": "h", "port": 22, "username": "u", "auth": {"type": "password", "password": "p"}}
        source_cfg = {"root": "/statements", "layout": "flat", "file_regex": "report_(?P<date>\\d{8})\\.csv"}
        echoes: list = []
        with (
            mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)),
            mock.patch.object(
                init_wizard.parse_mod, "read_header", side_effect=config.ConfigError("不是 utf-8-sig 编码")
            ),
        ):
            got = init_wizard._read_remote_sample(lambda p="": p, echoes.append, sftp_cfg, source_cfg)
        self.assertIsNone(got)
        self.assertTrue(any("读取表头失败" in str(line) for line in echoes), echoes)

    def test_unexpected_value_error_is_not_reported_as_cancel(self):
        """向导内部的 ValueError 不能被顶层当成"用户取消"（那会把真实错误吞成"已取消"）。"""

        class BoomAsk:
            def __call__(self, prompt=""):
                raise ValueError("内部解析出错")

        with self.assertRaises(ValueError):
            init_wizard.run_init(
                out_path=str(self.out_path), ask=BoomAsk(), ask_secret=BoomAsk(), echo=lambda *_a, **_k: None
            )

    def test_write_failure_returns_1_with_clean_message(self):
        """写文件失败（磁盘满/权限）按退出码 1 汇报，不是裸 traceback。"""
        script = self.base_script() + self.common_tail()
        asks = ScriptedAsk(script)
        with mock.patch.object(init_wizard.os, "open", side_effect=OSError("disk full")):
            rc = init_wizard.run_init(
                out_path=str(self.out_path), ask=asks, ask_secret=asks, echo=lambda *_a, **_k: None
            )
        self.assertEqual(rc, 1)
        self.assertFalse(self.out_path.exists())

    def test_unknown_yn_answer_is_reasked(self):
        """y/n 问答答"随便"这类无法识别的说法不能静默当成"否"：提示后重问；
        常见中文肯定（是/有/1）按是处理。"""
        echoes: list = []
        asks = iter(["随便", "y"])
        got = init_wizard._ask_bool(lambda p="": next(asks, ""), echoes.append, "测试项", default=False)
        self.assertTrue(got)
        self.assertTrue(any("无法识别" in str(line) for line in echoes), echoes)
        self.assertTrue(init_wizard._ask_bool(lambda p="": "有", echoes.append, "测试项", default=False))
        self.assertFalse(init_wizard._ask_bool(lambda p="": "n", echoes.append, "测试项", default=True))

    def test_secret_answers_not_stripped(self):
        """密钥类输入只去尾部换行：首尾空白可能是凭据的一部分（与 api2ods 同款）。"""
        self.assertEqual(init_wizard._ask_secret(lambda prompt="": " pw123 \n", "密码"), " pw123 ")
        self.assertEqual(init_wizard._ask_secret(lambda prompt="": "tok ", "Token"), "tok ")

    def test_local_sample_expanduser_error_retries(self):
        """~user 解析不了（本地无该用户 / HOME 未设置）时 expanduser 抛 RuntimeError：
        提示后重试，不能以裸 traceback 终止整个向导。"""
        echoes: list = []
        real_expand = Path.expanduser
        calls = {"n": 0}

        def flaky_expanduser(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Could not determine home directory.")
            return real_expand(self)

        asks = iter(["~notexist/sample.csv", str(self.csv_path)])
        with mock.patch.object(Path, "expanduser", flaky_expanduser):
            headers = init_wizard._read_local_sample(lambda p="": next(asks, ""), echoes.append)
        self.assertEqual(headers, ["Order ID", "Settlement amount", "Created time"])
        self.assertTrue(any("读取失败" in str(line) for line in echoes), echoes)

    def test_chmod_failure_after_write_still_reports_success(self):
        """os.replace 已完成、收尾 chmod 失败（只读挂载等）不能报"写文件失败（原文件未改动）"：
        新配置已在磁盘上，如实报成功 + 警告手工 chmod。"""
        script = self.base_script() + self.common_tail()
        asks = ScriptedAsk(script)
        echoes: list = []
        # os.name 按 posix 走：chmod 收紧分支在 Windows 上本来就不执行
        fake_os = mock.Mock(wraps=init_wizard.os)
        fake_os.name = "posix"
        fake_os.chmod.side_effect = OSError("read-only fs")
        with mock.patch.object(init_wizard, "os", fake_os):
            rc = init_wizard.run_init(
                out_path=str(self.out_path),
                ask=asks,
                ask_secret=asks,
                echo=lambda *a: echoes.append(" ".join(str(x) for x in a)),
            )
        self.assertEqual(rc, 0)
        self.assertTrue(self.out_path.is_file())
        self.assertTrue(any("权限收紧失败" in line for line in echoes), echoes)

    def test_keyboard_interrupt_cleans_tmp_file(self):
        """Ctrl+C 落在写盘途中（fsync）：含明文密钥的临时文件必须清掉，
        向导的"未生成任何文件"才属实（清理原来只接 Exception，中断会留下 .tmp）。"""
        script = self.base_script() + self.common_tail()
        asks = ScriptedAsk(script)
        with mock.patch.object(init_wizard.os, "fsync", side_effect=KeyboardInterrupt):
            rc = init_wizard.run_init(
                out_path=str(self.out_path), ask=asks, ask_secret=asks, echo=lambda *_a, **_k: None
            )
        self.assertEqual(rc, 130)  # Ctrl+C 一律 130（与问答阶段的中断同一约定）
        self.assertFalse(self.out_path.exists())
        self.assertEqual(list(self.out_path.parent.glob(f".{self.out_path.name}.*.tmp")), [])

    def test_tmp_cleanup_failure_is_warned_not_silent(self):
        """清理含明文密钥的临时文件失败（被占用/权限变化）必须醒目提示残留路径：
        否则向导一边报"未生成任何文件"、磁盘上却留着含明文密码/AK-SK 的 .tmp。"""
        script = self.base_script() + self.common_tail()
        asks = ScriptedAsk(script)
        echoes: list = []
        with (
            mock.patch.object(init_wizard.os, "replace", side_effect=OSError("locked")),
            mock.patch.object(Path, "unlink", side_effect=OSError("busy")),
        ):
            rc = init_wizard.run_init(
                out_path=str(self.out_path),
                ask=asks,
                ask_secret=asks,
                echo=lambda *a: echoes.append(" ".join(str(x) for x in a)),
            )
        self.assertEqual(rc, 1)
        joined = "\n".join(echoes)
        self.assertIn("清理临时文件失败", joined)
        # 残留确实存在（unlink 被模拟失败）——提示里给的路径就是它
        self.assertNotEqual(list(self.out_path.parent.glob(f".{self.out_path.name}.*.tmp")), [])

    def test_interrupt_right_after_replace_reports_written(self):
        """os.replace 成功但 Ctrl+C 恰落在 written_path 赋值前（字节码间投递）：
        tmp 已被移走——既不能误报"清理失败/残留密钥"，也不能报"未生成任何文件"。"""
        script = self.base_script() + self.common_tail()
        asks = ScriptedAsk(script)
        echoes: list = []
        real_replace = init_wizard.os.replace

        def replace_then_interrupt(src, dst):
            real_replace(src, dst)
            raise KeyboardInterrupt

        with mock.patch.object(init_wizard.os, "replace", new=replace_then_interrupt):
            rc = init_wizard.run_init(
                out_path=str(self.out_path),
                ask=asks,
                ask_secret=asks,
                echo=lambda *a: echoes.append(" ".join(str(x) for x in a)),
            )
        self.assertEqual(rc, 130)
        self.assertTrue(self.out_path.is_file())
        joined = "\n".join(echoes)
        self.assertIn("已写全", joined)
        self.assertNotIn("未生成任何文件", joined)
        self.assertNotIn("清理临时文件失败", joined)

    def test_broken_stdout_after_write_is_not_reported_as_failure(self):
        """落盘成功后收尾提示写 stdout 失败（`--init | head` 的 BrokenPipeError）不算失败：
        返回 0、不报"文件操作失败"，BrokenPipeError 也不逃出 run_init。"""
        script = self.base_script() + self.common_tail()
        asks = ScriptedAsk(script)
        echoes: list = []

        def broken_after_write(*a):
            text = " ".join(str(x) for x in a)
            if "已生成" in text:
                raise BrokenPipeError("stdout closed")
            echoes.append(text)

        rc = init_wizard.run_init(out_path=str(self.out_path), ask=asks, ask_secret=asks, echo=broken_after_write)
        self.assertEqual(rc, 0)
        self.assertTrue(self.out_path.is_file())
        joined = "\n".join(echoes)
        self.assertNotIn("文件操作失败", joined)
        self.assertNotIn("未生成任何文件", joined)

    def test_write_failure_keeps_existing_job_file(self):
        """写入失败（磁盘满/中断）时已有配置必须原样保留：原来是 O_TRUNC 直接覆盖，
        打开瞬间旧文件就没了，失败后磁盘上只剩 0 字节或半截 JSON。"""
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.write_text('{"job": "old"}', encoding="utf-8")
        script = self.base_script() + self.common_tail()
        asks = ScriptedAsk(script)
        with mock.patch.object(init_wizard.os, "fsync", side_effect=OSError("disk full")):
            rc = init_wizard.run_init(
                out_path=str(self.out_path), ask=asks, ask_secret=asks, echo=lambda *_a, **_k: None
            )
        self.assertEqual(rc, 1)
        self.assertEqual(self.out_path.read_text(encoding="utf-8"), '{"job": "old"}')


class TestWizardRemoteSampleGuard(WizardTestCase):
    """向导拉样本的落地路径也走"必须在 base 之内"的校验：越界名 → 拒绝、不下载。"""

    def _cfgs(self):
        sftp_cfg = {"host": "h", "port": 22, "username": "u", "auth": {"type": "password", "password": "p"}}
        source_cfg = {"root": "/statements", "layout": "flat", "file_regex": "report_(?P<date>\\d{8})\\.csv"}
        return sftp_cfg, source_cfg

    def test_out_of_base_name_is_rejected(self):
        fake = FakeSftp()
        add_file(fake, "/statements/report_20260920.csv", csv_bytes(["Order ID"], [["o1"]]))
        sftp_cfg, source_cfg = self._cfgs()
        echoes: list = []
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            # 模拟包含性校验判定越界：向导必须拒绝、且不触发下载
            with mock.patch.object(init_wizard, "local_path_within", side_effect=FatalSourceError("越界")):
                got = init_wizard._read_remote_sample(lambda p="": p, echoes.append, sftp_cfg, source_cfg)
        self.assertIsNone(got)
        self.assertTrue(any("下载失败" in str(line) for line in echoes), echoes)
        self.assertEqual(fake.downloaded, [], "越界名不应触发下载")

    def test_in_base_name_downloads_and_reads_header(self):
        fake = FakeSftp()
        add_file(fake, "/statements/report_20260920.csv", csv_bytes(["Order ID"], [["o1"]]))
        sftp_cfg, source_cfg = self._cfgs()
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            headers = init_wizard._read_remote_sample(lambda p="": p, lambda *_a, **_k: None, sftp_cfg, source_cfg)
        self.assertEqual(headers, ["Order ID"])
        self.assertEqual(fake.downloaded, ["/statements/report_20260920.csv"])


class TestSlugAndColumns(OfflineTestCase):
    def test_slugify(self):
        self.assertEqual(init_wizard.slugify("Order ID"), "order_id")
        self.assertEqual(init_wizard.slugify("下单 时间"), "")
        self.assertEqual(init_wizard.slugify("1st"), "c_1st")
        self.assertEqual(init_wizard.slugify(""), "")

    def test_build_columns_dedupe(self):
        columns = init_wizard.build_columns(["A", "A", "a"], {0}, {1})
        self.assertEqual([c["name"] for c in columns], ["a", "a_1", "a_2"])
        self.assertEqual(columns[0]["type"], "decimal(19,10)")
        self.assertEqual(columns[1]["type"], "bigint")
        self.assertEqual(columns[2]["type"], "string")

    def test_build_columns_dedupe_against_generated_suffix(self):
        # 补后缀后仍可能与已有列名撞车（amount_1 既是补出来的、也是源表头原生的）：
        # 必须继续找下一个可用名，不能生成两个 amount_1
        columns = init_wizard.build_columns(["Amount", "Amount", "Amount_1"], set(), set())
        self.assertEqual([c["name"] for c in columns], ["amount", "amount_1", "amount_1_1"])

    def test_match_columns_by_number_and_name(self):
        headers = ["Order ID", "Amount", "Currency"]
        self.assertEqual(init_wizard._match_columns(headers, "1, Amount"), {0, 1})
        self.assertEqual(init_wizard._match_columns(headers, "9"), set())

    def test_match_columns_tolerates_non_string_headers(self):
        """表头含 None/数字（空列名等脏数据）时不能抛 AttributeError 打挂向导：
        与 slugify 的 str(header or "") 同口径。"""
        self.assertEqual(init_wizard._match_columns([None, "Order ID", 123], "1, Order ID"), {0, 1})

    def test_collect_sample_headers_tolerates_non_string_headers(self):
        """表头展示行（'读到 N 列：…'）不能因非字符串表头 join 抛 TypeError。"""
        echoes: list = []
        with mock.patch.object(init_wizard, "_read_local_sample", return_value=[None, "Order ID", 123]):
            headers = init_wizard._collect_sample_headers(
                lambda p="": "2",  # ⑤ 表头来源 = 本地 CSV
                lambda *a: echoes.append(" ".join(str(x) for x in a)),
                {},
                {},
            )
        self.assertEqual(headers, [None, "Order ID", 123])
        self.assertTrue(any("读到 3 列" in e for e in echoes), echoes)


if __name__ == "__main__":
    unittest.main()
