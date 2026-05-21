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

SYSTEM_PROMPT = """你是销售质检专家.

业务的"带车型完整转换"定义:
  系统 SO 同时满足:
  (1) 购车品牌 非空
  (2) 购车型号 非空
  (3) 购车城市 / 购车时间 / 购车姓名 三项中至少 2 项 非空

你的任务:
  Step 1: 重读 transcript, 给系统 SO 中每个非空字段打一个标签:
    - match: 客户在 transcript 中亲口或明确肯定地表达过, OR
             客户给了发散性回答, agent 主动用具体词复述/确认, 客户没反驳 (视为客户认可)
    - invalid: SO 有值但 transcript 找不到合理证据 (凭空填 / 误听 / agent 单方面推断且客户从未回应 / 客户明确反驳)
    - null: SO 本来就是空

  Step 2: 把所有 invalid 字段视为 null, 重新看是否仍满足"品牌+型号+三项任意2"标准, 得出 verdict:
    - valid:             所有非空字段都 match, 原判完全成立
    - so_partial_wrong:  有 invalid 字段, 但剔除后仍满足完整转换标准 (即不影响最终判定)
    - conversion_broken: 剔除 invalid 字段后, 不再满足 (品牌/型号/2 项三选二) 标准, 系统判定不应成立

只返回 JSON, 不加 markdown 围栏:
{
  "verdict": "valid | so_partial_wrong | conversion_broken",
  "reason": "<≤30 字 中文, 指出最关键的 invalid 字段或证据>",
  "field_check": {
    "购车品牌": "match | invalid | null",
    "购车型号": "match | invalid | null",
    "购车城市": "match | invalid | null",
    "购车时间": "match | invalid | null",
    "购车姓名": "match | invalid | null",
    "购车意向": "match | invalid | null"
  },
  "new_so": {
    "购车品牌": "<提取值或null>",
    "购车型号": "<提取值或null>",
    "购车城市": "<提取值或null>",
    "购车时间": "<提取值或null>",
    "购车姓名": "<提取值或null>",
    "购车意向": "是 | 否 | null"
  }
}

特别注意 (针对城市 / 时间字段 - 重灾区, 别一刀切判 invalid):

  口语化时间表达, 客户说出后 agent 主动用规范词确认, 客户没反驳 = match:
    客户: "快了" / "等等再说" / "下个月吧" / "国庆前后" / "看看" / "应该挺快的"
    agent: "那您是计划 3 个月内购车是吗?"
    客户: "嗯" / "对" / "可以" / "好的" / "差不多" / 沉默并继续配合
    → 购车时间 = match (客户认可了 agent 的归类)

  口语化城市表达, agent 主动确认, 客户没反驳 = match:
    客户: "我在那边" / "苏南这边" / "山东这块"
    agent: "您是在济南吗?"
    客户: "嗯/对/没错"
    → 购车城市 = match

  反例 (才是 invalid):
    - 客户从未提及任何时间/城市相关字眼, agent 凭空填入
    - agent 确认, 客户明确反驳 ("不是" / "不对" / "换一个" / "我说的不是这个")
    - agent 单方面陈述, 客户没回应也没继续对话 (突然挂断/沉默)

  其他字段同理: 客户给模糊回答, 但 agent 主动归类 + 客户认可 = match

  品牌/型号/姓名 字段相对刚性, 应该有明确出现, 但同样适用 "agent 主动确认 + 客户认可" 的规则.

new_so 字段提取规则 (重要 - 反面例子见下):
  - new_so 是你"重新从 transcript 提取的客户真实表达", 跟 SO 的对错无关.
  - 如果客户明确表达过 (含 agent 确认 + 客户认可), 填客户认可的值
  - 如果原 SO 错了, transcript 里有客户说的正确值, *必须填正确值* (不要因为 field_check=invalid 就填 null)
  - 客户从未表达且 transcript 找不到任何相关字眼 → null
  - 客户给的是模糊词且 agent 没引导规范化 → null

例子: 客户说 "9月份提车", 系统 SO 误填 "9个月"
  正确返回:
    field_check.购车时间 = invalid (原 SO 是错的)
    new_so.购车时间 = "9月份"  ← *不要填 null*, 客户真的说过, 这是你提取的正确值
  错误返回:
    field_check.购车时间 = invalid
    new_so.购车时间 = null     ← 错! 客户其实说了 9 月份, 应该提取出来
"""


def build_user_prompt(transcript_text: str, structured: dict, agent_name: str) -> str:
    so_text = json.dumps(structured, ensure_ascii=False, indent=2)
    return f"""[Agent] {agent_name}

[完整 transcript]
{transcript_text}

[系统记录的最终 Structured Output]
{so_text}

请按 system 指令逐字段打 match/invalid/null, 并给出 verdict."""


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
