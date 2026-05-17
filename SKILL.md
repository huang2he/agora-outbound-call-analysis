---
name: agora-outbound-call-analysis
description: Analyze an Agora ConvoAI outbound-call batch (CSV or XLSX exported from the Console with columns Agent ID / Agent Name / Duration / Transcript / Structured Output / Hangup Reason / Audio Record File Download URL) and produce a polished single-file ECharts HTML dashboard with hero KPI cards, a connect→answer→full-conversion→intent funnel (overall and per Agent Name), turn-count distribution among human-answered calls, Duration distribution, 1/2/3-sentence early-hangup rates, and Hangup Reason pie. Use this skill whenever the user shares a `summary-*.csv` / call-history file from Agora / ConvoAI / 声网 outbound calls (外呼) and asks to analyze results, build a dashboard, look at funnel / conversion / 意向客户 / 真人接听 / 挂断 statistics, or evaluate a 外呼批次, even if they don't explicitly say "dashboard".
---

# Agora 外呼通话分析 → HTML Dashboard

## 触发场景

当用户做完一批 Agora ConvoAI 外呼电话，从 Console 导出 CSV/XLSX，想快速看一批通话的漏斗、转换率、挂断模式时使用。典型触发短语：

- "分析这批外呼" / "看下这批通话结果"
- "做个 dashboard / 看板 / 漏斗"
- "声网外呼数据 / ConvoAI 通话历史"
- 用户上传文件名是 `summary-*.csv` / 含 ConvoAI 相关 column 的表格

## 输入

一个 CSV 或 XLSX 文件，必须包含这些列（Agora ConvoAI Console 默认导出格式）：

| 列名 | 用途 |
|---|---|
| `Agent ID` | agent 唯一标识 |
| `Agent Name` | 批次/版本标识，按它分组 |
| `Duration (seconds)` | 通话时长，用于「接听」判断和分布图 |
| `Transcript` | JSON array，每条 turn 含 `turn_id` / `role` (`assistant`/`user`) / `content` |
| `Structured Output` | JSON object，预提取的收集字段（含 null 字段表示没收集到） |
| `Hangup Reason` | 通话结果枚举：NO_ANSWER / BUSY / USER_HANGUP / AI_HANGUP / VOICE_MAIL / TEMPORARY_FAILURE / AI_ASSISTANT_HANGUP / SILENCE_HANGUP / CALL_TIMEOUT 等 |
| `Audio Record File Download URL` | 录音 URL（v1 不分析录音，仅展示） |

## 怎么跑

**推荐用 `run.sh`，自动建 venv + 装依赖 + 起服务**：

```bash
bash ~/.claude/skills/agora-outbound-call-analysis/scripts/run.sh <input.csv-or-xlsx>
```

第一次跑会创建 `.venv` 并 pip 安装 pandas + openpyxl（~15 秒，仅一次）；以后秒起。

启动后自动打开浏览器到 `http://127.0.0.1:<port>/`，**端口策略**：先试 `--port` 指定的（默认 8765），冲突时直接让 OS 分配一个空闲端口（绑在实际 HTTPServer 上，无 TOCTOU 漏洞）。终端 Ctrl+C 停服。

### 开局域网访问（让同事在公司 Wi-Fi 上看）

加 `--host 0.0.0.0`：

```bash
bash ~/.claude/skills/agora-outbound-call-analysis/scripts/run.sh <input.csv> --host 0.0.0.0
```

启动时会列出所有非 loopback 的 IPv4，比如：

```
LAN access:  (挑一个发给同事，VPN / 公司 Wi-Fi 用不同 IP)
  http://10.103.1.131:8765/    ← 公司 Wi-Fi 走这个
  http://28.0.0.1:8765/        ← VPN 接口（同事得连同一 VPN）
```

把对应的 IP+port 发给同事即可。**注意：**

- macOS 首次会弹防火墙提示，点"允许"
- 服务没做 auth，谁 reach 这个 IP 谁就能看完整 dashboard + transcript + 音频。**别贴到外网**
- 关掉终端 / Ctrl+C 服务就停了

只要静态 HTML（不需要下载录音、可分发给同事）：

```bash
bash ~/.claude/skills/agora-outbound-call-analysis/scripts/run.sh --build <input.csv-or-xlsx> [-o output.html]
```

两条命令都会把漏斗 5 个关键数打印到 stdout。如果 venv 已存在也可以直接调 `.venv/bin/python scripts/serve_dashboard.py …` / `…/build_dashboard.py …`，不强制走 run.sh。

### 环境要求

- macOS / Linux 上的 `python3`（任意版本 ≥ 3.10 都行；脚本会按 `python3.12 / python3.11 / python3 / python` 顺序找）
- 联网（pip 安装 + ECharts/XLSX CDN + 服务端拉 OSS 录音）
- Windows 没测试过，理论上 `python -m scripts.serve_dashboard …` 也能跑（run.sh 只能在 Git Bash / WSL 里用）

### 没装依赖怎么办

第一次用之前如果 `.venv` 还不在：

```bash
cd ~/.claude/skills/agora-outbound-call-analysis && \
  python3 -m venv .venv && \
  .venv/bin/pip install -q pandas openpyxl
```

这步只做一次。

## 指标口径（已和用户对齐，别擅自改）

为什么要锁这套口径：这些定义是和用户一条条对过的，他在心里记住了"这一批就该是 N 个完整转换 / M 个意向"。如果换口径，dashboard 的数字就和他的预期对不上，他会怀疑数据出错。

| 指标 | 定义 | 备注 |
|---|---|---|
| 拨打总数 | 表里所有行 | 漏斗第 1 层 |
| 接听 | `Duration (seconds) > 0` | 漏斗第 2 层。不按 Hangup Reason 判，因为 VOICE_MAIL / IVR 这种也算接通了 |
| 真人接听 | `Hangup Reason ∈ {USER_HANGUP, AI_HANGUP}` | 漏斗第 3 层。其它（SILENCE_HANGUP / AI_ASSISTANT_HANGUP / TIMEOUT / VOICE_MAIL）即使 Duration>0 也不算真人 |
| 完整转换 | Structured Output 非空 **且所有字段都不是 null/空串** | 漏斗第 4 层，和「意向客户」**并列**不是嵌套 |
| 意向客户 | Structured Output 中 `购车意向 == "是"` | 漏斗第 5 层，和「完整转换」**并列**。一通电话可以两个都中，也可能只中一个 |
| 轮次 | `max(turn_id)` in transcript | 仅对真人接听算分布。等价于 assistant 轮数 |
| N 句挂断 | 真人接听里 assistant 轮数 **恰好等于 N** | 互斥分桶：1 句 / 2 句 / 3 句 是三个独立类别，不累积。用户明确要求"2 句不包含 1，3 句不包含 2 和 1" |

`Structured Output` 解析说明：列里是 JSON 字符串。空字符串/解析失败 → 当作"没结构化数据"，不算完整转换也不算意向。`null` 在 JSON 里解析成 Python `None`，靠这个判完整性。

## Dashboard 包含什么（脚本会自动生成）

白底 ECharts 单页面，**顶部下拉框**切换「全部 / 单个 Agent Name」，所有图表跟着重渲染：

1. **Hero KPI 卡片** — 5 个并排数字（拨打 / 接听 / 真人 / 完整转换 / 意向）+ 占总比例
2. **漏斗** — 5 层 funnel，**点击层级直接导出该批通话**
3. **轮次分布** — 三组 bar：真人接听全部 / 完整转换 / 意向客户。X 轴 `max turn_id`（含 agent+真人两方轮次）。**点柱导出**
4. **Duration 分布** — 全宽卡片，**每秒一柱**，底部 dataZoom 滑块 + 滚轮缩放。**点柱导出**该秒数的真人接听通话
5. **早期挂断 + Hangup 饼图** — grid-2。早期挂断分桶**仅按 agent 说话轮数**（恰好 1/2/3 句），**点行导出**
6. **Hangup Reason 全分布表** — 全宽

**点击导出**：每个图表的可导出位置点一下会弹三选对话框：

| 选项 | 单组（漏斗/Duration/早期挂断） | 三组（轮次分布点柱） |
|---|---|---|
| 只要 Excel | 一个 xlsx | 一个 zip 包含 3 个 xlsx（按 真人接听/完整转换/意向 分类） |
| 只要录音 | 一个 zip 含所有录音 | 一个 zip 含 3 个子目录（每类的录音独立放） |
| Excel + 录音 | 一个 zip 含 xlsx + 录音 | 一个 zip 含 3 子目录，各自有 xlsx + 录音 |

xlsx 列：Call ID / Agent ID / Agent Name / Duration / Hangup Reason / Max turn_id / Assistant turns / Is Human / Is Full / Is Intent / Transcript（已渲染成 `role: content` 多行）/ Audio URL。

**录音下载架构**：浏览器直接 fetch OSS URL 会被 CORS 拦死，所以走 `serve_dashboard.py` 起的 localhost 代理：

- 浏览器构造一个**隐藏 iframe + form POST**，把 URL 清单 form-encode 后发到 `/audio-zip`。这样下载流由浏览器原生 download manager 接管，**JS 堆里不会攒整个 zip**（多 GB 也安全）
- 服务端 16 worker 并行用 Python `urllib` 拉文件（不受 CORS 约束），用 `as_completed` 来一个写一个进 zip
- zip 通过 `_NonSeekableWriter` 直接流式写到 HTTP response body（`zipfile` 检测 stream 不可 seek 时自动用 data descriptor 模式），**服务端常驻内存 ~16MB 不论批量多大**

单次请求硬上限 **3000 通**（防 OOM / 防误点全量）。超过会返 HTTP 413 + 文字说明，浏览器侧从 iframe 内容里读到错误并 toast 提示。前端在 500 通时还会先 confirm 用户。

`file://` 模式下 dashboard 仍能开，但音频导出按钮会变灰，弹窗提示需用 `serve_dashboard.py` 启动。Excel 导出始终可用。

抓不到的录音（URL 失效 / 网络问题）会在 zip 根目录留一份 `failed_downloads.txt` 记录 Path + URL + Error。

**用户偏好笔记**：之前选过深色，后又改回白底——以后用户没特别说就直接出白底。如果要换主题改 CSS 变量 `--bg / --panel / --text / --border` 即可，chart 代码不动。

漏斗是并列分支（完整转换 + 意向客户），不是严格上下层级——这点在 dashboard 的「口径定义」横条里也明示了，避免用户误读 Funnel chart 的内置百分比。

## Tab 2 · Agent 视角 KDA 面板 (2026-05-18 新增)

Dashboard 顶部加了「产品总览 / Agent 视角 (KDA)」两个 tab。Tab 2 把 agent 当销售员管，按 Agent Name 评估 4 关闯关能力，6 维度雷达图：

| 关 | 通关条件（Structured Output 字段） |
|---|---|
| 第 1 关 车型 | `购车品牌` **AND** `购车型号` 都非空（**与 Tab 1 的"完整转换"用 OR 不同**）|
| 第 2 关 城市 | `购车城市` 非空 |
| 第 3 关 时间 | `购车时间` 非空 |
| 第 4 关 姓氏 | `购车姓名` 非空 |

**严格线性递进**：过第 N 关 ⇔ 第 1..N 全过。

6 维度（每个归一化 0-100）：

| 维度 | 简称 | 计算口径 |
|---|---|---|
| 全关通过率 | 击穿率 | `n_full / n_human` × 100 |
| 路径长度 | 轮效 | 全过通话的平均 `max(turn_id)`，5 轮=100 分，15 轮=0 |
| 早期击中 | 首杀 | T1 (车型关首次命中 turn) × 15 + T2 (城市关) × 8。v0 用关键词正则，v1 上 LLM |
| 闯关费劲 | 滑顺 | assistant 重复问同一关 → friction +1，每关一次封顶。`100 − Σ friction × 20` |
| 关卡均衡 | 不偏科 | 4 关各自通过率的方差 × 1000，反向 |
| 早挂断免疫 | 抗挂 | `(1 - 早挂断率) × 100`，早挂断 = assistant 轮数 ≤ 3 |

综合分加权：击穿率 0.3 + 轮效 0.2 + 首杀 0.15 + 滑顺 0.15 + 不偏科 0.1 + 抗挂 0.1。

**口径独立性**：Tab 2 的"4 关"和 Tab 1 的"完整转换/带车型完整转换"**故意不同**——Tab 2 是给销售员（agent）打分用的更严口径（车型 AND），Tab 1 是面向 4S 店转化率的并列分支（车型 OR）。两者数字预期会有差，**不要互相对齐**。

详细数据来源 → [METRICS.md](./METRICS.md)。设计依据 → 见 [Agent-视角指标设计](https://github.com/huang2he/agora-outbound-call-analysis/blob/main/docs/AGENT_KDA_DESIGN.md)（如已拷贝）。

## 多个文件并行分析

每次跑 `run.sh <file>` 都是**独立 Python 进程，独立端口，独立 DataFrame**。所以一次会话里用户连续让你分析 A.xlsx 然后 B.xlsx 时：

- 默认行为：**并行起两个面板**，A 在 `:8765`，B 在 OS 分配的空闲端口（比如 `:56123`），两个浏览器 tab 互不影响，可以对比看
- 不要预设要"覆盖"或"清掉前一个"

**只有当用户明确说**「换成 B」/「先关掉 A」/「停掉前面的」时才主动 `pkill -f serve_dashboard.py` 或针对 PID `kill` 掉前一个进程。

查看 / 清理命令（如果用户问起）：

```bash
ps aux | grep serve_dashboard | grep -v grep   # 列出活着的面板
pkill -f serve_dashboard.py                    # 一键关全部
```

## 输出给用户怎么说

跑完脚本后，把脚本 stdout 的 5 个漏斗数字直接 read 给用户（包括按 Agent Name 拆的），加一句「dashboard 写到 `<path>`，已经在浏览器打开」即可。具体图表交互让用户自己看，不要在对话里重复描述每张图。

如果有异常（某列缺失、JSON 解析失败超过 50% 等），脚本会直接报错；遇到这种情况先用 `head -1 <file>` 看下实际列名，可能用户拿到的是早期版本的 export schema。

## 规模 / 性能

实测（M-series Mac）：

| 批次大小 | Python 构建 | HTML 大小 | 浏览器加载 | 备注 |
|---|---|---|---|---|
| 90 行 | < 0.1 s | 165 KB | 即时 | 测试样本 |
| 10,080 行 | 0.87 s | 13 MB | 1-3 s | transcript 全部内嵌 |

行数能撑到 50k 以上不卡。**列**多少行不影响——脚本只按列名取需要的几列。

**音频导出实测（16 worker 并行 + 流式 zip）**：

- 37 通真实 OSS 录音：**6.7 秒**，55MB zip
- 1500 通假 URL（全失败）：~5 分钟（DNS/连接超时累积）
- 内存：服务端 RSS 稳定 ~155MB（zip 流式写出，不缓存）

单次请求**硬上限 3000 通**，超量返 413。前端 500 通时先 `confirm()` 提醒。典型用户工作流是 "点漏斗某层 / 某秒柱 → 几十到几百通"，远低于上限。

## 已知限制 / v1 不做

- 不调 LLM 看 transcript 内容做语义分析（如真意向 vs 软意向、TTS 错音）
- 不做跨批对比（一次只吃一个文件）
- 假定 Agent Name 是"批次标识"，按它分组；如果用户的导出里 Agent Name 没差异，按 Agent Name 拆的图就只有一组
- 服务端音频 zip 已是流式（zipfile + `_NonSeekableWriter`），但 form-encoded payload 还是一次性读到 server 内存里。10k+ URL 清单的 payload 也就 ~5MB JSON，影响不大
