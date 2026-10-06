import json
import logging
import re
import time
from config import load_config

# 复用主程序的 logger（写入 polish.log，并可在控制台实时输出）
log = logging.getLogger("polish")


def _active_system_prompt(cfg):
    """解析当前生效的提示词内容作为 system 消息。

    优先用 cfg['prompts'] 中 active_prompt_id 对应项的 content；
    找不到或无 prompts 列表时，安全回落到旧字段 system_prompt（向后兼容）。
    """
    prompts = cfg.get("prompts")
    aid = cfg.get("active_prompt_id")
    if prompts and aid:
        for p in prompts:
            if p.get("id") == aid:
                return p.get("content") or cfg.get("system_prompt", "")
    return cfg.get("system_prompt", "")


def build_messages(user_text, context, cfg, mode="polish"):
    """构造发送给大模型的 messages。

    mode="polish"：润色用户草稿（含上下文）。
    mode="reply"：输入框为空时，仅依据聊天上下文起草回复。
    """
    n = int(cfg.get("versions", 3))
    extra = cfg.get("extra_instruction", "")
    sys_prompt = _active_system_prompt(cfg)

    if mode == "reply":
        ctx = context.strip()
        user_msg = (
            f"根据下面的对话上下文，替我起草 {n} 条可以直接发送的专业回复。\n"
            f"要求：贴合对方最近发言、事实准确、语气得体简洁、"
            f"版本之间风格有差异（如：稳妥推荐版 / 简短版 / 正式版 / 承接对方话术版）。\n"
            f"{extra}\n"
            f"只返回一个 JSON 数组，元素是字符串，例如 [\"回复1\",\"回复2\",\"回复3\"]，"
            f"不要任何额外解释或代码围栏。\n"
            f"\n【对话上下文】\n{ctx}"
        )
        return [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ]

    ctx_block = ""
    if context and context.strip():
        ctx_block = (
            "\n\n【对话上下文：对方最近发言，仅供参考，不要编造事实】\n"
            + context.strip()
        )
    user_msg = (
        f"请把下面我要发送的内容润色成 {n} 个可直接发送的版本。\n"
        f"要求：保持原意与事实；语气专业得体、简洁；版本之间风格有差异"
        f"（如：稳妥推荐版 / 简短版 / 正式版 / 承接对方话术版）。\n"
        f"{extra}\n"
        f"只返回一个 JSON 数组，元素是字符串，例如 [\"版本1\",\"版本2\",\"版本3\"]，"
        f"不要任何额外解释或代码围栏。\n"
        f"\n【我的草稿】\n{user_text.strip()}"
        f"{ctx_block}"
    )
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]


def _parse_versions(content, fallback_n=3):
    """从模型回复里尽量解析出多个版本字符串。"""
    if not content:
        return []
    text = content.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            out = [str(x).strip() for x in data if str(x).strip()]
            if out:
                return out
    except Exception:
        pass
    # 降级：按版本标记 / 换行拆分
    parts = [
        p.strip()
        for p in re.split(r"版本\s*\d+\s*[：:]|[0-9]+\s*[.、]", text)
        if p.strip() and len(p.strip()) > 1
    ]
    if parts:
        return parts[:fallback_n]
    return [content.strip()] if content.strip() else []


_RETRY_WAIT = 0.8      # 连接类错误的重试间隔（秒）


def _post_with_retry(url, headers, payload, timeout, attempts=2):
    """POST 大模型接口，连接类错误自动重试。

    为什么要重试（实测）：在**开着代理 / 走公司网络**的机器上，TLS 握手会被中间设备
    偶发掐断，表现为 `SSLEOFError: UNEXPECTED_EOF_WHILE_READING` 或连接重置。
    `requests` 默认只对部分 HTTP 状态码重试，**对 SSL / 连接错误直接抛出**，
    于是一次网络抖动就白跑一趟；这类错误重试一次往往就通了。

    只重试连接类错误（含 SSLError，它是 ConnectionError 的子类）与超时；
    HTTP 状态码错误（401 等）原样返回给上层判断，不在这里重试。
    """
    import requests  # 惰性导入，便于无网络环境做单元测试

    last = None
    for i in range(max(1, attempts)):
        try:
            return requests.post(url, headers=headers, json=payload, timeout=timeout)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last = e
            if i + 1 < max(1, attempts):
                log.warning("请求大模型失败（%s），%.1fs 后重试一次：%s",
                            type(e).__name__, _RETRY_WAIT,
                            str(e).splitlines()[0][:160])
                time.sleep(_RETRY_WAIT)
    raise last


def polish(user_text, context="", cfg=None, timeout=30):
    """调用 DeepSeek（OpenAI 兼容）生成多个润色版本。"""
    import requests  # 惰性导入，便于无网络环境做单元测试

    cfg = cfg or load_config()
    if not cfg.get("api_key"):
        raise RuntimeError(
            "未配置 API Key：请在 config.json 填 api_key，或设置环境变量 AI_API_KEY"
        )
    mode = "reply" if not (user_text or "").strip() else "polish"
    messages = build_messages(user_text, context, cfg, mode=mode)
    url = f"{cfg['api_base'].rstrip('/')}/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1500,
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    # 是否记录完整提示词 / 响应原文（config.json 的 log_prompt）
    verbose = bool(cfg.get("log_prompt", True))

    # ---- 请求日志：完整提示词 ----
    log.info("========== 请求大模型 ==========")
    log.info("模型=%s  模式=%s  接口=%s", cfg["model"], mode, url)
    if verbose:
        for i, m in enumerate(messages, 1):
            log.info("--- 第 %d 条消息 [%s] ---\n%s", i, m["role"], m["content"])
    else:
        log.info("（完整提示词日志已关闭：把 config.json 的 log_prompt 设为 true 可开启）")

    t0 = time.time()
    resp = _post_with_retry(url, headers, payload, timeout)
    cost = time.time() - t0

    if resp.status_code != 200:
        log.error("AI 接口返回 %s: %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"AI 接口返回 {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    content = data["choices"][0]["message"]["content"]

    # ---- 响应日志：完整原文 ----
    log.info("========== 大模型响应 ==========")
    log.info("状态码=%s  耗时=%.2fs", resp.status_code, cost)
    usage = data.get("usage") or {}
    if usage:
        log.info(
            "Token 用量: prompt=%s completion=%s total=%s",
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("total_tokens"),
        )
    if verbose:
        log.info("--- 响应原文 ---\n%s", content)

    versions = _parse_versions(content, int(cfg.get("versions", 3)))
    log.info("解析得到 %d 个版本", len(versions))
    return versions


def test_api_key(cfg, timeout=15):
    """用最小请求验证 API Key 是否可用，返回 (是否可用, 中文提示)。

    仅供首次运行引导的「测试连接」使用：max_tokens=1，几乎不产生费用。
    """
    if not (cfg or {}).get("api_key"):
        return False, "请先填写 API Key"
    url = f"{cfg['api_base'].rstrip('/')}/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        resp = _post_with_retry(url, headers, payload, timeout)
    except Exception as e:
        # 只留第一行：原始异常常是几十行堆栈，弹窗里看着像崩溃
        return False, f"网络请求失败：{str(e).splitlines()[0][:160]}"
    if resp.status_code == 401:
        return False, "401：API Key 无效，请确认复制完整（应以 sk- 开头）"
    if resp.status_code != 200:
        return False, f"接口返回 {resp.status_code}：{resp.text[:200]}"
    return True, "连接成功，API Key 可用"
