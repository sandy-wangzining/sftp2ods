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
    except (OSError, json.JSONDecodeError) as exc:
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


def record_of(state: dict, keys, size: int, table: str, pt: str, project: str | None = None) -> bool:
    """台账里是否存在"与本次一致"的完成记录（keys 任一命中即可，兼容旧台账的键）。

    keys 里放当前键与历史键（如 date_dir 布局的 "20260920/xxx.csv" 与旧的 "xxx.csv"）。
    记录里带了 project 时也要一致：换过目标项目（如 dev → prod，表名相同）时不能拿
    另一个项目里的上传记录跳过——否则会把整段日期静默跳成"没数据"。
    旧脚本的台账没有 project 字段，按兼容处理（不因缺字段拒绝）。
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
        return True
    return False


def local_ready(target: Path, size: int) -> bool:
    """本地文件已存在且大小与远端一致（不用重新下载）。"""
    target = Path(target)
    return target.is_file() and target.stat().st_size == size


def log_skip(date: str, files: list) -> None:
    """跳过整个日期时打一条（按天粒度，不刷屏）。"""
    log(f"{date}：{len(files)} 个文件已上传过（大小与台账一致），跳过")
