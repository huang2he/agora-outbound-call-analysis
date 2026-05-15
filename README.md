# Agora Outbound Call Analysis

Claude Code skill 加 Python 后端，分析 [Agora ConvoAI](https://www.agora.io/) 外呼批次导出的 CSV/XLSX，生成单页 ECharts 仪表盘：5 层漏斗、轮次分布、Duration 分布、早期挂断、Hangup Reason 分布。每个图表都能点击导出对应通话的 Excel + 录音 zip（CORS 走本地代理）。

![dashboard preview](https://img.shields.io/badge/dashboard-ECharts-blue) ![python](https://img.shields.io/badge/python-3.10%2B-green) ![status](https://img.shields.io/badge/status-tested-success)

## 安装（一行）

```bash
curl -fsSL https://raw.githubusercontent.com/huang2he/agora-outbound-call-analysis/main/install.sh | bash
```

这条命令会：clone 到 `~/.claude/skills/agora-outbound-call-analysis`、建 `.venv`、pip install pandas + openpyxl。已装过的话再跑一次就是 `git pull` 更新。

装好后 Claude Code 自动发现这个 skill（通过 `SKILL.md` 元数据），用中文说"分析这批外呼"或者甩 CSV 给 Claude 就会触发。

### 手动安装（不想跑别人的 bash 脚本）

```bash
git clone https://github.com/huang2he/agora-outbound-call-analysis \
  ~/.claude/skills/agora-outbound-call-analysis
bash ~/.claude/skills/agora-outbound-call-analysis/scripts/run.sh path/to/any.csv
# 第一次运行 run.sh 会自己建 venv
```

## 直接用（不通过 Claude）

```bash
# 本机访问（默认 loopback 仅自己可见）
bash ~/.claude/skills/agora-outbound-call-analysis/scripts/run.sh path/to/summary.csv

# 局域网访问（同事在同一 Wi-Fi 可看）— 启动会列出可用 LAN IP
bash ~/.claude/skills/agora-outbound-call-analysis/scripts/run.sh path/to/summary.csv --host 0.0.0.0

# 只要离线 HTML（不需要下载录音 / LAN）
bash ~/.claude/skills/agora-outbound-call-analysis/scripts/run.sh --build path/to/summary.csv -o dashboard.html
```

浏览器会自动打开 `http://127.0.0.1:<port>/`。终端 Ctrl+C 停服。

**LAN 模式注意：** 服务没做 auth，谁能 reach 这个 IP 谁就能看完整 dashboard + transcript + 音频。**别贴到外网**。macOS 首次会弹防火墙提示，点"允许"。

## 输入格式

Agora ConvoAI Console 默认导出的 CSV/XLSX，需要包含这些列：

- `Agent ID` / `Agent Name`
- `Duration (seconds)`
- `Transcript`（JSON array）
- `Structured Output`（JSON object）
- `Hangup Reason`
- `Audio Record File Download URL`

## 指标口径

| 指标 | 定义 |
|---|---|
| 接听 | `Duration > 0` |
| 真人接听 | `Hangup Reason ∈ {USER_HANGUP, AI_HANGUP}` |
| 完整转换 | Structured Output 所有字段非 null/空 |
| 意向客户 | Structured Output 中 `购车意向 == "是"` |
| N 句挂断 | 真人接听里 assistant 轮数恰好等于 N（互斥分桶） |

完整数据字典和每个指标的精确定义看 [METRICS.md](./METRICS.md)。Skill 触发说明看 [SKILL.md](./SKILL.md)。

## 性能

实测（M-series Mac）：

- 90 行：< 0.1s 构建，165KB HTML
- 10,080 行：0.87s 构建，13MB HTML
- 音频导出：16 worker 并行流式打 zip，37 通真实 OSS 录音 **6.7 秒**（55MB zip）
- 服务端常驻内存 ~155MB（无视 zip 大小，流式打包）

单次音频导出上限 3000 通（防止超大批意外撞死服务）。

## 环境要求

- macOS / Linux
- `python3` ≥ 3.10
- 联网（pip + CDN + 服务端 OSS 拉取）

## License

MIT
