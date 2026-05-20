#!/usr/bin/env python3
"""按北京时间窗口 (+ 可选 Agent/Campaign 关键字) 筛选 Agora ConvoAI 原始 CSV/XLSX。

跟 dashboard 本体无关 — 只做"原始 → 清洗"的前置工具。输出一个新的 CSV，
列与输入完全一致，行数被时间/agent 窗口过滤。生成的文件可直接喂给
serve_dashboard.py / build_dashboard.py。

典型用法：

    bash run.sh tools/filter ~/Desktop/5-21/原始.csv --bjt "5-21 10:00 - 12:00"
    → 输出 ~/Desktop/5-21/原始_BJT-0521_10-12.csv

或直接 python：

    .venv/bin/python scripts/tools/filter.py ~/Desktop/5-21/原始.csv \\
        --from "5-21 10:00" --to "5-21 12:00" --agent lxc

时间解析：
- 默认按 **北京时间 (Asia/Shanghai, UTC+8)** 理解 --from / --to / --bjt
- 支持多种简化格式：
    "5-21 10:00"           当年 5 月 21 日 10:00 BJT
    "2026-05-21 10:00"     完整 BJT
    "2026-05-21T10:00+08:00" 带时区
- CSV 内的 Call Start Time 是 UTC (ISO Z)，脚本自动换算
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

BJT = timezone(timedelta(hours=8))
TIME_COL = "Call Start Time"

# Structured Output 里 dashboard 不识别 / 不需要的多余字段, 默认在清洗时去掉.
# 现在只有"购车省份" (PROVINCE_FALLBACK_FIELD, dead code, 完全无效).
SO_DROP_FIELDS_DEFAULT = ["购车省份"]


def strip_so_fields(raw: str, drop_fields: list[str]) -> str:
    """把 Structured Output JSON 字符串里指定字段去掉. 解析失败 / 空 → 原样返回."""
    if not raw or not str(raw).strip():
        return raw
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    if not isinstance(obj, dict):
        return raw
    changed = False
    for f in drop_fields:
        if f in obj:
            del obj[f]
            changed = True
    if not changed:
        return raw
    # ensure_ascii=False 保留中文, separators 紧凑 (和原始格式一致)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ─────────────────── 时间解析 ───────────────────

def parse_bjt(s: str) -> datetime:
    """把用户输入的简化字符串解析成带 BJT 时区的 datetime。"""
    s = s.strip()
    now = datetime.now(BJT)

    # 1. 'M-D HH:MM' (省略年份, 当年)
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        month, day, h, mi, sec = (int(x) if x else 0 for x in m.groups())
        return datetime(now.year, month, day, h, mi, sec, tzinfo=BJT)

    # 2. 'YYYY-M-D HH:MM[:SS]'
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        y, mo, d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
        return datetime(y, mo, d, h, mi, sec, tzinfo=BJT)

    # 3. 完整 ISO (含时区) → pandas 解析
    try:
        return pd.to_datetime(s, utc=False).to_pydatetime().astimezone(BJT)
    except Exception:
        pass

    raise SystemExit(f"无法解析时间: {s!r}\n支持格式: '5-21 10:00' / '2026-05-21 10:00' / ISO 含时区")


def parse_bjt_range(s: str) -> tuple[datetime, datetime]:
    """解析 '--bjt' 简写: '5-21 10:00 - 12:00' / '5-21 10:00 - 5-22 09:00'.

    要求分隔符两侧必须有空格 (' - ' / ' — ' / ' ~ ')，避免把日期里的 '-' 当成 range 分隔.
    """
    parts = re.split(r"\s+[-—~]\s+", s, maxsplit=1)
    if len(parts) != 2:
        raise SystemExit(f"--bjt 格式错: 需要 'START - END' (中间分隔符两侧必须空格)，"
                          f"得到 {s!r}")
    left, right = parts[0].strip(), parts[1].strip()
    f = parse_bjt(left)
    # 右侧如果只是 HH:MM，沿用左边的日期
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", right)
    if m:
        h, mi, sec = (int(x) if x else 0 for x in m.groups())
        t = datetime(f.year, f.month, f.day, h, mi, sec, tzinfo=BJT)
    else:
        t = parse_bjt(right)
    return f, t


# ─────────────────── 主流程 ───────────────────

def load(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str)
    return pd.read_csv(path, dtype=str)


def main() -> int:
    p = argparse.ArgumentParser(
        description="按 BJT 时间窗口 / agent / campaign 筛选 Agora 原始 CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("input", type=Path, help="原始 CSV 或 XLSX 路径")
    p.add_argument("-o", "--output", type=Path, help="输出 CSV 路径 (默认放在输入同目录, 文件名自动生成)")
    g = p.add_argument_group("时间窗口 (北京时间, BJT)")
    g.add_argument("--from", dest="from_", help='例: "5-21 10:00" / "2026-05-21 10:00"')
    g.add_argument("--to",   dest="to_",   help='例: "5-21 12:00"')
    g.add_argument("--bjt", help='简写: "5-21 10:00 - 12:00" (右侧只给 HH:MM 时沿用左侧日期)')
    p.add_argument("--agent", help="Agent Name 必须包含的关键字 (大小写不敏感)")
    p.add_argument("--campaign", help="Campaign Name 必须包含的关键字")
    p.add_argument("--hangup", help='Hangup Reason 白名单, 逗号分隔. 例: "USER_HANGUP,AI_HANGUP"')
    p.add_argument("--allow-empty-transcript", action="store_true",
                   help="允许 Transcript 为空 / '[]' 的行 (默认剔除)")
    p.add_argument("--allow-empty-recording", action="store_true",
                   help="允许 Audio Record File Download URL 为空的行 (默认剔除)")
    p.add_argument("--drop-so-field", action="append", default=None,
                   help='Structured Output JSON 里要剔除的字段, 可多次指定. '
                        f'默认去掉: {SO_DROP_FIELDS_DEFAULT}. 用 "--keep-all-so" 关闭剔除.')
    p.add_argument("--keep-all-so", action="store_true",
                   help="保留 Structured Output 原始字段, 不做任何剔除")
    p.add_argument("--dry-run", action="store_true", help="只显示筛选后的行数和分布, 不写文件")
    p.add_argument("--show-times", action="store_true", help="打印筛选后的时间窗口边界 (BJT 格式)")
    args = p.parse_args()

    if not args.input.exists():
        raise SystemExit(f"input 不存在: {args.input}")

    # 解析时间窗口
    if args.bjt and (args.from_ or args.to_):
        raise SystemExit("--bjt 不能和 --from/--to 同时用")
    t_from = t_to = None
    if args.bjt:
        t_from, t_to = parse_bjt_range(args.bjt)
    else:
        if args.from_: t_from = parse_bjt(args.from_)
        if args.to_:   t_to   = parse_bjt(args.to_)

    # 读 CSV
    df = load(args.input)
    n_total = len(df)
    print(f"读入 {args.input.name}: {n_total} 行", file=sys.stderr)

    if TIME_COL not in df.columns:
        raise SystemExit(f"输入文件没有 {TIME_COL!r} 列")

    # CSV 里时间是 UTC (ISO Z), 转成 tz-aware datetime
    ts = pd.to_datetime(df[TIME_COL], errors="coerce", utc=True)
    df["_ts_bjt"] = ts.dt.tz_convert("Asia/Shanghai")

    # 时间过滤
    mask = pd.Series(True, index=df.index)
    if t_from is not None:
        mask &= df["_ts_bjt"] >= t_from
        print(f"  从 BJT {t_from:%Y-%m-%d %H:%M:%S}+08", file=sys.stderr)
    if t_to is not None:
        mask &= df["_ts_bjt"] <= t_to
        print(f"  到 BJT {t_to:%Y-%m-%d %H:%M:%S}+08", file=sys.stderr)
    if args.agent:
        mask &= df["Agent Name"].fillna("").str.contains(args.agent, case=False, na=False)
        print(f"  Agent Name 含 {args.agent!r}", file=sys.stderr)
    if args.campaign:
        mask &= df["Campaign Name"].fillna("").str.contains(args.campaign, case=False, na=False)
        print(f"  Campaign Name 含 {args.campaign!r}", file=sys.stderr)
    if args.hangup:
        hangs = [x.strip() for x in args.hangup.split(",")]
        mask &= df["Hangup Reason"].isin(hangs)
        print(f"  Hangup Reason ∈ {hangs}", file=sys.stderr)

    # 默认剔除 Transcript / Audio URL 为空的行 (没法评估 agent / 没法听录音)
    if not args.allow_empty_transcript and "Transcript" in df.columns:
        t = df["Transcript"].fillna("").astype(str).str.strip()
        has_tr = ~(t.isin(["", "[]", "null"]))
        n_drop = (mask & ~has_tr).sum()
        mask &= has_tr
        if n_drop:
            print(f"  剔除 Transcript 为空 {n_drop} 行", file=sys.stderr)
    if not args.allow_empty_recording and "Audio Record File Download URL" in df.columns:
        a = df["Audio Record File Download URL"].fillna("").astype(str).str.strip()
        has_rec = a != ""
        n_drop = (mask & ~has_rec).sum()
        mask &= has_rec
        if n_drop:
            print(f"  剔除 录音 URL 为空 {n_drop} 行", file=sys.stderr)

    out = df[mask].drop(columns=["_ts_bjt"])
    n_out = len(out)
    print(f"过滤后 {n_out} 行 ({n_out/n_total*100:.1f}%)", file=sys.stderr)

    # 显示分布
    if n_out > 0:
        print(f"  Agent 分布: {dict(out['Agent Name'].value_counts())}", file=sys.stderr)
        if args.show_times:
            ts_out = pd.to_datetime(out[TIME_COL], utc=True).dt.tz_convert("Asia/Shanghai")
            print(f"  实际 BJT 区间: {ts_out.min():%Y-%m-%d %H:%M} → {ts_out.max():%Y-%m-%d %H:%M}", file=sys.stderr)

    if args.dry_run:
        return 0

    if n_out == 0:
        print("⚠️ 0 行被保留, 不写文件", file=sys.stderr)
        return 1

    # Structured Output 字段清洗
    if not args.keep_all_so and "Structured Output" in out.columns:
        drop = args.drop_so_field or SO_DROP_FIELDS_DEFAULT
        # 统计有多少行受影响, 给用户反馈
        before = out["Structured Output"].fillna("").astype(str)
        after = before.apply(lambda s: strip_so_fields(s, drop))
        diff_cnt = int((before != after).sum())
        if diff_cnt:
            print(f"  Structured Output 剔除字段 {drop}: 影响 {diff_cnt} 行", file=sys.stderr)
        out = out.assign(**{"Structured Output": after})

    # 决定输出路径
    if args.output:
        out_path = args.output
    else:
        stem = args.input.stem
        tag_parts = []
        if t_from and t_to:
            same_day = t_from.date() == t_to.date()
            if same_day:
                tag_parts.append(f"BJT-{t_from:%m%d}_{t_from:%H%M}-{t_to:%H%M}")
            else:
                tag_parts.append(f"BJT-{t_from:%m%d-%H%M}_to_{t_to:%m%d-%H%M}")
        elif t_from:
            tag_parts.append(f"BJT-from-{t_from:%m%d-%H%M}")
        elif t_to:
            tag_parts.append(f"BJT-to-{t_to:%m%d-%H%M}")
        if args.agent:
            tag_parts.append(f"agent-{re.sub(r'[^a-zA-Z0-9_-]', '_', args.agent)}")
        if args.campaign:
            tag_parts.append(f"camp-{re.sub(r'[^a-zA-Z0-9_-]', '_', args.campaign)}")
        tag = "_".join(tag_parts) if tag_parts else "filtered"
        out_path = args.input.with_name(f"{stem}_{tag}.csv")

    out.to_csv(out_path, index=False)
    print(f"✓ 写出 {out_path}", file=sys.stderr)
    print(out_path)  # stdout 只打印路径, 方便 shell pipeline
    return 0


if __name__ == "__main__":
    sys.exit(main())
