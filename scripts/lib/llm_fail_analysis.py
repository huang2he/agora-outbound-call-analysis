"""LLM 失败分析模块（Tab 2 v2 核心）。

对每个有效会话调用 LLM，输出该通"在哪一轮 agent 出问题 / 哪一轮被客户识破 /
卡在哪一关 / 失败类别 / 改进建议"。

LLM 客户端 (OpenAI chat-completions 兼容):
  - OPENAI_BASE_URL  默认 https://sub2api.agoraio.cn
  - OPENAI_API_KEY   必填
  - OPENAI_MODEL     默认 gpt-5.4
  - 备用：如果 OPENAI_* 都没设，回退 DashScope qwen3.6-plus

任务 model:
  job state 是 module-level 全局 dict，serve_dashboard.py 通过
  kickoff() / status_snapshot() 接入。和 LLM 意向真伪 job 完全独立。
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


# ─────────────────────── 配置读取 ────────────────────────

ENV_PATHS = [
    Path.home() / ".config" / "agora-outbound-call-analysis" / "env",
    Path.home() / ".config" / "agora-skill" / "env",
]


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in ENV_PATHS:
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:  # noqa: BLE001
            pass
    for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
              "OPENAI_REASONING_EFFORT", "DASHSCOPE_API_KEY", "LLM_MODEL"):
        if k in os.environ:
            out[k] = os.environ[k]
    return out


ENV = _load_env()

# 主选 OpenAI 反代；备用 DashScope（家里网不通 OpenAI 反代时用）
OPENAI_API_KEY = ENV.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = ENV.get("OPENAI_BASE_URL", "https://sub2api.agoraio.cn")
OPENAI_MODEL = ENV.get("OPENAI_MODEL", "gpt-5.4")
DASHSCOPE_API_KEY = ENV.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_MODEL = ENV.get("LLM_MODEL", "qwen3.6-plus")

# 并发：反代 gpt-5.4 在 8 并发下大量 503/429, 降到 4
LLM_WORKERS = 4
LLM_TIMEOUT_S = 90
LLM_MAX_RETRIES = 4         # 总尝试次数 (含首次)
LLM_RETRY_BACKOFF_S = [2, 5, 12, 25]  # 每次重试前的等待秒数


def active_backend() -> tuple[str, str, str, str] | None:
    """返回 (label, base_url, api_key, model)。优先 OpenAI 反代，回退 DashScope，
    都没配返回 None。"""
    if OPENAI_API_KEY:
        return ("openai-proxy", OPENAI_BASE_URL.rstrip("/"), OPENAI_API_KEY, OPENAI_MODEL)
    if DASHSCOPE_API_KEY:
        return ("dashscope", DASHSCOPE_BASE_URL, DASHSCOPE_API_KEY, DASHSCOPE_MODEL)
    return None


# ─────────────────────── Prompt ────────────────────────

SYSTEM_PROMPT = """你是一个外呼销售质检专家，专门评估 AI 外呼 agent 在汽车销售场景下的表现。

业务场景：AI agent 自动外呼陌生客户，目标是依次收集以下 4 个槽位信息（4 关）：
  第 1 关 车型：购车品牌 AND 购车型号（如：丰田凯美瑞）
  第 2 关 城市：客户购车的城市
  第 3 关 时间：购车时间（如：3 个月内）
  第 4 关 姓氏：客户的姓或姓名

严格线性递进——第 N 关必须在 1..N-1 全过的前提下才算过。

你的任务：分析一通已经结束的失败/部分成功通话（卡在 0-3 关之间），判断
  1. agent 是在哪一个 assistant turn 出了关键问题（导致客户从配合转向流失）
  2. 客户是在哪一个 user turn 表现出明显的不耐烦/识破/拒绝
  3. 失败的核心类别
  4. 一句话改进建议

只返回 JSON，schema 严格如下：
{
  "fail_turn": <int, assistant 的第几个 turn 出问题, 1-based>,
  "fail_reason": "<≤30 字, 描述 agent 出了什么问题>",
  "fail_category": "<其中之一: 开场太突兀 | 话术机械重复 | 没接客户上文 | 误判客户意图 | 提问跳跃 | 信息收集不彻底 | 客户主动拒绝 | 其他>",
  "user_detect_turn": <int 或 null, 客户在哪个 user turn 开始反感/识破>,
  "user_detect_signal": "<≤50 字, 客户原话或行为>",
  "user_sentiment_start": "<客户开局态度: 积极 | 中性 | 消极>",
  "user_sentiment_end":   "<客户结尾态度: 积极 | 中性 | 消极>"
}

注意:
- fail_turn 是 assistant 的第几次发言（不是 transcript 的 turn_id），从 1 开始数
- 如果 agent 没明显失败、纯粹客户态度问题，fail_turn 可填 1，fail_category 选"客户主动拒绝"
- user_sentiment 判定:
  · "积极" = 主动给信息 / 主动询问 / 配合度高
  · "中性" = 嗯哦敷衍 / 提问澄清 / 态度模糊
  · "消极" = 抗拒 / 反讽 / 骂人 / 多次拒绝
- 关注客户开局 (前 1-2 个 user turn) 和结尾 (最后 1-2 个 user turn) 的态度对比
- 必须返回上面所有字段，不要加任何 markdown 围栏。
"""


def build_user_prompt(transcript_text: str, pass_n: int, agent_name: str) -> str:
    return f"""[Agent] {agent_name}
[当前通话通过了第 {pass_n} 关 (0=一关没过 / 4=全过)]

[完整 transcript, 双方对话 turn 按顺序排列]
{transcript_text}

请按系统要求返回 JSON 分析。
"""


def format_transcript(transcript: list[dict], max_chars: int = 3500) -> str:
    """渲染为 'A1 agent: ...\\nU1 user: ...' 格式，给 LLM 看好分辨第几轮。"""
    lines: list[str] = []
    a_n = u_n = 0
    for t in transcript:
        role = t.get("role")
        content = str(t.get("content", "")).strip()
        if not content:
            continue
        if role == "assistant":
            a_n += 1
            lines.append(f"A{a_n} agent: {content}")
        elif role == "user":
            # 跳过系统兜底 / IVR（也告诉 LLM 这是系统注入）
            from .agent_kda import is_real_user_turn
            if is_real_user_turn(t):
                u_n += 1
                lines.append(f"U{u_n} user: {content}")
            # 否则不计数也不输出（保持 LLM 看到的对话干净）
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...(已截断)"
    return text


# ─────────────────────── LLM 调用 ────────────────────────

def _call_llm(messages: list[dict]) -> dict:
    """重试 LLM_MAX_RETRIES 次 (主要扛反代的 429 / 503)."""
    backend = active_backend()
    if not backend:
        return {"error": "no LLM backend configured (set OPENAI_API_KEY or DASHSCOPE_API_KEY)"}
    label, base_url, api_key, model = backend

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    url = (f"{base_url}/v1/chat/completions" if not base_url.endswith("/v1")
           else f"{base_url}/chat/completions")

    # 全错重试: 任何错误都进重试循环, 只要还没用完 attempts 就再试.
    # 反代偶尔会安全拒答 / 返回非 JSON / 给 4xx, 重试通常能成功.
    last_err = ""
    for attempt in range(LLM_MAX_RETRIES):
        wait_before_next = LLM_RETRY_BACKOFF_S[min(attempt, len(LLM_RETRY_BACKOFF_S) - 1)]
        req = urllib.request.Request(
            url, method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            # 反代偶尔返回的 message 没有 content (触发 safety 等)
            try:
                content = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                fr = (payload.get("choices") or [{}])[0].get("finish_reason", "")
                last_err = f"LLM 无 content (finish_reason={fr or '?'})"
                raise _RetryableError(last_err)
            if content is None:
                last_err = "LLM content 为 null (反代拒答)"
                raise _RetryableError(last_err)
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content
                if content.endswith("```"):
                    content = content.rsplit("```", 1)[0]
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                last_err = f"非 JSON 输出: {str(e)[:120]} · 原文头: {content[:80]}"
                raise _RetryableError(last_err)
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            last_err = f"HTTP {e.code}: {err_body[:200]}"
            # 全部 HTTP 错误都重试 (即使 4xx - 反代偶尔抽风返回 400/422)
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = f"network: {str(e)[:200]}"
        except _RetryableError:
            pass  # last_err 已设置
        except Exception as e:  # noqa: BLE001
            last_err = f"unexpected: {str(e)[:200]}"

        # 已 fall through 到这里 = 本次失败, 看还能否再试
        if attempt >= LLM_MAX_RETRIES - 1:
            return {"error": last_err or "exhausted retries"}
        time.sleep(wait_before_next)
    return {"error": last_err or "exhausted retries"}


class _RetryableError(Exception):
    """内部信号: 表示这次调用应该走重试逻辑."""
    pass


def analyze_call(call: dict) -> dict:
    """单通通话失败分析。

    call: {"call_id", "agent_name", "pass_n", "transcript": list[dict]}
    返回结构同 LLM JSON + 加 call_id / agent_name / pass_n 元信息。
    """
    transcript = call.get("transcript") or []
    transcript_text = format_transcript(transcript)
    user_msg = build_user_prompt(transcript_text, call.get("pass_n", 0),
                                  call.get("agent_name", "(unknown)"))
    result = _call_llm([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ])
    return {
        "call_id": call.get("call_id", ""),
        "agent_name": call.get("agent_name", ""),
        "pass_n": call.get("pass_n", 0),
        **result,
    }


# ─────────────────────── Job 状态 ────────────────────────

LLM_FAIL_JOB: dict[str, Any] = {
    "status": "idle",   # idle | running | done | error | skipped
    "total": 0,
    "done": 0,
    "results": [],
    "model": "",
    "backend": "",
    "started_at": None,
    "elapsed_s": 0,
    "error": None,
}
LLM_FAIL_LOCK = threading.Lock()


def status_snapshot() -> dict:
    """供 HTTP endpoint 拷一份状态快照（含全部 results）。"""
    with LLM_FAIL_LOCK:
        return {
            "status": LLM_FAIL_JOB["status"],
            "total": LLM_FAIL_JOB["total"],
            "done": LLM_FAIL_JOB["done"],
            "results": list(LLM_FAIL_JOB["results"]),
            "elapsed_s": LLM_FAIL_JOB["elapsed_s"],
            "error": LLM_FAIL_JOB["error"],
            "model": LLM_FAIL_JOB["model"],
            "backend": LLM_FAIL_JOB["backend"],
        }


def load_snapshot(snapshot_path: str | Path) -> bool:
    """从磁盘加载已经跑过的 LLM 结果, 用于重启时免去重新跑 LLM."""
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
    with LLM_FAIL_LOCK:
        LLM_FAIL_JOB["status"] = "done"
        LLM_FAIL_JOB["total"] = len(results)
        LLM_FAIL_JOB["done"] = len(results)
        LLM_FAIL_JOB["results"] = list(results)
        LLM_FAIL_JOB["elapsed_s"] = float(data.get("elapsed_s") or 0)
        LLM_FAIL_JOB["error"] = None
        LLM_FAIL_JOB["model"] = str(data.get("model") or "")
        LLM_FAIL_JOB["backend"] = str(data.get("backend") or "")
        LLM_FAIL_JOB["started_at"] = time.time()
    print(f"[llm-fail-auto] LOADED snapshot · {len(results)} results from {p}", flush=True)
    return True


def kickoff(df_enriched, sample_limit: int | None = None) -> None:
    """后台启动失败分析。仅对"有效会话 且 pass_n < 4"的通话跑。

    sample_limit: 如果不为 None，对每个 (agent, pass_n) bucket 内随机取 N 通
                  来限制成本。None = 全量。
    """
    from .agent_kda import (parse_transcript, parse_structured_output,
                             passed_levels_count, is_valid_session,
                             is_human_answered)

    # 准备样本
    work = df_enriched.copy()
    work["_transcript"] = work["Transcript"].apply(parse_transcript)
    work["_structured"] = work["Structured Output"].apply(parse_structured_output)
    work["_human"] = work["Hangup Reason"].apply(is_human_answered)
    work["_valid"] = work["_human"] & work["_transcript"].apply(is_valid_session)
    work["_pass_n"] = work["_structured"].apply(passed_levels_count)

    # 失败样本 = 有效会话 且 没全过
    fail = work[work["_valid"] & (work["_pass_n"] < 4)]

    if sample_limit:
        import random
        keep_idx = []
        for (agent, pn), sub in fail.groupby(["Agent Name", "_pass_n"]):
            n = min(sample_limit, len(sub))
            keep_idx.extend(random.sample(list(sub.index), n))
        fail = fail.loc[keep_idx]

    calls = [
        {
            "call_id": r.get("Call ID", ""),
            "agent_name": r.get("Agent Name", ""),
            "pass_n": int(r["_pass_n"]),
            "transcript": r["_transcript"],
        }
        for _, r in fail.iterrows()
    ]

    backend = active_backend()
    with LLM_FAIL_LOCK:
        LLM_FAIL_JOB["total"] = len(calls)
        LLM_FAIL_JOB["done"] = 0
        LLM_FAIL_JOB["results"] = []
        LLM_FAIL_JOB["started_at"] = time.time()
        LLM_FAIL_JOB["elapsed_s"] = 0
        LLM_FAIL_JOB["error"] = None
        if not calls:
            LLM_FAIL_JOB["status"] = "done"
            LLM_FAIL_JOB["model"] = ""
            LLM_FAIL_JOB["backend"] = ""
            return
        if not backend:
            LLM_FAIL_JOB["status"] = "skipped"
            LLM_FAIL_JOB["error"] = ("无 LLM key 配置。在 "
                                      "~/.config/agora-outbound-call-analysis/env 加 "
                                      "OPENAI_API_KEY=... 或 DASHSCOPE_API_KEY=...")
            return
        LLM_FAIL_JOB["status"] = "running"
        LLM_FAIL_JOB["model"] = backend[3]
        LLM_FAIL_JOB["backend"] = backend[0]

    def runner():
        t0 = time.time()
        try:
            with ThreadPoolExecutor(max_workers=LLM_WORKERS) as ex:
                futs = [ex.submit(analyze_call, c) for c in calls]
                for fut in as_completed(futs):
                    res = fut.result()
                    with LLM_FAIL_LOCK:
                        LLM_FAIL_JOB["results"].append(res)
                        LLM_FAIL_JOB["done"] = len(LLM_FAIL_JOB["results"])
                        LLM_FAIL_JOB["elapsed_s"] = round(time.time() - t0, 1)
            with LLM_FAIL_LOCK:
                LLM_FAIL_JOB["status"] = "done"
                LLM_FAIL_JOB["elapsed_s"] = round(time.time() - t0, 1)
            print(f"[llm-fail-auto] DONE {LLM_FAIL_JOB['done']}/{LLM_FAIL_JOB['total']} in "
                  f"{LLM_FAIL_JOB['elapsed_s']}s · {backend[0]}/{backend[3]}", flush=True)
        except Exception as e:  # noqa: BLE001
            with LLM_FAIL_LOCK:
                LLM_FAIL_JOB["status"] = "error"
                LLM_FAIL_JOB["error"] = str(e)[:300]
            traceback.print_exc()

    threading.Thread(target=runner, daemon=True, name="llm-fail-auto").start()
    print(f"[llm-fail-auto] START · {len(calls)} calls · {backend[0]}/{backend[3]} "
          f"· {LLM_WORKERS} workers", flush=True)
