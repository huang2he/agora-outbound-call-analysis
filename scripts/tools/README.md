# scripts/tools — 与 dashboard 解耦的预处理工具

dashboard 主流程 (`build_dashboard.py` / `serve_dashboard.py`) 不引用这里的脚本。
这些工具只做"研发原始导出 → dashboard 能吃的格式"之间的一些小整理。

## filter.py — 按时间窗口 (北京时间) / Agent / Campaign 筛选

### 经典工作流

研发把当批的原始 CSV 丢到桌面某个文件夹，比如 `~/Desktop/5-21 冠客外呼/原始.csv`。
你只想看某个时间段（比如 5-21 上午 10:00-12:00 BJT）+ 某个 agent 的数据：

```bash
# 1. 筛选
bash ~/.claude/skills/agora-outbound-call-analysis/scripts/tools/filter.sh \
  "~/Desktop/5-21 冠客外呼/原始.csv" \
  --bjt "5-21 10:00 - 12:00"
# → 输出 ~/Desktop/5-21 冠客外呼/原始_BJT-0521_1000-1200.csv

# 2. 喂给 dashboard
bash ~/.claude/skills/agora-outbound-call-analysis/scripts/run.sh \
  "~/Desktop/5-21 冠客外呼/原始_BJT-0521_1000-1200.csv" \
  --host 0.0.0.0
```

### 时间格式

`--bjt`, `--from`, `--to` 都按 **北京时间** 解析。CSV 里的 `Call Start Time` 是
UTC (ISO Z) 格式，脚本自动换算 (+8 小时)。

支持的字符串：

| 写法 | 含义 |
|---|---|
| `5-21 10:00`             | 今年 5 月 21 日 10:00 BJT |
| `5-21 10:00:30`          | 同上精确到秒 |
| `2026-05-21 10:00`       | 显式年份 |
| `2026-05-21T10:00:00+08:00` | 完整 ISO 含时区 |

`--bjt` 简写一次性给区间：`"5-21 10:00 - 12:00"`（分隔符 `-` 两侧必须有空格，避免和日期里的 `-` 混淆）。
右侧只给 `HH:MM` 时沿用左边的日期；要跨天就完整写 `"5-21 22:00 - 5-22 02:00"`。

### 常用参数

```
--bjt "5-21 10:00 - 12:00"        BJT 时间区间
--from "5-21 10:00"               起点 (单独用)
--to   "5-21 12:00"               终点 (单独用)
--agent lxc                       Agent Name 包含关键字 (大小写不敏感)
--campaign "外呼测试"             Campaign Name 包含关键字
--hangup USER_HANGUP,AI_HANGUP    Hangup Reason 白名单
--drop-so-field 购车省份          Structured Output 里要剔除的字段 (可多次)
--keep-all-so                     关闭剔除, 保留原始 Structured Output
-o output.csv                     显式指定输出路径 (默认自动取名)
--dry-run                         只打印筛选后行数和分布, 不写文件
--show-times                      额外打印筛选后实际的 BJT 起止时间
```

### Structured Output 字段清洗 (默认开启)

dashboard 只识别 6 个字段 (`购车品牌` / `购车型号` / `购车城市` / `购车时间` /
`购车姓名` / `购车意向`)。研发原始导出里还有一个 `购车省份` 字段，dashboard
不用，是 dead column。

**filter 默认会把 `购车省份` 从 Structured Output JSON 里剥掉**，让喂给
dashboard 的数据更干净。如果你想保留:

```bash
filter.sh raw.csv --bjt "5-21 10:00 - 12:00" --keep-all-so
```

如果以后研发又多加了别的没用字段, 用 `--drop-so-field` 追加:

```bash
filter.sh raw.csv --bjt "..." --drop-so-field 购车省份 --drop-so-field 购车备注
```

### 输出

默认放在输入文件**同目录**下，文件名规则：

```
{原 stem}_BJT-{月日}_{开始HHMM}-{结束HHMM}.csv
{原 stem}_BJT-{月日-HHMM}_to_{月日-HHMM}.csv   (跨天)
{原 stem}_{tags...}.csv                          (含 agent / campaign)
```

stdout **只输出输出路径一行**，方便 shell pipeline。所有诊断信息都在 stderr。

### 直接 chain 给 dashboard

```bash
OUT=$(bash scripts/tools/filter.sh raw.csv --bjt "5-21 10:00 - 12:00")
bash scripts/run.sh "$OUT" --host 0.0.0.0
```
