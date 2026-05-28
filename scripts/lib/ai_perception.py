"""用户 AI 感知分析 (纯规则匹配, 不调 LLM).

业务问题: 真人接听里, 多少客户真的"意识到了自己在和 AI 通话"?
检测 A-I 九维信号, 任一命中即算"用户明显感知 AI".

输入: enrich() 后的 df (含 _transcript / _human / _assistant_turns 等内部列)
输出: dict, 供 build_data 注入 DATA.ai_perception 给前端用.

来源: 复刻 ~/Desktop/agora-offline/gk-5-21-AI识别分析.html 的离线分析逻辑.
"""

from __future__ import annotations

import re
from collections import Counter


# ── 关键词字典 ──────────────────────────────────────────────

# A 口头识破: user 直接说出 AI / 机器人相关词
A_KEYWORDS = [
    "机器人", "是真人吗", "人机", "真人吗", "电脑",
    "假人", "假的", "人工还是", "AI", "ai", "机器",
]

# B 豪车 prank: user 报奢侈品牌 (100w+ 超豪车, 强信号 = 调戏)
B_KEYWORDS = [
    "劳斯莱斯", "保时捷", "宾利", "帕拉梅拉", "迈巴赫",
    "玛莎拉蒂", "兰博基尼", "法拉利", "阿斯顿马丁", "布加迪",
    "库里南", "幻影", "Urus",
]

# C 反问 agent 个人: 想测探 AI 边界
C_KEYWORDS = [
    "怎么称呼你", "你姓什么", "你叫什么", "你叫啥",
    "你是谁", "你哪位", "你是男是女", "你多大",
]

# D 情绪/重复: 由 D_REPEAT_FIELD_MAP 检测 agent 反复问同一槽位
# 字段同义词 (检测 agent 提问触发的关键词)
D_REPEAT_FIELD_MAP = {
    "型号": ["哪款", "什么车", "什么型号", "什么牌子", "哪个品牌", "哪个车型", "什么车型"],
    "城市": ["哪个城市", "什么城市", "哪里提车", "什么地方", "哪儿提"],
    "时间": ["什么时候", "计划什么时候", "几个月", "近期", "大概什么时候"],
    "姓名": ["贵姓", "您姓", "怎么称呼", "您怎么称呼"],
}
# 同字段提问 2+ 次 = D1, 3+ 次 = D2
D1_REPEAT_THRESHOLD = 2
D2_REPEAT_THRESHOLD = 3

# E 转人工
E_KEYWORDS = ["人工服务", "找人工", "转人工", "找真人", "要真人", "让真人"]

# F 测试问题: 故意试 AI 反应
F_KEYWORDS = ["喂喂喂", "测试一下", "你好你好你好", "1+1", "今天星期几"]

# G 粗鲁谩骂 (子类用多个字典)
G_SUBCATS = {
    "国骂": ["艹", "草", "操", "cao", "Cao", "他妈", "去他妈", "妈的", "卧槽", "我擦", "傻逼", "傻B", "sb", "SB"],
    "侮辱性": ["滚", "闭嘴", "蠢", "白痴", "傻"],
    "激烈拒绝": ["别打了", "神经病", "烦不烦", "再打报警", "骚扰"],
    "有病": ["有病", "疯了吗", "脑子有问题"],
}

# H 技术异常: 网络 / 信号问题
H_KEYWORDS = ["信号不好", "听不见", "听不清", "听不到", "信号差", "网络不好", "断断续续"]

# I 沉默压力: user 发言 ≤1 句 且 assistant 发言 ≥8 句
I_USER_MAX = 1
I_AGENT_MIN = 8


# ── 扫描器 ─────────────────────────────────────────────────

def _user_texts(transcript: list[dict]) -> list[str]:
    """提取 user 真实发言文本 (跳过空/系统兜底)."""
    out = []
    for t in transcript or []:
        if t.get("role") != "user":
            continue
        c = (t.get("content") or "").strip()
        if not c:
            continue
        # 跳过系统注入的静默兜底文本
        if "静默" in c or "兜底" in c or "未开口" in c:
            continue
        out.append(c)
    return out


def _agent_texts(transcript: list[dict]) -> list[str]:
    return [
        (t.get("content") or "").strip()
        for t in (transcript or [])
        if t.get("role") == "assistant" and (t.get("content") or "").strip()
    ]


def _match_keywords(texts: list[str], kws: list[str]) -> list[str]:
    """返回命中的关键词列表 (有重复就保留, 用于计数)."""
    hits = []
    joined = " || ".join(texts)
    for kw in kws:
        if kw in joined:
            hits.append(kw)
    return hits


def _count_field_repeats(agent_texts: list[str]) -> dict[str, int]:
    """统计 agent 对每个槽位字段的提问次数."""
    counts: dict[str, int] = {}
    for field, syns in D_REPEAT_FIELD_MAP.items():
        n = 0
        for txt in agent_texts:
            for syn in syns:
                if syn in txt:
                    n += 1
                    break
        counts[field] = n
    return counts


def detect_categories(transcript: list[dict]) -> dict:
    """对单通通话执行九维信号检测.

    返回结构:
    {
      "A": {"hit": True, "keywords": ["机器人", ...]},
      "B": {"hit": ..., "keywords": [...]},
      ...
      "I": {"hit": ..., "u_turns": 1, "a_turns": 9},
      "any_hit": bool,            # A-I 任一命中
      "hit_categories": ["A","D"], # 命中的分类列表
    }
    """
    u_texts = _user_texts(transcript)
    a_texts = _agent_texts(transcript)

    result = {}

    # A 口头识破
    a_hits = _match_keywords(u_texts, A_KEYWORDS)
    result["A"] = {"hit": bool(a_hits), "keywords": a_hits}

    # B 豪车 prank
    b_hits = _match_keywords(u_texts, B_KEYWORDS)
    result["B"] = {"hit": bool(b_hits), "keywords": b_hits}

    # C 反问 agent 个人
    c_hits = _match_keywords(u_texts, C_KEYWORDS)
    result["C"] = {"hit": bool(c_hits), "keywords": c_hits}

    # D 情绪/重复: 同字段被 agent 问多次
    field_counts = _count_field_repeats(a_texts)
    d2_fields = [f for f, n in field_counts.items() if n >= D2_REPEAT_THRESHOLD]
    d1_fields = [f for f, n in field_counts.items() if n >= D1_REPEAT_THRESHOLD and n < D2_REPEAT_THRESHOLD]
    d_hit = bool(d1_fields or d2_fields)
    d_subhit = "D2" if d2_fields else ("D1" if d1_fields else None)
    result["D"] = {
        "hit": d_hit,
        "field_counts": field_counts,
        "subcat": d_subhit,           # 'D1' / 'D2' / None
        "max_field": max(field_counts.items(), key=lambda kv: kv[1])[0] if any(field_counts.values()) else None,
    }

    # E 转人工
    e_hits = _match_keywords(u_texts, E_KEYWORDS)
    result["E"] = {"hit": bool(e_hits), "keywords": e_hits}

    # F 测试问题
    f_hits = _match_keywords(u_texts, F_KEYWORDS)
    result["F"] = {"hit": bool(f_hits), "keywords": f_hits}

    # G 粗鲁谩骂: 多子类
    g_subhits: dict[str, list[str]] = {}
    for sub, kws in G_SUBCATS.items():
        hits = _match_keywords(u_texts, kws)
        if hits:
            g_subhits[sub] = hits
    result["G"] = {"hit": bool(g_subhits), "subcats": g_subhits}

    # H 技术异常
    h_hits = _match_keywords(u_texts, H_KEYWORDS)
    result["H"] = {"hit": bool(h_hits), "keywords": h_hits}

    # I 沉默压力: u 发言 ≤1 且 a 发言 ≥8
    u_n = len(u_texts)
    a_n = len(a_texts)
    i_hit = (u_n <= I_USER_MAX) and (a_n >= I_AGENT_MIN)
    result["I"] = {"hit": i_hit, "u_turns": u_n, "a_turns": a_n}

    # 汇总
    hit_cats = [c for c in "ABCDEFGHI" if result[c]["hit"]]
    result["any_hit"] = bool(hit_cats)
    result["hit_categories"] = hit_cats
    return result


# ── 聚合 ────────────────────────────────────────────────────

def compute(df_enriched) -> dict:
    """对全量 df 跑 AI 感知分析, 输出适合前端展示的聚合结果.

    df_enriched 是 enrich() 后的 df, 至少含:
      _transcript / _human / _assistant_turns / Hangup Reason / Call ID / Agent Name / Duration (seconds)
    """
    total = len(df_enriched)

    # 漏斗
    answered_mask = df_enriched["Duration (seconds)"] > 0
    human_mask = df_enriched["_human"]
    # "有效对话" = 真人接听 + 至少 1 句真实 user 发言
    real_user_turns = df_enriched["_transcript"].apply(lambda t: len(_user_texts(t or [])))
    valid_mask = human_mask & (real_user_turns >= 1)

    # 对每个有效对话跑九维检测
    per_call_results = []
    for _, r in df_enriched.iterrows():
        is_valid = bool(human_mask.loc[r.name]) and len(_user_texts(r["_transcript"] or [])) >= 1
        det = detect_categories(r["_transcript"] or []) if is_valid else None
        per_call_results.append({
            "call_id": str(r.get("Call ID", "")),
            "agent_name": str(r.get("Agent Name", "")),
            "duration_s": int(r.get("Duration (seconds)", 0) or 0),
            "audio_url": str(r.get("Audio Record File Download URL", "") or ""),
            "is_valid": is_valid,
            "detect": det,
        })

    # 聚合: 各分类命中数 + 关键词分布
    per_cat_count: dict[str, int] = {c: 0 for c in "ABCDEFGHI"}
    per_cat_keywords: dict[str, Counter] = {c: Counter() for c in "ABCDEFGHI"}
    d_subcat_count = {"D1": 0, "D2": 0}
    d_field_kw_count = Counter()       # D 分类下 哪个字段被反复问的最多
    g_subcat_count: dict[str, int] = {sub: 0 for sub in G_SUBCATS.keys()}
    i_count = 0
    any_hit_count = 0
    hit_cases: list[dict] = []

    for pc in per_call_results:
        det = pc["detect"]
        if not det:
            continue
        if det["any_hit"]:
            any_hit_count += 1
            hit_cases.append({
                "call_id": pc["call_id"],
                "agent_name": pc["agent_name"],
                "duration_s": pc["duration_s"],
                "audio_url": pc["audio_url"],
                "hit_categories": det["hit_categories"],
                # snapshot 每个命中类的关键证据
                "evidence": {
                    c: {
                        k: v for k, v in det[c].items()
                        if k != "hit" and v
                    }
                    for c in det["hit_categories"]
                },
            })
        for cat in "ABCDEFGHI":
            if det[cat]["hit"]:
                per_cat_count[cat] += 1
        # A/B/C/E/F/H 关键词频次
        for cat in "ABCEFH":
            for kw in det[cat].get("keywords", []):
                per_cat_keywords[cat][kw] += 1
        # D 子类 + 字段
        if det["D"]["hit"]:
            sub = det["D"].get("subcat")
            if sub:
                d_subcat_count[sub] += 1
            for f, n in det["D"].get("field_counts", {}).items():
                if n >= D1_REPEAT_THRESHOLD:
                    d_field_kw_count[f] += 1
        # G 子类
        if det["G"]["hit"]:
            for sub, hits in det["G"].get("subcats", {}).items():
                if hits:
                    g_subcat_count[sub] += 1
                    per_cat_keywords["G"][sub] += len(hits)
        # I 不需要关键词

    n_valid = int(valid_mask.sum())
    n_human = int(human_mask.sum())
    n_answered = int(answered_mask.sum())

    return {
        "funnel": {
            "拨打": total,
            "接听": n_answered,
            "真人接听": n_human,
            "有效对话": n_valid,
            "用户明显感知 AI": any_hit_count,
        },
        "by_category": {
            "A": {"count": per_cat_count["A"], "title": "口头识破",       "keywords": dict(per_cat_keywords["A"].most_common(10))},
            "B": {"count": per_cat_count["B"], "title": "豪车 prank",     "keywords": dict(per_cat_keywords["B"].most_common(10))},
            "C": {"count": per_cat_count["C"], "title": "反问 agent 个人", "keywords": dict(per_cat_keywords["C"].most_common(10))},
            "D": {"count": per_cat_count["D"], "title": "不耐烦+反复问",
                   "subcat": d_subcat_count,
                   "field_keywords": dict(d_field_kw_count.most_common(10))},
            "E": {"count": per_cat_count["E"], "title": "主动要求真人",   "keywords": dict(per_cat_keywords["E"].most_common(10))},
            "F": {"count": per_cat_count["F"], "title": "探 AI 边界",     "keywords": dict(per_cat_keywords["F"].most_common(10))},
            "G": {"count": per_cat_count["G"], "title": "国骂/侮辱",
                   "subcat": g_subcat_count,
                   "keywords": dict(per_cat_keywords["G"].most_common(10))},
            "H": {"count": per_cat_count["H"], "title": "感知卡顿/信号",  "keywords": dict(per_cat_keywords["H"].most_common(10))},
            "I": {"count": per_cat_count["I"], "title": "≤1 句 user / ≥8 句 agent"},
        },
        "any_hit_count": any_hit_count,
        "any_hit_pct": round(any_hit_count / max(n_valid, 1) * 100, 2),
        "hit_cases": hit_cases,
        # debug 信息
        "_meta": {
            "n_total": total,
            "n_answered": n_answered,
            "n_human": n_human,
            "n_valid_conversation": n_valid,
        },
    }
