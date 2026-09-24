# -*- coding: utf-8 -*-
"""config：读取、占位符、默认值、校验、凭证 profile、目标解析。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import OfflineTestCase, make_args, minimal_job  # noqa: E402

from sftp2ods import config  # noqa: E402


def validated(job: dict) -> dict:
    """normalize + validate（作业已经 render 过的形态）。"""
    job = config.normalize_job(job)
    config.validate_job(job)
    return job


class TestLoadJson(OfflineTestCase):
    def test_missing_file(self):
        with self.assertRaises(SystemExit):
            config.load_json_file(Path("no_such_file_xyz.json"), "作业配置文件")

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
        rendered = config.render_job(job, {}, date(2026, 9, 18))
        self.assertEqual(rendered["sftp"]["auth"]["password"], "pw-inline")

    def test_render_job_renders_config_block(self):
        job = minimal_job()
        rendered = config.render_job(
            job, {"secrets": {"ak": "AK1"}, "maxcompute": {"access_key_id": "${secrets.ak}"}}, date(2026, 9, 18)
        )
        self.assertEqual(rendered["target"]["table"], "ods_demo_di")
        del rendered


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

    def test_block_type_error(self):
        with self.assertRaises(SystemExit):
            config.check_block_types({"sftp": "oops"})


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
        job["sftp"]["username"] = ""
        self.assert_invalid(job, "sftp.username")

    def test_sftp_port_and_retry(self):
        job = minimal_job()
        job["sftp"]["port"] = 0
        self.assert_invalid(job, "sftp.port")
        job = minimal_job()
        job["sftp"]["retry_times"] = -1
        self.assert_invalid(job, "sftp.retry_times")

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
        job["sftp"]["auth"] = {"type": "key", "key_file": "~/.ssh/x"}
        validated(job)

    def test_source_errors(self):
        job = minimal_job()
        job["source"]["root"] = ""
        self.assert_invalid(job, "source.root")
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
        job = minimal_job()
        job["target"]["allow_empty"] = "flase"
        self.assert_invalid(job, "allow_empty")

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

    def test_download_dir_default(self):
        job = minimal_job()
        path = Path("/tmp/jobs/demo.json")
        self.assertEqual(config.resolve_download_dir(job, path), Path("/tmp/jobs/download/demo"))
        # 作业名里的不安全字符会被过滤
        job["job"] = "a/b c"
        self.assertEqual(config.resolve_download_dir(job, path), Path("/tmp/jobs/download/a_b_c"))

    def test_download_dir_relative_and_absolute(self):
        job = minimal_job()
        job["source"]["download_dir"] = "files"
        path = Path("/tmp/jobs/demo.json")
        self.assertEqual(config.resolve_download_dir(job, path), Path("/tmp/jobs/files"))
        job["source"]["download_dir"] = "/abs/files"
        self.assertEqual(config.resolve_download_dir(job, path), Path("/abs/files"))

    def test_download_dir_tilde_expanded(self):
        job = minimal_job()
        job["source"]["download_dir"] = "~/sftp2ods-data"
        path = Path("/tmp/jobs/demo.json")
        self.assertEqual(config.resolve_download_dir(job, path), Path.home() / "sftp2ods-data")

    def test_summary_lines(self):
        job = validated(minimal_job())
        text = "\n".join(config.build_job_summary(job))
        self.assertIn("sftp.example.com", text)
        self.assertIn("缺文件检查", text)


if __name__ == "__main__":
    unittest.main()
