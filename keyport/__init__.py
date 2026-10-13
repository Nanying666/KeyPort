"""KeyPort —— 自托管多账号 API 反向代理网关。

将 ZCode (z.ai) 与腾讯 CodeBuddy / WorkBuddy 的账号额度统一转换为
OpenAI 兼容 API（/v1/chat/completions、/v1/responses）与
Anthropic Messages API（/v1/messages），并附带 Web 管理控制台。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
