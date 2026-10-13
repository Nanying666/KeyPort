"""上游适配层。

- `base`         公共类型：账号凭证、错误分类、稳定指纹派生
- `constants`    上游常量收口（URL / 版本 / 关键字 / 模型名）
- `fingerprint`  每账号设备档案（成套桌面 SKU，一号一台）
- `hostinfo`     宿主机真实指纹采集（诊断用，非入池默认源）
- `headers`      出站请求头构造（messages / billing / chat 三条通道）
- `client`       上游 HTTP 客户端（chat / 模型 / 额度 / 计费 / 领取）
- `claim`        限时套餐领取（preview / claim / 后台轮）
- `install`      官方首启安装序仿真
- `telemetry`    激活遥测上报（官方 event/report 单一事实源）
- `captcha`      阿里云无痕验证参数供给（农场）
"""

from .base import (
    AccountAuth,
    ChatMeta,
    ErrorKind,
    UpstreamError,
    classify_status,
    derive_machine_id,
    derive_session_id,
    is_risk_control,
    new_hex_id,
)

__all__ = [
    "AccountAuth",
    "ChatMeta",
    "ErrorKind",
    "UpstreamError",
    "classify_status",
    "derive_machine_id",
    "derive_session_id",
    "is_risk_control",
    "new_hex_id",
]
