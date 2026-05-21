"""LLM 带车型完整转换 真实性校验。

仅对 `_full_with_model=True` 的通话跑一次, 用 LLM 判断 Structured Output 是不是真实
从客户口中采集 vs 客户瞎编/敷衍/明确拒绝但 agent 硬填的假成单.

返回 JSON: {"verdict": "real|suspect|fake", "reason": "≤30 字", "evidence_turn": int|null}

复用 llm_fail_analysis 的 OpenAI proxy + DashScope 双 backend / 重试机制.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any


# ── Prompt ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是销售质检专家. 业务: AI 外呼销售收集 4 个槽位 + 意向, 系统已标"带车型完整转换"(品牌+型号+城市+时间+姓名 都填了).

任务: 重新基于 transcript 提取 *客户真实表达* 的 SO (new_so), 然后跟系统记录的旧 SO (old_so) 做字段级对比.

6 个槽位:
  购车品牌 / 购车型号 / 购车城市 / 购车时间 / 购车姓名 / 购车意向

提取 new_so 的原则:
  - 只填能在 transcript 找到清晰证据的内容. 没听清 / 客户没说 / 客户敷衍 → 填 null.
  - 客户用模糊词 ("随便" "都行" "看看") 不算明确表达, 应填 null.
  - 客户多次改口的话, 以最后明确表达为准.

字段级 diff 4 类:
  match              - 新旧值一致 (含都为 null)
  mismatch           - 新旧都有值但不同 (agent 误听 / 推断错)
  filled_no_evidence - 旧 SO 有值但 transcript 找不到任何证据 (agent 凭空填)
  missing_should_have- 旧 SO 是 null 但 transcript 客户有清晰表达 (agent 漏填)

整体 verdict:
  real    - 全部 6 字段 diff 都是 match, 且至少 3 个 match 非 null, 客户态度配合
  suspect - 出现至少 1 个 mismatch 或 filled_no_evidence, 但客户态度不算敌对
  fake    - 客户明确拒绝/挂断/没参与对话, 但 SO 仍标 4 槽全填

只返回 JSON, 不要加 markdown 围栏:
{
  "new_so": {
    "购车品牌": "<值或null>",
    "购车型号": "<值或null>",
    "购车城市": "<值或null>",
    "购车时间": "<值或null>",
    "购车姓名": "<值或null>",
    "购车意向": "是 | 否 | null"
  },
  "diff": {
    "购车品牌": "match | mismatch | filled_no_evidence | missing_should_have",
    "购车型号": "...",
    "购车城市": "...",
    "购车时间": "...",
    "购车姓名": "...",
    "购车意向": "..."
  },
  "verdict": "real | suspect | fake",
  "reason": "<≤30 字 中文, 关键依据>"
}
"""


def build_user_prompt(transcript_text: str, structured: dict, agent_name: str) -> str:
    so_text = json.dumps(structured, ensure_ascii=False, indent=2)
    return f"""[Agent] {agent_name}

[完整 transcript]
{transcript_text}

[系统记录的最终 Structured Output]
{so_text}

请判断这个 SO 是真实采集 (real) / 可疑 (suspect) / 明显造假 (fake)."""


# ── LLM 调用 (复用 llm_fail_analysis 的逻辑) ─────────────────────────────

def _get_backend_and_call():
    """从 llm_fail_analysis 拿 backend 配置 + 重试逻辑, 避免重复代码."""
    try:
        from . import llm_fail_analysis as F
    except ImportError:
        from lib import llm_fail_analysis as F  # type: ignore
    return F


def analyze_call(call: dict) -> dict:
    """单通通话真实性校验."""
    F = _get_backend_and_call()
    transcript_text = F.format_transcript(call.get("transcript") or [])
    user_msg = build_user_prompt(
        transcript_text,
        call.get("structured") or {},
        call.get("agent_name", "(unknown)"),
    )
    result = F._call_llm([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ])
    return {
        "call_id": call.get("call_id", ""),
        "agent_name": call.get("agent_name", ""),
        **result,
    }


# ── Job state ──────────────────────────────────────────────────────────────

VERIFY_JOB: dict[str, Any] = {
    "status": "idle",
    "total": 0,
    "done": 0,
    "results": [],
    "elapsed_s": 0,
    "error": None,
    "model": "",
    "backend": "",
}
VERIFY_LOCK = threading.Lock()


def status_snapshot() -> dict:
    with VERIFY_LOCK:
        return {
            "status": VERIFY_JOB["status"],
            "total": VERIFY_JOB["total"],
            "done": VERIFY_JOB["done"],
            "results": list(VERIFY_JOB["results"]),
            "elapsed_s": VERIFY_JOB["elapsed_s"],
            "error": VERIFY_JOB["error"],
            "model": VERIFY_JOB["model"],
            "backend": VERIFY_JOB["backend"],
        }


def kickoff(df_enriched) -> None:
    """后台启动: 对所有 _full_with_model=True 的通话跑校验."""
    F = _get_backend_and_call()
    backend = F.active_backend()

    # 准备样本
    work = df_enriched.copy()
    work["_transcript"] = work["Transcript"].apply(
        lambda x: __import__("json").loads(x) if isinstance(x, str) and x.strip() else []
    )
    fwm = work[work["_full_with_model"]] if "_full_with_model" in work.columns else work
    # _structured 已经在 enrich 里解析过, 直接用
    calls = []
    for _, r in fwm.iterrows():
        calls.append({
            "call_id": str(r.get("Call ID", "")),
            "agent_name": str(r.get("Agent Name", "")),
            "structured": r.get("_structured") or {},
            "transcript": r.get("_transcript") or [],
        })

    with VERIFY_LOCK:
        VERIFY_JOB["total"] = len(calls)
        VERIFY_JOB["done"] = 0
        VERIFY_JOB["results"] = []
        VERIFY_JOB["elapsed_s"] = 0
        VERIFY_JOB["error"] = None
        if not calls:
            VERIFY_JOB["status"] = "done"
            return
        if not backend:
            VERIFY_JOB["status"] = "skipped"
            VERIFY_JOB["error"] = "无 LLM key 配置"
            return
        VERIFY_JOB["status"] = "running"
        VERIFY_JOB["model"] = backend[3]
        VERIFY_JOB["backend"] = backend[0]

    def runner():
        t0 = time.time()
        try:
            # 复用同样的并发设置 (避免压垮反代)
            with ThreadPoolExecutor(max_workers=F.LLM_WORKERS) as ex:
                futs = [ex.submit(analyze_call, c) for c in calls]
                for fut in as_completed(futs):
                    res = fut.result()
                    with VERIFY_LOCK:
                        VERIFY_JOB["results"].append(res)
                        VERIFY_JOB["done"] = len(VERIFY_JOB["results"])
                        VERIFY_JOB["elapsed_s"] = round(time.time() - t0, 1)
            with VERIFY_LOCK:
                VERIFY_JOB["status"] = "done"
                VERIFY_JOB["elapsed_s"] = round(time.time() - t0, 1)
            print(f"[llm-verify-auto] DONE {VERIFY_JOB['done']}/{VERIFY_JOB['total']} in "
                  f"{VERIFY_JOB['elapsed_s']}s · {backend[0]}/{backend[3]}", flush=True)
        except Exception as e:  # noqa: BLE001
            with VERIFY_LOCK:
                VERIFY_JOB["status"] = "error"
                VERIFY_JOB["error"] = str(e)[:300]
            traceback.print_exc()

    threading.Thread(target=runner, daemon=True, name="llm-verify-auto").start()
    print(f"[llm-verify-auto] START · {len(calls)} calls · {backend[0]}/{backend[3]} "
          f"· {F.LLM_WORKERS} workers", flush=True)


def load_snapshot(snapshot_path) -> bool:
    """从磁盘加载已经跑过的结果."""
    from pathlib import Path
    p = Path(snapshot_path).expanduser().resolve()
    if not p.is_file():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    results = data.get("results") or []
    if not isinstance(results, list):
        return False
    with VERIFY_LOCK:
        VERIFY_JOB["status"] = "done"
        VERIFY_JOB["total"] = len(results)
        VERIFY_JOB["done"] = len(results)
        VERIFY_JOB["results"] = list(results)
        VERIFY_JOB["elapsed_s"] = float(data.get("elapsed_s") or 0)
        VERIFY_JOB["error"] = None
        VERIFY_JOB["model"] = str(data.get("model") or "")
        VERIFY_JOB["backend"] = str(data.get("backend") or "")
    print(f"[llm-verify-auto] LOADED snapshot · {len(results)} results from {p}", flush=True)
    return True
