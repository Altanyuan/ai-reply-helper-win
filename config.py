import json
import os
import uuid
from pathlib import Path

import paths
import key_utils


# ---- 5 个预埋提示词（内置，不可删除；可编辑内容）----
# builtin_general 的内容与历史默认 system_prompt 一致，老用户升级后行为不变。
BUILTIN_PROMPTS = [
    {
        "id": "builtin_general",
        "name": "通用润色",
        "builtin": True,
        "desc": "得体专业、可直接发送的通用话术",
        "content": (
            "你是一名专业的职场沟通润色助手，用户在微信或飞书等场景进行工作沟通。"
            "请根据对话上下文，把用户草拟的内容润色成得体、专业、可直接发送的话术。"
            "保持原意与事实，不要编造信息；语气尊重、简洁；可按不同风格给多个版本。"
        ),
    },
    {
        "id": "builtin_formal",
        "name": "商务正式",
        "builtin": True,
        "desc": "面向银行/客户的正式措辞",
        "content": (
            "你是一名严谨的商务沟通润色助手，面向客户、合作方等正式场合。"
            "请把用户草拟的内容改写为措辞正式、逻辑清晰、术语准确的专业话术，"
            "体现稳健与可信度；保持原意与事实，不编造信息；语气尊重、不卑不亢；"
            "可按不同风格给多个版本。"
        ),
    },
    {
        "id": "builtin_concise",
        "name": "简洁高效",
        "builtin": True,
        "desc": "精炼直接、去除冗余",
        "content": (
            "你是一名注重效率的沟通润色助手。"
            "请把用户草拟的内容精炼为简短、直接、重点突出的话术，"
            "去除冗余客套，保留核心信息与事实；语气礼貌但不啰嗦；"
            "可按不同风格给多个版本。"
        ),
    },
    {
        "id": "builtin_friendly",
        "name": "亲切友好",
        "builtin": True,
        "desc": "语气温暖、拉近距离",
        "content": (
            "你是一名亲和力强的沟通润色助手。"
            "请把用户草拟的内容改写为语气温暖、自然、易拉近距离的话术，"
            "适合日常对接与维护关系；保持原意与事实，不编造信息；"
            "可按不同风格给多个版本。"
        ),
    },
    {
        "id": "builtin_structured",
        "name": "条理清晰",
        "builtin": True,
        "desc": "分点分层、便于抓取重点",
        "content": (
            "你是一名重视结构表达的沟通润色助手。"
            "请把用户草拟的内容整理为分点、分层、条理清晰的话术，"
            "便于对方快速抓取关键信息；保持原意与事实，不编造信息；"
            "语气专业得体；可按不同风格给多个版本。"
        ),
    },
]

DEFAULTS = {
    "api_base": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "versions": 3,
    "context_messages": 5,
    "hotkey": "ctrl+alt+p",
    "grab_timeout": 10,          # 触发后等待/重试获取微信内容的最长秒数，超时才弹提示框
    "ocr_context": True,         # 微信 4.x 不暴露 UIA 控件，是否允许用 OCR 读取聊天上下文（WGC/截屏两条后端都走它）
    # 微信 4.x 的采集后端（3.x 一律走 UIA 直读，不受此项影响）：
    #   wgc = WGC 窗口捕获 + 像素锚点定位（推荐：被遮挡也能截、不抢前台不打断打字、识别更准）
    #   ocr = 旧版截屏 OCR（需要窗口在前台且不被遮挡，留作回退）
    "capture_4x": "wgc",
    "log_prompt": False,         # 是否把发给大模型的完整提示词与响应原文写入日志（默认关闭：聊天原文是隐私敏感内容）
    "console_log": True,         # 是否在控制台窗口实时输出日志（run.bat 窗口可见）
    "idle_gif": "1.webp",        # 右下角浮动动图：程序运行时显示
    "active_gif": "2.webp",      # 按热键后显示；关闭润色弹窗后切回 idle_gif
    "tray_size": 220,            # 动图窗口边长（像素）
    "tray_pos": None,            # 动图上次拖动后的位置 [x, y]；None=默认右下角（越界会自动复位）
    "system_prompt": BUILTIN_PROMPTS[0]["content"],   # 兼容旧逻辑/回落值
    "extra_instruction": (
        "按场景给出：1个稳妥推荐版 + 其余风格差异化（简短版 / 正式版 / 承接对方话术版）。"
    ),
    # 提示词列表与当前生效项（首次打开配置页时由 ensure_prompts_seeded 写入）
    "prompts": [dict(p) for p in BUILTIN_PROMPTS],
    "active_prompt_id": BUILTIN_PROMPTS[0]["id"],
}


def _config_path():
    # 打包后必须写到用户目录，否则单文件模式下配置会随临时目录一起丢失
    return paths.config_path()


def _read_json():
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[config] 读取 config.json 失败，使用默认: {e}")
        return {}


def load_config():
    """加载配置：config.json 打底，环境变量覆盖（便于保密 Key）。

    提示词列表（prompts / active_prompt_id）若已在 config.json 中则随之加载；
    若尚未写入（老用户首次升级），则返回内置 5 个，ai_client 会安全回落到 system_prompt。
    """
    cfg = dict(DEFAULTS)
    # 避免共享 DEFAULTS 里的可变列表引用
    cfg["prompts"] = [dict(p) for p in BUILTIN_PROMPTS]
    cfg["active_prompt_id"] = BUILTIN_PROMPTS[0]["id"]

    data = _read_json()
    if data:
        cfg.update(data)
        if "prompts" in data and isinstance(data["prompts"], list):
            cfg["prompts"] = data["prompts"]
        if "active_prompt_id" in data:
            cfg["active_prompt_id"] = data["active_prompt_id"]

    # 多模型配置：若存在 models，用它解析出「当前生效」的 api_base / model / api_key。
    # 放在环境变量之前，保证环境变量仍然拥有最高优先级（便于临时覆盖 / 保密）。
    _resolve_active_model(cfg)

    if os.getenv("AI_API_KEY"):
        cfg["api_key"] = os.getenv("AI_API_KEY")
    if os.getenv("AI_API_BASE"):
        cfg["api_base"] = os.getenv("AI_API_BASE")
    if os.getenv("AI_MODEL"):
        cfg["model"] = os.getenv("AI_MODEL")
    return cfg


def _key_of(profile):
    """从一条模型配置里取出明文 Key；没有则返回 ""。"""
    if not isinstance(profile, dict):
        return ""
    return (profile.get("api_key") or "").strip()


def _resolve_active_model(cfg):
    """把「当前生效模型」的地址 / 模型名 / Key 覆盖到顶层字段。

    这样所有既有调用方（ai_client 等）继续读 cfg['api_key'] 即可，
    无需关心多模型结构；没有 models 时本函数什么都不做（完全向后兼容）。
    """
    models = cfg.get("models")
    if not isinstance(models, list) or not models:
        return
    active_id = cfg.get("active_model_id")
    prof = None
    for m in models:
        if isinstance(m, dict) and m.get("id") == active_id:
            prof = m
            break
    if prof is None:
        prof = next((m for m in models if isinstance(m, dict)), None)
    if prof is None:
        return
    if prof.get("api_base"):
        cfg["api_base"] = prof["api_base"]
    if prof.get("model"):
        cfg["model"] = prof["model"]
    key = _key_of(prof)
    if key:
        cfg["api_key"] = key


def _remove_fields(*names):
    """从 config.json 中删除指定字段（用于迁移掉明文 Key / 清理旧字段）。"""
    path = _config_path()
    data = _read_json()
    changed = False
    for n in names:
        if n in data:
            data.pop(n, None)
            changed = True
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")


def save_config(cfg):
    path = _config_path()
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def update_config(partial):
    """局部更新 config.json：只写入 partial 里的字段，保留其余（不会把 DEFAULTS 全量落盘）。"""
    path = _config_path()
    data = _read_json()
    data.update(partial)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 提示词管理 ----------

def ensure_prompts_seeded():
    """首次迁移：若 config.json 没有 prompts，写入内置 5 个。

    若用户此前自定义过 system_prompt（且与任一内置不同），则把它作为
    “我的提示词”自定义项保留并设为生效，避免升级后丢失自定义内容。
    幂等：已存在 prompts 时直接返回。
    """
    data = _read_json()
    if "prompts" in data and isinstance(data["prompts"], list) and data["prompts"]:
        return
    prompts = [dict(p) for p in BUILTIN_PROMPTS]
    active_id = prompts[0]["id"]
    sp = (data.get("system_prompt") or "").strip()
    if sp and not any(sp == (p.get("content") or "").strip() for p in prompts):
        prompts.insert(0, {
            "id": "migrated",
            "name": "我的提示词",
            "builtin": False,
            "desc": "从旧配置迁移",
            "content": sp,
        })
        active_id = "migrated"
    update_config({"prompts": prompts, "active_prompt_id": active_id})


def get_prompts():
    """返回当前提示词列表（内置 + 自定义），按内置在前、自定义在后的顺序。"""
    ensure_prompts_seeded()
    cfg = load_config()
    prompts = cfg.get("prompts")
    if not prompts:
        return [dict(p) for p in BUILTIN_PROMPTS]
    # 保证内置项的 id/内容稳定；自定义项按用户添加顺序保留
    return prompts


def get_active_prompt_id():
    cfg = load_config()
    aid = cfg.get("active_prompt_id")
    if aid:
        return aid
    prompts = cfg.get("prompts") or BUILTIN_PROMPTS
    return prompts[0]["id"] if prompts else BUILTIN_PROMPTS[0]["id"]


def get_active_prompt():
    """返回当前生效的提示词 dict；找不到时回落到第一个。"""
    cfg = load_config()
    prompts = cfg.get("prompts") or [dict(p) for p in BUILTIN_PROMPTS]
    aid = cfg.get("active_prompt_id")
    if aid:
        for p in prompts:
            if p.get("id") == aid:
                return p
    return prompts[0] if prompts else dict(BUILTIN_PROMPTS[0])


def set_active_prompt(pid):
    """切换生效提示词（写盘，下次触发润色即用）。"""
    update_config({"active_prompt_id": pid})


def _new_id():
    return "c_" + uuid.uuid4().hex[:8]


def add_prompt(name, content, desc=""):
    """新增自定义提示词，返回新 id。不自动设为生效（由用户勾选决定）。"""
    name = (name or "").strip() or "未命名提示词"
    content = (content or "").strip()
    prompts = get_prompts()
    pid = _new_id()
    prompts.append({
        "id": pid,
        "name": name,
        "builtin": False,
        "desc": desc,
        "content": content,
    })
    update_config({"prompts": prompts})
    return pid


def update_prompt(pid, name, content, desc=None):
    """编辑已有提示词（内置项也可编辑内容/名称，但不可删除）。"""
    prompts = get_prompts()
    for p in prompts:
        if p.get("id") == pid:
            p["name"] = (name or "").strip() or p.get("name") or "未命名提示词"
            p["content"] = (content or "").strip()
            if desc is not None:
                p["desc"] = desc
            break
    update_config({"prompts": prompts})


def delete_prompt(pid):
    """删除自定义提示词（拒绝删除内置项）。若删的是生效项，回落到第一个。返回新的生效 id。"""
    prompts = get_prompts()
    target = None
    for p in prompts:
        if p.get("id") == pid:
            target = p
            break
    if target is None:
        return get_active_prompt_id()
    if target.get("builtin"):
        # 内置项不可删除
        return get_active_prompt_id()
    prompts = [p for p in prompts if p.get("id") != pid]
    active = get_active_prompt_id()
    if active == pid:
        active = prompts[0]["id"] if prompts else BUILTIN_PROMPTS[0]["id"]
        set_active_prompt(active)
    update_config({"prompts": prompts})
    return active


def reset_prompt(pid):
    """把内置提示词恢复为出厂内容（自定义项不支持，返回 False）。

    只重置内容与说明，保留用户改过的名称以外的元数据不动（名称也一并还原）。
    """
    for bp in BUILTIN_PROMPTS:
        if bp["id"] != pid:
            continue
        prompts = get_prompts()
        for p in prompts:
            if p.get("id") == pid:
                p["name"] = bp["name"]
                p["content"] = bp["content"]
                p["desc"] = bp["desc"]
                break
        update_config({"prompts": prompts})
        return True
    return False


# ---------- 多模型配置（含 API Key 的加密存储）----------

def _mirror_active(pid, api_base=None, model=None, api_key=None):
    """把「当前生效模型」的字段同步到顶层 api_base / model / api_key。

    顶层字段是历史约定（ai_client / first_run 都读它），保持同步可以让
    配置文件一眼看懂、也让旧版本程序仍能正常工作。
    """
    if get_active_model_id() != pid:
        return
    patch = {}
    if api_base:
        patch["api_base"] = api_base
    if model:
        patch["model"] = model
    if api_key is not None:
        patch["api_key"] = api_key
    if patch:
        update_config(patch)


def ensure_models_seeded():
    """把旧的 api_key / api_base / model 迁移成第一条模型配置（幂等）。

    Key 按用户要求**明文**存储（不再加密）；顶层 api_key 字段同时保留，
    这样回滚到旧版本程序也能直接读到 Key。
    返回 True 表示本次确实做了迁移。
    """
    data = _read_json()
    models = data.get("models")
    if isinstance(models, list) and models:
        return False
    legacy_key = (data.get("api_key") or "").strip()
    legacy_base = (data.get("api_base") or "").strip()
    if not legacy_key and not legacy_base:
        return False                       # 什么都没配，不凭空造一条
    prof = {
        "id": "m_default",
        "name": "默认模型",
        "api_base": legacy_base or DEFAULTS["api_base"],
        "model": (data.get("model") or "").strip() or DEFAULTS["model"],
        "api_key": legacy_key,
        "note": "由原有 api_key 配置自动迁移",
    }
    update_config({"models": [prof], "active_model_id": prof["id"]})
    return True


def ensure_default_model():
    """确保至少存在一条模型配置，返回它的 id（不存在时按默认地址/模型名创建）。"""
    ensure_models_seeded()
    models = _read_json().get("models")
    if isinstance(models, list) and models:
        return get_active_model_id() or models[0].get("id")
    prof = {
        "id": "m_default",
        "name": "默认模型",
        "api_base": DEFAULTS["api_base"],
        "model": DEFAULTS["model"],
        "note": "",
    }
    update_config({"models": [prof], "active_model_id": prof["id"]})
    return prof["id"]


def get_models():
    """返回模型配置列表；每条都会补上**解密后的明文 api_key**（仅供界面编辑）。

    非界面调用请改用 get_active_model()，避免把明文 Key 带到别处。
    """
    ensure_models_seeded()
    cfg = load_config()
    models = cfg.get("models")
    if not isinstance(models, list):
        return []
    out = []
    for m in models:
        if not isinstance(m, dict):
            continue
        item = dict(m)
        item["api_key"] = _key_of(m)
        item["has_key"] = bool(item["api_key"])
        out.append(item)
    return out


def get_active_model_id():
    cfg = load_config()
    models = cfg.get("models")
    if not isinstance(models, list) or not models:
        return None
    aid = cfg.get("active_model_id")
    if aid and any(isinstance(m, dict) and m.get("id") == aid for m in models):
        return aid
    first = next((m for m in models if isinstance(m, dict)), None)
    return first.get("id") if first else None


def get_active_model():
    """返回当前生效的模型配置（含明文 Key）；没有则返回 {}。"""
    models = get_models()
    if not models:
        return {}
    aid = get_active_model_id()
    for m in models:
        if m.get("id") == aid:
            return m
    return models[0]


def save_model(profile):
    """新增 / 更新一条模型配置（按 id 判断），Key 加密后落盘。

    返回 (是否成功, 给用户看的提示语)。提示语里**绝不出现完整 Key**（已脱敏）。
    """
    pid = (profile.get("id") or "").strip()
    name = (profile.get("name") or "").strip() or "未命名模型"
    base = (profile.get("api_base") or "").strip()
    model_name = (profile.get("model") or "").strip()
    key = (profile.get("api_key") or "").strip()
    note = profile.get("note", "")

    data = _read_json()
    models = data.get("models")
    if not isinstance(models, list):
        models = []
    models = [m for m in models if isinstance(m, dict)]

    is_new = not pid
    if is_new:
        pid = "m_" + uuid.uuid4().hex[:8]

    entry = next((m for m in models if m.get("id") == pid), None)
    if entry is None:
        entry = {"id": pid}
        models.append(entry)

    entry["name"] = name
    entry["api_base"] = base
    entry["model"] = model_name
    entry["note"] = note

    key_masked = ""
    if key:
        entry["api_key"] = key
        key_masked = key_utils.mask(key)
    else:
        entry["api_key"] = ""
    entry.pop("api_key_enc", None)         # 清理早期版本可能留下的密文字段

    patch = {"models": models}
    if not data.get("active_model_id"):
        patch["active_model_id"] = pid
    update_config(patch)

    # 生效模型把地址 / 模型名 / Key 一并同步到顶层（清空 Key 时也要同步，
    # 否则顶层会留着旧 Key，出现"看起来清空了其实还在用"的错觉）
    _mirror_active(pid, base, model_name, entry["api_key"])

    action = "已新增" if is_new else "已保存"
    if key_masked:
        return True, f"{action}模型「{name}」（Key：{key_masked}）"
    return True, f"{action}模型「{name}」（未填写 Key）"


def delete_model(mid):
    """删除一条模型配置；若删的是生效项则切到第一条。返回 (是否成功, 提示语)。"""
    data = _read_json()
    models = [m for m in (data.get("models") or []) if isinstance(m, dict)]
    target = next((m for m in models if m.get("id") == mid), None)
    if target is None:
        return False, "未找到该模型配置"
    if len(models) <= 1:
        return False, "至少保留一条模型配置；如要清空 Key，请直接编辑后清空 Key 输入框"
    models = [m for m in models if m.get("id") != mid]
    patch = {"models": models}
    if get_active_model_id() == mid:
        patch["active_model_id"] = models[0].get("id")
    update_config(patch)
    if patch.get("active_model_id"):
        active = next((m for m in models if m.get("id") == patch["active_model_id"]), {})
        _mirror_active(patch["active_model_id"], active.get("api_base"), active.get("model"))
    return True, f"已删除模型「{target.get('name', '未命名模型')}」"


def set_active_model(mid):
    """切换生效模型（写盘，下次调用大模型即用）。返回 (是否成功, 提示语)。"""
    models = [m for m in (_read_json().get("models") or []) if isinstance(m, dict)]
    target = next((m for m in models if m.get("id") == mid), None)
    if target is None:
        return False, "未找到该模型配置"
    if not _key_of(target):
        return False, "该模型还没有填写 API Key，请先填写并保存"
    update_config({"active_model_id": mid})
    _mirror_active(mid, target.get("api_base"), target.get("model"))
    return True, f"已切换生效模型：{target.get('name', '未命名模型')}"
