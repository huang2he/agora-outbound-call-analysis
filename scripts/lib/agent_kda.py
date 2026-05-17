"""Agent-视角 KDA 计算模块（Tab 2 数据后端）。

设计文档：
  20_项目/Agora 外呼分析看板/Agent-视角指标设计.md  （6 维度公式 § 3.2）
  20_项目/Agora 外呼分析看板/Tab2 实现计划.md         （字段映射 + 工时）

关键设计决策（v0）：
  - 第 1 关「车型」用 AND（购车品牌 AND 购车型号 都非空），依据 [[销冠Prompt v0]]
    的硬下限"品牌+车型 缺一不可"。**这与 Tab 1 老的"或"口径不同**，故 Tab 1
    现有 `is_full_with_model` 等逻辑保持不变，Tab 2 用本模块独立判定。
  - 维度 3「首杀」/ 维度 4「滑顺」用关键词正则做近似，v1 再上 LLM。
  - 通关判定严格线性（第 N 关 = 第 1..N 全过），与 Colin"一关关过"心智模型一致。
"""

from __future__ import annotations

import json
import re
import statistics
from typing import Any

import pandas as pd


# ─────────────────────── 关卡 / 字段映射 ────────────────────────

# 第 N 关 → 涉及的 Structured Output 字段及组合逻辑
LEVEL_FIELDS: dict[int, dict[str, Any]] = {
    1: {"name": "车型", "fields": ["购车品牌", "购车型号"], "logic": "all"},   # AND
    2: {"name": "城市", "fields": ["购车城市"],              "logic": "all"},
    3: {"name": "时间", "fields": ["购车时间"],              "logic": "all"},
    4: {"name": "姓氏", "fields": ["购车姓名"],              "logic": "all"},
}
PROVINCE_FALLBACK_FIELD = "购车省份"     # 城市的半通关辅助
INTENT_FIELD            = "购车意向"     # 不进 4 关，Tab 1 用

# 维度参数（计划文档 §3.2 锁定）
TURNS_FULL_PASS_MIN    = 5    # 5 轮拿全 = 100
TURNS_FULL_PASS_MAX    = 15   # 15 轮 = 0
FIRST_HIT_T1_WEIGHT    = 15
FIRST_HIT_T2_WEIGHT    = 8
FRICTION_MULTIPLIER    = 20
VARIANCE_MULTIPLIER    = 1000
EARLY_HANGUP_MAX_TURNS = 3    # assistant 轮数 ≤ 3 算"早挂断"

# 综合分加权
COMPOSITE_WEIGHTS: dict[str, float] = {
    "击穿率": 0.30, "轮效":   0.20, "首杀":   0.15,
    "滑顺":   0.15, "不偏科": 0.10, "抗挂":   0.10,
}

# 关键词词典（v0 近似）：扫 transcript 找"agent 问到 + 用户实质给答"
# 用 user turn 出现关键词作为命中标志（用户主动给出 = 信号最强）
LEVEL_KEYWORDS_USER: dict[int, list[str]] = {
    1: [  # 车型：车系/品牌/型号 词
        "宝马", "奔驰", "奥迪", "丰田", "本田", "大众", "比亚迪", "理想", "蔚来",
        "小鹏", "特斯拉", "保时捷", "雷克萨斯", "凯迪拉克", "沃尔沃", "捷豹", "路虎",
        "卡宴", "极氪", "红旗", "长安", "吉利", "五菱", "哈弗", "传祺",
    ],
    2: [  # 城市
        "上海", "北京", "广州", "深圳", "杭州", "成都", "重庆", "南京", "武汉", "西安",
        "天津", "苏州", "郑州", "长沙", "青岛", "宁波", "无锡", "佛山", "东莞",
        "合肥", "厦门", "济南", "福州", "贵阳", "昆明", "南宁", "兰州",
    ],
    3: [  # 时间
        "月", "年底", "今年", "明年", "马上", "近期", "尽快", "下周", "下月", "立刻",
        "半年", "一个月", "三个月", "现在", "随时", "等等", "考虑",
    ],
    4: [  # 姓氏
        "姓", "我姓", "免贵", "我叫", "先生", "女士", "小姐",
    ],
}

# Assistant 问询关键词（用来检测维度 4 "反复问"的 friction）
LEVEL_KEYWORDS_ASSISTANT: dict[int, list[str]] = {
    1: ["什么车", "哪款", "什么品牌", "什么车型", "您看重哪", "想买什么"],
    2: ["哪个城市", "哪儿", "在哪", "哪里上牌", "上牌"],
    3: ["什么时候", "几月", "什么时间", "近期", "什么时候买"],
    4: ["怎么称呼", "贵姓", "您姓", "怎么称呼您"],
}


# ────────────────────────── 工具函数 ──────────────────────────

def parse_structured_output(raw: Any) -> dict[str, Any]:
    """容错解析 Structured Output 列。空 / 非法 / 'null' / 非 dict → {}."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    s = str(raw).strip()
    if not s or s.lower() == "null":
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _val_filled(v: Any) -> bool:
    return v is not None and str(v).strip() not in ("", "null", "None")


def passed_level(so: dict, level: int) -> bool:
    """单通是否过第 N 关。`so` 是已解析的 Structured Output dict。"""
    cfg = LEVEL_FIELDS.get(level)
    if not cfg:
        return False
    checks = [_val_filled(so.get(f)) for f in cfg["fields"]]
    if cfg["logic"] == "all":
        return all(checks)
    if cfg["logic"] == "any":
        return any(checks)
    return False


def passed_levels_count(so: dict) -> int:
    """该通按严格线性递进 (第 N 关需要 1..N 全过) 通过到第几关。返回 0..4."""
    n = 0
    for level in (1, 2, 3, 4):
        if passed_level(so, level):
            n = level
        else:
            break
    return n


def is_full_pass(so: dict) -> bool:
    return passed_levels_count(so) == 4


def is_human_answered(hangup: Any) -> bool:
    return str(hangup or "").strip() in ("USER_HANGUP", "AI_HANGUP")


# ─────────────────── 维度 3 首杀 (v0 关键词近似) ───────────────────

def _user_first_hit_turn(transcript: list[dict], level: int) -> int | None:
    """扫 transcript 找用户首次说出第 N 关关键词所在的 turn_id。"""
    kws = LEVEL_KEYWORDS_USER.get(level, [])
    if not kws:
        return None
    for t in transcript:
        if t.get("role") != "user":
            continue
        content = str(t.get("content", ""))
        if any(kw in content for kw in kws):
            tid = t.get("turn_id")
            if isinstance(tid, int):
                return tid
    return None


def turn_at_level_hit(transcript: list[dict], level: int) -> int | None:
    """对外接口：第 N 关字段在 transcript 中首次"命中"的 turn_id（v0 关键词近似）。
    无 transcript / 没命中 → None。"""
    return _user_first_hit_turn(transcript or [], level)


# ─────────────────── 维度 4 滑顺 (v0 重复问近似) ───────────────────

def detect_friction(transcript: list[dict]) -> dict[int, int]:
    """v0：assistant turn 中第 N 关问询关键词出现次数 ≥ 2 → 记一次 friction。
    返回 {1: 0/1, 2: 0/1, 3: 0/1, 4: 0/1}."""
    counts = {1: 0, 2: 0, 3: 0, 4: 0}
    if not transcript:
        return counts
    for level in (1, 2, 3, 4):
        kws = LEVEL_KEYWORDS_ASSISTANT[level]
        hits = 0
        for t in transcript:
            if t.get("role") != "assistant":
                continue
            content = str(t.get("content", ""))
            if any(kw in content for kw in kws):
                hits += 1
                if hits >= 2:
                    counts[level] = 1
                    break
    return counts


# ─────────────────────── 产品视角 7 层漏斗 ───────────────────────

def compute_product_funnel(df: pd.DataFrame) -> dict:
    """7 层产品视角漏斗：拨打 → 接听 → 真人 → 过L1 → 过L2 → 过L3 → 全关"""
    if df.empty:
        return {"layers": [("拨打", 0), ("接听", 0), ("真人", 0),
                            ("过车型关", 0), ("过城市关", 0),
                            ("过时间关", 0), ("全关通过", 0)]}

    total = len(df)
    dur = pd.to_numeric(df["Duration (seconds)"], errors="coerce").fillna(0)
    answered = int((dur > 0).sum())
    human_mask = df["Hangup Reason"].apply(is_human_answered)
    human = int(human_mask.sum())

    so_parsed = df["Structured Output"].apply(parse_structured_output)

    # 严格线性：过第 N 关 ⇔ 1..N 全过
    pass_n = so_parsed.apply(passed_levels_count)
    l1 = int((pass_n >= 1).sum())
    l2 = int((pass_n >= 2).sum())
    l3 = int((pass_n >= 3).sum())
    l4 = int((pass_n >= 4).sum())
    return {
        "layers": [
            ("拨打", total),
            ("接听", answered),
            ("真人", human),
            ("过车型关", l1),
            ("过城市关", l2),
            ("过时间关", l3),
            ("全关通过", l4),
        ]
    }


# ─────────────────────── 6 维度雷达计算 ───────────────────────

def _safe_score(x: float) -> int:
    return max(0, min(100, int(round(x))))


def compute_agent_radar(df_agent: pd.DataFrame) -> dict:
    """一个 Agent Name 的所有 calls → 6 维度 + _raw 细节。

    所有维度都在"真人接听"范围内算（除维度 6"抗挂"本身用早挂断率，也基于真人接听）。
    如果某 Agent 没有真人接听，所有维度回退 0。
    """
    empty_raw = {
        "n_calls": len(df_agent), "n_human": 0, "pass_full_rate": 0.0,
        "avg_turns_when_full": 0.0,
        "L1_pass": 0.0, "L2_pass": 0.0, "L3_pass": 0.0, "L4_pass": 0.0,
        "avg_T1": None, "avg_T2": None,
        "avg_friction": 0.0,
        "early_hangup_rate": 0.0,
    }

    if df_agent.empty:
        return {**{k: 0 for k in COMPOSITE_WEIGHTS}, "_raw": empty_raw}

    # 准备衍生列（不改原 df）
    human_mask = df_agent["Hangup Reason"].apply(is_human_answered)
    human_df = df_agent[human_mask].copy()
    n_human = len(human_df)
    if n_human == 0:
        return {**{k: 0 for k in COMPOSITE_WEIGHTS}, "_raw": {**empty_raw, "n_human": 0}}

    so_parsed = human_df["Structured Output"].apply(parse_structured_output)
    pass_n = so_parsed.apply(passed_levels_count)

    # ── 维度 1 击穿率 ──
    n_full = int((pass_n == 4).sum())
    pass_full_rate = n_full / n_human
    s1 = _safe_score(pass_full_rate * 100)

    # ── 维度 2 轮效 ──（仅全过通话）
    if n_full > 0:
        transcripts = human_df.loc[pass_n == 4, "Transcript"].apply(_parse_transcript)
        max_turns = transcripts.apply(_max_turn).tolist()
        max_turns = [t for t in max_turns if t > 0]
        avg_turns = statistics.mean(max_turns) if max_turns else 0
        # 5 轮拿全 = 100, 15 轮 = 0
        s2 = _safe_score(100 - max(0, avg_turns - TURNS_FULL_PASS_MIN) * (100 / (TURNS_FULL_PASS_MAX - TURNS_FULL_PASS_MIN)))
    else:
        avg_turns = 0
        s2 = 0

    # ── 维度 3 首杀 ──（v0 关键词近似，对全部真人接听算）
    transcripts_all = human_df["Transcript"].apply(_parse_transcript)
    t1_hits = transcripts_all.apply(lambda t: turn_at_level_hit(t, 1))
    t2_hits = transcripts_all.apply(lambda t: turn_at_level_hit(t, 2))
    t1_vals = [v for v in t1_hits if isinstance(v, int)]
    t2_vals = [v for v in t2_hits if isinstance(v, int)]
    avg_t1 = statistics.mean(t1_vals) if t1_vals else None
    avg_t2 = statistics.mean(t2_vals) if t2_vals else None
    # 没命中视为最大惩罚（用 avg_turns 兜底，或干脆给 0 分项）
    penalty_t1 = (avg_t1 if avg_t1 is not None else 10) * FIRST_HIT_T1_WEIGHT / 10
    penalty_t2 = (avg_t2 if avg_t2 is not None else 10) * FIRST_HIT_T2_WEIGHT / 10
    s3 = _safe_score(100 - penalty_t1 - penalty_t2)

    # ── 维度 4 滑顺 ──（friction = 重复问的关数 0..4）
    fric_df = transcripts_all.apply(detect_friction)
    fric_per_call = fric_df.apply(lambda d: sum(d.values()))
    avg_friction = float(fric_per_call.mean()) if len(fric_per_call) else 0.0
    s4 = _safe_score(100 - avg_friction * FRICTION_MULTIPLIER)

    # ── 维度 5 不偏科 ──（4 关各自通过率的方差）
    l1p = int(so_parsed.apply(lambda s: passed_level(s, 1)).sum()) / n_human
    l2p = int(so_parsed.apply(lambda s: passed_level(s, 2)).sum()) / n_human
    l3p = int(so_parsed.apply(lambda s: passed_level(s, 3)).sum()) / n_human
    l4p = int(so_parsed.apply(lambda s: passed_level(s, 4)).sum()) / n_human
    var = statistics.pvariance([l1p, l2p, l3p, l4p])
    s5 = _safe_score(100 - var * VARIANCE_MULTIPLIER)

    # ── 维度 6 抗挂 ──（assistant 轮数 ≤ 3 算早挂断）
    asst_turns = transcripts_all.apply(_assistant_turn_count)
    early = int((asst_turns <= EARLY_HANGUP_MAX_TURNS).sum())
    early_rate = early / n_human
    s6 = _safe_score((1 - early_rate) * 100)

    composite = sum(
        v * COMPOSITE_WEIGHTS[k]
        for k, v in zip(("击穿率", "轮效", "首杀", "滑顺", "不偏科", "抗挂"),
                         (s1, s2, s3, s4, s5, s6))
    )

    return {
        "击穿率": s1, "轮效": s2, "首杀": s3,
        "滑顺": s4, "不偏科": s5, "抗挂": s6,
        "综合分": _safe_score(composite),
        "_raw": {
            "n_calls": len(df_agent),
            "n_human": n_human,
            "n_full": n_full,
            "pass_full_rate": round(pass_full_rate, 4),
            "avg_turns_when_full": round(avg_turns, 2),
            "L1_pass": round(l1p, 4),
            "L2_pass": round(l2p, 4),
            "L3_pass": round(l3p, 4),
            "L4_pass": round(l4p, 4),
            "avg_T1": round(avg_t1, 2) if avg_t1 is not None else None,
            "avg_T2": round(avg_t2, 2) if avg_t2 is not None else None,
            "avg_friction": round(avg_friction, 3),
            "early_hangup_rate": round(early_rate, 4),
        },
    }


def compute_agent_ranking(df: pd.DataFrame) -> list[dict]:
    """对每个 Agent Name 跑 radar，按综合分降序排，返回 list[dict]."""
    rows: list[dict] = []
    for name, sub in df.groupby("Agent Name"):
        r = compute_agent_radar(sub)
        r["agent"] = str(name)
        rows.append(r)
    rows.sort(key=lambda x: -x.get("综合分", 0))
    return rows


# ───────────────────── transcript 工具内联 ─────────────────────
# 注意：build_dashboard.py 也有自己的 transcript 工具。这里独立一份避免循环 import。

def _parse_transcript(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    s = str(raw).strip()
    if not s or s == "[]":
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _max_turn(transcript: list[dict]) -> int:
    ids = [t.get("turn_id") for t in transcript if isinstance(t.get("turn_id"), int)]
    return max(ids) if ids else 0


def _assistant_turn_count(transcript: list[dict]) -> int:
    return sum(1 for t in transcript if t.get("role") == "assistant")
