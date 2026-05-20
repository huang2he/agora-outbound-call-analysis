"""Agent-视角 KDA 计算模块（Tab 2 数据后端）— v2 重写。

核心思路：把"客观漏斗"（拨打 / 接听 / 真人）和 agent 表现解耦。
只在「有效会话」范围内分析 agent，避免首句挂断和系统兜底污染数据。

有效会话定义（user 锁定 5/18）：
  - 真人接听 (Hangup ∈ USER_HANGUP / AI_HANGUP)
  - 至少有 1 个"真实 user turn"
  - 真实 user turn = role=user AND 不是系统静默兜底 AND 不是 IVR/语音信箱

判断系统兜底 user turn 的特征（从真实数据观察）：
  1. metadata.source == "silence" → 静默超时系统注入的 think 占位
  2. content 含 "识别客户没有响应" / "走静默兜底" → 同上
  3. content 含 "请留下你的姓名" / "智语音留言" / "无法接听" / "帮你/您确认此人"
     / "是否方便接听" → IVR / 语音信箱机器音 ASR 识别出来的

4 关定义（Colin 5/17 锁定，5/18 修正第 1 关用 AND）：
  - 第 1 关 车型 = 购车品牌 AND 购车型号 都非空
  - 第 2 关 城市 = 购车城市 非空
  - 第 3 关 时间 = 购车时间 非空
  - 第 4 关 姓氏 = 购车姓名 非空

严格线性递进：过第 N 关 ⇔ 第 1..N 全过。每通用 passed_levels_count → 0..4。
"""

from __future__ import annotations

import json
import statistics
from typing import Any

import pandas as pd


# ─────────────────────── 关卡 / 字段映射 ────────────────────────

LEVEL_FIELDS: dict[int, dict[str, Any]] = {
    1: {"name": "车型", "fields": ["购车品牌", "购车型号"], "logic": "all"},   # AND
    2: {"name": "城市", "fields": ["购车城市"],              "logic": "all"},
    3: {"name": "时间", "fields": ["购车时间"],              "logic": "all"},
    4: {"name": "姓氏", "fields": ["购车姓名"],              "logic": "all"},
}
LEVEL_NAMES = [LEVEL_FIELDS[i]["name"] for i in (1, 2, 3, 4)]
INTENT_FIELD = "购车意向"  # 不进 4 关


# ─────────────────────── 系统兜底文本识别 ───────────────────────

# 系统注入 / IVR ASR 文本特征。匹配任一即视为非真实用户发言。
SYSTEM_USER_TURN_PATTERNS = [
    "识别客户没有响应",
    "走静默兜底",
    "请留下你的姓名",
    "请留下您的姓名",
    "请留下姓名",
    "智语音留言",
    "无法接听",
    "帮你确认此人",
    "帮您确认此人",
    "是否方便接听",
    "您拨打的电话",
    "已关机",
    "暂时无法接听",
    "稍后再拨",
    "录制留言",
]


def is_real_user_turn(turn: dict) -> bool:
    """user turn 是否算"客户真实开口"。

    排除两类：
    1. metadata.source == "silence" 的静默兜底占位
    2. 内容匹配 IVR / 语音信箱文本指纹
    """
    if turn.get("role") != "user":
        return False
    meta = turn.get("metadata") or {}
    if meta.get("source") == "silence":
        return False
    content = (turn.get("content") or "").strip()
    if not content:
        return False
    for pat in SYSTEM_USER_TURN_PATTERNS:
        if pat in content:
            return False
    return True


def real_user_turn_count(transcript: list[dict]) -> int:
    return sum(1 for t in transcript if is_real_user_turn(t))


def is_valid_session(transcript: list[dict]) -> bool:
    """有效会话 ⇔ 至少 1 个真实 user turn。"""
    return real_user_turn_count(transcript) >= 1


def total_turn_count(transcript: list[dict]) -> int:
    """通话的总轮次 (assistant + user)，作为"花了几轮"的度量。"""
    return sum(1 for t in transcript if t.get("role") in ("assistant", "user"))


def max_turn_id(transcript: list[dict]) -> int:
    ids = [t.get("turn_id") for t in transcript if isinstance(t.get("turn_id"), int)]
    return max(ids) if ids else 0


# ──────── 客户首句开局分类 (启发式) ─────────
# 看客户第 1 个"真实 user turn"的 content 关键词
FIRST_WORD_CATEGORIES = [
    # (category_name, [keyword, ...]) - 顺序优先
    ("积极线索", [
        "想买", "考虑", "考虑买", "正在看", "想看", "在看", "打算买", "准备买",
        "什么车", "什么型号", "什么品牌", "多少钱", "什么价",
        # 主流品牌：客户主动提名一般是积极信号
        "宝马", "奔驰", "奥迪", "丰田", "本田", "大众", "比亚迪", "理想", "蔚来",
        "小鹏", "特斯拉", "保时捷", "雷克萨斯", "凯迪拉克", "沃尔沃", "捷豹", "路虎",
        "卡宴", "极氪", "红旗", "长安", "吉利", "五菱", "哈弗", "传祺", "迈巴赫",
    ]),
    ("不友善", [
        "操", "傻逼", "滚", "妈了个", "草泥", "去你", "煞笔",
    ]),
    ("怀疑拒绝", [
        "不需要", "不买", "不考虑", "已经买", "买好了", "不想", "不感兴趣",
        "别打了", "别再打", "没钱", "没意向", "没空", "没时间",
        "你哪位", "哪里", "你谁", "你是谁", "什么公司", "怎么知道", "诈骗", "骚扰",
    ]),
]


def classify_first_word(transcript: list[dict]) -> str:
    """看客户第一个真实 user turn 的内容关键词，返回首句开局类别。

    返回: '积极线索' / '怀疑拒绝' / '不友善' / '中性问询' (默认)
    """
    if not transcript:
        return "中性问询"
    for t in transcript:
        if not is_real_user_turn(t):
            continue
        content = str(t.get("content") or "")
        # 顺序优先匹配
        for cat, kws in FIRST_WORD_CATEGORIES:
            if any(kw in content for kw in kws):
                return cat
        return "中性问询"
    return "中性问询"


# ────────────────────────── 工具函数 ──────────────────────────

def parse_structured_output(raw: Any) -> dict[str, Any]:
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


def parse_transcript(raw: Any) -> list[dict]:
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


def _val_filled(v: Any) -> bool:
    return v is not None and str(v).strip() not in ("", "null", "None")


def passed_level(so: dict, level: int) -> bool:
    cfg = LEVEL_FIELDS.get(level)
    if not cfg:
        return False
    checks = [_val_filled(so.get(f)) for f in cfg["fields"]]
    return all(checks) if cfg["logic"] == "all" else any(checks)


def passed_levels_count(so: dict) -> int:
    """按严格线性，0..4。第 1 关没过就停在 0。"""
    n = 0
    for level in (1, 2, 3, 4):
        if passed_level(so, level):
            n = level
        else:
            break
    return n


def filled_slots_independent(so: dict) -> set[str]:
    """4 关各自独立的填充情况（不要求线性）。给"缺哪个槽位"统计用。"""
    return {LEVEL_FIELDS[i]["name"] for i in (1, 2, 3, 4) if passed_level(so, i)}


def is_human_answered(hangup: Any) -> bool:
    return str(hangup or "").strip() in ("USER_HANGUP", "AI_HANGUP")


# ─────────────────────── 0 关未通关原因（启发式） ───────────────────────

# 用户拒绝 / 没意向相关的关键词
REJECT_KEYWORDS = ["不需要", "不买", "不考虑", "已经买", "买好了", "买了", "没意向",
                    "不想", "不感兴趣", "别打了", "别再打"]
BUSY_KEYWORDS   = ["在开车", "正忙", "在上班", "在开会", "在外面", "等会", "不方便"]
ANGRY_KEYWORDS  = ["操", "傻逼", "滚", "妈了个", "草泥"]
QUERY_KEYWORDS  = ["哪里来", "什么公司", "你哪位", "你谁", "怎么知道我"]


def classify_zero_pass_reason(transcript: list[dict]) -> str:
    """0 关通话的启发式原因分类。看真实 user turn 里的关键词。

    返回中文标签：'拒绝/无意向' / '忙/没空' / '客户骂人' / '质疑身份' / '聊别的/未触及' / '其他'
    """
    real_turns = [t for t in transcript if is_real_user_turn(t)]
    if not real_turns:
        return "其他"
    text = " ".join((t.get("content") or "") for t in real_turns)
    if any(kw in text for kw in ANGRY_KEYWORDS):
        return "客户骂人"
    if any(kw in text for kw in REJECT_KEYWORDS):
        return "拒绝/无意向"
    if any(kw in text for kw in BUSY_KEYWORDS):
        return "忙/没空"
    if any(kw in text for kw in QUERY_KEYWORDS):
        return "质疑身份"
    return "聊别的/未触及"


# ─────────────────────── 主入口：Tab 2 数据计算 ───────────────────────

def compute_efficiency_metrics(df: pd.DataFrame) -> dict:
    """三类 agent-视角效率画像 (纯统计, 不调 LLM):

    1. 机会浪费率 (Opportunity Waste Rate):
       客户在通话里至少开口了 N 句, 但 agent 一关都没问到的占比.
       这是 agent "把握对话机会" 的能力衡量.
       N = 3 (默认门槛).

    2. 槽位采集效率 (Slot Collection Efficiency):
       4 关全过的通话里, 4 (槽位) / max_turn_id 的均值.
       数字越高 = agent 越快收齐 4 关 (短平快).

    3. 首句开局画像 (First-Word Profile):
       按客户第 1 个真实发言的关键词分 4 类, 看 agent 在每种开局下
       的通过分布. 区分 "agent 把好开局聊砸了" vs "agent 把烂开局救回来了".

    所有指标都在 "有效会话" 范围内计算 (排除首句挂断 + 接通无应答).
    输入 df 需要预先做过 _human / _valid / _real_user_turns / _pass_n / _max_turn /
    _transcript 等标注 (见 compute_tab2_data 里的预处理).
    """
    valid = df[df["_valid"]]
    n_valid = len(valid)
    if n_valid == 0:
        return {
            "n_valid": 0,
            "opportunity": {"threshold": 3, "n_high_chance": 0, "n_wasted": 0, "rate": 0.0},
            "collection": {"n_full": 0, "slots_per_turn": 0.0, "median_turns_full": 0},
            "first_word": [],
        }

    # ── 1. 机会浪费率 ───────────────────────────────
    OPPORTUNITY_THRESHOLD = 3
    high_chance = valid[valid["_real_user_turns"] >= OPPORTUNITY_THRESHOLD]
    n_high = len(high_chance)
    n_wasted = int((high_chance["_pass_n"] == 0).sum())
    opportunity = {
        "threshold": OPPORTUNITY_THRESHOLD,
        "n_high_chance": n_high,
        "n_wasted": n_wasted,
        "rate": round(n_wasted / n_high * 100, 1) if n_high else 0.0,
        # 给个 by-pass-n 细分: 客户开口 ≥3 句的通话里, pass_n 怎么分布
        "by_pass_n": {
            i: int((high_chance["_pass_n"] == i).sum()) for i in range(5)
        },
    }

    # ── 2. 槽位采集效率 ────────────────────────────
    full_calls = valid[valid["_pass_n"] == 4]
    n_full = len(full_calls)
    if n_full:
        # 4 / max_turn_id, 然后取均值
        full_turns = full_calls["_max_turn"].clip(lower=1)
        slots_per_turn_series = 4 / full_turns
        slots_per_turn = round(float(slots_per_turn_series.mean()), 3)
        median_turns = int(full_turns.median())
        # 全过通话里 max_turn 最小的 5 通 (最高效的通)
        top_eff = full_calls.nsmallest(5, "_max_turn")
        top_eff_calls = top_eff["Call ID"].tolist() if "Call ID" in top_eff.columns else []
    else:
        slots_per_turn = 0.0
        median_turns = 0
        top_eff_calls = []
    collection = {
        "n_full": n_full,
        "slots_per_turn": slots_per_turn,
        "median_turns_full": median_turns,
        "top_efficient_call_ids": top_eff_calls,
    }

    # ── 3. 首句开局画像 ────────────────────────────
    fw_rows = []
    categories = ["积极线索", "中性问询", "怀疑拒绝", "不友善"]
    fw_classify = valid["_transcript"].apply(classify_first_word)
    for cat in categories:
        sub = valid[fw_classify == cat]
        cnt = len(sub)
        if cnt == 0:
            fw_rows.append({"category": cat, "count": 0, "pct": 0.0, "by_pass_n": {i: 0 for i in range(5)},
                            "pass_rate": 0.0, "avg_pass_n": 0.0})
            continue
        by_pn = {i: int((sub["_pass_n"] == i).sum()) for i in range(5)}
        pass_rate = (cnt - by_pn[0]) / cnt * 100   # 至少过 1 关的比例
        avg_pn = float(sub["_pass_n"].mean())
        fw_rows.append({
            "category": cat,
            "count": cnt,
            "pct": round(cnt / n_valid * 100, 1),
            "by_pass_n": by_pn,
            "pass_rate": round(pass_rate, 1),
            "avg_pass_n": round(avg_pn, 2),
        })

    return {
        "n_valid": n_valid,
        "opportunity": opportunity,
        "collection": collection,
        "first_word": fw_rows,
    }


def compute_tab2_data(df: pd.DataFrame) -> dict:
    """整张 Tab 2 的数据后端入口。

    输出形如:
    {
        "global": {
            "n_total": 1805,
            "n_human": 1722,
            "n_valid": 622,
            "n_first_hangup": 1100,
            "n_silence_or_ivr": 0,
            "bucket": [
                {"level": 0, "count": 320, "pct": 0.51, "avg_turns": 3.2, "avg_duration": 18.5,
                 "reasons": {"拒绝/无意向": 180, ...}},
                {"level": 1, ...},
                ...
            ],
            "slot_pass_in_valid": {"车型": 88, "城市": 64, "时间": 41, "姓氏": 23},
        },
        "agents": [
            {"agent": "汽车营销项目-5/14-Agent1-hz", ...same shape as global...},
            ...
        ]
    }
    """
    # 解析 transcript / structured（一次性，按行）
    work = df.copy()
    work["_transcript"] = work["Transcript"].apply(parse_transcript)
    work["_structured"] = work["Structured Output"].apply(parse_structured_output)
    work["_human"] = work["Hangup Reason"].apply(is_human_answered)
    work["_assistant_turns"] = work["_transcript"].apply(
        lambda t: sum(1 for x in t if x.get("role") == "assistant"))
    work["_real_user_turns"] = work["_transcript"].apply(real_user_turn_count)
    work["_valid"] = work["_human"] & (work["_real_user_turns"] >= 1)
    work["_pass_n"] = work["_structured"].apply(passed_levels_count)
    work["_total_turns"] = work["_transcript"].apply(total_turn_count)
    work["_max_turn"] = work["_transcript"].apply(max_turn_id)
    work["Duration (seconds)"] = pd.to_numeric(work["Duration (seconds)"], errors="coerce").fillna(0)

    return {
        "global": {
            **_bucket_breakdown(work, label="全部"),
            "efficiency": compute_efficiency_metrics(work),
        },
        "agents": [
            {
                **_bucket_breakdown(sub, label=name),
                "efficiency": compute_efficiency_metrics(sub),
            }
            for name, sub in work.groupby("Agent Name")
        ],
        "level_fields": {
            str(k): {"name": v["name"], "fields": v["fields"], "logic": v["logic"]}
            for k, v in LEVEL_FIELDS.items()
        },
    }


def _bucket_breakdown(df: pd.DataFrame, label: str) -> dict:
    """对一个 df（全部 or 单个 Agent）按通关数 0..4 分桶。"""
    n_total = len(df)
    n_human = int(df["_human"].sum())
    valid = df[df["_valid"]].copy()
    n_valid = len(valid)
    # 客户全程未开口 = 真人接听 AND 真实 user turn 数 == 0
    # 互斥拆成 2 个子类，使 n_first_hangup + n_silence_or_ivr + n_valid = n_human
    n_first_hangup   = int((df["_human"] & (df["_real_user_turns"] == 0) & (df["_assistant_turns"] == 1)).sum())
    n_silence_or_ivr = int((df["_human"] & (df["_real_user_turns"] == 0) & (df["_assistant_turns"]  > 1)).sum())
    n_silent_total   = n_first_hangup + n_silence_or_ivr

    # 4 关各自独立通过率（在有效会话内）
    slot_pass = {name: 0 for name in LEVEL_NAMES}
    for so in valid["_structured"]:
        for name in filled_slots_independent(so):
            slot_pass[name] += 1

    buckets = []
    for level in (0, 1, 2, 3, 4):
        sub = valid[valid["_pass_n"] == level]
        cnt = len(sub)
        if cnt:
            avg_turns = round(float(sub["_total_turns"].mean()), 2)
            avg_duration = round(float(sub["Duration (seconds)"].mean()), 1)
            avg_real_user = round(float(sub["_real_user_turns"].mean()), 2)
        else:
            avg_turns = avg_duration = avg_real_user = 0.0

        # 槽位填充情况 (仅对 1/2/3 关，因为 0 关全无、4 关全有)
        slot_fill = {name: 0 for name in LEVEL_NAMES}
        for so in sub["_structured"]:
            for name in filled_slots_independent(so):
                slot_fill[name] += 1

        # 0 关原因分布
        reasons = {}
        if level == 0:
            from collections import Counter
            r = Counter(classify_zero_pass_reason(t) for t in sub["_transcript"])
            reasons = dict(r.most_common())

        buckets.append({
            "level": level,
            "count": cnt,
            "pct_of_valid": round(cnt / n_valid * 100, 1) if n_valid else 0.0,
            "avg_turns": avg_turns,
            "avg_duration": avg_duration,
            "avg_real_user_turns": avg_real_user,
            "slot_fill": slot_fill,
            "reasons": reasons,
        })

    return {
        "label": label,
        "n_total": n_total,
        "n_human": n_human,
        "n_valid": n_valid,
        "n_first_hangup": n_first_hangup,
        "n_silence_or_ivr": n_silence_or_ivr,
        "n_silent_total": n_silent_total,
        "valid_rate_of_human": round(n_valid / n_human * 100, 1) if n_human else 0.0,
        "slot_pass_in_valid": slot_pass,
        "buckets": buckets,
    }
