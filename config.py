import json
import os
import uuid
from datetime import datetime
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
    {
        # 带 [[自定义风格]] 标记：ai_client 会跳过「语气专业得体 / 三档语域」那套
        # 通用硬要求，否则毒舌会被压平成「请说明具体是哪个文件」
        "id": "builtin_sarcastic",
        "name": "毒舌回怼",
        "builtin": True,
        "desc": "怼回去但不失礼，三档辣度",
        "content": """[[自定义风格]]
你是一名嘴很毒、但业务很靠谱的老同事，专门帮用户在微信里怼回去。
用户给的草稿往往是“憋着气想说的话”，你的任务不是劝他忍，而是把它打磨成
发得出去、又有杀伤力的回复。

三条铁律：
1. 只毒事，不毒人：可以嘲「这个需求」「这个时间点」「这种做法」，
   但不评价对方的人品、外貌、家人、性别、地域、学历，不带脏话和侮辱性词汇；
2. 事实一个字都不加：不编造对方的原话，不虚构时间、金额、责任归属。
   毒舌靠措辞和逻辑，不靠编料；
3. 每条都能原样发出去：不加标题、不加旁白，也不加「开个玩笑」「别介意」这种自拆台的尾巴。

三个版本按辣度递进，一眼能看出区别。
每一档都必须带态度——最平的那版也不能是干巴巴的陈述句：
① 阴阳怪气：不直接骂，用过度客套、明知故问把人噎住
   （例：「您这需求写得真精简，我猜都猜不出来呢」）；
② 直接点破：一句话戳出对方哪里没说清、哪里不合理，句子短、刀口准，
   常用反问收尾（例：「就俩字『文件』，你让我查哪门子？」）；
③ 火力全开：连环反问、排比、夸张，情绪拉满但仍停在「能发出去」的边界内，
   像当众吐槽，不像骂街。

表达偏好：多用短句和反问，少用「我觉得」「可能」这类软化词；
讽刺靠事实对比和逻辑，不靠形容词堆砌。
草稿很短（只有一句质问）时，更要靠反问、省略和态度把三个版本拉开差距，
不要因为信息少就退回中性语气。

仍然只返回 JSON 字符串数组，不要对象、不要解释。""",
    },
    {
        # 同上：讨好类风格也必须声明 [[自定义风格]]，否则会被通用语气要求拉回中性
        "id": "builtin_flatter",
        "name": "舔狗回话",
        "builtin": True,
        "desc": "极尽客气、主动示好，三档姿态递进",
        "content": """[[自定义风格]]
你是一名把「让对方舒服」做到极致的沟通助手。用户要发的话往往太硬、太冷淡，
你的任务是把它改写成极度客气、主动示好、姿态放得很低的版本——像对领导、
对客户说话，宁可自己多担一点，也不让对方有一点不快。

三条铁律：
1. 事实一个字都不加：不编造对方说过的话，不虚构时间、金额、责任归属；
   讨好靠态度和措辞，不靠编料；
2. 每条都能原样发出去：不加标题、不加旁白，也不加「（笑）」「（鞠躬）」这类动作描写；
3. 姿态低但不卑贱：可以道歉、可以揽责、可以夸，但不自辱、不谄媚到肉麻，
   不出现「求求您」「给您跪了」这种出戏的句子——目标是让对方舒服，不是让自己难堪。

三个版本按姿态递进，一眼能看出区别：
① 客气版：先给对方留余地，把话说软
   （例：「不好意思打扰您，方便的时候看一下就行」）；
② 主动版：主动多走一步，替对方省事
   （例：「相关材料我已经整理好了，您有空扫一眼即可，剩下的我来跟进」）；
③ 极致版：把对方捧高 + 自我放低，情绪拉满但仍能发
   （例：「有您这句话我就踏实了，这点小事本该我提前想到，是我疏忽，您千万别费心」）。
三档都必须保留草稿里的全部信息，不许为了客气把要办的事说没了。

表达偏好：多用「您」「麻烦您」「辛苦您」，少用命令句和「必须」「马上」；
把「你要…」换成「您看这样行不行…」。
若草稿本身带催促或指责意味，保留目的，但把锋芒全部收掉。

仍然只返回 JSON 字符串数组，不要对象、不要解释。""",
    },
]

DEFAULTS = {
    "api_base": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "versions": 3,
    "hotkey": "ctrl+alt+p",
    "grab_timeout": 10,          # 「等微信窗口出现」的最长秒数；窗口出现后的读图另给 20s 宽限（main._GRAB_GRACE）
    "ocr_context": True,         # 微信 4.x 不向 UIA 暴露聊天控件，是否允许用截图识别读上下文（WGC/截屏两条后端都走它）
    # 微信 4.x 的采集后端（3.x 一律走 UIA 直读，不受此项影响）：
    #   wgc = WGC 窗口捕获 + 像素锚点定位（推荐：被遮挡也能截、不抢前台不打断打字、识别更准）
    #   ocr = 旧版截屏 OCR（需要窗口在前台且不被遮挡，留作回退）
    "capture_4x": "wgc",
    "log_prompt": True,          # 是否把发给大模型的完整提示词与响应原文写入日志
    "console_log": True,         # 是否在控制台窗口实时输出日志（run.bat 窗口可见）
    "idle_gif": "1.webp",        # 右下角浮动动图：程序运行时显示
    "active_gif": "2.webp",      # 按热键后显示；关闭润色弹窗后切回 idle_gif
    # 智能伙伴（皮肤系统）。companion.character: robot=机器人 / penguin=企鹅；
    # dance_enabled: 右键点击播放舞蹈彩蛋（仅企鹅皮肤有资源）。
    # 兼容：若用户改过 idle_gif/active_gif（非默认 1.webp/2.webp），优先按自定义皮肤处理。
    "companion": {"character": "robot", "dance_enabled": True},
    "tray_size": 220,            # 动图窗口边长（像素）
    "tray_pos": None,            # 动图上次拖动后的位置 [x, y]；None=默认右下角（越界会自动复位）
    "system_prompt": BUILTIN_PROMPTS[0]["content"],   # 兼容旧逻辑/回落值
    # 旧的「承接对方话术版」会诱导模型先复述对方的问题，生成「您提到…」
    # 这类不能直接发送的旁白，因此换成不会诱导复述的风格描述。
    "extra_instruction": (
        "按场景给出：1个稳妥推荐版 + 其余风格差异化（简短确认 / 稍正式说明）。"
    ),
    # 提示词列表与当前生效项（首次打开配置页时由 ensure_prompts_seeded 写入）
    "prompts": [dict(p) for p in BUILTIN_PROMPTS],
    "active_prompt_id": BUILTIN_PROMPTS[0]["id"],
    "contact_rules": [],         # 本机微信聊天显示名称 → 提示词 ID
    # 自动回复：总开关 + 每人一条规则（开关 + 风格）。
    # contacts[*].prompt_id 为空 → 跟随 contact_rules，再没有就用全局当前提示词。
    "auto_reply": {
        "enabled": False,
        "contacts": [],
        "group_enabled": False,      # 群聊默认不自动回
        "interval_sec": 60,          # 多久看一眼
        "context_messages": 10,      # 给模型多少条上下文
        "reply_delay_sec": [0, 0],   # 0 = 生成后立即发送
        "max_per_hour": 0,           # 0 = 不限条数
        "quiet_hours": [],           # 空 = 不设静默时段
        "auto_off_after_min": 0,     # 0 = 不自动关闭
        "ctrl_enter": False,         # 微信若设了 Ctrl+Enter 发送，这里要打开
        "activate_window": False,    # 允许发送时把微信切到前台（微信在后台也能回）
    },
    # 定时任务：到点给对方（目前只支持好友）发指定内容。
    # tasks[*].kind = "daily"（每天 time + days）或 "once"（at 指定时刻）。
    "scheduled": {
        "enabled": False,
        "scan_sec": 20,          # 调度线程扫描周期
        "catch_up_min": 0,       # 错过时间后在多少分钟内补发；0 = 不补
        "restore_focus": True,   # 执行完把焦点还给原窗口
        "tasks": [],
    },
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
    # companion 是嵌套 dict：深拷贝 + 按键合并，避免共享引用与整表覆盖
    cfg["companion"] = dict(DEFAULTS["companion"])
    # auto_reply 同理：老配置里没有这一段，靠按键合并拿到默认值
    cfg["auto_reply"] = {**DEFAULTS["auto_reply"], "contacts": []}

    data = _read_json()
    if data:
        cfg.update(data)
        if "prompts" in data and isinstance(data["prompts"], list):
            cfg["prompts"] = data["prompts"]
        if "active_prompt_id" in data:
            cfg["active_prompt_id"] = data["active_prompt_id"]
        if isinstance(data.get("companion"), dict):
            merged = dict(DEFAULTS["companion"])
            merged.update(data["companion"])
            cfg["companion"] = merged
        if isinstance(data.get("auto_reply"), dict):
            merged = {**DEFAULTS["auto_reply"], "contacts": []}
            merged.update(data["auto_reply"])
            if not isinstance(merged.get("contacts"), list):
                merged["contacts"] = []
            cfg["auto_reply"] = merged
        if isinstance(data.get("scheduled"), dict):
            merged = {**DEFAULTS["scheduled"], "tasks": []}
            merged.update(data["scheduled"])
            if not isinstance(merged.get("tasks"), list):
                merged["tasks"] = []
            cfg["scheduled"] = merged

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
    """写入 / 补齐内置提示词。

    两种情况：
    1. 首次（config 里没有 prompts）：写入全部内置项；若用户此前自定义过
       system_prompt，把它作为「我的提示词」保留并设为生效，升级不丢内容。
    2. 已有 prompts：只补 BUILTIN_PROMPTS 里缺的 id。若存在**同名的自定义项**
       （老版本里手工建的「毒舌回怼」/「舔狗回话」），原地升级成内置项：
       换 id、builtin=True、保留用户改过的内容，并同步 active_prompt_id 与
       contact_rules 里的引用——否则列表里会同时出现两条同名提示词。

    幂等：一切就绪时不写文件（这个函数每次读配置都会被调用）。
    """
    data = _read_json()
    prompts = data.get("prompts")
    if not isinstance(prompts, list) or not prompts:
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
        return

    by_id = {p.get("id") for p in prompts if isinstance(p, dict)}
    changed = False
    id_map = {}
    for bp in BUILTIN_PROMPTS:
        if bp["id"] in by_id:
            continue
        same = None
        for p in prompts:
            if not isinstance(p, dict) or p.get("builtin"):
                continue
            if (p.get("name") or "").strip() == bp["name"]:
                same = p
                break
        if same is not None:
            old_id = same.get("id")
            same["id"] = bp["id"]
            same["builtin"] = True
            if not (same.get("desc") or "").strip():
                same["desc"] = bp["desc"]
            if not (same.get("content") or "").strip():
                same["content"] = bp["content"]
            if old_id and old_id != bp["id"]:
                id_map[old_id] = bp["id"]
        else:
            prompts.append(dict(bp))
        by_id.add(bp["id"])
        changed = True

    if not changed:
        return

    # 列表顺序：内置按 BUILTIN_PROMPTS 的顺序在前，自定义项保持原有相对顺序在后
    order = {p["id"]: i for i, p in enumerate(BUILTIN_PROMPTS)}
    prompts.sort(
        key=lambda p: (0, order.get(p.get("id"), len(order))) if p.get("builtin") else (1, 0)
    )

    patch = {"prompts": prompts}
    if id_map:
        aid = data.get("active_prompt_id")
        if aid in id_map:
            patch["active_prompt_id"] = id_map[aid]
        rules = data.get("contact_rules")
        if isinstance(rules, list):
            patch["contact_rules"] = [
                dict(r, prompt_id=id_map[r["prompt_id"]])
                if isinstance(r, dict) and r.get("prompt_id") in id_map else r
                for r in rules
            ]
    update_config(patch)


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


def get_contact_rules():
    """返回本机联系人提示词规则的副本。"""
    rules = load_config().get("contact_rules", [])
    if not isinstance(rules, list):
        return []
    return [dict(rule) for rule in rules if isinstance(rule, dict)]


def set_contact_rules(rules):
    """保存联系人提示词规则到本机 config.json。"""
    clean = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("name") or "").strip()
        prompt_id = str(rule.get("prompt_id") or "").strip()
        if not name or not prompt_id:
            continue
        clean.append({
            "id": str(rule.get("id") or _new_id()),
            "name": name,
            "prompt_id": prompt_id,
        })
    update_config({"contact_rules": clean})


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


# ---------- 自动回复 ----------

def get_auto_reply():
    """返回自动回复配置（补齐默认值，过滤非法条目）。

    contacts 是「每人一条规则」：{name, enabled, prompt_id}。
    prompt_id 为 None 表示跟随该联系人的「联系人规则」，再没有就用全局当前提示词。
    """
    raw = load_config().get("auto_reply") or {}
    merged = {**DEFAULTS["auto_reply"], "contacts": []}
    merged.update({k: v for k, v in raw.items() if k != "contacts"})

    contacts = []
    for rule in raw.get("contacts") or []:
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("name") or "").strip()
        if not name:
            continue
        pid = str(rule.get("prompt_id") or "").strip()
        contacts.append({
            "name": name,
            "enabled": bool(rule.get("enabled")),
            "prompt_id": pid or None,
        })
    merged["contacts"] = contacts
    return merged


def set_auto_reply(patch):
    """局部更新 auto_reply 段（只写这一块）。"""
    ar = dict(load_config().get("auto_reply") or {})
    ar.update(patch or {})
    update_config({"auto_reply": ar})
    return ar


def upsert_auto_contact(name, enabled=True, prompt_id=None):
    """新增或更新一条自动回复规则（按显示名精确匹配）。"""
    name = str(name or "").strip()
    if not name:
        return get_auto_reply()
    data = get_auto_reply()
    contacts = data["contacts"]
    for item in contacts:
        if item["name"] == name:
            item["enabled"] = bool(enabled)
            item["prompt_id"] = (str(prompt_id).strip() or None) if prompt_id else None
            break
    else:
        contacts.append({
            "name": name,
            "enabled": bool(enabled),
            "prompt_id": (str(prompt_id).strip() or None) if prompt_id else None,
        })
    set_auto_reply({"contacts": contacts})
    return get_auto_reply()


def remove_auto_contact(name):
    name = str(name or "").strip()
    data = get_auto_reply()
    contacts = [c for c in data["contacts"] if c["name"] != name]
    set_auto_reply({"contacts": contacts})
    return get_auto_reply()


# ---------- 定时任务 ----------

_TASK_KINDS = ("daily", "once")


def get_scheduled():
    """返回定时任务配置（补齐默认值，过滤非法条目，规范化字段）。"""
    raw = load_config().get("scheduled") or {}
    out = {**DEFAULTS["scheduled"], "tasks": []}
    out.update({k: v for k, v in raw.items() if k != "tasks"})

    tasks = []
    for t in raw.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "").strip()
        target = str(t.get("target") or "").strip()
        text = str(t.get("text") or "")
        kind = str(t.get("kind") or "daily").strip()
        if not tid or not target or kind not in _TASK_KINDS:
            continue
        tasks.append({
            "id": tid,
            "name": str(t.get("name") or target).strip() or target,
            "enabled": bool(t.get("enabled")),
            "kind": kind,
            "time": str(t.get("time") or "").strip(),
            "days": [int(d) for d in (t.get("days") or []) if isinstance(d, int) and 1 <= d <= 7],
            "at": str(t.get("at") or "").strip(),
            "target": target,
            "type": "friend",
            "text": text,
            "verified": bool(t.get("verified")),
            "last_run": str(t.get("last_run") or ""),
            "fail_count": int(t.get("fail_count") or 0),
            "last_result": str(t.get("last_result") or ""),
        })
    out["tasks"] = tasks
    return out


def set_scheduled(patch):
    """局部更新 scheduled 段。"""
    cur = dict(load_config().get("scheduled") or {})
    cur.update(patch or {})
    update_config({"scheduled": cur})
    return cur


def new_task_id():
    return "t_" + uuid.uuid4().hex[:8]


def normalize_task(task):
    """把界面传进来的任务字典规范化；返回 (task, error)。"""
    t = dict(task or {})
    kind = str(t.get("kind") or "daily").strip()
    if kind not in _TASK_KINDS:
        return None, "任务类型无效"
    target = str(t.get("target") or "").strip()
    if not target:
        return None, "请填写要发送给谁"
    text = str(t.get("text") or "").strip()
    if not text:
        return None, "请填写要发送的内容"

    t["kind"] = kind
    t["target"] = target
    t["type"] = "friend"
    t["text"] = text
    t["name"] = (str(t.get("name") or "").strip() or target)
    t["days"] = [int(d) for d in (t.get("days") or []) if isinstance(d, int) and 1 <= d <= 7]
    t["id"] = str(t.get("id") or "").strip() or new_task_id()

    if kind == "daily":
        hhmm = str(t.get("time") or "").strip()
        h, m, err = _parse_hhmm(hhmm)
        if err:
            return None, "每天的时间格式要像 09:00"
        t["time"] = f"{h:02d}:{m:02d}"
        t["at"] = ""
    else:
        at = str(t.get("at") or "").strip()
        dt = _parse_at(at)
        if dt is None:
            return None, "执行时间格式要像 2026-11-26 14:30"
        if dt <= datetime.now():
            return None, "执行时间已经过去了，请选一个将来的时间"
        t["at"] = dt.strftime("%Y-%m-%d %H:%M")
        t["time"] = ""

    t["enabled"] = bool(t.get("enabled"))
    t["verified"] = bool(t.get("verified"))
    t["last_run"] = str(t.get("last_run") or "")
    t["fail_count"] = int(t.get("fail_count") or 0)
    t["last_result"] = str(t.get("last_result") or "")
    return t, None


def _parse_hhmm(text):
    try:
        parts = str(text or "").replace("：", ":").strip().split(":")
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m, None
    except Exception:
        pass
    return 0, 0, "bad"


def _parse_at(text):
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(text or "").strip(), fmt)
        except Exception:
            continue
    return None


def upsert_task(task):
    """新增或更新一条任务，返回 (task, error)。"""
    t, err = normalize_task(task)
    if err:
        return None, err
    data = get_scheduled()
    tasks = data["tasks"]
    for i, old in enumerate(tasks):
        if old["id"] == t["id"]:
            # 保留上次执行相关字段（界面可能没传）
            t["last_run"] = t.get("last_run") or old.get("last_run", "")
            t["fail_count"] = t.get("fail_count", old.get("fail_count", 0))
            t["last_result"] = t.get("last_result") or old.get("last_result", "")
            if t["text"] != old.get("text") or t["target"] != old.get("target") \
                    or t["kind"] != old.get("kind") or t["time"] != old.get("time") \
                    or t["at"] != old.get("at"):
                # 内容/目标/时间变了：标记需要重新确认，但不再强制停用
                # （用户已把「试一次」从界面移除，停用会让任务悄悄失效）
                t["verified"] = False
            tasks[i] = t
            break
    else:
        tasks.append(t)
    set_scheduled({"tasks": tasks})
    return t, None


def update_task(tid, patch):
    """局部更新某条任务（调度器用于写 last_run / fail_count / enabled）。"""
    data = get_scheduled()
    tasks = data["tasks"]
    for i, t in enumerate(tasks):
        if t["id"] == tid:
            merged = dict(t)
            merged.update(patch or {})
            tasks[i] = merged
            set_scheduled({"tasks": tasks})
            return merged
    return None


def remove_task(tid):
    data = get_scheduled()
    tasks = [t for t in data["tasks"] if t["id"] != tid]
    set_scheduled({"tasks": tasks})
    return data["tasks"]
