"""API Key 的展示与校验小工具（**不做加密**）。

存储策略（按用户要求）：
  Key 以**明文**保存在本机 config.json 里，不做加密 —— 换来的是
  配置可读、可手工改、可回滚到旧版本；代价是这个文件本身就是凭据，
  因此：
    - 界面上默认打码显示，只有点「显示」才展开；
    - 任何提示语 / 日志只输出脱敏后的形式（见 mask）；
    - 分发与备份时不要带上 config.json（打包脚本已排除）。

本模块只负责「怎么显示」和「像不像一个 Key」，不碰存储格式。
"""
_MASK_BULLET = "•"


def mask(text, head=3, tail=4):
    """脱敏展示：保留开头少量字符与末 4 位，中间打码。

    例：``sk-EXAMPLE...abcd`` → ``sk-••••••••abcd``
    太短的串整体打码，避免"脱敏后反而把内容暴露全了"。
    """
    s = (text or "").strip()
    if not s:
        return "（未设置）"
    if len(s) <= head + tail:
        return _MASK_BULLET * len(s)
    return f"{s[:head]}{_MASK_BULLET * 8}{s[-tail:]}"


def looks_like_key(text):
    """粗略判断像不像一个 API Key —— **只用于提醒，不作为硬校验**。

    只看「够长 + 不含空白」。不强求 sk- 前缀：很多兼容 OpenAI 协议的服务
    （自建网关、企业代理）Key 并不以 sk- 开头，硬卡前缀会挡住正常用户。
    """
    s = (text or "").strip()
    if len(s) < 16:
        return False
    return not any(ch.isspace() for ch in s)


def is_http_url(text):
    """API 地址是否像个合法 URL（只做基本形态校验）。"""
    s = (text or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")
