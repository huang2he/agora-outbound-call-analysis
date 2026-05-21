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

SYSTEM_PROMPT = """你是销售质检专家. 评判要宽容, 关注 agent 是否扭曲了客户原意, 不要苛求字面一致.

业务的"带车型完整转换"定义:
  系统 SO 同时满足:
  (1) 购车品牌 非空
  (2) 购车型号 非空
  (3) 购车城市 / 购车时间 / 购车姓名 三项中至少 2 项 非空

核心默认规则:
  *客户不反驳 = 客户认可* — 只要客户没明确反驳/纠正/否定, 都视为认可 agent 的归类.
  反驳的判定: 客户明确说 "不是"/"不对"/"换一个"/"我说的不是这个" 等否定词. 沉默 / 嗯啊 / 继续配合下一题 都算认可.

字段级标签 (field_check):
  - match:   客户亲口说过, OR agent 主动归类后客户没反驳 (即默认认可)
  - invalid: 客户从未提及该字段相关内容 (agent 凭空填), OR 客户明确反驳过 agent 的归类
  - null:    SO 本来就是空

车型 / 品牌 字段的隐含关系 (重要):
  购车型号是 match 时, 购车品牌自动视为 match (型号本身蕴含品牌, 不必苛责品牌字段写法).
  举例: 客户说 "汉兰达" → SO 品牌=汉兰达 / 型号=四驱精英版 也算 ok (汉兰达是车系, 销售场景品牌字段写它合理)

购车时间字段的特别处理:
  当前日期信息会在 user prompt 顶部以 [CURRENT_DATE: YYYY-MM-DD] 提供, 必须参考.
  - 客户说的时间字段必须是"未来或当下" (在 CURRENT_DATE 当天或之后), 才能算有效购车意向
  - 如果客户说 "1月份提车" 但 CURRENT_DATE 是 5 月, 这是已过去的时间, 客户应是无效意向 → invalid
  - 如果客户说"X月份"或"X个月"或"快了"等表达, agent 主动归类为某个具体时间区间且客户未反驳 → match
  - "6月份" / "6 个月" / "6 月内" 在销售场景下数字一致 (都指接下来 1-6 个月), agent 归类合理则 match

整体 verdict (Step 2):
  - valid:             所有非空字段都 match, 原判完全成立
  - so_partial_wrong:  有 invalid 字段, 但剔除后仍满足 (品牌+型号+三选二)
  - conversion_broken: 剔除 invalid 后, 不再满足完整转换标准, 系统判定不应成立

new_so 字段提取规则:
  - new_so 是你"重新从 transcript 提取的客户真实表达", 跟 SO 对错无关
  - 客户明确表达过 (含 agent 归类+客户未反驳) → 填客户认可的归类值
  - 原 SO 错了, transcript 里有客户原话 → *必须填客户原话提取值* (不要因为 invalid 就填 null)
  - 客户从未表达且 transcript 找不到相关字眼 → null

只返回 JSON, 不加 markdown 围栏:
{
  "verdict": "valid | so_partial_wrong | conversion_broken",
  "reason": "<≤30 字 中文, 关键证据>",
  "gray_area": "luxury_tease | brand_model_mismatch | null",
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

gray_area 灰色地带识别 (单独标, 优先级最高):
  - luxury_tease: 客户态度随意敷衍但 SO 标了 100w+ 超豪车 (保时捷帕拉梅拉/劳斯莱斯/宾利/迈巴赫/法拉利/兰博基尼 等),
    客户回答短/笑/带玩笑口吻/姓氏怪异 (如"姓吹""姓牛"), 不像真正购车需求, 而是调戏 / 测试 / 戏耍.
    判定要点: 价格 100w 以上的车 + 客户态度不严肃 = luxury_tease
  - brand_model_mismatch: SO 品牌字段和型号字段是不同的车 (如 "本田" 配 "凯美瑞", "奔驰" 配 "X5"), 客户原话本身就矛盾, 或 agent 拼装错误
    豁免: 子品牌情况不算 mismatch (如 梅赛德斯-迈巴赫 / 劳斯莱斯-库里南 / 兰博基尼-Urus)
  - null: 不属于上述任何灰色情况

灰色样例:
  case A (luxury_tease):
    SO 品牌=保时捷, 型号=帕拉梅拉, 姓=吹, 客户"姓吹牛, 吹", 态度笑闹
    → gray_area = "luxury_tease"
  case B (brand_model_mismatch):
    SO 品牌=梅赛德斯奔驰, 型号=迈巴赫 (规范应该是 梅赛德斯-迈巴赫 一个品牌, 但写成两个字段不严谨)
    → gray_area = "brand_model_mismatch" (品牌和型号不是同一辆车的常规命名)

正反例参考:

  case 1 (购车时间宽容):
    客户: "合适的话可以是 6 月份" → agent: "6 月份提车" → SO 时间="6个月"
    判定: field_check.购车时间 = match (客户说的 6 月份, SO 写 6 个月在销售场景同义)
    new_so.购车时间 = "6 月份"

  case 2 (车型蕴含品牌):
    客户: "汉兰达" 然后 "四驱精英版" → SO 品牌="汉兰达" 型号="四驱精英版"
    判定: 购车品牌 = match (型号在, 品牌自动 ok, 汉兰达是合理品牌字段值)
          购车型号 = match (客户原话)
    new_so.购车品牌 = "汉兰达", new_so.购车型号 = "汉兰达 四驱精英版" 或 "四驱精英版"

  case 3 (过去时间无效):
    CURRENT_DATE = 2026-05-21, 客户: "1 月份提车"
    判定: 购车时间 = invalid (1 月已过, 不可能是未来意向)
    new_so.购车时间 = null (除非客户后面修正了)
"""


def build_user_prompt(transcript_text: str, structured: dict, agent_name: str) -> str:
    so_text = json.dumps(structured, ensure_ascii=False, indent=2)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    return f"""[CURRENT_DATE: {today}]
[Agent] {agent_name}

[完整 transcript]
{transcript_text}

[系统记录的最终 Structured Output]
{so_text}

请按 system 指令逐字段打 match/invalid/null, 并给出 verdict. 评判要宽容, 客户不反驳即认可."""


# ── verdict 严格计算 (基于业务标准, 不依赖 LLM 数数) ─────────────────────

def _is_empty(v) -> bool:
    if v is None:
        return True
    s = str(v).strip()
    return s == "" or s.lower() == "null"


def derive_verdict(old_so: dict, field_check: dict, llm_gray: str | None = None) -> str:
    """按业务定义严格判 verdict.
    带车型完整转换标准 = 品牌非空 + 型号非空 + (城市/时间/姓名) 三选二.
    把所有 invalid 字段视作空, 再看是否仍满足.

    优先级:
      1. llm_gray 非空 → gray_area (LLM 识别为豪车调戏 / 品牌型号不符)
      2. 不满足品牌+型号+三选二 → conversion_broken (硬性条件不达标)
      3. 有 invalid 字段 → so_partial_wrong
      4. 全 match → valid
    """
    old_so = old_so or {}
    field_check = field_check or {}

    if llm_gray and str(llm_gray).strip().lower() not in ("", "null", "none"):
        return "gray_area"

    def slot_ok(f: str) -> bool:
        v = old_so.get(f)
        if _is_empty(v):
            return False
        return field_check.get(f) != "invalid"

    has_brand = slot_ok("购车品牌")
    has_model = slot_ok("购车型号")
    # 车系蕴含品牌: 型号 ok 时品牌默认 ok (汉兰达/凯美瑞 等口语品牌)
    if has_model and not has_brand:
        has_brand = True

    extras = sum(1 for f in ["购车城市", "购车时间", "购车姓名"] if slot_ok(f))
    qualified = has_brand and has_model and extras >= 2

    if not qualified:
        # 硬性条件不满足 (型号或品牌空, 或三选二不够) → conversion_broken
        return "conversion_broken"

    has_invalid = any(v == "invalid" for v in field_check.values())
    if has_invalid:
        return "so_partial_wrong"
    return "valid"


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
    # 后端强制覆盖 verdict: LLM 数数容易错, 严格按业务标准重算
    if isinstance(result, dict) and not result.get("error"):
        fc = result.get("field_check") or {}
        if fc:
            llm_verdict = result.get("verdict")
            llm_gray = result.get("gray_area")
            derived = derive_verdict(call.get("structured") or {}, fc, llm_gray)
            result["verdict"] = derived
            if llm_verdict and llm_verdict != derived:
                # 保留 LLM 原判作为调试参考 (前端不用)
                result["_llm_verdict_raw"] = llm_verdict
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
