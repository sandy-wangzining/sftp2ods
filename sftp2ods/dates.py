# -*- coding: utf-8 -*-
"""日期与时区：参数解析、缺文件核对、处理范围规划。

本工具的日期模型（与结算文件的现实一致）：
- 远端每个文件都能提取出一个"文件日期"（文件名或日期子目录），它就是该文件数据所属的业务日；
- 写入时 pt = 文件日期，一个日期一个分区（一天多个文件合并进同一个 pt）；
- --bizdate 只处理那一天；--start-date/--end-date 处理区间；
- 缺文件核对：对 [核对区间] 内每一天，远端至少要有匹配文件；缺了只告警 + 跳过缺失日期
  （照常同步已有文件），只有"显式点名单日（--bizdate/环境变量 bizdate）整天无文件"才失败。
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .utils import ConfigError

DEFAULT_TZ = "Asia/Shanghai"
DATE_FMT = "%Y%m%d"
# \A...\Z 而不是 ^...$：$ 会放过结尾的换行（"20260920\n" 静默通过校验）
_DAY_COMPACT_RE = re.compile(r"\A\d{8}\Z", re.ASCII)  # 只认 ASCII 数字：全角数字不该被当成业务日
_DAY_ISO_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_GRACE_RE = re.compile(r"\A(\d{1,2}):(\d{2})\Z")


def load_zone(name: str) -> ZoneInfo:
    """按名称加载时区（如 Asia/Shanghai、UTC、America/New_York）。"""
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # 只认"时区名不认识/不合法"两类：别的异常（如 TypeError）是编程错误，不该伪装成时区问题
        raise ConfigError(
            f"无法识别时区：{name}（如 Asia/Shanghai / UTC / America/New_York；Windows 本机需 pip install tzdata）"
        ) from exc


def parse_day_arg(text: str) -> date:
    """解析日期参数：YYYYMMDD 或 YYYY-MM-DD。

    两条正则白名单而不是 date.fromisoformat：3.11 起 fromisoformat 还认 ISO 周日期
    （"2026-W36-1" 会静默解析成 2026-08-31），同一个 --bizdate 在不同 Python 版本上
    行为不同，写错的日子会静默落到错的文件日期上。
    """
    value = str(text or "").strip()
    if _DAY_COMPACT_RE.match(value):
        try:
            return date(int(value[:4]), int(value[4:6]), int(value[6:]))
        except ValueError as exc:
            raise ConfigError(f"日期不存在：{text!r}（{exc}）")
    if _DAY_ISO_RE.match(value):
        try:
            return date(int(value[:4]), int(value[5:7]), int(value[8:10]))
        except ValueError as exc:
            raise ConfigError(f"日期不存在：{text!r}（{exc}）")
    raise ConfigError(f"日期格式应为 YYYYMMDD 或 YYYY-MM-DD：{text!r}")


def norm_date(text: str, flag: str) -> str:
    """日期参数归一化成 YYYYMMDD（容忍 2026-09-21 / 2026/09/21 写法）；写错直接报错。

    用结构化白名单而不是"删掉所有 - 和 /"：后者会把 `20-2609-21` 这类明显写错的日期
    也归一化成合法值，静默落到错误的 pt 上（同一个参数的 --bizdate 路径是严格正则，
    两套标准）。分隔符只允许"两处都有且一致"或"都没有"。
    """
    value = str(text or "").strip()
    # re.ASCII：\d 默认匹配全角数字、int() 也认——放行的话返回值会是全角字符串
    # （字符串比较里 '２'(U+FF12) > '2'），plan_dates 的过滤会把真实日期整段滤掉、
    # 作业"成功"却一个文件没同步，调度侧看不到报错
    match = re.fullmatch(r"\A(\d{4})([-/]?)(\d{2})\2(\d{2})\Z", value, re.ASCII)
    if not match:
        raise ConfigError(f"{flag} 应为 YYYYMMDD 或 YYYY-MM-DD，实际 {text!r}")
    year, _, month, day = match.groups()
    try:
        moment = datetime(int(year), int(month), int(day))
    except ValueError:
        raise ConfigError(f"{flag} 不是有效日期：{text!r}")
    # 强制按 ASCII 拼回：不给"匹配用的字符集"与"返回值的字符集"留缝
    return f"{moment.year:04d}{moment.month:02d}{moment.day:02d}"


def env_bizdate(strict: bool = True) -> date | None:
    """DataWorks 环境变量 bizdate / SKYNET_BIZDATE（YYYYMMDD）；没设置返回 None。

    设置了却解析不出来时必须报错，**不能**默默回退"处理全部日期"：那会多处理一批
    本不该动的日期（每个都是先删再填），而调度侧完全看不出来。
    同一个错值 --bizdate 会立刻报错，环境变量走静默回退属于两套标准。

    strict=False 只给"只读体检"（--check）用：它不写库，落哪些 pt 只是看一眼。
    """
    # 按"第一个非空白值"取：bizdate=" " 这类空白值不能把后面有效的 SKYNET_BIZDATE 一起
    # 吞掉（原来是 `a or b`，空白串是真值、直接短路）
    raw = ""
    seen = False
    for name in ("bizdate", "SKYNET_BIZDATE"):
        if name in os.environ:
            seen = True
            if os.environ[name].strip():
                raw = os.environ[name]
                break
    text = raw.strip()
    if not text:
        # "变量不存在"与"变量存在但值为空白"是两回事：后者是调度侧传参失误
        # （`export bizdate=$1` 且 $1 为空、`export bizdate=`），当成"未设置"会静默回退
        # 处理全部日期（每个 pt 先删再填），而调度侧完全看不出来——与 --bizdate 同标准报错
        if seen:
            message = (
                "环境变量 bizdate/SKYNET_BIZDATE 已设置但值为空白："
                "按未设置回退会处理全部日期（每个 pt 先删再填），拒绝执行；"
                "请修正调度侧传参，或 unset 该变量"
            )
            if not strict:
                from .utils import log

                log(f"  警告：{message}；只读体检（--check）不写库，按未设置继续")
                return None
            raise ConfigError(message)
        return None
    try:
        return parse_day_arg(text)
    except ConfigError as exc:
        if not strict:
            from .utils import log

            log(
                f"  警告：环境变量 bizdate/SKYNET_BIZDATE 的值不是合法日期：{raw!r}；只读体检（--check）不写库，按未设置继续"
            )
            return None
        raise ConfigError(
            f"环境变量 bizdate/SKYNET_BIZDATE 的值不是合法日期：{raw!r}"
            f"（应为 YYYYMMDD 或 YYYY-MM-DD）；不打算用它请先 unset，或用 --bizdate 显式指定"
        ) from exc


def parse_grace(text: str) -> tuple[int, int]:
    """解析消息里的 grace（"HH:MM"），返回 (小时, 分钟)；空串返回 (0, 0)。"""
    value = str(text or "").strip()
    if not value:
        return (0, 0)
    match = _GRACE_RE.match(value)
    if not match:
        raise ConfigError(f"missing.grace 应是 HH:MM（如 02:30），实际 {text!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ConfigError(f"missing.grace 不是有效时刻：{text!r}")
    return (hour, minute)


def expected_latest(tz: ZoneInfo, grace: str = "", now: datetime | None = None) -> str:
    """预期最新文件日期 = 该时区的昨天；配了 grace 且当前时刻早于它时再往前一天。

    背景：源方每天凌晨才生成 T-1 的文件（如 Clink 是 UTC 02:00 生成前一天的）。
    调度比生成时刻跑得早时，把预期上界后退一天防误报；调度正常传 --bizdate 时
    这个兜底根本不参与（核对的只是那一天的 --bizdate）。
    """
    if now is None:
        moment = datetime.now(tz)
    elif now.tzinfo is None:
        # 朴素 datetime 不能交给 astimezone()——那会按**本机时区**解释（同一份传入值
        # 在不同机器上算出不同日期）；按调用方给的 tz 解释
        moment = now.replace(tzinfo=tz)
    else:
        moment = now.astimezone(tz)
    latest = moment.date() - timedelta(days=1)
    hour, minute = parse_grace(grace)
    if (moment.hour, moment.minute) < (hour, minute):
        latest -= timedelta(days=1)
    return latest.strftime(DATE_FMT)


def find_missing(all_dates, start: str, end: str) -> list[str]:
    """[start, end]（含）逐日核对，返回缺失日期列表（内部断档 + 最新缺失都会命中）。"""
    have, missing = set(all_dates), []
    try:
        day, end_day = datetime.strptime(start, DATE_FMT).date(), datetime.strptime(end, DATE_FMT).date()
    except ValueError as exc:
        # 走库调用方可能绕过 CLI 的 --start-date 校验：给配置错而不是裸 ValueError
        raise ConfigError(f"核对区间必须是 yyyyMMdd：start={start!r}, end={end!r}") from exc
    while day <= end_day:
        key = day.strftime(DATE_FMT)
        if key not in have:
            missing.append(key)
        day += timedelta(days=1)  # 逐日递增，缺哪天记哪天
    return missing


def plan_dates(
    all_dates: list[str],
    bizdate: str = "",
    start: str = "",
    end: str = "",
    expected: str = "",
    check_missing: bool = True,
) -> tuple[list[str], list[str], str, str]:
    """规划 [核对区间]（缺文件核对）与 [处理日期]。

    返回 (missing, dates, range_start, range_end)：
    - 核对区间：--bizdate 时只核对那一天；否则 [max(远端最早, --start-date), --end-date 或预期最新]。
      显式 start 早于远端最早时按远端最早算，避免把"源方本来就没有的更早历史"误报成缺失。
    - 处理日期：--bizdate 单日；否则按 --start-date/--end-date 过滤全部远端日期（都没给 = 全部）。
    - check_missing=False 时不做缺文件核对（missing 恒为空）。
    """
    if not all_dates:
        return [], [], "", ""
    if bizdate:
        range_start = range_end = bizdate
    else:
        range_start = max(min(all_dates), start) if start else min(all_dates)
        if end:
            range_end = end
        elif check_missing and expected:
            range_end = expected
        else:
            range_end = max(all_dates)
    missing = find_missing(all_dates, range_start, range_end) if check_missing and range_start <= range_end else []

    proc_start = bizdate or start
    proc_end = bizdate or end
    dates = [d for d in all_dates if (not proc_start or d >= proc_start) and (not proc_end or d <= proc_end)]
    return missing, dates, range_start, range_end
