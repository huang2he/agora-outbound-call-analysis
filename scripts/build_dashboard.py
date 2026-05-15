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


def is_full_conversion(structured: dict | None) -> bool:
    if not structured:
        return False
    return all(v is not None and v != "" for v in structured.values())


def is_intent(structured: dict | None) -> bool:
    if not structured:
        return False
    return str(structured.get("购车意向", "")).strip() == "是"


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["_transcript"] = df["Transcript"].apply(parse_transcript)
    df["_structured"] = df["Structured Output"].apply(parse_structured)
    df["_assistant_turns"] = df["_transcript"].apply(assistant_turn_count)
    df["_max_turn_id"] = df["_transcript"].apply(max_turn_id)
    df["_answered"] = df["Duration (seconds)"] > 0
    df["_human"] = df["Hangup Reason"].isin(HUMAN_HANGUP)
    df["_full"] = df["_structured"].apply(is_full_conversion)
    df["_intent"] = df["_structured"].apply(is_intent)
    return df


# ---------- metric extraction ----------

FUNNEL_LABELS = ["拨打总数", "接听", "真人接听", "完整转换", "意向客户"]


def funnel_counts(df: pd.DataFrame) -> list[int]:
    return [
        len(df),
        int(df["_answered"].sum()),
        int(df["_human"].sum()),
        int(df["_full"].sum()),
        int(df["_intent"].sum()),
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
    """恰好 N 句挂断（互斥分桶）."""
    human = df[df["_human"]]
    total = len(human)
    if total == 0:
        return []
    rows = []
    for n, label in [(1, "首句挂断 (1 句)"), (2, "2 句挂断"), (3, "3 句挂断"), (4, "4 句挂断"), (5, "5 句挂断")]:
        cnt = int((human["_assistant_turns"] == n).sum())
        rows.append({"label": label, "count": cnt, "pct": round(cnt / total * 100, 1)})
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
               first_dur_x_max: int) -> dict:
    """All charts for one slice (全部 or single agent).

    Turn distribution is 真人接听内, so 完整转换/意向 series here are restricted to
    human-answered calls (the funnel/hero counts remain parallel definitions).
    """
    human = df_slice[df_slice["_human"]]
    full_in_human = human[human["_full"]]
    intent_in_human = human[human["_intent"]]

    dur_labels, dur_human = duration_histogram(human["Duration (seconds)"], dur_x_max)

    # 首句挂断 = 真人接听 且 assistant 轮数 == 1。看这部分通话的 Duration 分布，
    # 直观看出 AI 第一句还没说完就被掐掉的比例。
    first_sentence = human[human["_assistant_turns"] == 1]
    first_dur_labels, first_dur_counts = duration_histogram(
        first_sentence["Duration (seconds)"], first_dur_x_max
    )

    totals = funnel_counts(df_slice)

    return {
        "n": len(df_slice),
        "totals": {"labels": FUNNEL_LABELS, "values": totals},
        # Funnel denominators for the hero KPI percentages: 总 / 接听 / 真人接听.
        # JS divides each numerator by these to produce the three percentage rows.
        "denominators": {
            "total": totals[0],
            "answered": totals[1],
            "human": totals[2],
        },
        "turn_dist": {
            "x": list(range(1, turn_x_max + 1)),
            "series": [
                {"name": "真人接听 (全部)", "data": turn_histogram(human["_max_turn_id"], turn_x_max)},
                {"name": "完整转换", "data": turn_histogram(full_in_human["_max_turn_id"], turn_x_max)},
                {"name": "意向客户", "data": turn_histogram(intent_in_human["_max_turn_id"], turn_x_max)},
            ],
        },
        "duration_dist": {
            "x": dur_labels,
            "series": [{"name": "真人接听", "data": dur_human}],
        },
        "early_hangup": early_hangup_rows(df_slice),
        "first_sentence_dur": {
            "x": first_dur_labels,
            "data": first_dur_counts,
            "n": len(first_sentence),
        },
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

    agents = sorted(df_enriched["Agent Name"].unique())
    datasets = {ALL_KEY: slice_data(df_enriched, turn_x_max, dur_x_max, first_dur_x_max)}
    for a in agents:
        datasets[a] = slice_data(df_enriched[df_enriched["Agent Name"] == a],
                                 turn_x_max, dur_x_max, first_dur_x_max)

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
        rows.append(rec)

    return {
        "options": [{"key": ALL_KEY, "label": f"{ALL_LABEL} (n={len(df_enriched)})"}]
        + [{"key": a, "label": f"{a} (n={datasets[a]['n']})"} for a in agents],
        "datasets": datasets,
        "rows": rows,
        "all_key": ALL_KEY,
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
     拨打=blue · 接听=green · 真人=amber · 完整转换=cyan · 意向=purple.
     Background is a very light wash of the accent so dark text stays readable. */
  .stat {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; position: relative; overflow: hidden; box-shadow: 0 1px 2px rgba(15,23,42,0.04); }}
  .stat::after {{ content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px; }}
  .stat:nth-child(1) {{ background: #eff6ff; border-color: #bfdbfe; }}
  .stat:nth-child(1)::after {{ background: #2563eb; }}
  .stat:nth-child(2) {{ background: #ecfdf5; border-color: #a7f3d0; }}
  .stat:nth-child(2)::after {{ background: #10b981; }}
  .stat:nth-child(3) {{ background: #fffbeb; border-color: #fde68a; }}
  .stat:nth-child(3)::after {{ background: #f59e0b; }}
  .stat:nth-child(4) {{ background: #ecfeff; border-color: #a5f3fc; }}
  .stat:nth-child(4)::after {{ background: #06b6d4; }}
  .stat:nth-child(5) {{ background: #faf5ff; border-color: #d8b4fe; }}
  .stat:nth-child(5)::after {{ background: #a855f7; }}
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
</style>
</head>
<body>
<div class="wrap">

<header>
  <div>
    <h1>Agora 外呼分析 <span class="accent">·</span> <span style="color:var(--muted); font-weight:400;">{source}</span></h1>
    <div class="meta" style="margin-top:6px;">总通话 <code>{total}</code> · Agent Name 数 <code>{n_agents}</code></div>
  </div>
  <div class="controls">
    <label for="agent-select">范围</label>
    <select id="agent-select">
      {select_options}
    </select>
  </div>
</header>

<div class="defs">
  接听 = <code>Duration &gt; 0</code> · 真人接听 = <code>USER/AI_HANGUP</code> · 完整转换 = <code>Structured Output 无 null</code> · 意向 = <code>购车意向="是"</code> · N 句挂断 = <code>真人接听里 assistant 轮数恰好 = N</code>
</div>

<div class="hero-funnel">
  <div class="stats" id="hero-stats"></div>
  <div class="funnel-wrap">
    <h2>1 · 漏斗 <span class="export-hint">点击层级导出</span></h2>
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
  <h3 class="turn-card-title">完整转换</h3>
  <div class="turn-card-body">
    <div class="turn-bar"><div id="chart-turn-full" class="chart"></div></div>
    <div class="turn-donut"><div id="chart-turn-full-donut" class="chart"></div></div>
  </div>
</div>
<div class="turn-card">
  <h3 class="turn-card-title">意向客户</h3>
  <div class="turn-card-body">
    <div class="turn-bar"><div id="chart-turn-intent" class="chart"></div></div>
    <div class="turn-donut"><div id="chart-turn-intent-donut" class="chart"></div></div>
  </div>
</div>

<h2>3 · Duration 分布 (真人接听) <span class="export-hint">点击柱子导出</span></h2>
<p class="section-note">横轴单位 <b>秒</b>（一秒一柱）；拖动下方滑块或滚轮缩放查看任意区间。点单根柱子导出该秒数对应的真人接听通话。</p>
<div class="card"><div id="chart-duration" class="chart tall"></div></div>

<h2>4 · 早期挂断（真人接听内 · 互斥分桶）</h2>
<div class="grid-2">
  <div class="card">
    <h3 style="margin:0 0 6px; font-size:12px; color:var(--muted); font-weight:500; text-transform: uppercase; letter-spacing:0.6px;">分句数汇总 <span class="export-hint">点行导出</span></h3>
    <p class="section-note" style="margin:0 0 12px;">备注：<b>仅计算 agent 说话轮次</b>。集中在前几句的部分代表 <b>AI 表现不好 · 很快被客户识破</b>。</p>
    <div id="early-hangup-table"></div>
  </div>
  <div class="card">
    <h3 style="margin:0 0 6px; font-size:12px; color:var(--muted); font-weight:500; text-transform: uppercase; letter-spacing:0.6px;">首句挂断 · Duration 分布</h3>
    <p class="section-note" style="margin:0 0 12px;">看"AI 刚说完第一句就被掐掉"的通话有多短。<b>横轴=秒</b>，柱高=该秒数挂断的通话数，<b>百分比相对首句挂断总数</b>。</p>
    <div id="chart-first-sentence-dur" class="chart"></div>
  </div>
</div>

<div id="toast" class="toast"></div>

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

const PALETTE = ['#2563eb', '#10b981', '#f59e0b', '#06b6d4', '#a855f7', '#f43f5e', '#0ea5e9'];
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
                  'chart-duration', 'chart-first-sentence-dur'];
const charts = {{}};
chartIds.forEach(id => {{ charts[id] = echarts.init(document.getElementById(id)); }});

// Bar series name → corresponding subset filter for click-to-export.
const TURN_SERIES = [
  {{ key: 'human',  name: '真人接听 (全部)', barId: 'chart-turn-human',  donutId: 'chart-turn-human-donut',  color: '#2563eb', filter: r => r._human }},
  {{ key: 'full',   name: '完整转换',        barId: 'chart-turn-full',   donutId: 'chart-turn-full-donut',   color: '#10b981', filter: r => r._human && r._full }},
  {{ key: 'intent', name: '意向客户',        barId: 'chart-turn-intent', donutId: 'chart-turn-intent-donut', color: '#f59e0b', filter: r => r._human && r._intent }},
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
    const fns = [r => true, r => r._answered, r => r._human, r => r._full, r => r._intent];
    return [scope.filter(fns[idx]), `funnel-${{['all','answered','human','full','intent'][idx]}}`];
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
    total:    {{ label: '占总',   value: denominators.total }},
    answered: {{ label: '占接听', value: denominators.answered }},
    human:    {{ label: '占真人', value: denominators.human }},
  }};
  const SHOW = [
    [],                                  // 拨打总数
    ['total'],                           // 接听
    ['total', 'answered'],               // 真人接听
    ['total', 'answered', 'human'],      // 完整转换
    ['total', 'answered', 'human'],      // 意向客户
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
  charts['chart-duration'].setOption({{
    color: ['#f59e0b'],
    tooltip: tooltipBase({{
      trigger: 'axis', axisPointer: {{ type: 'shadow' }},
      formatter: params => {{
        const p = params[0];
        return `<b>${{p.name}} 秒</b><br>${{p.seriesName}}: <b>${{p.value}}</b> 通`;
      }},
    }}),
    legend: {{ data: dd.series.map(s => s.name), textStyle: {{ color: TEXT, fontSize: 12 }}, top: 8 }},
    grid: Object.assign({{}}, baseGrid, {{ bottom: 56 }}),
    xAxis: Object.assign({{ type: 'category', name: '秒', data: dd.x, axisLabel: {{ color: MUTED, fontSize: 10, interval: 0 }} }}, baseAxis),
    yAxis: Object.assign({{ type: 'value', name: '真人接听数' }}, baseAxis),
    dataZoom: [
      {{ type: 'inside', xAxisIndex: 0 }},
      {{ type: 'slider', xAxisIndex: 0, height: 18, bottom: 16, borderColor: BORDER, backgroundColor: '#f8fafc',
         fillerColor: 'rgba(37,99,235,0.12)', handleStyle: {{ color: '#2563eb' }}, textStyle: {{ color: MUTED, fontSize: 10 }} }},
    ],
    series: dd.series.map(s => ({{
      name: s.name, type: 'bar', data: s.data,
      itemStyle: {{ borderRadius: [3, 3, 0, 0] }},
    }})),
  }}, true);
}}

function renderFirstSentenceDur(fd) {{
  if (!fd || !fd.n) {{
    charts['chart-first-sentence-dur'].clear();
    charts['chart-first-sentence-dur'].setOption({{
      title: {{ text: '无首句挂断数据', left: 'center', top: 'middle',
                textStyle: {{ color: MUTED, fontSize: 12, fontWeight: 'normal' }} }},
    }});
    return;
  }}
  const total = fd.n;
  // Truncate trailing zero tail for readability — find the last second with data.
  let lastIdx = fd.data.length - 1;
  while (lastIdx > 0 && fd.data[lastIdx] === 0) lastIdx--;
  const x = fd.x.slice(0, lastIdx + 1);
  const data = fd.data.slice(0, lastIdx + 1);

  charts['chart-first-sentence-dur'].setOption({{
    color: ['#f43f5e'],
    tooltip: tooltipBase({{
      trigger: 'axis', axisPointer: {{ type: 'shadow' }},
      formatter: params => {{
        const p = params[0];
        const pct = (p.value / total * 100).toFixed(1);
        return `<b>${{p.name}} 秒</b><br>${{p.value}} 通 · ${{pct}}% / 首句挂断 (n=${{total}})`;
      }},
    }}),
    grid: {{ left: 48, right: 16, top: 28, bottom: 36, containLabel: true }},
    xAxis: Object.assign({{ type: 'category', name: '秒', data: x,
                            axisLabel: {{ color: MUTED, fontSize: 10, interval: 0,
                                          rotate: x.length > 15 ? 35 : 0 }} }}, baseAxis),
    yAxis: Object.assign({{ type: 'value', name: `通话数 (n=${{total}})` }}, baseAxis),
    series: [{{
      name: '首句挂断', type: 'bar', data,
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
      return `<tr class="clickable" data-n="${{i+1}}"><td>${{r.label}}</td><td class="num">${{r.count}}</td><td class="pct-bar"><div class="fill" style="width:${{w}}%;"></div><span class="pct-text">${{r.pct}}%</span></td></tr>`;
    }}).join('')
  }}</tbody></table>`;
  el.querySelectorAll('tr.clickable').forEach(tr => {{
    tr.addEventListener('click', () => {{
      const n = parseInt(tr.getAttribute('data-n'), 10);
      const [rows, hint] = FILTERS.earlyHangup(n, scopedRows());
      exportRows(rows, hint);
    }});
  }});
}}

function render(key) {{
  const d = DATA.datasets[key];
  renderHero(d.totals, d.denominators);
  renderFunnel(d.totals);
  renderTurnTriad(d.turn_dist);
  renderDuration(d.duration_dist);
  renderEarlyHangupTable(d.early_hangup);
  renderFirstSentenceDur(d.first_sentence_dur);
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

charts['chart-duration'].on('click', p => {{
  if (p.componentType !== 'series') return;
  const sec = parseInt(p.name, 10);
  const [rows, hint] = FILTERS.duration(sec, scopedRows());
  exportRows(rows, hint);
}});

window.addEventListener('resize', () => {{
  Object.values(charts).forEach(c => c.resize());
}});

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
