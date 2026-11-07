"""本机联系人显示名称到提示词 ID 的安全匹配逻辑。

此模块不记录、不发送联系人名称；调用方仅使用返回的提示词 ID。
"""


def resolve_prompt_id(chat_name, rules, prompts, default_prompt_id):
    """严格匹配当前聊天名称；任何缺失、重名或失效数据均回落到默认项。

    比较前只去除名称首尾空格，区分大小写，不做模糊匹配。
    返回 ``(prompt_id, reason)``；reason 仅供本机界面状态判断，不应写日志。
    """
    name = str(chat_name or "").strip()
    if not name:
        return default_prompt_id, "no_name"

    matches = [
        rule for rule in (rules or [])
        if isinstance(rule, dict)
        and str(rule.get("name") or "").strip() == name
    ]
    if not matches:
        return default_prompt_id, "no_match"
    if len(matches) != 1:
        return default_prompt_id, "ambiguous"

    prompt_id = str(matches[0].get("prompt_id") or "").strip()
    prompt_matches = [
        prompt for prompt in (prompts or [])
        if isinstance(prompt, dict) and prompt.get("id") == prompt_id
    ]
    if not prompt_id or len(prompt_matches) != 1:
        return default_prompt_id, "invalid"
    return prompt_id, "matched"


def config_for_prompt(cfg, prompt_id):
    """创建单次生成配置副本，不改动全局默认提示词。"""
    run_cfg = dict(cfg or {})
    run_cfg["active_prompt_id"] = prompt_id
    return run_cfg
