# -*- coding: utf-8 -*-
"""init_wizard：脚本化问答生成配置（不连 SFTP、不连数仓）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import OfflineTestCase, csv_bytes  # noqa: E402

from sftp2ods import (
    config,  # noqa: E402
    init_wizard,  # noqa: E402
)


class ScriptedAsk:
    """按"提示词片段"顺序回答；顺序即问答顺序，匹配到的规则会被消费。"""

    def __init__(self, script):
        self.script = list(script)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        for index, (fragment, value) in enumerate(self.script):
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
            ("缺文件检查", "y"),
            ("时区", "UTC"),
            ("几点前跑", "02:30"),
            ("webhook", "https://open.feishu.cn/open-apis/bot/v2/hook/zzz"),
            ("项目名", "sitindw"),
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
        job = config.normalize_job(config.render_job(raw, {}, date(2026, 9, 23)))
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
        self.assertEqual(job["maxcompute"]["project"], "sitindw")

    def test_no_sample_placeholder(self):
        script = self.base_script(sample_choice="3")
        # 占位表头名 + 没有 skip 问题，直接接尾部规则
        tail = [
            ("第一列表头", "Order ID"),
            ("合计行", "n"),
            ("缺文件检查", "n"),
            ("webhook", ""),
            ("项目名", "sitindw"),
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

    def test_out_path_is_directory(self):
        with self.assertRaises(SystemExit):
            self.run_wizard(self.base_script() + self.common_tail(), out_path=self.tmp)


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
                ("缺文件检查", "n"),
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

    def test_match_columns_by_number_and_name(self):
        headers = ["Order ID", "Amount", "Currency"]
        self.assertEqual(init_wizard._match_columns(headers, "1, Amount"), {0, 1})
        self.assertEqual(init_wizard._match_columns(headers, "9"), set())


if __name__ == "__main__":
    unittest.main()
