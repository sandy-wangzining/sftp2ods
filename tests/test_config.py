# -*- coding: utf-8 -*-
"""config：读取、占位符、默认值、校验、凭证 profile、目标解析。"""

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

from _helpers import OfflineTestCase, make_args, minimal_job  # noqa: E402

from sftp2ods import config  # noqa: E402


def validated(job: dict) -> dict:
    """normalize + validate（作业已经 render 过的形态）。"""
    job = config.normalize_job(job)
    config.validate_job(job)
    return job


class TestLoadJson(OfflineTestCase):
    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                config.load_json_file(Path(tmp) / "no_such_file_xyz.json", "作业配置文件")

    def test_bom_and_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"job": "x"}).encode("utf-8"))
            self.assertEqual(config.load_json_file(path, "作业")["job"], "x")

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text("{oops", encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                config.load_json_file(path, "作业")
            self.assertIn("不是合法 JSON", str(ctx.exception))

    def test_top_level_must_be_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(SystemExit):
                config.load_json_file(path, "作业")


class TestPlaceholders(OfflineTestCase):
    def test_nested_and_mixed(self):
        ctx = {"secrets": {"k": "S"}, "bizdate": "20260918"}
        value = {"a": "Bearer ${secrets.k}", "b": ["${bizdate}", 1], "c": {"d": "${bizdate}-x"}}
        self.assertEqual(
            config.deep_substitute(value, ctx),
            {"a": "Bearer S", "b": ["20260918", 1], "c": {"d": "20260918-x"}},
        )

    def test_whole_placeholder_keeps_type(self):
        self.assertEqual(config.deep_substitute("${secrets.lst}", {"secrets": {"lst": [1, 2]}}), [1, 2])

    def test_unknown_placeholder_raises_with_hint(self):
        with self.assertRaises(SystemExit) as ctx:
            config.deep_substitute("${secrets.nope}", {"secrets": {}})
        self.assertIn("secrets", str(ctx.exception))

    def test_unclosed_placeholder_raises(self):
        with self.assertRaises(SystemExit):
            config.deep_substitute("${secrets.k", {"secrets": {"k": "v"}})

    def test_unclosed_placeholder_error_does_not_echo_secret(self):
        """报错回显的配置值要先过 redact：密码里带 "${" 这类字符时不能把密码写进日志。"""
        with self.assertRaises(SystemExit) as err:
            config.deep_substitute("p@ss token=abcd1234${", {"secrets": {}})
        self.assertNotIn("abcd1234", str(err.exception))
        self.assertIn("***", str(err.exception))

    def test_credential_field_keeps_literal_placeholder_chars(self):
        """凭据字段是自由文本：密码里的 "${" 只是字符，不该让整份配置加载失败。"""
        value = {"password": "p@ss${word", "passphrase": "a${b}c", "user": "${bizdate}"}
        self.assertEqual(
            config.deep_substitute(value, {"secrets": {}, "bizdate": "20260918"}),
            {"password": "p@ss${word", "passphrase": "a${b}c", "user": "20260918"},
        )

    def test_credential_field_keeps_whole_literal_placeholder(self):
        """整串 ${...} 但键不存在：凭据字段按字面量保留（密码本身可能就是这个字符串）。"""
        self.assertEqual(
            config.deep_substitute({"password": "${secrets.old}"}, {"secrets": {}}),
            {"password": "${secrets.old}"},
        )

    def test_credential_field_still_resolves_real_placeholders(self):
        value = {"password": "${secrets.pw}", "passphrase": "pre-${secrets.pw}"}
        self.assertEqual(
            config.deep_substitute(value, {"secrets": {"pw": "S"}}),
            {"password": "S", "passphrase": "pre-S"},
        )

    def test_non_credential_field_still_rejects_unclosed(self):
        with self.assertRaises(SystemExit):
            config.deep_substitute({"url": "https://x/${bad"}, {"secrets": {}})

    def test_inline_placeholder_null_raises(self):
        """secrets.x 为 null 时内联解析不能静默变 "None"：与键路径同口径报错。

        普通字段与凭据字段都算——"password": "pre-${secrets.ns}" 拼出 "pre-None"
        只会让认证失败，报错却指不到配置上。
        """
        with self.assertRaises(SystemExit) as ctx:
            config.deep_substitute({"root": "/data/${secrets.ns}/daily"}, {"secrets": {"ns": None}})
        self.assertIn("不是标量", str(ctx.exception))
        with self.assertRaises(SystemExit):
            config.deep_substitute({"password": "pre-${secrets.ns}"}, {"secrets": {"ns": None}})
        # 整串占位符解析成 null 同样拒绝（原样进入运行时配置 = 拿着 None 去连目录/主机）
        with self.assertRaises(SystemExit):
            config.deep_substitute("${secrets.ns}", {"secrets": {"ns": None}})

    def test_comment_keys_not_rendered(self):
        value = {"//说明": "${secrets.not_configured} 的写法", "k": "${bizdate}"}
        self.assertEqual(
            config.deep_substitute(value, {"secrets": {}, "bizdate": "20260918"}),
            {"//说明": "${secrets.not_configured} 的写法", "k": "20260918"},
        )

    def test_render_job_inline_secrets(self):
        job = minimal_job()
        job["secrets"] = {"sftp_password": "pw-inline"}
        job["sftp"]["auth"]["password"] = "${secrets.sftp_password}"
        rendered, _config = config.render_job(job, {}, date(2026, 9, 18))
        self.assertEqual(rendered["sftp"]["auth"]["password"], "pw-inline")

    def test_render_job_renders_config_block_without_mutating_input(self):
        job = minimal_job()
        conf = {"secrets": {"ak": "AK1"}, "maxcompute": {"access_key_id": "${secrets.ak}"}}
        rendered, rendered_config = config.render_job(job, conf, date(2026, 9, 18))
        self.assertEqual(rendered["target"]["table"], "ods_demo_di")
        self.assertEqual(rendered_config["maxcompute"]["access_key_id"], "AK1")
        # 不就地改写入参：同一进程里用同一份 config 再跑一个作业时，
        # 不会把上一个作业已替换的密钥/日期串进这一次
        self.assertEqual(conf["maxcompute"]["access_key_id"], "${secrets.ak}")


class TestNormalize(OfflineTestCase):
    def test_defaults(self):
        job = config.normalize_job(minimal_job())
        self.assertEqual(job["sftp"]["port"], 22)
        self.assertEqual(job["sftp"]["retry_times"], 3)
        self.assertEqual(job["source"]["layout"], "flat")
        self.assertEqual(job["parse"]["encoding"], "utf-8-sig")
        self.assertEqual(job["parse"]["delimiter"], "auto")
        self.assertEqual(job["parse"]["on_missing_header"], "error")
        self.assertTrue(job["parse"]["strict_columns"])
        self.assertTrue(job["target"]["allow_empty"])
        self.assertTrue(job["missing"]["check"])
        self.assertEqual(job["missing"]["timezone"], "Asia/Shanghai")

    def test_layout_case_is_normalized(self):
        """布局取值统一小写：validate_job 按 .lower() 校验，运行时也不该按字面量区分大小写。

        不归一化时 `"layout": "DATE_DIR"` 能过校验，但 SftpSource 会按 flat 去列目录
        （结果为空、报"远端目录下没有任何匹配文件"，与真正的配置错指不到一起）。
        """
        job = config.normalize_job(
            minimal_job(
                source={
                    "root": "/d",
                    "layout": "DATE_DIR",
                    "file_regex": "x.csv",
                    "date_dir_regex": "(?P<date>\\d{8})",
                }
            )
        )
        self.assertEqual(job["source"]["layout"], "date_dir")
        config.validate_job(job)  # 归一化后照样通过校验
        # 概要打印也跟着小写口径（否则会显示成"平铺"，与实际行为对不上）
        summary = "\n".join(config.build_job_summary(job))
        self.assertIn("日期子目录", summary)

    def test_block_type_error(self):
        with self.assertRaises(SystemExit):
            config.check_block_types({"sftp": "oops"})

    def test_missing_check_null_is_displayed_as_default_true(self):
        """missing.check 显式 null：normalize 会补默认值 True，概要显示要与校验路径一致（开）。"""
        job = config.normalize_job(minimal_job(missing={"check": None}))
        self.assertIs(job["missing"]["check"], True)
        summary = "\n".join(config.build_job_summary(job))
        self.assertIn("缺文件核对: 开", summary)

    def test_sftp_auth_wrong_type_gives_config_error(self):
        """sftp.auth 写成字符串：dict("password") 会抛无上下文的裸 ValueError，先给中文报错。"""
        job = minimal_job()
        job["sftp"]["auth"] = "password"
        with self.assertRaises(SystemExit) as err:
            config.normalize_job(job)
        self.assertIn("sftp.auth", str(err.exception))


class TestValidate(OfflineTestCase):
    def assert_invalid(self, job: dict, fragment: str):
        with self.assertRaises(SystemExit) as ctx:
            validated(job)
        self.assertIn(fragment, str(ctx.exception))

    def test_valid_minimal(self):
        validated(minimal_job())

    def test_required_blocks(self):
        self.assert_invalid(config.normalize_job({}), "sftp")
        job = minimal_job()
        del job["sftp"]
        self.assert_invalid(job, "sftp")
        job = minimal_job()
        job["sftp"]["host"] = ""
        self.assert_invalid(job, "sftp.host")
        job = minimal_job()
        job["sftp"]["host"] = ["10.0.0.1"]
        self.assert_invalid(job, "sftp.host 必须是字符串")
        job = minimal_job()
        job["sftp"]["username"] = ""
        self.assert_invalid(job, "sftp.username")
        job = minimal_job()
        job["sftp"]["username"] = 42
        self.assert_invalid(job, "sftp.username 必须是字符串")

    def test_sftp_port_and_retry(self):
        job = minimal_job()
        job["sftp"]["port"] = 0
        self.assert_invalid(job, "sftp.port")
        job = minimal_job()
        job["sftp"]["retry_times"] = -1
        self.assert_invalid(job, "sftp.retry_times")

    def test_sftp_zero_timeout_and_retry_are_valid(self):
        """0 是有意义的取值（不重试 / 不等待 / 不限制超时），配置校验必须放行。"""
        job = minimal_job()
        job["sftp"]["retry_times"] = 0
        job["sftp"]["retry_delay"] = 0
        job["sftp"]["connect_timeout"] = 0
        job["sftp"]["io_timeout"] = 0
        validated(job)

    def test_sftp_auth_errors(self):
        job = minimal_job()
        job["sftp"]["auth"] = {"type": "magic"}
        self.assert_invalid(job, "sftp.auth.type")
        job = minimal_job()
        job["sftp"]["auth"] = {"type": "password"}
        self.assert_invalid(job, "sftp.auth.password")
        job = minimal_job()
        job["sftp"]["auth"] = {"type": "key"}
        self.assert_invalid(job, "sftp.auth.key_file")
        job = minimal_job()
        # 容器/数字不能被真值判断放行（会原样传进 paramiko/SFTP 层）
        job["sftp"]["auth"] = {"type": "password", "password": ["not", "a", "string"]}
        self.assert_invalid(job, "sftp.auth.password 必须是字符串")
        job = minimal_job()
        job["sftp"]["auth"] = {"type": "key", "key_file": {"path": "~/.ssh/x"}}
        self.assert_invalid(job, "sftp.auth.key_file 必须是字符串")
        job = minimal_job()
        job["sftp"]["auth"] = {"type": "key", "key_file": "~/.ssh/x", "passphrase": 123}
        self.assert_invalid(job, "sftp.auth.passphrase 必须是字符串")
        job = minimal_job()
        job["sftp"]["auth"] = {"type": "key", "key_file": "~/.ssh/x"}
        validated(job)

    def test_sftp_host_key_enum(self):
        # 不写 = 严格校验；auto_accept = 显式降级；其它值报配置错
        validated(minimal_job())
        job = minimal_job()
        job["sftp"]["host_key"] = "auto_accept"
        validated(job)
        job = minimal_job()
        job["sftp"]["host_key"] = "trust_anything"
        self.assert_invalid(job, "sftp.host_key")

    def test_source_errors(self):
        job = minimal_job()
        job["source"]["root"] = ""
        self.assert_invalid(job, "source.root")
        job = minimal_job()
        # 容器/数字不能被 str() 兜底拼成非空字符串放行（运行期才在 SFTP 层崩）
        job["source"]["root"] = ["/statements", "/settlements"]
        self.assert_invalid(job, "source.root 必须是字符串")
        job = minimal_job()
        job["source"]["root"] = 123
        self.assert_invalid(job, "source.root 必须是字符串")
        job = minimal_job()
        job["source"]["layout"] = "nested"
        self.assert_invalid(job, "source.layout")
        job = minimal_job()
        job["source"]["file_regex"] = "report_(\\d{8})\\.csv"
        self.assert_invalid(job, "(?P<date>")
        job = minimal_job()
        job["source"]["file_regex"] = "report_(?P<date>\\d{8}.csv"
        self.assert_invalid(job, "不是合法正则")
        job = minimal_job()
        job["source"] = {"root": "/d", "layout": "date_dir", "file_regex": "x_(?P<date>\\d{8})\\.csv"}
        self.assert_invalid(job, "date_dir_regex")
        job = minimal_job()
        job["source"] = {
            "root": "/d",
            "layout": "date_dir",
            "file_regex": "x_(?P<date>\\d{8})\\.csv",
            "date_dir_regex": "\\d{8}",
        }
        # date_dir 的目录正则必须带 date 捕获组
        self.assert_invalid(job, "(?P<date>")

    def test_parse_errors_delegated(self):
        job = minimal_job()
        del job["parse"]
        self.assert_invalid(job, "parse")
        job = minimal_job()
        job["parse"]["columns"] = []
        self.assert_invalid(job, "parse.columns")

    def test_target_errors(self):
        job = minimal_job()
        del job["target"]
        self.assert_invalid(job, "target.table")
        job = minimal_job()
        job["target"]["table"] = "bad-name"
        self.assert_invalid(job, "target.table")
        job = minimal_job()
        job["target"]["lifecycle_days"] = True
        self.assert_invalid(job, "lifecycle_days")
        # NaN/Infinity（json.load 默认接受这些字面量）要给带字段名的配置错，而不是裸 ValueError
        for bad in (float("nan"), float("inf"), float("-inf")):
            job = minimal_job()
            job["target"]["lifecycle_days"] = bad
            self.assert_invalid(job, "lifecycle_days")
        job = minimal_job()
        job["target"]["allow_empty"] = "flase"
        self.assert_invalid(job, "allow_empty")

    def test_job_tz_of_rejects_non_object_missing(self):
        """missing 写成字符串时给中文报错，而不是在 .get 处抛裸 AttributeError。"""
        with self.assertRaises(SystemExit) as ctx:
            config.job_tz_of({"missing": "Asia/Shanghai"})
        self.assertIn("missing", str(ctx.exception))

    def test_summary_tolerates_non_object_columns(self):
        """概要打印是比配置校验更早的路径：列元素不是对象时不能崩在 AttributeError 上。"""
        job = minimal_job()
        job["parse"]["columns"] = ["bad", {"type": "string"}]
        text = "\n".join(config.build_job_summary(job))
        self.assertIn("解析", text)

    def test_missing_errors(self):
        job = minimal_job()
        job["missing"] = {"timezone": "Not/AZone"}
        self.assert_invalid(job, "时区")
        job = minimal_job()
        job["missing"] = {"grace": "25:99"}
        self.assert_invalid(job, "grace")

    def test_notify_errors(self):
        job = minimal_job()
        job["notify"] = {"enabled": "maybe"}
        self.assert_invalid(job, "notify.enabled")
        job = minimal_job()
        job["notify"] = {"webhook": 123}
        self.assert_invalid(job, "notify.webhook")

    def test_warning_for_unknown_keys(self):
        job = minimal_job()
        job["sftp"]["hots"] = "typo"
        job["parse"]["colums"] = []
        job["parse"]["columns"][0]["nmae"] = "x"
        config.validate_job(config.normalize_job(job))
        warnings = config.collect_warnings(config.normalize_job(job))
        text = "\n".join(warnings)
        self.assertIn("sftp.hots", text)
        self.assertIn("parse.colums", text)
        self.assertIn("nmae", text)

    def test_warning_skips_comment_keys(self):
        job = minimal_job()
        job["//说明"] = "x"
        job["parse"]["columns"][0]["//注"] = "y"
        warnings = config.collect_warnings(config.normalize_job(job))
        self.assertEqual(warnings, [])

    def test_collect_warnings_non_iterable_columns_do_not_crash(self):
        """columns 写成非数组（5/true 手误）时收集告警不能裸 TypeError——告警收集早于
        validate_job，这里跳过、交给校验阶段报「columns 必须是数组」。"""
        warnings = config.collect_warnings({"parse": {"columns": 5}})
        self.assertIsInstance(warnings, list)

    def test_collect_warnings_sftp_and_auth_type_guard(self):
        """sftp / sftp.auth 不是对象时不能 AttributeError，也不能把字符串拆成逐字符假告警。"""
        warnings = config.collect_warnings({"sftp": "host", "parse": {}})
        text = "\n".join(warnings)
        self.assertNotIn("sftp.h", text)
        self.assertNotIn("sftp.auth.", text)

        warnings = config.collect_warnings({"sftp": {"host": "h", "auth": "password"}, "parse": {}})
        text = "\n".join(warnings)
        self.assertNotIn("sftp.auth.p", text)

    def test_collect_warnings_non_object_blocks_do_not_crash(self):
        """source/target/missing/notify/parse 不是对象时不能按字符告警，也不能 AttributeError。"""
        warnings = config.collect_warnings(
            {
                "source": "root",
                "target": ["table"],
                "missing": "check",
                "notify": 1,
                "parse": [{"columns": []}],
            }
        )
        text = "\n".join(warnings)
        self.assertNotIn("source.r", text)
        self.assertNotIn("target.t", text)
        self.assertNotIn("missing.c", text)

    def test_null_sftp_numbers_get_defaults(self):
        """JSON null / 空串不能跳过默认值，否则 port 等会带着 None 进连接层。"""
        job = minimal_job()
        job["sftp"]["port"] = None
        job["sftp"]["connect_timeout"] = None
        job["sftp"]["io_timeout"] = ""
        job["sftp"]["retry_times"] = None
        job["sftp"]["retry_delay"] = None
        out = validated(job)
        self.assertEqual(out["sftp"]["port"], 22)
        self.assertEqual(out["sftp"]["connect_timeout"], 30)
        self.assertEqual(out["sftp"]["io_timeout"], 600)
        self.assertEqual(out["sftp"]["retry_times"], 3)
        self.assertEqual(out["sftp"]["retry_delay"], 10)


class TestProfiles(OfflineTestCase):
    def test_job_maxcompute(self):
        job = minimal_job()
        job["maxcompute"] = {"project": "p1", "access_key_id": "a", "access_key_secret": "b"}
        meta = config.get_mc_profile_meta({}, job, make_args())
        self.assertEqual(meta["project"], "p1")

    def test_job_profiles_named(self):
        job = minimal_job()
        job["profiles"] = {"prod": {"project": "p2", "access_key_id": "a", "access_key_secret": "b"}}
        args = make_args(mc_profile="prod")
        self.assertEqual(config.get_mc_profile_meta({}, job, args)["project"], "p2")

    def test_config_file_fallback(self):
        job = minimal_job()
        del job["maxcompute"]
        conf = {"maxcompute": {"project": "pc", "access_key_id": "a", "access_key_secret": "b"}}
        self.assertEqual(config.get_mc_profile_meta(conf, job, make_args())["project"], "pc")

    def test_missing_profile_raises(self):
        job = minimal_job()
        with self.assertRaises(SystemExit):
            config.get_mc_profile_meta({}, job, make_args(mc_profile="prod"))

    def test_profile_not_object(self):
        job = minimal_job()
        job["profiles"] = {"prod": "oops"}
        with self.assertRaises(SystemExit):
            config.get_mc_profile_meta({}, job, make_args(mc_profile="prod"))

    def test_non_mapping_source_clean_error_in_download_dir(self):
        """source 写成字符串时 resolve_download_dir 给中文配置错，不是裸 AttributeError。"""
        job = minimal_job()
        job["source"] = "./data"
        with self.assertRaises(SystemExit) as ctx:
            config.resolve_download_dir(job, Path("x.json"))
        self.assertIn("source", str(ctx.exception))

    def test_non_mapping_target_gives_clean_error(self):
        """target 写成字符串/数组（漏了大括号）时给配置错，不是裸 AttributeError。"""
        job = minimal_job()
        job["target"] = "ods.tbl"
        job.pop("maxcompute", None)  # 没有回退来源时，非对象 target 必须给配置错
        # resolve_target 是非对象 target 的第一道取值点：给中文配置错而不是裸 AttributeError
        with self.assertRaises(SystemExit) as ctx:
            config.resolve_target(job, {}, make_args())
        self.assertIn("目标项目", str(ctx.exception))

    def test_profiles_comment_keys_are_ignored(self):
        """profiles 里按约定写 "//" 注释键不能让整个作业报"必须是对象"。"""
        job = minimal_job()
        job["profiles"] = {"//": "共享凭证统一放 --config", "prod": {"project": "p2"}}
        config.validate_job(config.normalize_job(job))  # 不抛


class TestResolveTargetAndDirs(OfflineTestCase):
    def test_target_project_from_profile(self):
        job = minimal_job()
        del job["maxcompute"]
        job["target"] = {"table": "ods_x_di"}
        conf = {"maxcompute": {"project": "pp", "access_key_id": "a", "access_key_secret": "b"}}
        self.assertEqual(config.resolve_target(job, conf, make_args()), ("pp", "ods_x_di"))

    def test_target_project_missing(self):
        job = minimal_job()
        job["maxcompute"] = {}
        job["target"] = {"table": "ods_x_di"}
        with self.assertRaises(SystemExit):
            config.resolve_target(job, {}, make_args())

    def test_safe_job_name_keeps_legal_lowercase_name(self):
        """只含合法字符且全小写的名字（含首尾下划线）原样保留：否则下载目录被改名、旧文件不复用。"""
        self.assertEqual(config.safe_job_name({"job": "recon_"}, Path("x.json")), "recon_")

    def test_safe_job_name_keeps_caseless_name(self):
        """不含大小写字符的名字（纯数字/纯下划线）也原样保留：islower() 对它们返回 False。"""
        self.assertEqual(config.safe_job_name({"job": "20240101"}, Path("x.json")), "20240101")
        self.assertEqual(config.safe_job_name({"job": "___"}, Path("x.json")), "___")

    def test_safe_job_name_case_folds_to_distinct_dirs(self):
        """含大写字母的名字补哈希：大小写不敏感文件系统上 Recon/recon 不能共用一个下载目录。"""
        upper = config.safe_job_name({"job": "Recon"}, Path("x.json"))
        lower = config.safe_job_name({"job": "recon"}, Path("x.json"))
        self.assertNotEqual(upper, lower)
        self.assertEqual(lower, "recon")

    def test_download_dir_default(self):
        job = minimal_job()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs" / "demo.json"
            self.assertEqual(config.resolve_download_dir(job, path), path.parent / "download" / "demo")
            # 作业名里的不安全字符会被过滤，并补名字哈希（防"对账A"与"A"这类归一后撞名
            # 的作业共用下载目录互串文件）
            job["job"] = "a/b c"
            filtered = config.resolve_download_dir(job, path)
            self.assertTrue(filtered.name.startswith("a_b_c-"), filtered)
            job["job"] = "对账A"
            dirty = config.resolve_download_dir(job, path)
            job["job"] = "A"
            plain = config.resolve_download_dir(job, path)
            self.assertNotEqual(dirty, plain, "归一后同名的两个作业不能共用下载目录")
            # 含大写字母的名字也补哈希：大小写不敏感文件系统上 "A" 与 "a" 会同目录
            self.assertTrue(plain.name.startswith("A-"), plain)

    def test_download_dir_relative_and_absolute(self):
        job = minimal_job()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs" / "demo.json"
            job["source"]["download_dir"] = "files"
            self.assertEqual(config.resolve_download_dir(job, path), path.parent / "files")
            abs_dir = Path(tmp) / "abs" / "files"
            job["source"]["download_dir"] = str(abs_dir)
            self.assertEqual(config.resolve_download_dir(job, path), abs_dir)

    def test_download_dir_tilde_expanded(self):
        job = minimal_job()
        job["source"]["download_dir"] = "~/sftp2ods-data"
        with tempfile.TemporaryDirectory() as tmp:
            # 把 "~" 的展开固定到临时目录：不依赖跑测试的机器上真实的用户主目录
            # （Path.expanduser 走 os.environ 的 HOME/USERPROFILE，不是 pathlib.Path.home）
            home = Path(tmp)
            with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
                self.assertEqual(config.resolve_download_dir(job, home / "jobs" / "demo.json"), home / "sftp2ods-data")

    def test_summary_lines(self):
        job = validated(minimal_job())
        text = "\n".join(config.build_job_summary(job))
        self.assertIn("sftp.example.com", text)
        self.assertIn("缺文件核对", text)


if __name__ == "__main__":
    unittest.main()
