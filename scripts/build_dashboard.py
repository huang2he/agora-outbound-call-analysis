#!/usr/bin/env python3
"""Build an ECharts HTML dashboard from an Agora ConvoAI outbound-call summary.

Input: CSV / XLSX exported from Agora ConvoAI Console (one row per call).
Required columns:
  Agent ID, Agent Name, Duration (seconds), Transcript,
  Structured Output, Hangup Reason

Metric definitions (locked with the user):
  - 接听 (answered)            = Duration > 0
  - 真人接听 (human answered)  = Hangup Reason in {USER_HANGUP, AI_HANGUP}
  - 完整转换 (full conversion) = Structured Output non-empty AND no field is null/empty
  - 意向客户 (intent)          = Structured Output contains 购车意向 == "是"
  - 首句挂断                    = 真人接听 AND assistant turn count == 1
  - 2 句挂断                    = 真人接听 AND assistant turn count == 2  (exact)
  - 3 句挂断                    = 真人接听 AND assistant turn count == 3  (exact)
  - 轮次                        = max turn_id in transcript

The dashboard has a "全部 / by Agent Name" dropdown that re-filters every chart.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

REQUIRED_COLS = [
    "Agent ID",
    "Agent Name",
    "Duration (seconds)",
    "Transcript",
    "Structured Output",
    "Hangup Reason",
]
HUMAN_HANGUP = {"USER_HANGUP", "AI_HANGUP"}
ALL_KEY = "__ALL__"
ALL_LABEL = "全部"


# ---------- parsing ----------

def load_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str)
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise SystemExit(
            f"CSV/XLSX is missing required columns: {missing}\n"
            f"Available: {list(df.columns)}"
        )
    df["Duration (seconds)"] = pd.to_numeric(df["Duration (seconds)"], errors="coerce").fillna(0).astype(int)
    df["Hangup Reason"] = df["Hangup Reason"].fillna("").astype(str)
    df["Agent Name"] = df["Agent Name"].fillna("(unknown)").astype(str)
    return df


def parse_transcript(raw: str) -> list[dict]:
    if not raw or str(raw).strip() in {"", "[]"}:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def parse_structured(raw: str) -> dict | None:
    if not raw or not str(raw).strip():
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def assistant_turn_count(transcript: list[dict]) -> int:
    return sum(1 for t in transcript if t.get("role") == "assistant")


def max_turn_id(transcript: list[dict]) -> int:
    ids = [t.get("turn_id") for t in transcript if isinstance(t.get("turn_id"), int)]
    return max(ids) if ids else 0


# Conversion slot model (locked with user):
# 4 slots — 车型 / 时间 / 城市 / 姓名. 车型 槽 is satisfied if EITHER 购车品牌 OR
# 购车型号 is non-null (they're conceptually linked — either alone is enough to
# pass the lead to a 4S 店, since 4S 店 can clarify on follow-up). Intent
# (购车意向) is NOT a conversion slot — it's a separate funnel branch.
CONVERSION_SLOT_NAMES = ["车型", "时间", "城市", "姓名"]
CONVERSION_SLOT_FIELDS: dict[str, list[str]] = {
    "车型": ["购车品牌", "购车型号"],   # any one non-null → slot filled
    "时间": ["购车时间"],
    "城市": ["购车城市"],
    "姓名": ["购车姓名"],
}
FULL_CONVERSION_MIN = 3  # ≥ 3 of 4 slots filled → counts as 完整转换


def _val_filled(v) -> bool:
    return v is not None and str(v).strip() != ""


def filled_slots(structured: dict | None) -> list[str]:
    """Return the names of the conversion slots that are filled in this row."""
    if not structured:
        return []
    return [
        name for name in CONVERSION_SLOT_NAMES
        if any(_val_filled(structured.get(f)) for f in CONVERSION_SLOT_FIELDS[name])
    ]


def is_full_conversion(structured: dict | None) -> bool:
    """≥ 3 of the 4 conversion slots filled."""
    return len(filled_slots(structured)) >= FULL_CONVERSION_MIN


def is_full_with_model(structured: dict | None) -> bool:
    """完整转换 (≥3 槽位) 且 车型槽 (购车型号) 已填。子集，比纯字段数更刚性 —
    没有车型的"完整转换"对销售线索基本没用。"""
    slots = filled_slots(structured)
    return len(slots) >= FULL_CONVERSION_MIN and "车型" in slots


def is_intent(structured: dict | None) -> bool:
    """购车意向 字段 == "是"。注意：字段也会被填 "否" (客户明确拒绝)，所以仅看
    "非 null" 会把拒绝的客户也算成意向。漏斗"意向客户"只算明确说要买的。"""
    if not structured:
        return False
    return str(structured.get("购车意向", "")).strip() == "是"


def collected_field_count(structured: dict | None) -> int:
    """How many of the 4 conversion slots are filled (0..4). Used by the
    "完整转换分布" bar chart. 购车意向 is intentionally NOT a slot here."""
    return len(filled_slots(structured))


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["_transcript"] = df["Transcript"].apply(parse_transcript)
    df["_structured"] = df["Structured Output"].apply(parse_structured)
    df["_assistant_turns"] = df["_transcript"].apply(assistant_turn_count)
    df["_max_turn_id"] = df["_transcript"].apply(max_turn_id)
    df["_answered"] = df["Duration (seconds)"] > 0
    df["_human"] = df["Hangup Reason"].isin(HUMAN_HANGUP)
    df["_filled_slots"] = df["_structured"].apply(filled_slots)
    df["_field_count"] = df["_filled_slots"].apply(len)
    df["_full"] = df["_field_count"] >= FULL_CONVERSION_MIN
    df["_full_with_model"] = df.apply(
        lambda r: r["_full"] and "车型" in r["_filled_slots"], axis=1
    )
    df["_intent"] = df["_structured"].apply(is_intent)
    return df


# ---------- metric extraction ----------

FUNNEL_LABELS = ["拨打总数", "真人接听", "意向客户", "完整转换", "带车型完整转换"]


def funnel_counts(df: pd.DataFrame) -> list[int]:
    return [
        len(df),
        int(df["_human"].sum()),
        int(df["_intent"].sum()),
        int(df["_full"].sum()),
        int(df["_full_with_model"].sum()),
    ]


def turn_histogram(series: pd.Series, max_x: int) -> list[int]:
    counts = Counter(int(v) for v in series if v > 0)
    return [counts.get(i, 0) for i in range(1, max_x + 1)]


def duration_histogram(series: pd.Series, max_x: int) -> tuple[list[str], list[int]]:
    """One bar per second. Label is the exact second."""
    n_bins = max_x + 1
    counts = [0] * n_bins
    for v in series:
        if v <= 0:
            continue
        idx = min(int(v), n_bins - 1)
        counts[idx] += 1
    labels = [str(i) for i in range(n_bins)]
    return labels, counts


def early_hangup_rows(df: pd.DataFrame) -> list[dict]:
    """恰好 N 句挂断（互斥分桶）+ "10s 内首句挂断" 单独一行."""
    human = df[df["_human"]]
    total = len(human)
    if total == 0:
        return []
    rows = []
    for n, label in [(1, "首句挂断 (1 句)"), (2, "2 句挂断"), (3, "3 句挂断"), (4, "4 句挂断"), (5, "5 句挂断")]:
        cnt = int((human["_assistant_turns"] == n).sum())
        rows.append({"label": label, "count": cnt, "pct": round(cnt / total * 100, 1)})
    # 新增：首句挂断里时长 < 10 秒的，也就是 agent 开场白还没说完就被切话的硬核 case.
    # 这个不破坏上面的互斥分桶，单独追加在表底。
    cnt10 = int(((human["_assistant_turns"] == 1) & (human["Duration (seconds)"] < 10)).sum())
    rows.append({
        "label": "首句挂断 (<10秒)",
        "count": cnt10,
        "pct": round(cnt10 / total * 100, 1),
        "is_subset": True,   # UI 可识别为"父行的子集统计"展示成缩进
    })
    return rows


def hangup_breakdown(df: pd.DataFrame) -> list[dict]:
    total = len(df)
    if total == 0:
        return []
    out = []
    for reason, cnt in df["Hangup Reason"].value_counts(dropna=False).items():
        out.append({
            "reason": reason if reason else "(empty)",
            "count": int(cnt),
            "pct": round(int(cnt) / total * 100, 1),
        })
    return out


def slice_data(df_slice: pd.DataFrame, turn_x_max: int, dur_x_max: int,
               first_dur_x_max: int, field_x_max: int) -> dict:
    """All charts for one slice (全部 or single agent).

    Turn distribution is 真人接听内, so 完整转换/意向 series here are restricted to
    human-answered calls (the funnel/hero counts remain parallel definitions).
    """
    human = df_slice[df_slice["_human"]]
    full_in_human = human[human["_full"]]
    intent_in_human = human[human["_intent"]]

    dur_labels, dur_human  = duration_histogram(human["Duration (seconds)"],        dur_x_max)
    _,          dur_full   = duration_histogram(full_in_human["Duration (seconds)"], dur_x_max)
    _,          dur_intent = duration_histogram(intent_in_human["Duration (seconds)"], dur_x_max)

    def _avg_dur(s: pd.Series) -> float:
        s = s[s > 0]
        return round(float(s.mean()), 1) if len(s) else 0.0

    avg_dur_human  = _avg_dur(human["Duration (seconds)"])
    avg_dur_full   = _avg_dur(full_in_human["Duration (seconds)"])
    avg_dur_intent = _avg_dur(intent_in_human["Duration (seconds)"])

    # 首句挂断 = 真人接听 且 assistant 轮数 == 1。看这部分通话的 Duration 分布，
    # 直观看出 AI 第一句还没说完就被掐掉的比例。
    first_sentence = human[human["_assistant_turns"] == 1]
    first_dur_labels, first_dur_counts = duration_histogram(
        first_sentence["Duration (seconds)"], first_dur_x_max
    )

    # 完整转换槽位分布：真人接听里，4 槽位中填了几个 (0..4)。
    # X 轴范围用全局 field_x_max 锁定，方便跨 Agent 横向对照。
    field_counts = [int((human["_field_count"] == i).sum()) for i in range(field_x_max + 1)]

    # 完整转换下钻：在 ≥3 槽位的子集里，4/4 vs 仅 3/4 的占比，以及 3/4 通话缺的是哪个槽位。
    full_calls = human[human["_full"]]
    exactly_4 = int((full_calls["_field_count"] == 4).sum())
    exactly_3 = int((full_calls["_field_count"] == 3).sum())
    missing_3of4: dict[str, int] = {name: 0 for name in CONVERSION_SLOT_NAMES}
    for slots in full_calls[full_calls["_field_count"] == 3]["_filled_slots"]:
        for name in CONVERSION_SLOT_NAMES:
            if name not in slots:
                missing_3of4[name] += 1
    full_conv_drill = {
        "total_human": len(human),
        "full_count": len(full_calls),
        "exactly_4": exactly_4,
        "exactly_3": exactly_3,
        "missing_3of4": missing_3of4,
    }

    totals = funnel_counts(df_slice)

    return {
        "n": len(df_slice),
        "totals": {"labels": FUNNEL_LABELS, "values": totals},
        # Funnel denominators for the hero KPI percentages: 总 / 真人 / 完整转换.
        # FUNNEL_LABELS 顺序 = [拨打, 真人, 意向, 完整, 带车型完整]，索引对应:
        "denominators": {
            "total": totals[0],   # 拨打总数
            "human": totals[1],   # 真人接听
            "full":  totals[3],   # 完整转换
        },
        "turn_dist": {
            "x": list(range(1, turn_x_max + 1)),
            # 顺序与漏斗一致：真人 → 意向 → 完整
            "series": [
                {"name": "真人接听 (全部)", "data": turn_histogram(human["_max_turn_id"], turn_x_max)},
                {"name": "意向客户", "data": turn_histogram(intent_in_human["_max_turn_id"], turn_x_max)},
                {"name": "完整转换", "data": turn_histogram(full_in_human["_max_turn_id"], turn_x_max)},
            ],
        },
        "duration_dist": {
            "x": dur_labels,
            "series": [
                {"name": "真人接听", "data": dur_human,  "avg": avg_dur_human,  "n": int((human["Duration (seconds)"] > 0).sum())},
                {"name": "完整转换", "data": dur_full,   "avg": avg_dur_full,   "n": int((full_in_human["Duration (seconds)"] > 0).sum())},
                {"name": "意向客户", "data": dur_intent, "avg": avg_dur_intent, "n": int((intent_in_human["Duration (seconds)"] > 0).sum())},
            ],
        },
        "early_hangup": early_hangup_rows(df_slice),
        "first_sentence_dur": {
            "x": first_dur_labels,
            "data": first_dur_counts,
            "n": len(first_sentence),
        },
        "field_count_dist": {
            "x": list(range(field_x_max + 1)),
            "data": field_counts,
            "n": len(human),
        },
        "full_conversion_drill": full_conv_drill,
        "hangup_breakdown": hangup_breakdown(df_slice),
    }


def transcript_readable(transcript: list[dict]) -> str:
    lines = []
    for t in transcript:
        role = t.get("role", "?")
        content = str(t.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def row_for_export(row: pd.Series) -> dict:
    return {
        "Call ID": row.get("Call ID", ""),
        "Agent ID": row.get("Agent ID", ""),
        "Agent Name": row.get("Agent Name", ""),
        "Duration (s)": int(row["Duration (seconds)"]),
        "Hangup Reason": row.get("Hangup Reason", ""),
        "Max turn_id": int(row["_max_turn_id"]),
        "Assistant turns": int(row["_assistant_turns"]),
        "Is Human Answered": bool(row["_human"]),
        "Is Full Conversion": bool(row["_full"]),
        "Is Intent": bool(row["_intent"]),
        "Transcript": transcript_readable(row["_transcript"]),
        "Audio URL": row.get("Audio Record File Download URL", ""),
    }


def build_data(df_enriched: pd.DataFrame) -> dict:
    # Compute global x-axis maxima once so cross-agent comparisons are aligned.
    human_all = df_enriched[df_enriched["_human"]]
    answered_all = df_enriched[df_enriched["_answered"]]
    first_sent_all = human_all[human_all["_assistant_turns"] == 1]
    turn_x_max = max(int(human_all["_max_turn_id"].max()) if len(human_all) else 1, 1)
    dur_x_max = max(int(answered_all["Duration (seconds)"].max()) if len(answered_all) else 30, 30)
    first_dur_x_max = max(int(first_sent_all["Duration (seconds)"].max()) if len(first_sent_all) else 10, 10)
    # X axis is always 0..4 since we now track 4 conversion slots.
    field_x_max = 4

    agents = sorted(df_enriched["Agent Name"].unique())
    datasets = {ALL_KEY: slice_data(df_enriched, turn_x_max, dur_x_max, first_dur_x_max, field_x_max)}
    for a in agents:
        datasets[a] = slice_data(df_enriched[df_enriched["Agent Name"] == a],
                                 turn_x_max, dur_x_max, first_dur_x_max, field_x_max)

    # Per-row export records. Each row carries `_agent`, `_human`, `_full`, `_intent`,
    # `_duration`, `_max_turn`, `_assistant_turns` so JS can filter without re-parsing.
    rows = []
    for _, r in df_enriched.iterrows():
        rec = row_for_export(r)
        rec["_agent"] = r["Agent Name"]
        rec["_human"] = bool(r["_human"])
        rec["_full"] = bool(r["_full"])
        rec["_intent"] = bool(r["_intent"])
        rec["_answered"] = bool(r["_answered"])
        rec["_duration"] = int(r["Duration (seconds)"])
        rec["_max_turn"] = int(r["_max_turn_id"])
        rec["_assistant_turns"] = int(r["_assistant_turns"])
        rec["_field_count"] = int(r["_field_count"])
        rec["_full_with_model"] = bool(r["_full_with_model"])
        # Carry the parsed Structured Output along so the browser can ship it
        # straight to the LLM endpoint for the intent-truth check.
        rec["_structured"] = r["_structured"]
        rows.append(rec)

    # ── Tab 2 数据（Agent 闯关分桶）─────────────────────────────
    # 关键设计：只在「有效会话」范围内看 agent 表现。过滤掉首句挂断 +
    # 系统静默兜底 + IVR 语音信箱，避免污染。Tab 1 数字保持不变。
    try:
        from lib import agent_kda
    except ImportError:
        from scripts.lib import agent_kda
    tab2 = agent_kda.compute_tab2_data(df_enriched)

    return {
        "options": [{"key": ALL_KEY, "label": f"{ALL_LABEL} (n={len(df_enriched)})"}]
        + [{"key": a, "label": f"{a} (n={datasets[a]['n']})"} for a in agents],
        "datasets": datasets,
        "rows": rows,
        "all_key": ALL_KEY,
        "tab2": tab2,
    }


# ---------- HTML rendering ----------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agora 外呼分析 — {source}</title>
<!-- VENDOR_SCRIPTS -->
<style>
  :root {{
    --bg: #f8fafc;
    --panel: #ffffff;
    --panel-2: #f1f5f9;
    --border: #e2e8f0;
    --text: #0f172a;
    --muted: #64748b;
    --accent: #2563eb;
    --accent-2: #10b981;
    --accent-3: #f59e0b;
    --accent-4: #06b6d4;
    --accent-5: #f43f5e;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Inter", sans-serif;
    font-feature-settings: "tnum" 1;
    line-height: 1.5;
  }}
  .wrap {{ max-width: none; margin: 0; padding: 12px 14px 24px; }}
  header {{ display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-bottom: 10px; flex-wrap: wrap; }}
  h1 {{ font-size: 18px; margin: 0; letter-spacing: 0.2px; }}
  h1 .accent {{ color: var(--accent); }}
  .meta {{ color: var(--muted); font-size: 12px; }}
  .meta code {{ background: var(--panel-2); padding: 1px 6px; border-radius: 3px; color: var(--text); }}

  .controls {{ display: flex; align-items: center; gap: 8px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; box-shadow: 0 1px 2px rgba(15,23,42,0.04); }}
  .controls label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; }}
  select {{ font: inherit; font-size: 13px; padding: 5px 26px 5px 8px; border-radius: 5px; border: 1px solid var(--border); background: var(--panel); color: var(--text); appearance: none; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 10 10'%3E%3Cpath d='M1 3l4 4 4-4' stroke='%2364748b' fill='none' stroke-width='1.5'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 8px center; min-width: 220px; }}
  select:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; }}

  h2 {{ font-size: 11px; font-weight: 600; color: var(--muted); margin: 14px 0 4px; text-transform: uppercase; letter-spacing: 0.7px; }}
  .section-note {{ font-size: 11px; color: var(--muted); margin: 0 0 8px; line-height: 1.5; }}
  .section-note b {{ color: var(--text); font-weight: 600; }}

  .stats {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }}
  @media (max-width: 900px) {{ .stats {{ grid-template-columns: repeat(2, 1fr); }} }}

  /* Hero+Funnel split: 5 KPI cards stacked on the left, funnel chart on the right */
  .hero-funnel {{ display: grid; grid-template-columns: 340px 1fr; gap: 10px; align-items: stretch; }}
  @media (max-width: 1100px) {{ .hero-funnel {{ grid-template-columns: 1fr; }} }}
  .hero-funnel .stats {{ grid-template-columns: 1fr; gap: 8px; }}
  .hero-funnel .stat {{ padding: 10px 14px; }}
  .hero-funnel .stat .val {{ font-size: 24px; }}
  .hero-funnel .funnel-wrap {{ display: flex; flex-direction: column; }}
  .hero-funnel .funnel-wrap h2 {{ margin-top: 0; }}
  .hero-funnel .funnel-wrap .card {{ flex: 1; }}
  .hero-funnel .funnel-wrap .chart {{ height: 100%; min-height: 460px; }}
  /* Tint each KPI card to match its slice color on the funnel:
     拨打=blue · 真人=amber · 意向=purple · 完整=cyan · 带车型完整=teal. */
  .stat {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; position: relative; overflow: hidden; box-shadow: 0 1px 2px rgba(15,23,42,0.04); }}
  .stat::after {{ content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px; }}
  .stat:nth-child(1) {{ background: #eff6ff; border-color: #bfdbfe; }}
  .stat:nth-child(1)::after {{ background: #2563eb; }}
  .stat:nth-child(2) {{ background: #fffbeb; border-color: #fde68a; }}
  .stat:nth-child(2)::after {{ background: #f59e0b; }}
  .stat:nth-child(3) {{ background: #faf5ff; border-color: #d8b4fe; }}
  .stat:nth-child(3)::after {{ background: #a855f7; }}
  .stat:nth-child(4) {{ background: #ecfeff; border-color: #a5f3fc; }}
  .stat:nth-child(4)::after {{ background: #06b6d4; }}
  .stat:nth-child(5) {{ background: #f0fdfa; border-color: #99f6e4; }}
  .stat:nth-child(5)::after {{ background: #14b8a6; }}
  .stat .label {{ font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }}
  .stat .val {{ font-size: 26px; font-weight: 700; margin-top: 4px; color: var(--text); line-height: 1.1; }}
  .stat .pct {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}

  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px; box-shadow: 0 1px 2px rgba(15,23,42,0.04); }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  @media (max-width: 900px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}

  .chart {{ width: 100%; height: 320px; }}
  .chart.tall {{ height: 380px; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table th, table td {{ text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--border); }}
  table th {{ background: var(--panel-2); color: var(--muted); font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; font-size: 11px; }}
  table tr:last-child td {{ border-bottom: none; }}
  table td.num {{ text-align: right; font-variant-numeric: tabular-nums; color: var(--text); }}
  /* Excel-style data-bar: full-cell tinted fill from left up to the percentage,
     thin right edge in the accent color, text floats on top right-aligned.
     Replaces the previous 3px-thin bar at the bottom which looked detached. */
  table td.pct-bar {{ position: relative; padding: 0; }}
  table td.pct-bar .fill {{ position: absolute; left: 0; top: 4px; bottom: 4px; background: rgba(37, 99, 235, 0.13); border-right: 2px solid #2563eb; border-radius: 0 2px 2px 0; min-width: 2px; }}
  table td.pct-bar .pct-text {{ position: relative; display: block; text-align: right; padding: 9px 12px; font-variant-numeric: tabular-nums; color: var(--text); }}

  .defs {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; color: var(--muted); font-size: 11px; margin-bottom: 8px; }}
  .defs code {{ background: var(--panel-2); padding: 1px 5px; border-radius: 3px; color: var(--text); }}

  .empty {{ color: var(--muted); padding: 20px; text-align: center; font-size: 13px; }}

  /* "1 · 漏斗" + LLM trigger button on the same line */
  .section-row {{ display: flex; align-items: center; gap: 10px; margin: 14px 0 4px; flex-wrap: wrap; }}
  .section-row h2 {{ margin: 0; }}
  .llm-btn {{ background: linear-gradient(135deg, #6366f1, #a855f7); color: white; border: none;
              padding: 5px 12px; border-radius: 6px; font: inherit; font-size: 11px; font-weight: 600;
              cursor: pointer; box-shadow: 0 1px 3px rgba(99,102,241,0.3); display: inline-flex;
              align-items: center; gap: 6px; }}
  .llm-btn:hover {{ filter: brightness(1.08); }}
  .llm-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .llm-btn .dot {{ width: 6px; height: 6px; border-radius: 50%; background: white; }}

  /* Small inline tab toggle (for views like 首句挂断 全部 vs 短挂断) */
  .view-toggle {{ display: inline-flex; gap: 0; margin-left: 8px; }}
  .view-toggle button {{ background: var(--panel); color: var(--muted); border: 1px solid var(--border);
                          font: inherit; font-size: 10px; padding: 3px 9px; cursor: pointer; }}
  .view-toggle button:first-child {{ border-radius: 4px 0 0 4px; border-right: none; }}
  .view-toggle button:last-child {{ border-radius: 0 4px 4px 0; }}
  .view-toggle button.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
  .view-toggle button:hover:not(.active) {{ color: var(--text); }}

  /* LLM result summary chips inside the LLM modal */
  .llm-summary {{ display: flex; gap: 10px; margin: 12px 0; flex-wrap: wrap; }}
  .llm-summary .chip {{ flex: 1; min-width: 90px; background: var(--panel-2); border-radius: 6px;
                         padding: 10px 12px; text-align: center; border: 1px solid var(--border); }}
  .llm-summary .chip .ct {{ font-size: 22px; font-weight: 700; line-height: 1.1; }}
  .llm-summary .chip .lb {{ font-size: 11px; color: var(--muted); margin-top: 3px; }}
  .llm-summary .chip.real {{ background: #ecfdf5; border-color: #a7f3d0; }}
  .llm-summary .chip.real .ct {{ color: #047857; }}
  .llm-summary .chip.fake {{ background: #fef2f2; border-color: #fecaca; }}
  .llm-summary .chip.fake .ct {{ color: #b91c1c; }}
  .llm-summary .chip.mid  {{ background: #fffbeb; border-color: #fde68a; }}
  .llm-summary .chip.mid .ct  {{ color: #b45309; }}
  .llm-summary .chip.err  {{ background: #fef2f2; border-color: #fca5a5; }}
  .llm-summary .chip.err .ct  {{ color: #b91c1c; }}
  /* 完整转换下钻面板 */
  .drill {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; align-items: start; }}
  @media (max-width: 900px) {{ .drill {{ grid-template-columns: 1fr; }} }}
  .drill-head {{ display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }}
  .drill-head .num {{ font-size: 24px; font-weight: 700; color: var(--text); }}
  .drill-head .lbl {{ font-size: 12px; color: var(--muted); }}
  .drill-split {{ display: flex; gap: 8px; }}
  .drill-split .seg {{ flex: 1; background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; }}
  .drill-split .seg.full {{ background: #ecfdf5; border-color: #a7f3d0; }}
  .drill-split .seg.full .num {{ color: #047857; }}
  .drill-split .seg.three {{ background: #fffbeb; border-color: #fde68a; }}
  .drill-split .seg.three .num {{ color: #b45309; }}
  .drill-split .seg .num {{ font-size: 22px; font-weight: 700; line-height: 1.1; }}
  .drill-split .seg .pct {{ font-size: 11px; color: var(--muted); }}
  .drill-split .seg .lbl {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 4px; }}
  .drill-miss h4 {{ margin: 0 0 6px; font-size: 12px; font-weight: 600; color: var(--text); }}
  .drill-miss .row {{ display: grid; grid-template-columns: 60px 1fr 56px; gap: 8px; align-items: center; font-size: 12px; margin: 4px 0; }}
  .drill-miss .row .label {{ color: var(--muted); }}
  .drill-miss .row .bar {{ height: 10px; background: var(--panel-2); border-radius: 3px; overflow: hidden; position: relative; }}
  .drill-miss .row .bar > div {{ height: 100%; background: linear-gradient(90deg, #f43f5e, #f59e0b); }}
  .drill-miss .row .val {{ text-align: right; color: var(--text); font-variant-numeric: tabular-nums; font-size: 11px; }}

  .modal .modal-actions {{ display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }}
  .modal button.primary {{ background: var(--accent); color: white; border: 1px solid var(--accent);
                           padding: 7px 16px; border-radius: 6px; font: inherit; font-size: 12px;
                           cursor: pointer; }}
  .modal button.primary:hover {{ filter: brightness(1.05); }}
  .modal button.primary:disabled {{ opacity: 0.5; cursor: not-allowed; }}

  /* Percentages render on a single horizontal line, wrapping only when the card
     is too narrow to fit all three (e.g. mobile). */
  .stat .pcts {{ margin-top: 6px; display: flex; flex-wrap: wrap; gap: 2px 10px; align-items: baseline; }}
  .stat .pcts > div {{ display: inline-flex; align-items: baseline; gap: 4px; }}
  .stat .pcts .lbl {{ color: var(--muted); font-size: 11px; }}
  .stat .pcts .num {{ color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; font-size: 13px; }}

  .turn-card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 1px 2px rgba(15,23,42,0.04); padding: 10px 14px 12px; margin-bottom: 8px; }}
  .turn-card-title {{ margin: 0 0 6px; font-size: 13px; font-weight: 600; color: var(--text); }}
  .turn-card-body {{ display: grid; grid-template-columns: 2fr 1fr; gap: 10px; align-items: stretch; }}
  @media (max-width: 900px) {{ .turn-card-body {{ grid-template-columns: 1fr; }} }}
  .turn-card .chart {{ height: 240px; }}

  .export-hint {{ display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--accent); background: rgba(37,99,235,0.08); border: 1px solid rgba(37,99,235,0.2); padding: 2px 8px; border-radius: 999px; margin-left: 8px; vertical-align: middle; }}
  .export-hint::before {{ content: "↓"; font-weight: 600; }}
  table tr.clickable {{ cursor: pointer; }}
  table tr.clickable:hover td {{ background: rgba(37,99,235,0.06); }}
  .toast {{ position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: var(--text); color: white; padding: 10px 18px; border-radius: 8px; font-size: 13px; box-shadow: 0 6px 20px rgba(15,23,42,0.18); opacity: 0; transition: opacity 0.2s; pointer-events: none; z-index: 200; }}
  .toast.show {{ opacity: 1; }}

  .modal-backdrop {{ position: fixed; inset: 0; background: rgba(15,23,42,0.5); display: none; align-items: center; justify-content: center; z-index: 150; }}
  .modal-backdrop.show {{ display: flex; }}
  .modal {{ background: var(--panel); border-radius: 12px; padding: 22px 24px; min-width: 340px; max-width: 480px; box-shadow: 0 20px 60px rgba(15,23,42,0.25); }}
  .modal h3 {{ margin: 0 0 6px; font-size: 16px; color: var(--text); }}
  .modal .sub {{ font-size: 13px; color: var(--muted); margin-bottom: 16px; }}
  .modal .sub b {{ color: var(--text); }}
  .modal .options {{ display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }}
  .modal button.opt {{ display: flex; justify-content: space-between; align-items: center; width: 100%; padding: 12px 14px; border: 1px solid var(--border); background: var(--panel); border-radius: 8px; font: inherit; font-size: 13px; color: var(--text); cursor: pointer; text-align: left; transition: all 0.12s; }}
  .modal button.opt:hover {{ border-color: var(--accent); background: rgba(37,99,235,0.04); }}
  .modal button.opt:disabled {{ opacity: 0.4; cursor: not-allowed; }}
  .modal button.opt .hint {{ color: var(--muted); font-size: 11px; }}
  .modal .actions {{ display: flex; justify-content: flex-end; gap: 8px; }}
  .modal button.cancel {{ background: none; border: 1px solid var(--border); color: var(--muted); padding: 7px 14px; border-radius: 6px; font: inherit; font-size: 12px; cursor: pointer; }}
  .modal button.cancel:hover {{ color: var(--text); }}
  .modal .progress {{ margin-top: 12px; font-size: 12px; color: var(--muted); }}
  .modal .progress .bar {{ background: var(--panel-2); border-radius: 4px; height: 6px; margin-top: 6px; overflow: hidden; }}
  .modal .progress .bar > div {{ background: var(--accent); height: 100%; width: 0%; transition: width 0.2s; }}

  /* ─── Tab 切换 ─── */
  .tab-bar {{ display: flex; gap: 0; border-bottom: 2px solid var(--border); margin-bottom: 12px; }}
  .tab-btn {{ background: none; border: none; padding: 9px 16px; font: inherit; font-size: 13px; font-weight: 500; color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; }}
  .tab-btn:hover {{ color: var(--text); }}
  .tab-btn.active {{ color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}

  /* ─── Tab 2 layout ─── */
  .t2-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  @media (max-width: 1100px) {{ .t2-grid {{ grid-template-columns: 1fr; }} }}
  .t2-card-title {{ margin: 0 0 6px; font-size: 13px; font-weight: 600; color: var(--text); }}

  /* 范围漏斗 stats 行 */
  #t2-scope-stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }}
  @media (max-width: 900px) {{ #t2-scope-stats {{ grid-template-columns: repeat(2, 1fr); }} }}
  #t2-scope-stats .stat {{ background: var(--panel); border: 1px solid var(--border); }}
  #t2-scope-stats .stat::after {{ background: var(--muted); }}
  #t2-scope-stats .stat.lost::after {{ background: #f43f5e; }}
  #t2-scope-stats .stat.valid::after {{ background: #10b981; }}

  /* 4 关独立通过率柱 */
  .t2-slot-row {{ display: grid; grid-template-columns: 80px 1fr 110px; gap: 10px; align-items: center; padding: 6px 0; font-size: 13px; }}
  .t2-slot-row .label {{ color: var(--text); font-weight: 500; }}
  .t2-slot-row .bar {{ height: 14px; background: var(--panel-2); border-radius: 3px; overflow: hidden; position: relative; }}
  .t2-slot-row .bar > div {{ height: 100%; background: linear-gradient(90deg, #2563eb, #14b8a6); border-radius: 3px; }}
  .t2-slot-row .val {{ text-align: right; color: var(--text); font-variant-numeric: tabular-nums; font-size: 12px; }}
  .t2-slot-row .val b {{ color: var(--text); font-size: 14px; }}

  /* 通关分桶卡片 */
  .t2-bucket-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }}
  @media (max-width: 1100px) {{ .t2-bucket-grid {{ grid-template-columns: repeat(3, 1fr); }} }}
  @media (max-width: 700px)  {{ .t2-bucket-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
  .t2-bucket {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; cursor: pointer; transition: all 0.12s; position: relative; overflow: hidden; }}
  .t2-bucket::after {{ content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px; }}
  .t2-bucket[data-lv="0"] {{ background: #fef2f2; border-color: #fecaca; }}
  .t2-bucket[data-lv="0"]::after {{ background: #f43f5e; }}
  .t2-bucket[data-lv="1"] {{ background: #fffbeb; border-color: #fde68a; }}
  .t2-bucket[data-lv="1"]::after {{ background: #f59e0b; }}
  .t2-bucket[data-lv="2"] {{ background: #fefce8; border-color: #fde047; }}
  .t2-bucket[data-lv="2"]::after {{ background: #eab308; }}
  .t2-bucket[data-lv="3"] {{ background: #ecfeff; border-color: #a5f3fc; }}
  .t2-bucket[data-lv="3"]::after {{ background: #06b6d4; }}
  .t2-bucket[data-lv="4"] {{ background: #ecfdf5; border-color: #a7f3d0; }}
  .t2-bucket[data-lv="4"]::after {{ background: #10b981; }}
  .t2-bucket:hover {{ filter: brightness(0.98); }}
  .t2-bucket.active {{ box-shadow: 0 0 0 2px var(--accent), 0 0 0 4px rgba(37,99,235,0.20); }}
  .t2-bucket .label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; font-weight: 500; }}
  .t2-bucket .ct {{ font-size: 28px; font-weight: 700; color: var(--text); line-height: 1.1; margin-top: 4px; font-variant-numeric: tabular-nums; }}
  .t2-bucket .pct {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  .t2-bucket .meta {{ font-size: 11px; color: var(--muted); margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); line-height: 1.5; }}
  .t2-bucket .meta b {{ color: var(--text); font-variant-numeric: tabular-nums; font-weight: 600; }}

  /* Transcript 案例行（agent / user 区分颜色） */
  .t2-case-line {{ padding: 2px 6px; white-space: pre-wrap; word-break: break-word; }}
  .t2-case-line.agent {{ color: #1e3a8a; }}
  .t2-case-line.user  {{ color: #047857; }}

  /* LLM 失败案例表格 */
  .t2-cases-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .t2-cases-table th {{ background: var(--panel-2); color: var(--muted); font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; font-size: 10px; padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 1; }}
  .t2-cases-table td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  .t2-cases-table tr:hover td {{ background: rgba(37,99,235,0.03); }}
  .t2-cases-table tr.expanded {{ background: rgba(37,99,235,0.05); }}
  .t2-cases-table tr.expanded + tr.case-detail {{ background: var(--panel-2); }}
  .t2-cases-table .col-id {{ width: 100px; }}
  .t2-cases-table .col-id code {{ background: var(--panel-2); padding: 2px 6px; border-radius: 3px; font-size: 10px; font-family: monospace; cursor: pointer; }}
  .t2-cases-table .col-id code:hover {{ background: rgba(37,99,235,0.15); }}
  .t2-cases-table .col-agent {{ max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }}
  .t2-cases-table .col-pn   {{ width: 56px; text-align: center; }}
  .t2-cases-table .col-pn .chip {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; }}
  .t2-cases-table .col-ft   {{ width: 56px; text-align: center; color: #b91c1c; font-weight: 600; }}
  .t2-cases-table .col-cat  {{ width: 130px; }}
  .t2-cases-table .col-cat .chip {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 10px; background: rgba(244,63,94,0.10); color: #b91c1c; }}
  .t2-cases-table .col-reason {{ color: var(--text); }}
  .t2-cases-table .col-ut   {{ width: 56px; text-align: center; color: #b45309; font-weight: 600; }}
  .t2-cases-table .col-signal {{ color: var(--muted); max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .t2-cases-table .col-toggle {{ width: 32px; text-align: center; }}
  .t2-cases-table .col-toggle button {{ background: none; border: 1px solid var(--border); color: var(--muted); width: 22px; height: 22px; border-radius: 4px; cursor: pointer; font-size: 11px; padding: 0; }}
  .t2-cases-table .col-toggle button:hover {{ border-color: var(--accent); color: var(--accent); }}
  .t2-cases-table tr.case-detail td {{ padding: 0; }}
  .t2-cases-table tr.case-detail .detail-box {{ padding: 12px 16px; background: var(--panel-2); border-left: 3px solid var(--accent); margin: 0 0 8px; }}
  .pn-chip-0 {{ background: rgba(244,63,94,0.15); color: #b91c1c; }}
  .pn-chip-1 {{ background: rgba(245,158,11,0.18); color: #b45309; }}
  .pn-chip-2 {{ background: rgba(234,179,8,0.18);  color: #92400e; }}
  .pn-chip-3 {{ background: rgba(6,182,212,0.15);  color: #0e7490; }}

  /* 0 关原因分布柱 */
  .t2-reason-row {{ display: grid; grid-template-columns: 110px 1fr 70px; gap: 10px; align-items: center; padding: 4px 0; font-size: 12px; }}
  .t2-reason-row .label {{ color: var(--text); }}
  .t2-reason-row .bar {{ height: 10px; background: var(--panel-2); border-radius: 2px; overflow: hidden; }}
  .t2-reason-row .bar > div {{ height: 100%; background: linear-gradient(90deg, #f43f5e, #f59e0b); border-radius: 2px; }}
  .t2-reason-row .val {{ text-align: right; font-variant-numeric: tabular-nums; }}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div>
    <h1>Agora 外呼分析 <span class="accent">·</span> <span style="color:var(--muted); font-weight:400;">{source}</span></h1>
    <div class="meta" style="margin-top:6px;">总通话 <code>{total}</code> · Agent Name 数 <code>{n_agents}</code></div>
  </div>
  <div class="controls" id="tab1-controls">
    <label for="agent-select">范围</label>
    <select id="agent-select">
      {select_options}
    </select>
  </div>
</header>

<div class="tab-bar">
  <button class="tab-btn active" data-tab="overview">产品总览</button>
  <button class="tab-btn" data-tab="agent">Agent 视角 (KDA)</button>
</div>

<div id="tab-overview" class="tab-content active">

<div class="defs">
  接听 = <code>Duration &gt; 0</code> · 真人接听 = <code>USER/AI_HANGUP</code> · 完整转换 = <code>Structured Output 无 null</code> · 意向 = <code>购车意向="是"</code> · N 句挂断 = <code>真人接听里 assistant 轮数恰好 = N</code>
</div>

<div class="hero-funnel">
  <div class="stats" id="hero-stats"></div>
  <div class="funnel-wrap">
    <div class="section-row" style="margin-top:0;">
      <h2>1 · 漏斗 <span class="export-hint">点击层级导出</span></h2>
      <button id="btn-llm-intent" class="llm-btn" title="用大模型重新判定意向客户的真假">
        <span class="dot"></span>LLM 意向真伪分析
      </button>
    </div>
    <div class="card"><div id="chart-funnel" class="chart tall"></div></div>
  </div>
</div>

<h2>2 · 轮次分布 (max turn_id, 真人接听内) <span class="export-hint">点击柱子导出</span></h2>
<p class="section-note">备注：<b>max turn_id 同时包含 agent 和真人两方的轮次</b>（assistant + user 共享 turn_id 序号）。三张图分别看每个子集的轮次构成，左边柱状（绝对数量），右边环形（每根柱子在该子集里的占比）。</p>

<div class="turn-card">
  <h3 class="turn-card-title">真人接听 (全部)</h3>
  <div class="turn-card-body">
    <div class="turn-bar"><div id="chart-turn-human" class="chart"></div></div>
    <div class="turn-donut"><div id="chart-turn-human-donut" class="chart"></div></div>
  </div>
</div>
<div class="turn-card">
  <h3 class="turn-card-title">意向客户</h3>
  <div class="turn-card-body">
    <div class="turn-bar"><div id="chart-turn-intent" class="chart"></div></div>
    <div class="turn-donut"><div id="chart-turn-intent-donut" class="chart"></div></div>
  </div>
</div>
<div class="turn-card">
  <h3 class="turn-card-title">完整转换</h3>
  <div class="turn-card-body">
    <div class="turn-bar"><div id="chart-turn-full" class="chart"></div></div>
    <div class="turn-donut"><div id="chart-turn-full-donut" class="chart"></div></div>
  </div>
</div>

<h2>3 · Duration 分布 (真人接听) <span class="export-hint">点击柱子导出</span></h2>
<p class="section-note">横轴单位 <b>秒</b>（一秒一柱）；拖动下方滑块或滚轮缩放查看任意区间。点单根柱子导出该秒数对应的真人接听通话。</p>
<div class="card"><div id="chart-duration" class="chart tall"></div></div>

<h2>4 · 完整转换槽位分布 (真人接听内) <span class="export-hint">点击柱子导出</span></h2>
<p class="section-note">备注：4 个槽位 — <b>车型</b> (购车品牌 或 购车型号 任一非 null) · <b>时间</b> · <b>城市</b> · <b>姓名</b>。<b>≥ 3 个填齐</b> 算完整转换。购车意向 不计入槽位，是独立漏斗分支。</p>
<div class="turn-card">
  <div class="turn-card-body">
    <div class="turn-bar"><div id="chart-field-count" class="chart"></div></div>
    <div class="turn-donut"><div id="chart-field-count-donut" class="chart"></div></div>
  </div>
</div>
<div class="card" id="full-conv-drill" style="margin-top:8px;"></div>

<h2>5 · 早期挂断（真人接听内 · 互斥分桶）</h2>
<div class="grid-2">
  <div class="card">
    <h3 style="margin:0 0 6px; font-size:12px; color:var(--muted); font-weight:500; text-transform: uppercase; letter-spacing:0.6px;">分句数汇总 <span class="export-hint">点行导出</span></h3>
    <p class="section-note" style="margin:0 0 12px;">备注：<b>仅计算 agent 说话轮次</b>。集中在前几句的部分代表 <b>AI 表现不好 · 很快被客户识破</b>。</p>
    <div id="early-hangup-table"></div>
  </div>
  <div class="card">
    <h3 style="margin:0 0 6px; font-size:12px; color:var(--muted); font-weight:500; text-transform: uppercase; letter-spacing:0.6px; display:flex; align-items:center; flex-wrap:wrap;">
      首句挂断 · Duration 分布
      <span class="view-toggle" id="fs-view-toggle">
        <button data-view="all" class="active">全部</button>
        <button data-view="short">短挂断 (&lt;10秒)</button>
      </span>
      <span class="export-hint" style="margin-left:auto;">点柱导出</span>
    </h3>
    <p class="section-note" style="margin:0 0 12px;">"AI 刚说完第一句就被掐掉"的通话时长分布。"短挂断"视角聚焦 <b>&lt; 10 秒</b> 的，那些通常是开场白没说完就被切话；"全部"视角包括开场白说完后客户才挂的。</p>
    <div id="chart-first-sentence-dur" class="chart"></div>
  </div>
</div>

</div><!-- /tab-overview -->

<div id="tab-agent" class="tab-content">

  <div class="defs">
    Tab 2 只看 <b>有效会话</b>：真人接听 (USER/AI_HANGUP) <b>且至少 1 句"真实"用户发言</b>。
    三级筛子：① 剔除<b>首句挂断</b>（agent 一开口就被挂）② 剔除<b>接通无应答</b>（客户全程未开口，agent 自言自语到挂断）③ 剩下是有效会话。
    4 关 = <code>车型 (品牌 AND 型号)</code> · <code>城市</code> · <code>时间</code> · <code>姓氏</code>，严格线性。
  </div>

  <div class="section-row" style="margin-top: 6px;">
    <label for="t2-agent-select" style="font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px;">Agent</label>
    <select id="t2-agent-select" style="font: inherit; font-size: 13px; padding: 5px 26px 5px 8px; border-radius: 5px; border: 1px solid var(--border); background: var(--panel); color: var(--text); min-width: 280px;"></select>
  </div>

  <h2>1 · 数据范围分流 (桑基图)</h2>
  <p class="section-note">
    <b>有效会话</b> = 真人接听 (Hangup ∈ USER/AI_HANGUP) <b>且</b> 至少 1 句"真实用户发言"。
    "真实用户发言" = role=user 的 turn，且 <code>metadata.source ≠ silence</code>（不是系统注入的静默占位）、content 非空、不含 IVR 语音信箱关键词（"请留下你的姓名"/"智语音留言"/"帮你确认此人" 等）。
    剩下两类是<b>客户全程未开口</b>的通话：<b>首句挂断</b>（agent 说 1 句开场白客户没回）+ <b>接通无应答</b>（agent 反复追问客户始终不说话）。
  </p>
  <div class="card"><div id="t2-chart-sankey" class="chart" style="height: 560px;"></div></div>

  <h2>2 · 通关分桶 — 在第几关被卡住</h2>
  <p class="section-note">
    按"过了几关"分 5 桶（严格线性递进：第 N 关 ⇔ 第 1..N 全过）。每桶 = "卡在第 N+1 关"的通话集合。
    比如 <b>"0 关"桶 = agent 一关都没问到</b>；<b>"3 关"桶 = 收到了车型/城市/时间但卡在第 4 关姓氏</b>；<b>"4 关"桶 = 全过</b>。
    点桶 → 看下方 3 个相关数据。
  </p>
  <div id="t2-buckets" class="t2-bucket-grid"></div>

  <h2>3 · 选中桶详情 <span id="t2-bk-title" style="color: var(--muted); font-weight: 400; font-size: 13px; margin-left: 6px;"></span></h2>
  <div class="t2-grid">
    <div class="card">
      <h3 class="t2-card-title">该桶为什么卡住 <span style="font-weight:400;color:var(--muted);font-size:11px;">· LLM 判定的失败类别</span></h3>
      <p class="section-note">把该桶的 LLM 失败画像聚合：核心是 agent 卡在 <b id="t2-bk-stuck">-</b>。</p>
      <div id="t2-bk-reasons"></div>
    </div>
    <div class="card">
      <h3 class="t2-card-title">该桶 typical 案例 <span style="font-weight:400;color:var(--muted);font-size:11px;">· 5 个最长 transcript</span></h3>
      <p class="section-note">看实际通话长什么样。<span style="color:#b91c1c;font-weight:600;">红色高亮</span> = LLM 判定 agent 出问题的那一句。</p>
      <div id="t2-bk-cases" style="max-height: 420px; overflow-y: auto;"></div>
    </div>
  </div>

  <h2>4 · LLM 失败画像 <span id="t2-llm-status" style="color: var(--muted); font-weight: 400; font-size: 12px; margin-left: 6px;">加载中…</span></h2>
  <p class="section-note">服务端用大模型 (gpt-5.4 / qwen3.6-plus) 逐通分析"agent 在哪一轮出问题 / 客户在哪一轮识破 / 失败类别"。后台跑，每 5 秒自动刷新。</p>
  <div class="card" id="t2-llm-progress-wrap" style="display: none;">
    <div class="progress" style="display: block;">
      <div id="t2-llm-progress-text"></div>
      <div class="bar"><div id="t2-llm-progress-bar"></div></div>
    </div>
  </div>

  <div class="t2-grid">
    <div class="card">
      <h3 class="t2-card-title">Agent 在第几轮出问题</h3>
      <p class="section-note">横轴 = assistant 的第几次发言（A1 / A2 / …）</p>
      <div id="t2-chart-fail-turn" class="chart" style="height: 260px;"></div>
    </div>
    <div class="card">
      <h3 class="t2-card-title">客户在第几轮识破/反感</h3>
      <p class="section-note">横轴 = user 的第几次真实发言</p>
      <div id="t2-chart-detect-turn" class="chart" style="height: 260px;"></div>
    </div>
  </div>

  <div class="card">
    <h3 class="t2-card-title">失败类别分布</h3>
    <div id="t2-chart-fail-cat" class="chart" style="height: 320px;"></div>
  </div>

  <h2>5 · LLM 失败案例列表 <span id="t2-cases-meta" style="color: var(--muted); font-weight: 400; font-size: 12px; margin-left: 6px;"></span></h2>
  <p class="section-note">
    每行 = 一个失败通话。表格直接显示 Call ID 和 LLM 给的全部判定。
    点击 <b>展开按钮</b> 看完整 transcript。
    顶部可按 agent / 桶 / 失败类别筛选。
  </p>
  <div class="card" id="t2-cases-card">
    <div class="t2-cases-filters" style="display: flex; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; align-items: center;">
      <label style="font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px;">桶：</label>
      <select id="t2-cases-filter-pn" style="font: inherit; font-size: 12px; padding: 4px 22px 4px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--panel);">
        <option value="">全部</option>
        <option value="0">0 关</option><option value="1">1 关</option>
        <option value="2">2 关</option><option value="3">3 关</option>
      </select>
      <label style="font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px;">类别：</label>
      <select id="t2-cases-filter-cat" style="font: inherit; font-size: 12px; padding: 4px 22px 4px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--panel); min-width: 150px;">
        <option value="">全部</option>
      </select>
      <label style="font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px;">每页：</label>
      <select id="t2-cases-page-size" style="font: inherit; font-size: 12px; padding: 4px 22px 4px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--panel);">
        <option value="50">50</option>
        <option value="100" selected>100</option>
        <option value="200">200</option>
        <option value="9999">全部</option>
      </select>
      <span id="t2-cases-count" style="margin-left: auto; font-size: 11px; color: var(--muted);"></span>
    </div>
    <div id="t2-cases-table-wrap"></div>
  </div>

</div><!-- /tab-agent -->

<div id="toast" class="toast"></div>

<div id="llm-modal" class="modal-backdrop">
  <div class="modal" style="min-width: 460px; max-width: 640px;">
    <h3>LLM 意向真伪分析</h3>
    <div class="sub" id="llm-sub"></div>
    <div id="llm-stage-pre" style="display:none;">
      <p style="font-size:12px; color:var(--muted); line-height:1.6;" id="llm-pre-text"></p>
      <div class="modal-actions">
        <button class="cancel" id="llm-cancel-pre">关闭</button>
      </div>
    </div>
    <div id="llm-stage-running" style="display:none;">
      <p style="font-size:12px; color:var(--muted); margin: 0 0 10px;">服务端 16 路并行调 LLM，每 5 秒自动刷新。可以关掉弹窗，结果不会丢。</p>
      <div class="progress" style="display:block;">
        <div id="llm-progress-text"></div>
        <div class="bar"><div id="llm-progress-bar"></div></div>
      </div>
    </div>
    <div id="llm-stage-done" style="display:none;">
      <div class="llm-summary" id="llm-summary"></div>
      <p style="font-size:11px; color:var(--muted); margin: 4px 0 8px;">每行点开可以看完整 reason + 证据；导出 Excel 含全部字段。</p>
      <div id="llm-results-table" style="max-height:300px; overflow:auto;"></div>
      <div class="modal-actions">
        <button class="cancel" id="llm-close">关闭</button>
        <button class="primary" id="llm-export">导出 Excel</button>
      </div>
    </div>
  </div>
</div>

<div id="export-modal" class="modal-backdrop">
  <div class="modal">
    <h3>导出</h3>
    <div class="sub" id="modal-sub"></div>
    <div id="modal-server-note" style="display:none; font-size:11px; color:var(--muted); background:var(--panel-2); padding:8px 10px; border-radius:6px; margin-bottom:12px; line-height:1.5;">
      ⓘ 当前是 <code style="background:white; padding:1px 4px; border-radius:3px;">file://</code> 模式，只能导 Excel。要拉录音请改用 <code style="background:white; padding:1px 4px; border-radius:3px;">serve_dashboard.py</code> 启动 dashboard。
    </div>
    <div class="options">
      <button class="opt" data-mode="excel">
        <span>只要 Excel</span><span class="hint">含 transcript / Audio URL</span>
      </button>
      <button class="opt" data-mode="audio">
        <span>只要录音 (zip)</span><span class="hint" id="opt-audio-hint"></span>
      </button>
      <button class="opt" data-mode="both">
        <span>Excel + 录音 (zip)</span><span class="hint" id="opt-both-hint"></span>
      </button>
    </div>
    <div class="progress" id="modal-progress" style="display:none;">
      <div id="progress-text"></div>
      <div class="bar"><div id="progress-bar"></div></div>
    </div>
    <div class="actions">
      <button class="cancel" id="modal-cancel">取消</button>
    </div>
  </div>
</div>

</div>

<script>
const DATA = {data_json};

// Funnel slot colors in label order (blue/amber/purple/cyan/teal). 顺序与 hero
// KPI 卡 + Funnel 图 layer 完全一致：拨打/真人/意向/完整/带车型完整。
const PALETTE = ['#2563eb', '#f59e0b', '#a855f7', '#06b6d4', '#14b8a6', '#10b981', '#f43f5e', '#0ea5e9'];
const TEXT = '#0f172a';
const MUTED = '#64748b';
const BORDER = '#e2e8f0';
const TOOLTIP_BG = '#ffffff';

const baseGrid = {{ left: 50, right: 24, top: 50, bottom: 36, containLabel: true }};
const baseAxis = {{
  axisLine: {{ lineStyle: {{ color: BORDER }} }},
  axisTick: {{ lineStyle: {{ color: BORDER }} }},
  axisLabel: {{ color: MUTED, fontSize: 11 }},
  splitLine: {{ lineStyle: {{ color: BORDER, type: 'dashed' }} }},
  nameTextStyle: {{ color: MUTED, fontSize: 11 }},
}};

function tooltipBase(extra) {{
  return Object.assign({{
    backgroundColor: TOOLTIP_BG,
    borderColor: BORDER,
    borderWidth: 1,
    textStyle: {{ color: TEXT, fontSize: 12 }},
    extraCssText: 'box-shadow: 0 4px 12px rgba(15,23,42,0.08); border-radius: 6px;',
  }}, extra || {{}});
}}

const chartIds = ['chart-funnel',
                  'chart-turn-human', 'chart-turn-human-donut',
                  'chart-turn-full',  'chart-turn-full-donut',
                  'chart-turn-intent','chart-turn-intent-donut',
                  'chart-duration', 'chart-first-sentence-dur',
                  'chart-field-count', 'chart-field-count-donut',
                  't2-chart-sankey',
                  't2-chart-fail-turn', 't2-chart-detect-turn', 't2-chart-fail-cat'];
const charts = {{}};
chartIds.forEach(id => {{ charts[id] = echarts.init(document.getElementById(id)); }});

// Bar series name → corresponding subset filter for click-to-export.
// 顺序：真人 → 意向 → 完整 (和漏斗顺序一致)
// 配色与 hero KPI 卡 + 漏斗 layer 完全一致 (蓝/紫/青)
const TURN_SERIES = [
  {{ key: 'human',  name: '真人接听 (全部)', barId: 'chart-turn-human',  donutId: 'chart-turn-human-donut',  color: '#2563eb', filter: r => r._human }},
  {{ key: 'intent', name: '意向客户',        barId: 'chart-turn-intent', donutId: 'chart-turn-intent-donut', color: '#a855f7', filter: r => r._human && r._intent }},
  {{ key: 'full',   name: '完整转换',        barId: 'chart-turn-full',   donutId: 'chart-turn-full-donut',   color: '#06b6d4', filter: r => r._human && r._full }},
];

let currentAgentKey = DATA.all_key;

function scopedRows() {{
  if (currentAgentKey === DATA.all_key) return DATA.rows;
  return DATA.rows.filter(r => r._agent === currentAgentKey);
}}

function showToast(msg) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(showToast._h);
  showToast._h = setTimeout(() => t.classList.remove('show'), 2400);
}}

function safeFilename(s) {{
  return String(s).replace(/[\\\\/:*?"<>|]/g, '_').slice(0, 80);
}}

const SERVER_MODE = (window.location.protocol === 'http:' || window.location.protocol === 'https:');

const EXCEL_COLS = ['Call ID', 'Agent ID', 'Agent Name', 'Duration (s)', 'Hangup Reason',
                    'Max turn_id', 'Assistant turns', 'Is Human Answered', 'Is Full Conversion',
                    'Is Intent', 'Transcript', 'Audio URL'];

function buildWorkbook(rows) {{
  const aoa = [EXCEL_COLS].concat(rows.map(r => EXCEL_COLS.map(c => r[c] ?? '')));
  const ws = XLSX.utils.aoa_to_sheet(aoa);
  ws['!cols'] = [12, 14, 28, 8, 16, 10, 10, 10, 10, 8, 60, 28].map(w => ({{ wch: w }}));
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'calls');
  return wb;
}}

function workbookBase64(rows) {{
  const wb = buildWorkbook(rows);
  // SheetJS 'binary' returns a string; convert to base64
  const bin = XLSX.write(wb, {{ bookType: 'xlsx', type: 'binary' }});
  let s = '';
  for (let i = 0; i < bin.length; i++) s += String.fromCharCode(bin.charCodeAt(i) & 0xff);
  return btoa(s);
}}

function downloadBlob(blob, filename) {{
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}}

// Trigger server-streamed zip download via hidden iframe so the browser's native
// download manager handles the bytes (no JS heap accumulation of multi-GB blobs).
// Returns a promise that resolves when the request is dispatched — actual
// download completion is owned by the browser.
function dispatchAudioZip(zipFilename, groups) {{
  return new Promise((resolve, reject) => {{
    let iframe = document.getElementById('download-iframe');
    if (!iframe) {{
      iframe = document.createElement('iframe');
      iframe.id = 'download-iframe';
      iframe.name = 'download-iframe';
      iframe.style.display = 'none';
      document.body.appendChild(iframe);
    }}
    // Catch server-side rejection (413, etc): browser will render the error page in
    // the iframe instead of triggering a download. Check after a short delay.
    let settled = false;
    iframe.onload = () => {{
      if (settled) return;
      // If the response was a download, iframe stays blank (cross-origin or attachment).
      // If it was an error page, we can read body text from same-origin iframe.
      try {{
        const txt = iframe.contentDocument && iframe.contentDocument.body
          ? iframe.contentDocument.body.innerText : '';
        if (txt && txt.toLowerCase().includes('refused')) {{
          settled = true;
          reject(new Error(txt.split('\\n')[0]));
        }}
      }} catch (e) {{ /* attachment / cross-origin: success path */ }}
    }};

    const form = document.createElement('form');
    form.method = 'POST';
    form.action = '/audio-zip';
    form.target = 'download-iframe';
    form.enctype = 'application/x-www-form-urlencoded';
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'payload';
    input.value = JSON.stringify({{ zip_filename: zipFilename, groups }});
    form.appendChild(input);
    document.body.appendChild(form);
    form.submit();
    setTimeout(() => document.body.removeChild(form), 1000);
    // Give server ~0.5s headstart to validate (e.g. emit 413). After that we
    // assume the response is a streaming download and the user UI can move on.
    setTimeout(() => {{ if (!settled) {{ settled = true; resolve(); }} }}, 800);
  }});
}}

function buildScopeName() {{
  return currentAgentKey === DATA.all_key ? '全部' : currentAgentKey;
}}

function audioFilename(row) {{
  const url = row['Audio URL'] || '';
  let ext = 'audio';
  try {{
    const path = new URL(url).pathname;
    const tail = path.split('.').pop();
    if (tail && tail.length <= 5) ext = tail;
  }} catch (e) {{ /* ignore */ }}
  const id = (row['Call ID'] || 'unknown').replace(/[^A-Za-z0-9_-]/g, '_');
  return `${{id}}.${{ext}}`;
}}

const modal = document.getElementById('export-modal');
const modalSub = document.getElementById('modal-sub');
const optAudioHint = document.getElementById('opt-audio-hint');
const optBothHint = document.getElementById('opt-both-hint');
const progressWrap = document.getElementById('modal-progress');
const progressText = document.getElementById('progress-text');
const progressBar = document.getElementById('progress-bar');

let currentExport = null;

function setOptDisabled(disabled) {{
  modal.querySelectorAll('button.opt').forEach(b => {{ b.disabled = disabled; }});
}}

function audioEnabled() {{ return SERVER_MODE; }}

function makeGroup(name, rows) {{
  return {{ name, rows, audioRows: rows.filter(r => r['Audio URL']) }};
}}

function openExportDialog(config) {{
  // config: {{ kind: 'single' | 'triple', groups: [{{name, rows, audioRows}}], filenameHint }}
  const totalRows = config.groups.reduce((s, g) => s + g.rows.length, 0);
  if (!totalRows) {{ showToast('该选项无数据'); return; }}
  const totalAudio = config.groups.reduce((s, g) => s + g.audioRows.length, 0);
  currentExport = Object.assign({{ cancelled: false }}, config);

  let summary;
  if (config.kind === 'triple') {{
    const parts = config.groups.map(g => `${{g.name}} <b>${{g.rows.length}}</b>`).join(' · ');
    summary = `将分类导出 ${{parts}}（共 <b>${{totalRows}}</b> 通 · <b>${{totalAudio}}</b> 个录音）`;
  }} else {{
    summary = `将导出 <b>${{totalRows}}</b> 通通话 · 其中 <b>${{totalAudio}}</b> 通有录音`;
  }}
  modalSub.innerHTML = summary;

  const audioOk = totalAudio > 0 && audioEnabled();
  if (!audioEnabled()) {{
    optAudioHint.textContent = '需用 serve_dashboard.py 启动';
    optBothHint.textContent = '需用 serve_dashboard.py 启动';
  }} else if (totalAudio === 0) {{
    optAudioHint.textContent = '无录音可下载';
    optBothHint.textContent = '仅 Excel';
  }} else {{
    optAudioHint.textContent = `${{totalAudio}} 个录音 → 一个 zip`;
    optBothHint.textContent = `xlsx + ${{totalAudio}} 个录音 → 一个 zip`;
  }}
  modal.querySelector('[data-mode="audio"]').disabled = !audioOk;
  modal.querySelector('[data-mode="both"]').disabled = totalRows === 0 || (!audioEnabled() && totalAudio > 0);

  progressWrap.style.display = 'none';
  setOptDisabled(false);
  if (!audioOk) modal.querySelector('[data-mode="audio"]').disabled = true;
  if (!audioEnabled() && totalAudio > 0) modal.querySelector('[data-mode="both"]').disabled = true;
  document.getElementById('modal-server-note').style.display = (!audioEnabled() && totalAudio > 0) ? 'block' : 'none';
  modal.classList.add('show');
}}

function closeExportDialog() {{
  if (currentExport) currentExport.cancelled = true;
  modal.classList.remove('show');
  currentExport = null;
}}

document.getElementById('modal-cancel').addEventListener('click', closeExportDialog);
modal.addEventListener('click', e => {{ if (e.target === modal) closeExportDialog(); }});

async function runExport(mode) {{
  if (!currentExport) return;
  const {{ groups, filenameHint, kind }} = currentExport;
  const scope = buildScopeName();
  const useFolders = kind === 'triple';
  const zipBase = safeFilename(`agora-${{scope}}-${{filenameHint}}`);

  // EXCEL-ONLY ───────────────────────────────────
  if (mode === 'excel') {{
    if (kind === 'single') {{
      const g = groups[0];
      const name = safeFilename(`${{zipBase}}-n${{g.rows.length}}`) + '.xlsx';
      XLSX.writeFile(buildWorkbook(g.rows), name);
      showToast(`已导出 ${{g.rows.length}} 通 → ${{name}}`);
    }} else {{
      // Triple: pack 3 xlsx into one zip via server (no audio).
      if (!SERVER_MODE) {{
        // file:// fallback: download 3 xlsx sequentially via SheetJS.
        setOptDisabled(true);
        progressWrap.style.display = 'block';
        for (let i = 0; i < groups.length; i++) {{
          const g = groups[i];
          if (!g.rows.length) continue;
          progressText.textContent = `导出 ${{i+1}}/${{groups.length}}: ${{g.name}}`;
          progressBar.style.width = `${{((i+1) / groups.length) * 100}}%`;
          const name = safeFilename(`${{zipBase}}-${{g.name}}-n${{g.rows.length}}`) + '.xlsx';
          XLSX.writeFile(buildWorkbook(g.rows), name);
          await new Promise(r => setTimeout(r, 400));
        }}
        showToast('已导出 3 个 xlsx 文件');
      }} else {{
        setOptDisabled(true);
        progressWrap.style.display = 'block';
        progressText.textContent = '打包 zip…';
        progressBar.style.width = '60%';
        const serverGroups = groups.filter(g => g.rows.length > 0).map(g => ({{
          folder: g.name,
          xlsx_b64: workbookBase64(g.rows),
          xlsx_filename: safeFilename(`${{g.name}}-n${{g.rows.length}}`) + '.xlsx',
          files: [],
        }}));
        await dispatchAudioZip(zipBase + '.zip', serverGroups);
        progressBar.style.width = '100%';
        showToast(`已导出 3 个 xlsx → ${{zipBase}}.zip`);
      }}
    }}
    closeExportDialog();
    return;
  }}

  // AUDIO / BOTH ─────────────────────────────────
  if (!SERVER_MODE) {{
    showToast('请用 serve_dashboard.py 启动后再下载录音');
    return;
  }}

  // Safety threshold: warn before pulling huge batches. Each audio is ~1MB and the
  // server zips in memory, so 500+ recordings means a multi-GB zip / multi-minute wait.
  const totalAudio = groups.reduce((s, g) => s + g.audioRows.length, 0);
  const HARD_LIMIT = 500;
  if (totalAudio > HARD_LIMIT) {{
    const ok = confirm(`即将下载 ${{totalAudio}} 个录音（预计 ${{Math.ceil(totalAudio * 0.8 / 60)}} 分钟，zip 体积约 ${{Math.ceil(totalAudio)}} MB）。\\n超过 ${{HARD_LIMIT}} 通建议先用漏斗/过滤缩小范围。继续吗？`);
    if (!ok) {{ setOptDisabled(false); progressWrap.style.display = 'none'; return; }}
  }}

  setOptDisabled(true);
  progressWrap.style.display = 'block';

  // ~0.8s per audio file with 16 parallel workers, plus 2s baseline for handshake/zip
  const estimatedSec = Math.max(3, totalAudio * 0.8 + 2);
  const fakeTimer = startFakeProgress(estimatedSec, '服务端拉取录音中…');

  const serverGroups = groups.filter(g => g.rows.length > 0).map(g => {{
    const item = {{
      folder: useFolders ? g.name : '',
      files: g.audioRows.map(r => ({{ filename: audioFilename(r), url: r['Audio URL'] }})),
    }};
    if (mode === 'both' && g.rows.length) {{
      item.xlsx_b64 = workbookBase64(g.rows);
      item.xlsx_filename = useFolders
        ? safeFilename(`${{g.name}}-n${{g.rows.length}}`) + '.xlsx'
        : safeFilename(`${{zipBase}}-n${{g.rows.length}}`) + '.xlsx';
    }}
    return item;
  }});

  try {{
    await dispatchAudioZip(zipBase + '.zip', serverGroups);
    stopFakeProgress(fakeTimer);
    progressBar.style.width = '100%';
    progressText.textContent = '已交给浏览器下载 · 进度看浏览器下载区';
    showToast(`服务端正在流式打包 → ${{zipBase}}.zip（浏览器自己接收）`);
  }} catch (err) {{
    stopFakeProgress(fakeTimer);
    showToast(`服务端拒绝: ${{err.message}}`);
  }}
  setTimeout(closeExportDialog, 1500);
}}

function startFakeProgress(estimatedSec, label) {{
  progressBar.style.width = '0%';
  const startMs = Date.now();
  progressText.textContent = `${{label}} 0s`;
  const handle = setInterval(() => {{
    const elapsed = (Date.now() - startMs) / 1000;
    // Asymptotic ease-out: approaches 95% but never reaches it
    const pct = 95 * (1 - Math.exp(-elapsed / (estimatedSec * 0.4)));
    progressBar.style.width = pct.toFixed(1) + '%';
    progressText.textContent = `${{label}} ${{elapsed.toFixed(0)}}s · 预计 ~${{estimatedSec.toFixed(0)}}s`;
  }}, 200);
  return handle;
}}

function stopFakeProgress(handle) {{
  if (handle) clearInterval(handle);
}}

modal.querySelectorAll('button.opt').forEach(btn => {{
  btn.addEventListener('click', () => runExport(btn.getAttribute('data-mode')));
}});

function exportRows(rows, filenameHint) {{
  openExportDialog({{ kind: 'single', groups: [makeGroup('export', rows)], filenameHint }});
}}

function exportTurnTriple(turnId) {{
  const groups = turnTriple(turnId, scopedRows()).map(g => makeGroup(g.name, g.rows));
  openExportDialog({{ kind: 'triple', groups, filenameHint: `turn-id${{turnId}}` }});
}}

// Single-group filters
const FILTERS = {{
  funnel: (idx, scope) => {{
    // 顺序：0 拨打 / 1 真人 / 2 意向 / 3 完整 / 4 带车型完整
    const fns = [r => true, r => r._human, r => r._intent, r => r._full, r => r._full_with_model];
    return [scope.filter(fns[idx]), `funnel-${{['all','human','intent','full','full-with-model'][idx]}}`];
  }},
  duration: (sec, scope) => {{
    return [scope.filter(r => r._human && r._duration === sec), `duration-${{sec}}s`];
  }},
  earlyHangup: (n, scope) => {{
    return [scope.filter(r => r._human && r._assistant_turns === n), `hangup-${{n}}sentence`];
  }},
}};

// Triple groups for turn distribution: each click on a turn_id yields 3 buckets
function turnTriple(turnId, scope) {{
  const base = scope.filter(r => r._human && r._max_turn === turnId);
  return [
    {{ name: '真人接听',  rows: base }},
    {{ name: '完整转换',  rows: base.filter(r => r._full) }},
    {{ name: '意向客户',  rows: base.filter(r => r._intent) }},
  ];
}}

function renderHero(totals, denominators) {{
  // Per-card denominator selection (per boss request):
  //   0 拨打总数   — no percentages at all
  //   1 接听       — only 占总
  //   2 真人接听   — 占总 + 占接听
  //   3 完整转换   — 占总 + 占接听 + 占真人
  //   4 意向客户   — 占总 + 占接听 + 占真人
  // Indices map to which denominator keys apply (rest hidden, not "—").
  const DENS = {{
    total: {{ label: '占总',   value: denominators.total }},
    human: {{ label: '占真人', value: denominators.human }},
    full:  {{ label: '占完整', value: denominators.full }},
  }};
  const SHOW = [
    [],                              // 0 拨打总数
    ['total'],                       // 1 真人接听
    ['total', 'human'],              // 2 意向客户
    ['total', 'human'],              // 3 完整转换
    ['total', 'human', 'full'],      // 4 带车型完整转换
  ];
  const html = totals.labels.map((label, i) => {{
    const v = totals.values[i];
    const keys = SHOW[i] || [];
    const rows = keys.map(k => {{
      const d = DENS[k];
      if (!d || !d.value) return `<div><span class="lbl">${{d ? d.label : ''}}</span><span class="num">—</span></div>`;
      const pct = (v / d.value * 100).toFixed(1);
      return `<div><span class="lbl">${{d.label}}</span><span class="num">${{pct}}%</span></div>`;
    }}).join('');
    return `<div class="stat"><div class="label">${{label}}</div><div class="val">${{v}}</div><div class="pcts">${{rows}}</div></div>`;
  }}).join('');
  document.getElementById('hero-stats').innerHTML = html;
}}

function renderFunnel(totals) {{
  const total = totals.values[0] || 1;
  const data = totals.labels.map((name, i) => ({{
    name, value: totals.values[i],
    pct: (totals.values[i] / total * 100).toFixed(1),
  }}));
  charts['chart-funnel'].setOption({{
    color: PALETTE,
    tooltip: tooltipBase({{
      trigger: 'item',
      formatter: p => `<b>${{p.data.name}}</b><br>数量: <b>${{p.data.value}}</b><br>占总: ${{p.data.pct}}%`,
    }}),
    series: [{{
      type: 'funnel',
      sort: 'descending',
      gap: 4,
      left: '6%', right: '6%', top: '2%', bottom: '2%',
      label: {{
        show: true, position: 'inside', color: '#ffffff', fontWeight: 600,
        formatter: p => `${{p.data.name}}  ${{p.data.value}}`,
      }},
      labelLine: {{ show: false }},
      itemStyle: {{ borderColor: '#ffffff', borderWidth: 2 }},
      emphasis: {{ label: {{ fontSize: 14 }} }},
      data,
    }}],
  }}, true);
}}

function renderTurnTriad(td) {{
  // Three independent cards (真人 / 完整转换 / 意向). Each card: left bar (absolute
  // counts at each turn) + right donut (each bar's share of that subset's total).
  TURN_SERIES.forEach((spec, i) => {{
    const series = td.series[i];
    const total = series.data.reduce((s, v) => s + v, 0) || 1;
    // 平均轮次 = sum(turn_id * count) / sum(count). td.x 是 1..N。
    const weightedSum = series.data.reduce((s, v, idx) => s + v * td.x[idx], 0);
    const avgTurn = total > 0 ? (weightedSum / total) : 0;
    // 更新卡片标题旁的"平均"小字
    const titleEl = document.querySelector(`#${{spec.barId}}`)
      .closest('.turn-card')?.querySelector('.turn-card-title');
    if (titleEl) {{
      const baseTitle = titleEl.dataset.baseTitle || titleEl.textContent;
      titleEl.dataset.baseTitle = baseTitle;
      titleEl.innerHTML = `${{baseTitle}}  <span style="color:var(--muted); font-weight:400; font-size:11px; margin-left:6px;">平均轮次 <b style="color:var(--text); font-variant-numeric: tabular-nums;">${{avgTurn.toFixed(1)}}</b></span>`;
    }}

    // Trim trailing zeros so the visible bars fill the chart area instead of
    // huddling on the left half. Each subset has its own tail length (真人接听
    // typically goes furthest; 完整转换/意向 shorter), so cards stay independent.
    let lastIdx = series.data.length - 1;
    while (lastIdx > 0 && series.data[lastIdx] === 0) lastIdx--;
    const xTrim = td.x.slice(0, lastIdx + 1);
    const dataTrim = series.data.slice(0, lastIdx + 1);

    // Bar
    charts[spec.barId].setOption({{
      color: [spec.color],
      tooltip: tooltipBase({{
        trigger: 'axis', axisPointer: {{ type: 'shadow' }},
        formatter: params => {{
          const p = params[0];
          const pct = (p.value / total * 100).toFixed(1);
          return `<b>turn_id ${{p.name}}</b><br>${{p.value}} 通 · ${{pct}}% / ${{spec.name.split(' ')[0]}}`;
        }},
      }}),
      // Tight margins + containLabel = ECharts auto-fits the axis-label gutter
      // and the plot fills the canvas. nameGap pulls the axis-name label closer.
      grid: {{ left: 4, right: 8, top: 22, bottom: 4, containLabel: true }},
      xAxis: Object.assign({{ type: 'category', name: 'max turn_id', data: xTrim,
                              nameGap: 18,
                              axisLabel: {{ color: MUTED, fontSize: 10, interval: 0,
                                            rotate: xTrim.length > 18 ? 35 : 0 }} }}, baseAxis),
      yAxis: Object.assign({{ type: 'value', name: `n=${{total}}`, nameGap: 8 }}, baseAxis),
      series: [{{
        name: spec.name, type: 'bar', data: dataTrim,
        itemStyle: {{ borderRadius: [3, 3, 0, 0] }},
        label: {{
          show: true, position: 'top', color: MUTED, fontSize: 10,
          formatter: p => p.value > 0 ? `${{(p.value / total * 100).toFixed(0)}}%` : '',
        }},
      }}],
    }}, true);

    // Donut: each non-zero turn bin is a slice, percentage relative to subset total.
    // Title placed in the donut hole shows the subset's grand total (n).
    const slices = series.data
      .map((v, j) => ({{ name: `${{td.x[j]}} 轮`, value: v }}))
      .filter(d => d.value > 0);
    charts[spec.donutId].setOption({{
      color: [spec.color, '#60a5fa', '#a855f7', '#06b6d4', '#10b981', '#f43f5e', '#f59e0b', '#0ea5e9', '#a3e635', '#fb7185'],
      title: {{
        text: `${{total}}`,
        subtext: '通',
        left: '50%', top: '48%',
        textAlign: 'center', textVerticalAlign: 'middle',
        textStyle: {{ fontSize: 22, fontWeight: 700, color: TEXT }},
        subtextStyle: {{ fontSize: 11, color: MUTED }},
        itemGap: 2,
      }},
      tooltip: tooltipBase({{
        trigger: 'item',
        formatter: p => `<b>${{p.name}}</b><br>${{p.value}} 通 · ${{p.percent.toFixed(1)}}%`,
      }}),
      legend: {{ show: false }},
      series: [{{
        type: 'pie', radius: ['52%', '74%'], center: ['50%', '52%'],
        avoidLabelOverlap: true,
        itemStyle: {{ borderColor: '#ffffff', borderWidth: 1.5 }},
        label: {{
          color: TEXT, fontSize: 11,
          formatter: p => p.percent >= 4 ? `${{p.name}}\\n${{p.percent.toFixed(0)}}%` : '',
        }},
        labelLine: {{ length: 6, length2: 4, lineStyle: {{ color: MUTED }} }},
        data: slices,
      }}],
    }}, true);
  }});
}}

function renderDuration(dd) {{
  // 3 个 series — 真人接听 (橙) / 完整转换 (青) / 意向客户 (紫)
  // legend 的 label 上同时显示该 series 的平均时长 (来自后端预计算)
  const SERIES_COLORS = ['#f59e0b', '#06b6d4', '#a855f7'];
  const legendData = dd.series.map(s => ({{
    name: s.name,
    icon: 'roundRect',
  }}));
  charts['chart-duration'].setOption({{
    color: SERIES_COLORS,
    tooltip: tooltipBase({{
      trigger: 'axis', axisPointer: {{ type: 'shadow' }},
      formatter: params => {{
        let html = `<b>${{params[0].name}} 秒</b><br>`;
        params.forEach(p => {{
          if (p.value > 0) html += `${{p.marker}} ${{p.seriesName}}: <b>${{p.value}}</b> 通<br>`;
        }});
        return html;
      }},
    }}),
    legend: {{
      data: legendData, textStyle: {{ color: TEXT, fontSize: 12 }}, top: 8,
      formatter: name => {{
        const s = dd.series.find(x => x.name === name);
        if (!s) return name;
        return `${{name}}  (n=${{s.n}} · 平均 ${{s.avg}}s)`;
      }},
    }},
    grid: Object.assign({{}}, baseGrid, {{ bottom: 56, top: 38 }}),
    xAxis: Object.assign({{ type: 'category', name: '秒', data: dd.x, axisLabel: {{ color: MUTED, fontSize: 10, interval: 0 }} }}, baseAxis),
    yAxis: Object.assign({{ type: 'value', name: '通话数' }}, baseAxis),
    dataZoom: [
      {{ type: 'inside', xAxisIndex: 0 }},
      {{ type: 'slider', xAxisIndex: 0, height: 18, bottom: 16, borderColor: BORDER, backgroundColor: '#f8fafc',
         fillerColor: 'rgba(37,99,235,0.12)', handleStyle: {{ color: '#2563eb' }}, textStyle: {{ color: MUTED, fontSize: 10 }} }},
    ],
    series: dd.series.map((s, i) => ({{
      name: s.name, type: 'bar', data: s.data,
      itemStyle: {{ borderRadius: [3, 3, 0, 0] }},
      // 用 markLine 在该 series 的平均时长位置画一条垂直虚线
      markLine: s.avg ? {{
        symbol: 'none',
        lineStyle: {{ color: SERIES_COLORS[i], type: 'dashed', width: 1.5, opacity: 0.8 }},
        label: {{
          formatter: `μ=${{s.avg}}s`, color: SERIES_COLORS[i], fontSize: 10,
          position: 'insideEndTop',
        }},
        data: [{{ xAxis: String(Math.round(s.avg)) }}],
      }} : undefined,
    }})),
  }}, true);
}}

// View mode for the 首句挂断 · Duration chart: 'all' or 'short' (<10s only).
let firstSentenceView = 'all';

function firstSentenceData(viewMode) {{
  // Compute the histogram fresh from DATA.rows so the toggle (and any future
  // filter) doesn't need a backend round-trip. Restricted to the current
  // dropdown scope as usual.
  const base = scopedRows().filter(r => r._human && r._assistant_turns === 1);
  const rows = viewMode === 'short' ? base.filter(r => r._duration < 10) : base;
  let maxSec = 0;
  rows.forEach(r => {{ if (r._duration > maxSec) maxSec = r._duration; }});
  // Always show at least 0..5s buckets so even an empty/short view looks like a chart.
  if (viewMode === 'short') maxSec = Math.min(Math.max(maxSec, 5), 9);
  else maxSec = Math.max(maxSec, 5);
  const data = new Array(maxSec + 1).fill(0);
  rows.forEach(r => {{ if (r._duration >= 0 && r._duration <= maxSec) data[r._duration]++; }});
  return {{ x: data.map((_, i) => String(i)), data, n: rows.length, viewMode }};
}}

function renderFirstSentenceDur() {{
  const fd = firstSentenceData(firstSentenceView);
  if (!fd.n) {{
    charts['chart-first-sentence-dur'].clear();
    charts['chart-first-sentence-dur'].setOption({{
      title: {{ text: '无首句挂断数据', left: 'center', top: 'middle',
                textStyle: {{ color: MUTED, fontSize: 12, fontWeight: 'normal' }} }},
    }});
    return;
  }}
  const total = fd.n;
  let lastIdx = fd.data.length - 1;
  while (lastIdx > 0 && fd.data[lastIdx] === 0) lastIdx--;
  const x = fd.x.slice(0, lastIdx + 1);
  const data = fd.data.slice(0, lastIdx + 1);
  const viewLabel = fd.viewMode === 'short' ? '短挂断' : '首句挂断';

  charts['chart-first-sentence-dur'].setOption({{
    color: ['#f43f5e'],
    tooltip: tooltipBase({{
      trigger: 'axis', axisPointer: {{ type: 'shadow' }},
      formatter: params => {{
        const p = params[0];
        const pct = (p.value / total * 100).toFixed(1);
        return `<b>${{p.name}} 秒</b><br>${{p.value}} 通 · ${{pct}}% / ${{viewLabel}} (n=${{total}})`;
      }},
    }}),
    grid: {{ left: 4, right: 8, top: 22, bottom: 4, containLabel: true }},
    xAxis: Object.assign({{ type: 'category', name: '秒', data: x, nameGap: 18,
                            axisLabel: {{ color: MUTED, fontSize: 10, interval: 0,
                                          rotate: x.length > 15 ? 35 : 0 }} }}, baseAxis),
    yAxis: Object.assign({{ type: 'value', name: `n=${{total}}`, nameGap: 8 }}, baseAxis),
    series: [{{
      name: viewLabel, type: 'bar', data,
      itemStyle: {{ borderRadius: [3, 3, 0, 0] }},
      label: {{
        show: true, position: 'top', color: MUTED, fontSize: 10,
        formatter: p => p.value > 0 ? `${{(p.value / total * 100).toFixed(0)}}%` : '',
      }},
    }}],
  }}, true);
}}

function renderEarlyHangupTable(rows) {{
  const el = document.getElementById('early-hangup-table');
  if (!rows.length) {{ el.innerHTML = '<div class="empty">无真人接听数据</div>'; return; }}
  el.innerHTML = `<table><thead><tr><th>类别</th><th style="text-align:right;">数量</th><th style="text-align:right;">占真人接听</th></tr></thead><tbody>${{
    rows.map((r, i) => {{
      const w = Math.min(r.pct, 100);
      const cls = r.is_subset ? 'clickable subset' : 'clickable';
      const labelHtml = r.is_subset ? `<span style="color:var(--muted); margin-right:6px;">└</span>${{r.label}}` : r.label;
      // 用 data-n 给互斥分桶 (1-5)；data-special 给 10s 内首句挂断
      const dataAttr = r.is_subset ? `data-special="first-10s"` : `data-n="${{i+1}}"`;
      return `<tr class="${{cls}}" ${{dataAttr}}><td>${{labelHtml}}</td><td class="num">${{r.count}}</td><td class="pct-bar"><div class="fill" style="width:${{w}}%;"></div><span class="pct-text">${{r.pct}}%</span></td></tr>`;
    }}).join('')
  }}</tbody></table>`;
  el.querySelectorAll('tr.clickable').forEach(tr => {{
    tr.addEventListener('click', () => {{
      const special = tr.getAttribute('data-special');
      if (special === 'first-10s') {{
        const subset = scopedRows().filter(r => r._human && r._assistant_turns === 1 && r._duration < 10);
        exportRows(subset, 'first-hangup-under-10s');
        return;
      }}
      const n = parseInt(tr.getAttribute('data-n'), 10);
      const [rows, hint] = FILTERS.earlyHangup(n, scopedRows());
      exportRows(rows, hint);
    }});
  }});
}}

function renderFieldCountDist(fc) {{
  // Bar (left 2/3) + donut (right 1/3), styled to match the turn-distribution
  // cards. X is "# of non-null collected fields" (0..N). Each bar shows the
  // count of human-answered calls with that many slots filled; label = share
  // of the 真人接听 total. Donut shows the same data as a per-bucket pie.
  const total = fc && fc.n ? fc.n : 0;
  if (!total) {{
    charts['chart-field-count'].clear();
    charts['chart-field-count-donut'].clear();
    return;
  }}
  // bar
  charts['chart-field-count'].setOption({{
    color: ['#06b6d4'],
    tooltip: tooltipBase({{
      trigger: 'axis', axisPointer: {{ type: 'shadow' }},
      formatter: params => {{
        const p = params[0];
        const pct = (p.value / total * 100).toFixed(1);
        return `<b>采集 ${{p.name}} 个字段</b><br>${{p.value}} 通 · ${{pct}}% / 真人接听 (n=${{total}})`;
      }},
    }}),
    grid: {{ left: 4, right: 8, top: 22, bottom: 4, containLabel: true }},
    xAxis: Object.assign({{ type: 'category', name: '字段数', data: fc.x,
                            nameGap: 18,
                            axisLabel: {{ color: MUTED, fontSize: 11, interval: 0 }} }}, baseAxis),
    yAxis: Object.assign({{ type: 'value', name: `n=${{total}}`, nameGap: 8 }}, baseAxis),
    series: [{{
      name: '收集字段数量', type: 'bar', data: fc.data,
      itemStyle: {{ borderRadius: [3, 3, 0, 0] }},
      label: {{
        show: true, position: 'top', color: MUTED, fontSize: 10,
        formatter: p => p.value > 0 ? `${{(p.value / total * 100).toFixed(0)}}%` : '',
      }},
    }}],
  }}, true);
  // donut
  const slices = fc.data.map((v, i) => ({{ name: `${{fc.x[i]}} 个`, value: v }})).filter(d => d.value > 0);
  charts['chart-field-count-donut'].setOption({{
    color: ['#06b6d4', '#0ea5e9', '#3b82f6', '#6366f1', '#8b5cf6', '#a855f7', '#d946ef', '#f43f5e'],
    title: {{
      text: `${{total}}`, subtext: '通',
      left: '50%', top: '48%',
      textAlign: 'center', textVerticalAlign: 'middle',
      textStyle: {{ fontSize: 22, fontWeight: 700, color: TEXT }},
      subtextStyle: {{ fontSize: 11, color: MUTED }},
      itemGap: 2,
    }},
    tooltip: tooltipBase({{
      trigger: 'item',
      formatter: p => `<b>${{p.name}}</b><br>${{p.value}} 通 · ${{p.percent.toFixed(1)}}%`,
    }}),
    legend: {{ show: false }},
    series: [{{
      type: 'pie', radius: ['52%', '74%'], center: ['50%', '52%'],
      avoidLabelOverlap: true,
      itemStyle: {{ borderColor: '#ffffff', borderWidth: 1.5 }},
      label: {{
        color: TEXT, fontSize: 11,
        formatter: p => p.percent >= 4 ? `${{p.name}}\\n${{p.percent.toFixed(0)}}%` : '',
      }},
      labelLine: {{ length: 6, length2: 4, lineStyle: {{ color: MUTED }} }},
      data: slices,
    }}],
  }}, true);
}}

function renderFullConversionDrill(drill) {{
  const el = document.getElementById('full-conv-drill');
  if (!drill || !drill.full_count) {{
    el.innerHTML = '<div class="empty">当前 scope 没有"完整转换 (≥3 槽位)"通话</div>';
    return;
  }}
  const fullPct = (drill.full_count / Math.max(1, drill.total_human) * 100).toFixed(1);
  const sharePctOf = (n) => drill.full_count ? (n / drill.full_count * 100).toFixed(0) : 0;
  // missing breakdown rows — only show slots that actually have missing counts
  const missingTotal = Object.values(drill.missing_3of4).reduce((s, v) => s + v, 0);
  const order = ['车型', '时间', '城市', '姓名'];
  const rows = order.map(slot => {{
    const n = drill.missing_3of4[slot] || 0;
    const pct = missingTotal ? (n / missingTotal * 100) : 0;
    return `<div class="row">
      <span class="label">缺 ${{slot}}</span>
      <div class="bar"><div style="width:${{Math.min(pct, 100)}}%;"></div></div>
      <span class="val">${{n}} · ${{pct.toFixed(0)}}%</span>
    </div>`;
  }}).join('');

  el.innerHTML = `
    <div class="drill-head">
      <span class="num">${{drill.full_count}}</span>
      <span class="lbl">完整转换 通话 · ${{fullPct}}% / 真人接听 (${{drill.total_human}})</span>
    </div>
    <div class="drill">
      <div class="drill-split">
        <div class="seg full">
          <div class="lbl">4 / 4 全齐</div>
          <div class="num">${{drill.exactly_4}}</div>
          <div class="pct">${{sharePctOf(drill.exactly_4)}}% / 完整转换</div>
        </div>
        <div class="seg three">
          <div class="lbl">仅 3 / 4 满足</div>
          <div class="num">${{drill.exactly_3}}</div>
          <div class="pct">${{sharePctOf(drill.exactly_3)}}% / 完整转换</div>
        </div>
      </div>
      <div class="drill-miss">
        <h4>仅 3/4 的通话里 · 缺哪个槽位</h4>
        ${{missingTotal ? rows : '<div style="color:var(--muted); font-size:12px;">（无仅 3/4 满足通话）</div>'}}
      </div>
    </div>
  `;
}}

function render(key) {{
  const d = DATA.datasets[key];
  renderHero(d.totals, d.denominators);
  renderFunnel(d.totals);
  renderTurnTriad(d.turn_dist);
  renderDuration(d.duration_dist);
  renderFieldCountDist(d.field_count_dist);
  renderFullConversionDrill(d.full_conversion_drill);
  renderEarlyHangupTable(d.early_hangup);
  renderFirstSentenceDur();
}}

const sel = document.getElementById('agent-select');
sel.addEventListener('change', e => {{ currentAgentKey = e.target.value; render(e.target.value); }});
render(sel.value);

// Chart click → export
charts['chart-funnel'].on('click', p => {{
  if (p.componentType !== 'series') return;
  const idx = DATA.datasets[currentAgentKey].totals.labels.indexOf(p.data.name);
  if (idx < 0) return;
  const [rows, hint] = FILTERS.funnel(idx, scopedRows());
  exportRows(rows, hint);
}});

// Each of the three turn-distribution bar charts: clicking a bar exports just
// that subset at that turn_id (no triple bundle — the cards are already split).
TURN_SERIES.forEach(spec => {{
  charts[spec.barId].on('click', p => {{
    if (p.componentType !== 'series') return;
    const turnId = parseInt(p.name, 10);
    const subset = scopedRows().filter(spec.filter).filter(r => r._max_turn === turnId);
    exportRows(subset, `turn-${{spec.key}}-id${{turnId}}`);
  }});
}});

charts['chart-field-count'].on('click', p => {{
  if (p.componentType !== 'series') return;
  const n = parseInt(p.name, 10);
  const subset = scopedRows().filter(r => r._human && r._field_count === n);
  exportRows(subset, `fields-${{n}}`);
}});

// 首句挂断 Duration bar: click → export the calls hung up at exactly that
// second. Respects the current view (all vs short < 10s).
charts['chart-first-sentence-dur'].on('click', p => {{
  if (p.componentType !== 'series') return;
  const sec = parseInt(p.name, 10);
  const base = scopedRows().filter(r => r._human && r._assistant_turns === 1 && r._duration === sec);
  const subset = firstSentenceView === 'short' ? base.filter(r => r._duration < 10) : base;
  const tag = firstSentenceView === 'short' ? 'short' : 'all';
  exportRows(subset, `first-sentence-${{sec}}s-${{tag}}`);
}});

// 首句挂断 视角 toggle
document.getElementById('fs-view-toggle').addEventListener('click', e => {{
  const btn = e.target.closest('button[data-view]');
  if (!btn) return;
  const view = btn.getAttribute('data-view');
  if (view === firstSentenceView) return;
  firstSentenceView = view;
  document.querySelectorAll('#fs-view-toggle button').forEach(b => {{
    b.classList.toggle('active', b.getAttribute('data-view') === view);
  }});
  renderFirstSentenceDur();
}});

charts['chart-duration'].on('click', p => {{
  if (p.componentType !== 'series') return;
  const sec = parseInt(p.name, 10);
  const [rows, hint] = FILTERS.duration(sec, scopedRows());
  exportRows(rows, hint);
}});

window.addEventListener('resize', () => {{
  Object.values(charts).forEach(c => c.resize());
}});

// ──────────────────────────────────────────────────────────────────────────
// Tab 2 · Agent 闯关分桶
// ──────────────────────────────────────────────────────────────────────────

const T2 = DATA.tab2 || null;
let t2CurrentAgentKey = '__ALL__';
let t2CurrentBucketLv = 0;

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function t2CurrentData() {{
  if (!T2) return null;
  if (t2CurrentAgentKey === '__ALL__') return T2.global;
  return T2.agents.find(a => a.label === t2CurrentAgentKey) || T2.global;
}}

function renderT2AgentSelect() {{
  if (!T2) return;
  const sel = document.getElementById('t2-agent-select');
  const opts = [
    `<option value="__ALL__">全部 (n=${{T2.global.n_total}}, 有效会话 ${{T2.global.n_valid}})</option>`,
  ];
  T2.agents.forEach(a => {{
    opts.push(`<option value="${{escapeHtml(a.label)}}">${{escapeHtml(a.label)}} (有效 ${{a.n_valid}})</option>`);
  }});
  sel.innerHTML = opts.join('');
  sel.addEventListener('change', e => {{
    t2CurrentAgentKey = e.target.value;
    renderT2All();
  }});
}}

function renderT2Scope() {{
  // 桑基图：真人接听 → {{客户全程未开口, 有效会话}} → {{首句挂断, 接通无应答, 0/1/2/3/4 关}}
  const d = t2CurrentData();
  if (!d) return;

  // 节点定义
  const nodes = [
    {{ name: `真人接听\\n${{d.n_human}}` }},
    {{ name: `客户未开口\\n${{d.n_silent_total}}` }},
    {{ name: `有效会话\\n${{d.n_valid}}` }},
    {{ name: `首句挂断\\n${{d.n_first_hangup}}` }},
    {{ name: `接通无应答\\n${{d.n_silence_or_ivr}}` }},
  ];
  const links = [
    {{ source: `真人接听\\n${{d.n_human}}`,    target: `客户未开口\\n${{d.n_silent_total}}`, value: d.n_silent_total }},
    {{ source: `真人接听\\n${{d.n_human}}`,    target: `有效会话\\n${{d.n_valid}}`,         value: d.n_valid }},
    {{ source: `客户未开口\\n${{d.n_silent_total}}`, target: `首句挂断\\n${{d.n_first_hangup}}`,   value: d.n_first_hangup }},
    {{ source: `客户未开口\\n${{d.n_silent_total}}`, target: `接通无应答\\n${{d.n_silence_or_ivr}}`, value: d.n_silence_or_ivr }},
  ];
  // 有效会话 → 5 个通关分桶
  d.buckets.forEach(b => {{
    const lvName = b.level === 0 ? '0 关 (一关没过)' : `${{b.level}} 关`;
    nodes.push({{ name: `${{lvName}}\\n${{b.count}}` }});
    links.push({{ source: `有效会话\\n${{d.n_valid}}`, target: `${{lvName}}\\n${{b.count}}`, value: b.count }});
  }});

  // 过滤掉 value=0 的 link，否则 ECharts 会画细线噪点
  const linksFiltered = links.filter(l => l.value > 0);
  const usedNodes = new Set(linksFiltered.flatMap(l => [l.source, l.target]));
  const nodesFiltered = nodes.filter(n => usedNodes.has(n.name));

  // 配色（按业务语义）
  const colorMap = {{}};
  nodesFiltered.forEach(n => {{
    if (n.name.startsWith('真人接听'))     colorMap[n.name] = '#2563eb';
    else if (n.name.startsWith('客户未开口')) colorMap[n.name] = '#f43f5e';
    else if (n.name.startsWith('有效会话')) colorMap[n.name] = '#10b981';
    else if (n.name.startsWith('首句挂断'))  colorMap[n.name] = '#f43f5e';
    else if (n.name.startsWith('接通无应答'))colorMap[n.name] = '#fb923c';
    else if (n.name.startsWith('0 关'))     colorMap[n.name] = '#94a3b8';
    else if (n.name.startsWith('1 关'))     colorMap[n.name] = '#f59e0b';
    else if (n.name.startsWith('2 关'))     colorMap[n.name] = '#eab308';
    else if (n.name.startsWith('3 关'))     colorMap[n.name] = '#06b6d4';
    else if (n.name.startsWith('4 关'))     colorMap[n.name] = '#10b981';
  }});

  charts['t2-chart-sankey'].setOption({{
    tooltip: tooltipBase({{
      trigger: 'item',
      formatter: p => {{
        if (p.dataType === 'edge') {{
          const sName = p.data.source.split('\\n')[0];
          const tName = p.data.target.split('\\n')[0];
          const total = d.n_human || 1;
          return `<b>${{sName}} → ${{tName}}</b><br>${{p.data.value}} 通 · ${{(p.data.value / total * 100).toFixed(1)}}% / 真人接听`;
        }}
        const parts = p.name.split('\\n');
        return `<b>${{parts[0]}}</b><br>${{parts[1] || ''}} 通`;
      }},
    }}),
    series: [{{
      type: 'sankey',
      data: nodesFiltered.map(n => {{
        const parts = n.name.split('\\n');
        const label = parts[0];
        const val = parts[1] || '';
        return {{
          name: n.name,
          itemStyle: {{ color: colorMap[n.name] || '#94a3b8' }},
          // 通关 0/1/2/3/4 节点的小数字往右挪，避免和 0 关挤
          label: {{
            position: 'right',
            formatter: () => `{{a|${{label}}}}\\n{{b|${{val}}}}`,
            rich: {{
              a: {{ color: TEXT, fontSize: 12, fontWeight: 600, lineHeight: 16 }},
              b: {{ color: MUTED, fontSize: 11, lineHeight: 14 }},
            }},
          }},
        }};
      }}),
      links: linksFiltered,
      nodeAlign: 'justify',
      nodeWidth: 16,
      nodeGap: 18,
      // 右边节点 label 文字较长，给 22% 边距；上下 +1% 防顶
      left: '3%', right: '22%', top: '3%', bottom: '3%',
      lineStyle: {{ curveness: 0.5, color: 'gradient', opacity: 0.5 }},
      emphasis: {{ focus: 'adjacency', lineStyle: {{ opacity: 0.85 }} }},
      // 给小节点一个视觉最小高度（数据真实，渲染时强制不被压成线）
      layoutIterations: 64,
    }}],
  }}, true);
}}

function renderT2Buckets() {{
  const d = t2CurrentData();
  if (!d) return;
  // 桶标题里直接写"卡在第 X 关"，让用户秒懂
  const stuckLabel = lv => lv === 0 ? '卡在 第 1 关 (车型)'
                        : lv === 1 ? '卡在 第 2 关 (城市)'
                        : lv === 2 ? '卡在 第 3 关 (时间)'
                        : lv === 3 ? '卡在 第 4 关 (姓氏)'
                        : '✓ 全过 (4/4)';
  const html = d.buckets.map(b => {{
    return `<div class="t2-bucket ${{b.level === t2CurrentBucketLv ? 'active' : ''}}" data-lv="${{b.level}}">
      <div class="label">${{b.level}} 关</div>
      <div class="ct">${{b.count}}</div>
      <div class="pct">${{b.pct_of_valid}}% / 有效会话</div>
      <div class="meta" style="font-size: 10px; font-weight: 600; color: var(--text); padding-top: 6px; padding-bottom: 6px; border-bottom: 1px solid var(--border); margin-bottom: 6px;">${{stuckLabel(b.level)}}</div>
      <div class="meta" style="border-top: none; padding-top: 0;">
        平均总轮次 <b>${{b.avg_turns}}</b><br>
        平均用户开口 <b>${{b.avg_real_user_turns}}</b><br>
        平均时长 <b>${{b.avg_duration}}</b> s
      </div>
    </div>`;
  }}).join('');
  const el = document.getElementById('t2-buckets');
  el.innerHTML = html;
  el.querySelectorAll('.t2-bucket').forEach(btn => {{
    btn.addEventListener('click', () => {{
      t2CurrentBucketLv = parseInt(btn.getAttribute('data-lv'), 10);
      renderT2Buckets();
      renderT2BucketDetail();
    }});
  }});
}}

function renderT2BucketDetail() {{
  const d = t2CurrentData();
  if (!d) return;
  const b = d.buckets.find(x => x.level === t2CurrentBucketLv);
  if (!b) return;
  const stuckText = b.level === 4 ? '全过 (没卡住)'
    : `第 ${{b.level + 1}} 关 (${{['车型','城市','时间','姓氏'][b.level]}})`;
  document.getElementById('t2-bk-title').textContent =
    `· ${{b.level}} 关桶 (n=${{b.count}}) · ${{stuckText}}`;
  document.getElementById('t2-bk-stuck').textContent = stuckText;

  // 左卡：失败原因分布
  // 0 关：用启发式原因 (该桶后端就计算过)
  // 1-3 关：从 LLM 失败画像里取该 pass_n 的 fail_category 聚合
  // 4 关：成功桶，不展示原因
  const reasonsEl = document.getElementById('t2-bk-reasons');
  if (b.level === 4) {{
    reasonsEl.innerHTML = '<div style="color: #047857; font-size: 13px; padding: 12px;">✓ 全过桶不展示失败原因。看右侧"成功 transcript"参考。</div>';
  }} else if (b.level === 0 && Object.keys(b.reasons || {{}}).length) {{
    // 0 关用启发式 + LLM 双源对比（如果 LLM 跑完了）
    const heur = b.reasons || {{}};
    const heurTotal = Object.values(heur).reduce((s, v) => s + v, 0) || 1;
    const sorted = Object.entries(heur).sort((a, b) => b[1] - a[1]);
    let html = '<div style="font-size:11px;color:var(--muted);margin-bottom:6px;">关键词启发式分类 (本地秒出，看趋势)</div>';
    html += sorted.map(([name, n]) => {{
      const pct = (n / heurTotal * 100);
      return `<div class="t2-reason-row">
        <div class="label">${{escapeHtml(name)}}</div>
        <div class="bar"><div style="width: ${{Math.min(pct, 100)}}%;"></div></div>
        <div class="val">${{n}} · ${{pct.toFixed(0)}}%</div>
      </div>`;
    }}).join('');
    // 如果有 LLM 结果，追加 LLM fail_category
    const llmBucketRes = (t2LlmResultsCache || []).filter(r => !r.error && r.pass_n === b.level);
    if (llmBucketRes.length) {{
      const cat = {{}};
      llmBucketRes.forEach(r => {{ const c = r.fail_category || '?'; cat[c] = (cat[c] || 0) + 1; }});
      const ct = Object.values(cat).reduce((s, v) => s + v, 0) || 1;
      html += `<div style="font-size:11px;color:var(--muted);margin:12px 0 6px;">LLM 失败类别 (n=${{llmBucketRes.length}})</div>`;
      html += Object.entries(cat).sort((a,b) => b[1]-a[1]).map(([name, n]) => {{
        const pct = n / ct * 100;
        return `<div class="t2-reason-row">
          <div class="label">${{escapeHtml(name)}}</div>
          <div class="bar"><div style="width: ${{Math.min(pct, 100)}}%; background: linear-gradient(90deg,#a855f7,#06b6d4);"></div></div>
          <div class="val">${{n}} · ${{pct.toFixed(0)}}%</div>
        </div>`;
      }}).join('');
    }}
    reasonsEl.innerHTML = html;
  }} else {{
    // 1-3 关：只看 LLM
    const llmBucketRes = (t2LlmResultsCache || []).filter(r => !r.error && r.pass_n === b.level);
    if (!llmBucketRes.length) {{
      reasonsEl.innerHTML = '<div style="color: var(--muted); font-size: 12px; padding: 12px;">等 LLM 失败画像跑完……或该桶样本不足。</div>';
    }} else {{
      const cat = {{}};
      llmBucketRes.forEach(r => {{ const c = r.fail_category || '?'; cat[c] = (cat[c] || 0) + 1; }});
      const ct = Object.values(cat).reduce((s, v) => s + v, 0) || 1;
      reasonsEl.innerHTML = `<div style="font-size:11px;color:var(--muted);margin-bottom:6px;">LLM 判定 (n=${{llmBucketRes.length}}, gpt-5.4)</div>` +
        Object.entries(cat).sort((a,b) => b[1]-a[1]).map(([name, n]) => {{
          const pct = n / ct * 100;
          return `<div class="t2-reason-row">
            <div class="label">${{escapeHtml(name)}}</div>
            <div class="bar"><div style="width: ${{Math.min(pct, 100)}}%; background: linear-gradient(90deg,#a855f7,#06b6d4);"></div></div>
            <div class="val">${{n}} · ${{pct.toFixed(0)}}%</div>
          </div>`;
        }}).join('');
    }}
  }}

  // 右卡：典型 transcript 案例
  renderT2BucketCases(b);
}}

function renderT2BucketCases(bucket) {{
  // 从 DATA.rows 取该桶的真实通话（_human + scope filter + _pass_n === level）
  // _pass_n 没在 rows 里，需要重新算: 用 _structured 字段 OR 按 _full / _full_with_model + _intent?
  // 简单办法：用 LLM 结果里 call_id 反查；如果没 LLM 结果，用 _structured 严格线性算 pass_n
  const inScopeRows = scopedRows().filter(r => r._human);
  // 计算每行的 pass_n（基于 _structured）
  const passN = (r) => {{
    const s = r._structured || {{}};
    const fb = (s['购车品牌'] || '').trim();
    const fm = (s['购车型号'] || '').trim();
    const fc = (s['购车城市'] || '').trim();
    const ft = (s['购车时间'] || '').trim();
    const fn = (s['购车姓名'] || '').trim();
    if (!(fb && fm)) return 0;
    if (!fc) return 1;
    if (!ft) return 2;
    if (!fn) return 3;
    return 4;
  }};
  // 找出该桶的有效会话（assistant>=2 OR 客户开口）的样本——这里简化用 _assistant_turns>=2
  const sub = inScopeRows.filter(r => passN(r) === bucket.level && r._assistant_turns >= 2);
  if (!sub.length) {{
    document.getElementById('t2-bk-cases').innerHTML =
      '<div style="color:var(--muted); font-size:12px; padding:12px;">该桶无典型 transcript 样本。</div>';
    return;
  }}
  // 按总轮次降序取 5 通最有内容的
  sub.sort((a, b) => (b._max_turn || 0) - (a._max_turn || 0));
  const cases = sub.slice(0, 5);

  // 找出每通对应的 LLM 结果（如果有）
  const llmByCallId = {{}};
  (t2LlmResultsCache || []).forEach(r => {{ if (r.call_id) llmByCallId[r.call_id] = r; }});

  document.getElementById('t2-bk-cases').innerHTML = cases.map(r => {{
    const callId = r['Call ID'] || '';
    const llm = llmByCallId[callId];
    const failTurnIdx = llm && llm.fail_turn ? parseInt(llm.fail_turn, 10) : null;
    // Transcript 字段是渲染过的 "role: content" 多行字符串
    const lines = (r['Transcript'] || '').split('\\n');
    // 标记 assistant 出问题那句 (按 assistant 出现的第几次)
    let aCount = 0;
    const linesHtml = lines.map(line => {{
      const isAgent = line.startsWith('assistant');
      if (isAgent) aCount++;
      const isFail = (isAgent && failTurnIdx && aCount === failTurnIdx);
      const cls = isAgent ? 'agent' : 'user';
      const style = isFail
        ? 'background: rgba(244,63,94,0.12); color: #b91c1c; border-left: 3px solid #f43f5e; padding-left: 6px; font-weight: 600;'
        : '';
      const tag = isFail ? '  ⚠️ LLM 判此句出问题' : '';
      return `<div class="t2-case-line ${{cls}}" style="${{style}}">${{escapeHtml(line)}}${{tag}}</div>`;
    }}).join('');

    const llmHint = llm && !llm.error
      ? `<div style="font-size:11px; color:var(--muted); padding:6px 8px; background:var(--panel-2); border-radius:4px; margin-top:6px;">
          <b style="color:#b91c1c;">LLM 失败类别:</b> ${{escapeHtml(llm.fail_category || '?')}} · <b>原因:</b> ${{escapeHtml(llm.fail_reason || '-')}}<br>
          <b>客户识破:</b> U${{llm.user_detect_turn || '?'}} · ${{escapeHtml(llm.user_detect_signal || '-')}}
        </div>` : '';

    return `<details style="border-top: 1px solid var(--border); padding: 6px 8px;">
      <summary style="cursor: pointer; font-size: 12px;">
        <code style="background: var(--panel-2); padding: 1px 5px; border-radius: 3px; font-size: 10px;">${{callId.slice(-10)}}</code>
        <span style="color: var(--muted); margin-left: 6px;">${{r['Duration (s)'] || '?'}}s · ${{r._max_turn || '?'}} 轮</span>
        ${{llm ? `<span style="background: rgba(244,63,94,0.12); color: #b91c1c; padding: 1px 5px; border-radius: 3px; font-size: 10px; margin-left: 6px;">A${{llm.fail_turn || '?'}} ${{escapeHtml(llm.fail_category || '')}}</span>` : ''}}
      </summary>
      <div style="font-size: 11px; line-height: 1.7; padding: 8px 0; color: var(--text);">
        ${{linesHtml}}
      </div>
      ${{llmHint}}
    </details>`;
  }}).join('');
}}

function renderT2All() {{
  renderT2Scope();
  renderT2Buckets();
  renderT2BucketDetail();
}}

// ── Tab 切换 ──
document.querySelectorAll('.tab-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    const tabId = 'tab-' + btn.dataset.tab;
    document.getElementById(tabId).classList.add('active');
    document.getElementById('tab1-controls').style.display = btn.dataset.tab === 'overview' ? '' : 'none';
    setTimeout(() => Object.values(charts).forEach(c => c.resize()), 50);
  }});
}});

if (T2) {{
  renderT2AgentSelect();
  renderT2All();
}}

// ── Tab 2 · LLM 失败画像（轮询 /llm-fail-status）──
let t2LlmPollTimer = null;
let t2LlmResultsCache = [];

function startT2LlmPoll() {{
  pollT2Llm();
  if (!t2LlmPollTimer) t2LlmPollTimer = setInterval(pollT2Llm, 5000);
}}

async function pollT2Llm() {{
  try {{
    const resp = await fetch('/llm-fail-status');
    if (!resp.ok) return;
    const d = await resp.json();
    t2LlmResultsCache = d.results || [];
    renderT2LlmStatus(d);
    renderT2LlmCharts();
    if (d.status === 'done' || d.status === 'error' || d.status === 'skipped') {{
      if (t2LlmPollTimer) {{ clearInterval(t2LlmPollTimer); t2LlmPollTimer = null; }}
    }}
  }} catch (e) {{ /* 网络抖动，下次再试 */ }}
}}

function renderT2LlmStatus(d) {{
  const statusEl = document.getElementById('t2-llm-status');
  const wrapEl = document.getElementById('t2-llm-progress-wrap');
  if (d.status === 'skipped') {{
    statusEl.innerHTML = `<span style="color:#b45309;">未启动: ${{d.error || '-'}}</span>`;
    wrapEl.style.display = 'none';
    return;
  }}
  if (d.status === 'error') {{
    statusEl.innerHTML = `<span style="color:#b91c1c;">错误: ${{d.error || 'unknown'}}</span>`;
    wrapEl.style.display = 'none';
    return;
  }}
  if (d.status === 'done') {{
    statusEl.innerHTML = `完成 · 模型 ${{d.model}} · 共 ${{d.total}} 通 / 耗时 ${{d.elapsed_s}}s`;
    wrapEl.style.display = 'none';
    return;
  }}
  // running / idle
  const pct = d.total ? (d.done / d.total * 100).toFixed(1) : '0';
  statusEl.innerHTML = `跑中 (${{d.backend}}/${{d.model}}) · ${{d.done}} / ${{d.total}} · 已 ${{d.elapsed_s}}s`;
  wrapEl.style.display = 'block';
  document.getElementById('t2-llm-progress-text').textContent =
    `LLM 分析中 · ${{d.done}} / ${{d.total}} 通 · 已耗时 ${{d.elapsed_s}}s`;
  document.getElementById('t2-llm-progress-bar').style.width = pct + '%';
}}

function renderT2LlmCharts() {{
  // 按当前 agent 过滤
  let results = t2LlmResultsCache;
  if (t2CurrentAgentKey !== '__ALL__') {{
    results = results.filter(r => r.agent_name === t2CurrentAgentKey);
  }}
  // 过滤掉 error 的
  const ok = results.filter(r => !r.error);
  if (!ok.length) {{
    ['t2-chart-fail-turn','t2-chart-detect-turn','t2-chart-fail-cat'].forEach(id => charts[id].clear());
    const tw = document.getElementById('t2-cases-table-wrap');
    if (tw) tw.innerHTML = '<div style="color:var(--muted); padding: 12px; font-size: 12px;">尚无 LLM 分析结果。</div>';
    const m = document.getElementById('t2-cases-meta');
    if (m) m.textContent = '';
    return;
  }}

  // fail_turn 分布
  const failTurnCount = {{}};
  let maxTurn = 0;
  ok.forEach(r => {{
    const t = parseInt(r.fail_turn, 10);
    if (!isNaN(t) && t > 0) {{
      failTurnCount[t] = (failTurnCount[t] || 0) + 1;
      if (t > maxTurn) maxTurn = t;
    }}
  }});
  const ftX = [];
  const ftY = [];
  for (let i = 1; i <= Math.min(maxTurn, 20); i++) {{
    ftX.push('A' + i);
    ftY.push(failTurnCount[i] || 0);
  }}
  charts['t2-chart-fail-turn'].setOption({{
    color: ['#f43f5e'],
    tooltip: tooltipBase({{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }}),
    grid: {{ left: 4, right: 8, top: 18, bottom: 22, containLabel: true }},
    xAxis: Object.assign({{ type: 'category', data: ftX, axisLabel: {{ color: MUTED, fontSize: 11, interval: 0 }} }}, baseAxis),
    yAxis: Object.assign({{ type: 'value', name: `n=${{ok.length}}` }}, baseAxis),
    series: [{{
      type: 'bar', data: ftY, itemStyle: {{ borderRadius: [3,3,0,0] }},
      label: {{ show: true, position: 'top', color: MUTED, fontSize: 10,
                formatter: p => p.value > 0 ? p.value : '' }},
    }}],
  }}, true);

  // detect_turn 分布
  const dtCount = {{}};
  let maxDt = 0;
  ok.forEach(r => {{
    const t = parseInt(r.user_detect_turn, 10);
    if (!isNaN(t) && t > 0) {{
      dtCount[t] = (dtCount[t] || 0) + 1;
      if (t > maxDt) maxDt = t;
    }}
  }});
  const dtX = [];
  const dtY = [];
  for (let i = 1; i <= Math.min(maxDt, 20); i++) {{
    dtX.push('U' + i);
    dtY.push(dtCount[i] || 0);
  }}
  charts['t2-chart-detect-turn'].setOption({{
    color: ['#f59e0b'],
    tooltip: tooltipBase({{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }}),
    grid: {{ left: 4, right: 8, top: 18, bottom: 22, containLabel: true }},
    xAxis: Object.assign({{ type: 'category', data: dtX, axisLabel: {{ color: MUTED, fontSize: 11, interval: 0 }} }}, baseAxis),
    yAxis: Object.assign({{ type: 'value', name: `识破 n=${{dtY.reduce((s,v)=>s+v,0)}}` }}, baseAxis),
    series: [{{
      type: 'bar', data: dtY, itemStyle: {{ borderRadius: [3,3,0,0] }},
      label: {{ show: true, position: 'top', color: MUTED, fontSize: 10,
                formatter: p => p.value > 0 ? p.value : '' }},
    }}],
  }}, true);

  // fail_category 饼图
  const catCount = {{}};
  ok.forEach(r => {{
    const c = r.fail_category || '未知';
    catCount[c] = (catCount[c] || 0) + 1;
  }});
  const catData = Object.entries(catCount)
    .sort((a,b) => b[1] - a[1])
    .map(([name, value]) => ({{ name, value }}));
  charts['t2-chart-fail-cat'].setOption({{
    color: ['#f43f5e', '#f59e0b', '#06b6d4', '#a855f7', '#10b981', '#0ea5e9', '#94a3b8'],
    tooltip: tooltipBase({{
      trigger: 'item',
      formatter: p => `<b>${{p.data.name}}</b><br>${{p.data.value}} 通 · ${{p.percent.toFixed(1)}}%`,
    }}),
    title: {{
      text: ok.length + '', subtext: '通',
      left: '50%', top: '48%', textAlign: 'center', textVerticalAlign: 'middle',
      textStyle: {{ fontSize: 22, fontWeight: 700, color: TEXT }},
      subtextStyle: {{ fontSize: 11, color: MUTED }}, itemGap: 2,
    }},
    series: [{{
      type: 'pie', radius: ['46%', '72%'], center: ['50%', '52%'],
      avoidLabelOverlap: true,
      itemStyle: {{ borderColor: '#fff', borderWidth: 1.5 }},
      label: {{ color: TEXT, fontSize: 11,
                formatter: p => p.percent >= 4 ? `${{p.name}}\\n${{p.percent.toFixed(0)}}%` : '' }},
      labelLine: {{ length: 6, length2: 4, lineStyle: {{ color: MUTED }} }},
      data: catData,
    }}],
  }}, true);

  // 案例列表 (新版表格 + 筛选)
  t2CasesAllOk = ok;
  // 填充 category filter 下拉选项
  const catSet = new Set(ok.map(r => r.fail_category).filter(Boolean));
  const catSel = document.getElementById('t2-cases-filter-cat');
  const curCat = catSel.value;
  catSel.innerHTML = ['<option value="">全部</option>',
    ...[...catSet].sort().map(c => `<option value="${{escapeHtml(c)}}">${{escapeHtml(c)}}</option>`)
  ].join('');
  if ([...catSet].includes(curCat)) catSel.value = curCat;
  renderT2CasesTable();
}}

let t2CasesAllOk = [];        // 当前 scope 下的所有 LLM ok 结果
const t2ExpandedCases = new Set();

function renderT2CasesTable() {{
  const pnVal = document.getElementById('t2-cases-filter-pn').value;
  const catVal = document.getElementById('t2-cases-filter-cat').value;
  const pageSize = parseInt(document.getElementById('t2-cases-page-size').value || '100', 10);

  let cases = t2CasesAllOk.slice();
  if (pnVal !== '') cases = cases.filter(r => String(r.pass_n) === pnVal);
  if (catVal !== '') cases = cases.filter(r => r.fail_category === catVal);
  cases.sort((a, b) => (b.pass_n||0) - (a.pass_n||0) || ((a.fail_turn||99) - (b.fail_turn||99)));

  document.getElementById('t2-cases-count').textContent =
    `${{cases.length}} 条 (筛选前 ${{t2CasesAllOk.length}})`;
  const shown = cases.slice(0, pageSize);

  // 行内 transcript 渲染（用 DATA.rows 反查）
  const rowByCallId = new Map(DATA.rows.map(r => [r['Call ID'], r]));

  const headerHtml = `<thead><tr>
      <th class="col-toggle"></th>
      <th class="col-id">Call ID</th>
      <th class="col-agent">Agent</th>
      <th class="col-pn">桶</th>
      <th class="col-ft">A#</th>
      <th class="col-cat">失败类别</th>
      <th class="col-reason">失败原因 (≤30 字)</th>
      <th class="col-ut">U#</th>
      <th class="col-signal">客户识破证据</th>
    </tr></thead>`;
  const bodyHtml = shown.map((r, idx) => {{
    const cid = r.call_id || '';
    const expanded = t2ExpandedCases.has(cid);
    const mainRow = `<tr data-cid="${{escapeHtml(cid)}}" class="${{expanded ? 'expanded' : ''}}">
      <td class="col-toggle"><button class="t2-case-toggle">${{expanded ? '–' : '+'}}</button></td>
      <td class="col-id"><code title="点击复制">${{escapeHtml(cid)}}</code></td>
      <td class="col-agent" title="${{escapeHtml(r.agent_name || '')}}">${{escapeHtml(r.agent_name || '-')}}</td>
      <td class="col-pn"><span class="chip pn-chip-${{r.pass_n}}">${{r.pass_n}} 关</span></td>
      <td class="col-ft">A${{r.fail_turn || '?'}}</td>
      <td class="col-cat"><span class="chip">${{escapeHtml(r.fail_category || '-')}}</span></td>
      <td class="col-reason">${{escapeHtml(r.fail_reason || '-')}}</td>
      <td class="col-ut">${{r.user_detect_turn ? 'U' + r.user_detect_turn : '-'}}</td>
      <td class="col-signal" title="${{escapeHtml(r.user_detect_signal || '')}}">${{escapeHtml(r.user_detect_signal || '-')}}</td>
    </tr>`;
    if (!expanded) return mainRow;
    // 展开行：transcript + LLM 详情
    const src = rowByCallId.get(cid) || {{}};
    const failTurnIdx = r.fail_turn ? parseInt(r.fail_turn, 10) : null;
    const lines = (src['Transcript'] || '').split('\\n');
    let aCount = 0;
    const linesHtml = lines.map(line => {{
      const isAgent = line.startsWith('assistant');
      if (isAgent) aCount++;
      const isFail = (isAgent && failTurnIdx && aCount === failTurnIdx);
      const cls = isAgent ? 'agent' : 'user';
      const style = isFail
        ? 'background: rgba(244,63,94,0.12); color: #b91c1c; border-left: 3px solid #f43f5e; padding-left: 6px; font-weight: 600;'
        : '';
      const tag = isFail ? '  ⚠️ LLM 判此句出问题' : '';
      return `<div class="t2-case-line ${{cls}}" style="${{style}}">${{escapeHtml(line)}}${{tag}}</div>`;
    }}).join('');
    const audio = src['Audio URL']
      ? `<a href="${{escapeHtml(src['Audio URL'])}}" target="_blank" style="color: var(--accent); font-size: 11px;">▶ 听录音</a>` : '';
    const detail = `<tr class="case-detail"><td colspan="9">
      <div class="detail-box">
        <div style="font-size:11px;color:var(--muted);margin-bottom:8px;">
          <b>时长</b> ${{src['Duration (s)'] || '?'}}s ·
          <b>max_turn_id</b> ${{src._max_turn || '?'}} ·
          <b>assistant 轮</b> ${{src._assistant_turns || '?'}} ·
          ${{audio}}
        </div>
        <div style="font-size:11px; line-height:1.7;">${{linesHtml}}</div>
      </div>
    </td></tr>`;
    return mainRow + detail;
  }}).join('');

  const el = document.getElementById('t2-cases-table-wrap');
  el.innerHTML = `<div style="max-height: 640px; overflow-y: auto;">
    <table class="t2-cases-table">${{headerHtml}}<tbody>${{bodyHtml}}</tbody></table>
  </div>`;
  // toggle
  el.querySelectorAll('.t2-case-toggle').forEach(btn => {{
    btn.addEventListener('click', e => {{
      e.stopPropagation();
      const tr = btn.closest('tr');
      const cid = tr.getAttribute('data-cid');
      if (t2ExpandedCases.has(cid)) t2ExpandedCases.delete(cid);
      else t2ExpandedCases.add(cid);
      renderT2CasesTable();
    }});
  }});
  // 复制 Call ID
  el.querySelectorAll('.col-id code').forEach(code => {{
    code.addEventListener('click', () => {{
      navigator.clipboard.writeText(code.textContent).then(() => showToast('已复制 ' + code.textContent));
    }});
  }});
}}

// 绑定 filter
['t2-cases-filter-pn', 't2-cases-filter-cat', 't2-cases-page-size'].forEach(id => {{
  const el = document.getElementById(id);
  if (el) el.addEventListener('change', renderT2CasesTable);
}});

// 启动轮询
startT2LlmPoll();

// agent 切换时也重新过滤 LLM 数据
const _origRenderT2All = renderT2All;
renderT2All = function() {{ _origRenderT2All(); renderT2LlmCharts(); }};

// ──────────────────────────────────────────────────────────────────────────
// LLM 意向真伪分析
// ──────────────────────────────────────────────────────────────────────────

const llmModal = document.getElementById('llm-modal');
const llmSubEl = document.getElementById('llm-sub');
const llmStagePre = document.getElementById('llm-stage-pre');
const llmStageRun = document.getElementById('llm-stage-running');
const llmStageDone = document.getElementById('llm-stage-done');
const llmProgressText = document.getElementById('llm-progress-text');
const llmProgressBar = document.getElementById('llm-progress-bar');
const llmSummaryEl = document.getElementById('llm-summary');
const llmResultsTable = document.getElementById('llm-results-table');

// Server-side LLM job is auto-kicked off at dashboard start. The modal here is
// just a viewer that polls /llm-intent-status every 5s while open.
let llmResults = [];
let llmPollTimer = null;
let llmLastModel = '';

function openLLMIntentDialog() {{
  llmModal.classList.add('show');
  llmStagePre.style.display = 'none';
  llmStageRun.style.display = 'block';
  llmStageDone.style.display = 'none';
  llmProgressBar.style.width = '0%';
  llmProgressText.textContent = '加载中…';
  llmSubEl.innerHTML = '查询服务端状态…';
  pollLLMStatus();
  if (!llmPollTimer) {{
    llmPollTimer = setInterval(pollLLMStatus, 5000);
  }}
}}

function closeLLMModal() {{
  llmModal.classList.remove('show');
  if (llmPollTimer) {{ clearInterval(llmPollTimer); llmPollTimer = null; }}
}}

async function pollLLMStatus() {{
  try {{
    const resp = await fetch('/llm-intent-status');
    if (!resp.ok) return;
    const data = await resp.json();
    renderLLMStatus(data);
    if (data.status === 'done' || data.status === 'error' || data.status === 'skipped') {{
      if (llmPollTimer) {{ clearInterval(llmPollTimer); llmPollTimer = null; }}
    }}
  }} catch (e) {{
    // Transient errors are fine — next tick will retry.
  }}
}}

function renderLLMStatus(data) {{
  llmLastModel = data.model || llmLastModel;
  const scope = buildScopeName();
  const inScopeIds = new Set(scopedRows().filter(r => r._intent).map(r => r['Call ID']));
  const scopedResults = (data.results || []).filter(r => inScopeIds.has(r.call_id));
  llmResults = scopedResults;

  llmSubEl.innerHTML = `模型 <code style="background:var(--panel-2);padding:1px 5px;border-radius:3px;">${{data.model || '-'}}</code> · scope <b>${{scope}}</b> · 当前 scope 意向客户 <b>${{inScopeIds.size}}</b>`;

  if (data.status === 'skipped') {{
    llmStagePre.style.display = 'block';
    llmStageRun.style.display = 'none';
    llmStageDone.style.display = 'none';
    document.getElementById('llm-pre-text').innerHTML = `<span style="color:#b45309;">LLM 自动分析未启动：${{data.error || '未知原因'}}</span>`;
    return;
  }}

  if (data.status === 'error') {{
    llmStagePre.style.display = 'block';
    llmStageRun.style.display = 'none';
    llmStageDone.style.display = 'none';
    document.getElementById('llm-pre-text').innerHTML = `<span style="color:#b91c1c;">分析出错: ${{data.error || 'unknown'}}</span>`;
    return;
  }}

  if (data.status === 'running' || data.status === 'idle') {{
    llmStagePre.style.display = 'none';
    llmStageRun.style.display = 'block';
    llmStageDone.style.display = 'none';
    const total = data.total || 1;
    const pct = (data.done / total * 100).toFixed(1);
    llmProgressBar.style.width = pct + '%';
    llmProgressText.textContent = `全局进度: ${{data.done}} / ${{data.total}}  · 已 ${{data.elapsed_s}}s`;
    // Also surface partial results table even mid-flight, so user sees real-time output.
    showLLMResults(true);
    return;
  }}

  // done
  llmStageRun.style.display = 'none';
  showLLMResults(false);
}}

function showLLMResults(midflight) {{
  if (!midflight) llmStageRun.style.display = 'none';
  llmStageDone.style.display = 'block';
  // Summary chips
  const counts = {{ '真意向': 0, '假意向': 0, '模糊': 0, '失败': 0 }};
  for (const r of llmResults) {{
    if (r.error) counts['失败']++;
    else if (counts[r.verdict] !== undefined) counts[r.verdict]++;
    else counts['模糊']++;
  }}
  const total = llmResults.length || 1;
  llmSummaryEl.innerHTML = `
    <div class="chip real"><div class="ct">${{counts['真意向']}}</div><div class="lb">真意向 · ${{(counts['真意向']/total*100).toFixed(0)}}%</div></div>
    <div class="chip fake"><div class="ct">${{counts['假意向']}}</div><div class="lb">假意向 · ${{(counts['假意向']/total*100).toFixed(0)}}%</div></div>
    <div class="chip mid"><div class="ct">${{counts['模糊']}}</div><div class="lb">模糊 · ${{(counts['模糊']/total*100).toFixed(0)}}%</div></div>
    ${{counts['失败'] ? `<div class="chip err"><div class="ct">${{counts['失败']}}</div><div class="lb">调用失败</div></div>` : ''}}
  `;
  // Results table
  llmResultsTable.innerHTML = `<table style="font-size:11px;"><thead><tr>
    <th>Call ID</th><th>判定</th><th>依据</th><th>证据</th></tr></thead><tbody>${{
    llmResults.map(r => {{
      const v = r.error ? '⚠ 失败' : (r.verdict || '?');
      const color = r.error ? '#b91c1c' :
                    r.verdict === '真意向' ? '#047857' :
                    r.verdict === '假意向' ? '#b91c1c' : '#b45309';
      return `<tr>
        <td><code style="font-size:10px; background:var(--panel-2); padding:1px 4px; border-radius:3px;">${{(r.call_id || '').slice(-8)}}</code></td>
        <td style="color:${{color}}; font-weight:600;">${{v}}</td>
        <td>${{r.error || r.reason || ''}}</td>
        <td style="color:var(--muted);">${{r.evidence || ''}}</td>
      </tr>`;
    }}).join('')
  }}</tbody></table>`;
}}

function exportLLMResults() {{
  // Join results back to source rows so the xlsx has the full pic.
  const byId = new Map(scopedRows().filter(r => r._intent).map(r => [r['Call ID'], r]));
  const cols = ['Call ID', 'Agent Name', 'Duration (s)', 'Hangup Reason',
                'LLM Verdict', 'LLM Reason', 'LLM Evidence', 'LLM Error',
                'Is Full Conversion', 'Assistant turns', 'Transcript', 'Audio URL'];
  const aoa = [cols];
  for (const r of llmResults) {{
    const src = byId.get(r.call_id) || {{}};
    aoa.push([
      r.call_id,
      src['Agent Name'] || '',
      src['Duration (s)'] ?? '',
      src['Hangup Reason'] || '',
      r.verdict || '',
      r.reason || '',
      r.evidence || '',
      r.error || '',
      src['Is Full Conversion'] ?? '',
      src['Assistant turns'] ?? '',
      src['Transcript'] || '',
      src['Audio URL'] || '',
    ]);
  }}
  const ws = XLSX.utils.aoa_to_sheet(aoa);
  ws['!cols'] = [16, 28, 8, 16, 8, 36, 36, 24, 8, 10, 60, 28].map(w => ({{ wch: w }}));
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'llm-intent');
  const fname = safeFilename(`agora-llm-intent-${{buildScopeName()}}-n${{llmResults.length}}`) + '.xlsx';
  XLSX.writeFile(wb, fname);
  showToast(`导出 ${{llmResults.length}} 条 → ${{fname}}`);
}}

document.getElementById('btn-llm-intent').addEventListener('click', openLLMIntentDialog);
document.getElementById('llm-cancel-pre').addEventListener('click', closeLLMModal);
document.getElementById('llm-close').addEventListener('click', closeLLMModal);
document.getElementById('llm-export').addEventListener('click', exportLLMResults);
llmModal.addEventListener('click', e => {{ if (e.target === llmModal) closeLLMModal(); }});

// ECharts locks the canvas size at init() time. Flex/grid layouts (like the
// hero+funnel split where the funnel card stretches to match the left KPI
// column) can grow the container after init, leaving empty space below the
// chart. ResizeObserver fires whenever the chart's host div changes size and
// triggers an echarts resize so the visualization fills its container.
const _chartRO = new ResizeObserver(entries => {{
  for (const entry of entries) {{
    const inst = echarts.getInstanceByDom(entry.target);
    if (inst) inst.resize();
  }}
}});
Object.values(charts).forEach(c => {{ try {{ _chartRO.observe(c.getDom()); }} catch (e) {{}} }});
</script>
</body>
</html>
"""


def render_select_options(options: list[dict]) -> str:
    return "\n      ".join(
        f'<option value="{o["key"]}">{o["label"]}</option>' for o in options
    )


def _vendor_scripts_block() -> str:
    """Inline vendor JS if present, else fall back to CDN tags."""
    vendor_dir = Path(__file__).resolve().parent.parent / "vendor"
    libs = [("echarts.min.js", "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"),
            ("xlsx.full.min.js", "https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js")]
    parts = []
    for fname, cdn in libs:
        local = vendor_dir / fname
        if local.is_file():
            parts.append(f"<script>/* {fname} (inlined) */\n{local.read_text(encoding='utf-8')}\n</script>")
        else:
            parts.append(f'<script src="{cdn}"></script>')
    return "\n".join(parts)


def build_html(df_enriched: pd.DataFrame, source: str) -> str:
    data = build_data(df_enriched)
    html = HTML_TEMPLATE.format(
        source=source,
        total=len(df_enriched),
        n_agents=df_enriched["Agent Name"].nunique(),
        select_options=render_select_options(data["options"]),
        data_json=json.dumps(data, ensure_ascii=False),
    )
    return html.replace("<!-- VENDOR_SCRIPTS -->", _vendor_scripts_block())


# ---------- main ----------

def main() -> None:
    p = argparse.ArgumentParser(description="Agora 外呼 CSV/XLSX → ECharts HTML dashboard")
    p.add_argument("input", help="CSV or XLSX path")
    p.add_argument("-o", "--output", help="Output HTML path (default: alongside input)")
    args = p.parse_args()

    inp = Path(args.input).expanduser().resolve()
    if not inp.exists():
        raise SystemExit(f"Input not found: {inp}")

    out = Path(args.output).expanduser().resolve() if args.output else inp.with_suffix(".dashboard.html")
    df = load_table(inp)
    enriched = enrich(df)
    html = build_html(enriched, source=inp.name)
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out}")
    print("\nFunnel summary (全部):")
    for label, val in zip(FUNNEL_LABELS, funnel_counts(enriched)):
        print(f"  {label}: {val}")


if __name__ == "__main__":
    sys.exit(main())
