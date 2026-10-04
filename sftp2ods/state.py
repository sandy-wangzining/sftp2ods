# -*- coding: utf-8 -*-
"""上传台账：记录每个文件写到了哪个表/分区/大小/行数，重跑靠它跳过已上传的。

台账就是下载目录里的 .uploaded.json（与两个结算脚本同款、同字段），
两个工具可以共用一份台账（旧脚本已经写过的日期，迁移后不用重导）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .utils import log

STATE_FILE_NAME = ".uploaded.json"


def load_state(path: Path) -> dict:
    """读台账；文件不存在返回空；内容坏了给出明确报错（不静默丢弃）。"""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        # JSONDecodeError 与 UnicodeDecodeError（文件被截断/编码损坏）都是 ValueError 子类；
        # 只接 JSONDecodeError 会让损坏的台账以裸 traceback 冒出来，而不是这句明确报错
        raise SystemExit(
            f"上传台账读不了：{path}（{exc}）；确认文件损坏可改名或删除，重跑会按本地文件重新上传（先删再填，幂等）"
        )
    if not isinstance(data, dict):
        raise SystemExit(f"上传台账格式不对（顶层应为对象）：{path}")
    return data


def save_state(path: Path, state: dict) -> None:
    """写台账（先写临时文件再原子替换，半截文件不会覆盖好台账）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def record_of(state: dict, keys, size: int, table: str, pt: str, project: str | None = None) -> dict | None:
    """台账里是否存在"与本次一致"的完成记录（keys 任一命中即可，兼容旧台账的键）。

    keys 里放当前键与历史键（如 date_dir 布局的 "20260920/xxx.csv" 与旧的 "xxx.csv"）。
    记录里带了 project 时也要一致：换过目标项目（如 dev → prod，表名相同）时不能拿
    另一个项目里的上传记录跳过——否则会把整段日期静默跳成"没数据"。
    旧脚本的台账没有 project 字段，按兼容处理（不因缺字段拒绝）。
    返回命中的记录 dict（调用方可能需要里面的 md5），没有返回 None。
    """
    for key in keys:
        record = state.get(key)
        if not isinstance(record, dict):
            continue
        if record.get("table") != table or record.get("pt") != pt or record.get("size") != size:
            continue
        recorded_project = record.get("project")
        if recorded_project and project and recorded_project != project:
            continue
        return record
    return None


def md5_of(path: Path) -> str:
    """文件的 md5 十六进制串（流式计算，几十 MB 也只用一次读 IO）；文件不存在/读不了返回空串。"""
    import hashlib

    digest = hashlib.md5()  # noqa: S324 - 这里只做完整性校验（本地文件 vs 台账），不涉及密码学场景
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 256), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def local_ready(target: Path, size: int | None, md5: str = "") -> bool:
    """本地文件已存在且大小与远端一致（不用重新下载）；md5 给定时（台账里有）再校验内容。

    旧台账没有 md5 字段：md5 传空串，退回只比大小（兼容迁移前的记录）。
    size=None（远端没给大小）时只要求文件存在：拿不到基准就没法比，重下也解决不了。
    """
    target = Path(target)
    if not target.is_file():
        return False
    if size is not None and target.stat().st_size != size:
        return False
    if md5:
        return md5_of(target) == md5
    return True


def log_skip(date: str, files: list) -> None:
    """跳过整个日期时打一条（按天粒度，不刷屏）。"""
    log(f"{date}：{len(files)} 个文件已上传过（大小与台账一致），跳过")
