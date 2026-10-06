# -*- coding: utf-8 -*-
"""飞书告警：缺文件 / 远端目录为空 / 显式业务日无文件 / 源文件新增列时发群卡片。

与两个结算脚本同款卡片；request 类失败不抛出（告警本身不该把主流程的报错盖掉），
webhook 是凭证：日志里不出现它，抛出/回显的文本由调用方过 collect_secret_values+redact_secrets。
"""

from __future__ import annotations

from .utils import log, redact

try:
    import requests
except ImportError:  # pragma: no cover - 未安装时告警降级为一条日志
    requests = None


def notify(
    webhook: str, title: str, lines: list[str], footer: str = "", enabled: bool = True, timeout: int = 15
) -> bool:
    """发飞书群卡片（interactive）；成功返回 True。

    - webhook 未配置 / enabled=False / requests 缺失 → 静默跳过并返回 False；
    - 发送失败只记一条日志（不抛）：告警失败不该改变任务本身的退出码。
    """
    if not enabled or not webhook:
        return False
    if requests is None:
        log("  警告：缺少 requests，飞书通知跳过（pip install requests）")
        return False
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "red", "title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(str(x) for x in lines)}}],
        },
    }
    if footer:
        card["card"]["elements"].append({"tag": "hr"})
        card["card"]["elements"].append({"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]})
    try:
        resp = requests.post(webhook, json=card, timeout=timeout)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - 告警失败不影响主流程
        # requests 的异常消息里带完整 URL，末段 hook id 就是凭证：必须脱敏后再进日志
        log(f"  警告：飞书通知发送失败：{type(exc).__name__}: {redact(str(exc))}")
        return False
    # isinstance 判断不能省：非对象响应（JSON 数组/字符串/null，网关错误页等）没有 .get，
    # 直接调用会抛 AttributeError 打断主流程（告警失败不影响业务是函数的约定）
    code = data.get("code", data.get("StatusCode", None)) if isinstance(data, dict) else None
    # `False == 0`、`0.0 == 0` 都是真：布尔 false / 浮点 0 的"失败"响应不能当成成功码
    if resp.status_code == 200 and type(code) is int and code == 0:
        log("飞书通知已发送")
        return True
    if resp.status_code == 200 and isinstance(data, dict) and not data:
        # 少数转发网关成功时只回 {}：保留按 HTTP 200 判定的宽容，但把依据说清楚
        log("飞书通知已发送（响应为空，按 HTTP 200 判定）")
        return True
    # 有 JSON 但没有 code/StatusCode（如误填成其它接口的地址）：不能当成功——
    # 告警通道静默失效比报错更危险
    log(f"  警告：飞书通知发送失败：HTTP {resp.status_code} {redact(str(data)[:200])}")
    return False
