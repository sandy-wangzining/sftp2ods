# -*- coding: utf-8 -*-
"""state（台账）与 notify（飞书告警）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 只在路径缺失时追加（不插到最前面）：避免把仓库根/tests 目录置于标准库与第三方库
# 之前遮蔽同名模块；按 README 在仓库根运行（或 CI 里 pip install -e .）时，
# 本地包本来就在搜索路径最前（python -m 会把当前目录放在 sys.path[0]）
for _path in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

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

    def test_save_state_uses_unique_tmp_name(self):
        """两个 writer 不能共用固定的 .tmp，临时名要带 pid/uuid。"""
        path = self.tmp / "state.json"
        seen = []
        real_replace = os.replace

        def capture(src, dst):
            seen.append(Path(src).name)
            real_replace(src, dst)

        with mock.patch.object(state_mod.os, "replace", capture):
            state_mod.save_state(path, {"a": 1})
            state_mod.save_state(path, {"b": 2})
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])
        for name in seen:
            self.assertTrue(name.startswith("state.json."), name)
            self.assertTrue(name.endswith(".tmp"), name)
            self.assertNotEqual(name, "state.json.tmp")

    def test_save_creates_parent_dir(self):
        path = self.tmp / "deep" / "dir" / ".uploaded.json"
        state_mod.save_state(path, {"a": {"table": "t", "pt": "1", "size": 1}})
        self.assertTrue(path.is_file())

    def test_corrupt(self):
        path = self.tmp / "state.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(SystemExit):
            state_mod.load_state(path)

    def test_corrupt_encoding_gives_clean_error(self):
        """台账含非法 UTF-8 字节（被截断/编码损坏）时也要走"明确报错"，不是裸 UnicodeDecodeError。"""
        path = self.tmp / "state.json"
        path.write_bytes(b"\xff\xfe{}{}")
        with self.assertRaises(SystemExit) as ctx:
            state_mod.load_state(path)
        self.assertIn("台账", str(ctx.exception))

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

    def test_record_of_size_unknown_matches_like_local_ready(self):
        """远端没给大小（size=None）时与 local_ready 同口径：其余字段一致即命中。"""
        state = {"a.csv": {"table": "t", "pt": "20260920", "size": 3, "rows": 1}}
        self.assertTrue(state_mod.record_of(state, ("a.csv",), None, "t", "20260920"))
        # 旧台账把 size 写成字符串也要能归一化命中
        legacy = {"a.csv": {"table": "t", "pt": "20260920", "size": "3", "rows": 1}}
        self.assertTrue(state_mod.record_of(legacy, ("a.csv",), 3, "t", "20260920"))

    def test_record_of_project_guard(self):
        """换过目标项目（表名相同）时不能拿另一个项目的上传记录跳过。"""
        state = {"a.csv": {"project": "prod", "table": "t", "pt": "20260920", "size": 3, "rows": 1}}
        self.assertTrue(state_mod.record_of(state, ("a.csv",), 3, "t", "20260920", project="prod"))
        self.assertFalse(state_mod.record_of(state, ("a.csv",), 3, "t", "20260920", project="dev"))
        # 旧脚本台账没有 project 字段：兼容放行
        legacy = {"a.csv": {"table": "t", "pt": "20260920", "size": 3, "rows": 1}}
        self.assertTrue(state_mod.record_of(legacy, ("a.csv",), 3, "t", "20260920", project="dev"))

    def test_record_with_project_not_accepted_when_caller_omits_project(self):
        """本次没传 project 时，带 project 的旧记录不能当成"已上传"跳过
        （迁移后某条调用路径没带 project，会把整段日期静默跳成"没数据"）。"""
        state = {"a.csv": {"project": "dev", "table": "t", "pt": "20260920", "size": 3, "rows": 1}}
        self.assertFalse(state_mod.record_of(state, ("a.csv",), 3, "t", "20260920"))
        self.assertTrue(state_mod.record_of(state, ("a.csv",), 3, "t", "20260920", project="dev"))

    def test_local_ready(self):
        path = self.tmp / "f.csv"
        path.write_bytes(b"123")
        self.assertTrue(state_mod.local_ready(path, 3))

    def test_empty_ledger_treated_as_missing(self):
        """0 字节台账（崩溃丢数据的典型形态）按"没有台账"继续：台账只是派生数据，
        一次崩溃不该让后续每次运行都以"台账读不了"硬失败；非空但损坏的照旧明确报错。"""
        path = self.tmp / "state.json"
        path.write_text("", encoding="utf-8")
        self.assertEqual(state_mod.load_state(path), {})
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(SystemExit):
            state_mod.load_state(path)
        self.assertFalse(state_mod.local_ready(path, 4))
        self.assertFalse(state_mod.local_ready(self.tmp / "nope", 0))

    def test_local_ready_with_md5(self):
        path = self.tmp / "f.csv"
        path.write_bytes(b"123")
        good = state_mod.md5_of(path)
        self.assertTrue(state_mod.local_ready(path, 3, md5=good))
        # 内容变了但大小没变：md5 兜住（旧逻辑只比大小会静默放过）
        path.write_bytes(b"456")
        self.assertFalse(state_mod.local_ready(path, 3, md5=good))
        # 旧台账无 md5（空串）：退回只比大小，兼容迁移前记录
        self.assertTrue(state_mod.local_ready(path, 3, md5=""))

    def test_md5_of(self):
        path = self.tmp / "f.csv"
        path.write_bytes(b"123")
        self.assertEqual(len(state_mod.md5_of(path)), 32)
        self.assertEqual(state_mod.md5_of(self.tmp / "nope"), "")

    def test_save_state_merges_concurrent_records(self):
        """后一次 save 不能整文件覆盖掉磁盘上另一进程刚写入的键。"""
        path = self.tmp / "state.json"
        state_mod.save_state(path, {"a.csv": {"table": "t", "pt": "1", "size": 1}})
        state_mod.save_state(path, {"b.csv": {"table": "t", "pt": "2", "size": 2}})
        data = state_mod.load_state(path)
        self.assertIn("a.csv", data)
        self.assertIn("b.csv", data)

    def test_save_state_merges_disk_state_read_after_lock(self):
        """合并必须在**拿到锁之后**重读磁盘：锁外已被别的进程改过的台账不能被整文件覆盖。

        顺序调用两次 save_state 测不出这一点；这里用"进锁瞬间磁盘上已经变了"
        模拟另一进程刚写完的那一刻。
        """
        path = self.tmp / "state.json"
        state_mod.save_state(path, {"a.csv": {"pt": "1"}})

        from contextlib import contextmanager

        @contextmanager
        def fake_lock(_lock_path):
            # 模拟另一进程在本进程拿锁前刚写完（a.csv 已被它的整表替换掉）
            state_mod._write_state_unlocked(path, {"b.csv": {"pt": "2"}})
            yield

        with mock.patch.object(state_mod, "interprocess_lock", fake_lock):
            state_mod.save_state(path, {"c.csv": {"pt": "3"}})
        data = state_mod.load_state(path)
        self.assertEqual(set(data), {"b.csv", "c.csv"})

    def test_save_state_uses_interprocess_lock(self):
        path = self.tmp / "state.json"
        seen = []

        from contextlib import contextmanager

        @contextmanager
        def fake_lock(lock_path):
            seen.append(str(lock_path))
            yield

        with mock.patch.object(state_mod, "interprocess_lock", fake_lock):
            state_mod.save_state(path, {"a": 1})
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].endswith(".lock"))


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

    def post(self, url, **kwargs):
        # 用 **kwargs 而不是把形参命名成 json：那个名字会遮蔽模块级 import json，
        # 方法体/后续维护里再用 json 模块就会踩坑；调用方仍按 requests 的习惯传 json=...
        self.calls.append({"url": url, "json": kwargs.get("json"), "timeout": kwargs.get("timeout")})
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
        # 告警是同步调用：漏传 timeout 会把主流程无限期卡住（requests 默认没有超时）
        self.assertEqual(fake.calls[0]["timeout"], 15)
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

    def test_non_object_json_response_is_failure_not_crash(self):
        """响应是 JSON 数组/字符串等非对象（网关错误页等）时按失败处理：
        不能抛 AttributeError 打断主流程（告警失败不影响业务是函数的约定）。"""
        for payload in ([], "oops", 3, True):
            fake = FakeRequests(response=FakeResponse(status_code=200, payload=payload))
            with mock.patch.object(notify_mod, "requests", fake):
                self.assertFalse(notify_mod.notify("https://hook", "t", ["x"]))

    def test_missing_code_is_failure(self):
        """缺 code/StatusCode 的 200 响应不能算「已发送」：webhook 误填成其它接口
        （回 {"msg": "ok"} 这类）时会静默失效；仅空 {} 保留按 HTTP 200 判定的宽容。"""
        fake = FakeRequests(response=FakeResponse(status_code=200, payload={"msg": "ok"}))
        with mock.patch.object(notify_mod, "requests", fake):
            self.assertFalse(notify_mod.notify("https://hook", "t", ["x"]))
        fake = FakeRequests(response=FakeResponse(status_code=200, payload={}))
        with mock.patch.object(notify_mod, "requests", fake):
            self.assertTrue(notify_mod.notify("https://hook", "t", ["x"]))

    def test_failure_log_redacts_webhook(self):
        """发送失败时 requests 的异常消息里带完整 URL/路径，hook id 是凭证，不能明文进日志。"""
        hook_id = "9f8e7d6c-5b4a-3210-fedc-ba9876543210"
        hook = f"https://open.feishu.cn/open-apis/bot/v2/hook/{hook_id}"
        fake = FakeRequests(exc=OSError(f"Max retries exceeded with url: /open-apis/bot/v2/hook/{hook_id}"))
        logged = []
        with mock.patch.object(notify_mod, "requests", fake):
            with mock.patch.object(notify_mod, "log", logged.append):
                self.assertFalse(notify_mod.notify(hook, "t", ["x"]))
        joined = "\n".join(str(line) for line in logged)
        self.assertNotIn(hook_id, joined)
        self.assertIn("/hook/***", joined)


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
