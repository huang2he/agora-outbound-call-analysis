# Dashboard 指标说明 · 数据来自哪一列

> 给同事看的速查表。每个图 / 每个数字，**就告诉你来自原始 CSV 哪一列、怎么算的**。

---

## 0. 用到的 CSV 列

| 列名 | 含义 |
|---|---|
| `Call ID` | 每通通话的唯一编号 |
| `Switch Call ID` | 通话另一个编号（导出 Excel 时带上） |
| `Agent ID` | Agent 模板唯一编号 |
| `Agent Name` | Agent 批次/版本名（用来分组） |
| `Duration (seconds)` | 通话总时长（秒） |
| `Call Start Time` | 通话开始时间（UTC 时间，会自动换算成北京时间） |
| `Transcript` | 通话全部对话（JSON 数组，含每一轮的 role+content） |
| `Structured Output` | 系统在通话中收集的客户信息（JSON 对象，6 个字段：品牌/型号/城市/时间/姓名/意向） |
| `Hangup Reason` | 通话结束方式（一个枚举词，如 USER_HANGUP / AI_HANGUP / SILENCE_DETECTED / ...） |
| `Audio Record File Download URL` | 录音文件链接（用于在 dashboard 播放/下载） |

---

## 1. 漏斗 5 个数字

| 层 | 怎么算 |
|---|---|
| **拨打总数** | CSV 总行数 |
| **真人接听** | `Hangup Reason` ∈ {USER_HANGUP, AI_HANGUP}（客户或 AI 主动挂断，说明有人接） |
| **意向客户** | `Structured Output` 里 `购车意向 == "是"` |
| **完整转换** | `Structured Output` 4 个槽位（车型 / 时间 / 城市 / 姓名）**填齐 ≥3 个**。「车型槽」= 品牌或型号任一非空 |
| **带车型完整转换** | 是完整转换 + **车型槽必须填齐**（更刚性，因为没车型的成单对销售没用） |

> 完整转换 / 意向客户 / 带车型完整转换 是 **并列分支**，一通通话可以同时满足多个。

---

## 2. 成单热力图（Tab 1 Section 2）

只看「带车型完整转换」的通话（来自漏斗最后一层）。

- **X 轴 时段**：从 `Call Start Time` 转北京时间，按 10/20/60 分钟分桶（可切换）
- **Y 轴 Agent**：`Agent Name`
- **每格三行数字**：
  - 接听数 = 该时段该 agent 的真人接听数
  - 成单数 = 该时段该 agent 的带车型完整转换数
  - 转单率 = 成单数 / 接听数
- **格子颜色深浅**：可切「按成单数」或「按转单率」着色

---

## 3. 成单真实性校验（Tab 1 Section 3, AI 模块）

把每一通**带车型完整转换**送给大模型 (gpt-5.4)，让它对照 `Transcript` 重判系统标的 `Structured Output` 是不是真有效。

### 3.1 KPI 6 张卡

| 指标 | 怎么算 |
|---|---|
| 系统记录成单 | 漏斗里的「带车型完整转换」原数 |
| ✓ 真实成单（校验后） | 原判生效 + 有误不影响（这是真有效的线索数） |
| ✓ 原判生效 | LLM 判每个非空字段都 match |
| ⚠ 有误但不影响 | LLM 判有字段 invalid，但剔除后仍满足「品牌 + 型号 + 三选二」 |
| ✗ 影响转换结果 | LLM 判剔除 invalid 字段后，不再满足完整转换标准，应剔除 |
| ◐ 灰色地带 | LLM 判属于豪车调戏 / 品牌车型不匹配 |

### 3.2 字段级标签（field_check）

LLM 对 `Structured Output` 的每个字段单独打一个标签：

| 标签 | 含义 |
|---|---|
| match | 客户在 `Transcript` 里亲口说过，或 agent 主动归类后客户没反驳 |
| invalid | 客户从没提过 + agent 凭空填 / 误听 / 推断错 / 客户明确反驳了 |
| null | `Structured Output` 这个字段本来就空 |

### 3.3 案例表每一列

| 列 | 来自哪 |
|---|---|
| 判定 | LLM verdict（原判生效 / 有误·不影响 / 影响转换 / 灰色地带） |
| 依据 | LLM 给的 ≤30 字解释 |
| Agent | `Agent Name`（短名） |
| Agent ID | `Agent ID`（完整） |
| SO 原 vs 新 | 左 = `Structured Output` 原值 / 右 = LLM 重读 transcript 后**新提取**的值 |
| 👁 查看 | 点开看完整 `Transcript`（弹窗） |
| 录音 | `Audio Record File Download URL`（点 ▶ 播放，可下载） |

### 3.4 5 个 filter

```
[全部] [真实成单 (校验后)] [原判准确] [有误不影响] [影响转换] [灰色地带]
```

右边「⬇ Excel」「⬇ 录音 zip」可一键下载当前 filter 下的全部数据。

---

## 4. 早期挂断（Tab 1 Section 4）

只看真人接听里**只说了几句 agent 就被挂**的情况。

| 类别 | 怎么算 |
|---|---|
| 首句挂断（1 句） | 真人接听 + `Transcript` 里 agent 只说了 1 句 |
| └首句挂断（<10 秒） | 上一行的子集 + `Duration (seconds)` < 10 |
| 2 / 3 / 4 / 5 句挂断 | agent 说了恰好 N 句的 |

右边「首句挂断 Duration 分布」 = 上面"首句挂断（1 句）"那批通话的 `Duration (seconds)` 分布图。默认只看 <10 秒的（短挂断），可切「全部」。

---

## 5. 轮次分布（Tab 1 Section 5）

3 个子集（真人接听 / 意向客户 / 完整转换）各画一张柱状图，**X 轴 = `Transcript` 里 max turn_id**（agent + user 共享编号）。看每个子集里通话长度的分布。

---

## 6. Duration 分布（Tab 1 Section 6）

只看真人接听。X 轴 = `Duration (seconds)`（一秒一柱），Y 轴 = 该秒数通话数。点单根柱可导出。

---

## 7. 完整转换槽位分布（Tab 1 Section 7）

只看真人接听。柱状图 X 轴 = 客户填齐的槽位数（0~4），Y 轴 = 通话数。

槽位数 = `Structured Output` 中 (品牌或型号任一) + (时间) + (城市) + (姓名) 四项里非空的数量。

---

## 8. Tab 2 · Agent 视角

只看**有效会话**：真人接听 **且** `Transcript` 里至少 1 句"真实"用户发言（剔除系统静默兜底 / IVR 语音信箱）。

### 8.1 Section 1 数据范围分流（桑基图）

```
真人接听 ─┬─ 客户未开口 ─┬─ 首句挂断
          │              └─ 接通无应答
          └─ 有效会话 ───┬─ 0 关
                          ├─ 1 关
                          ├─ 2 关
                          ├─ 3 关
                          └─ 4 关
```

"关数" = `Structured Output` 4 个槽位严格线性通过几关（必须先过 1 才算过 2）。槽位顺序：车型 → 城市 → 时间 → 姓名。

### 8.2 Section 2 通关分桶 5 桶

| 桶 | 怎么算 |
|---|---|
| 卡在第 1 关 | 0 关全过（车型都没拿到） |
| 卡在第 2 关 | 车型拿到但城市没拿 |
| 卡在第 3 关 | 车型+城市拿到，时间没拿 |
| 卡在第 4 关 | 前 3 关都拿到，姓名没拿 |
| 全过 | 4 关全过 |

### 8.3 Section 3 Agent 效率画像

| KPI | 怎么算 |
|---|---|
| 有效会话 | 真人接听 + 至少 1 句真实 user 发言 |
| 高机会样本 | 有效会话里**真实 user 发言 ≥3 句**的（说明客户配合度高） |
| 机会浪费率 | 高机会样本里**卡 0 关**的占比（客户给机会但 agent 没拿到任何信息） |
| 采集效率 | 4 关全过的通话里 4 / 平均轮次 = 平均一轮拿几个槽位 |

旁边两图：
- **机会浪费分布**：高机会样本按通过几关分布
- **首句开局 × 通关分布**：按客户开局态度（积极/中性/拒绝/不友善，关键词分类）× pass_n 堆叠

### 8.4 Section 4 LLM 失败画像

对**没全过 4 关的有效会话**调 gpt-5.4 分析。

| KPI | 怎么算 |
|---|---|
| 首轮翻车率 (A1) | LLM 判 agent 在第 1 句就翻车的通话占比 |
| 前 2 轮翻车率 (A1+A2) | 同上扩到前 2 句 |
| agent 责任失败占比 | 总数减去 LLM 判「客户主动拒绝」的部分 |
| 机会客户被聊死 | 客户开局中性/积极 → 最终卡 0 关 的占比 |

底部图：
- agent 在第几轮出问题（柱）
- 客户在第几轮识破/反感（柱）
- 失败类别（饼图）：开场太突兀 / 话术机械重复 / 没接客户上文 / 误判客户意图 / 提问跳跃 / 信息收集不彻底 / 客户主动拒绝 / 其他

### 8.5 Section 5 客户态度反转

LLM 判定的客户**开局态度 vs 结尾态度**矩阵（积极 / 中性 / 消极 3×3 热力图）。

- 🔴 聊跑：积极/中性开局 → 消极结尾（agent 把好客户聊死）
- 🟢 挽留：消极开局 → 中性/积极结尾（agent 救回来）
- ⚪ 不变：态度稳定

右侧列出 5 个聊跑 + 5 个挽留典型案例。

### 8.6 Section 6 LLM 失败案例列表

每行 = 一通失败通话，列：Call ID / Agent / pass_n / fail_turn / fail_category / fail_reason / user_detect_turn / user_detect_signal / sentiment_start / sentiment_end。点展开看完整 `Transcript`，红色高亮 LLM 判定的「翻车句」。

可按 桶 / 失败类别 筛选。

---

## 9. 导出 Excel 的列

任何图表点击导出 Excel 都会包含：

| 列 | 来源 |
|---|---|
| Call ID | `Call ID` |
| Switch Call ID | `Switch Call ID` |
| Agent ID / Agent Name | 同名列 |
| Duration (s) | `Duration (seconds)` |
| Hangup Reason | `Hangup Reason` |
| Max turn_id | `Transcript` 里最大的 turn_id |
| Assistant turns | `Transcript` 里 agent 说了几句 |
| Is Human Answered | `Hangup Reason` ∈ {USER_HANGUP, AI_HANGUP} |
| Is Full Conversion | 是否完整转换 |
| Is Intent | 是否意向客户 |
| Structured Output | 原始 JSON 字符串 |
| Transcript | 渲染过的"role: content"多行字符串 |
| Audio URL | `Audio Record File Download URL` |

校验表导出额外带：AI 校验判定 / 依据 / new_so / field_check 4 列。

文件名格式：`{类目}-n{数量}-{YYYY-MM-DD}.xlsx`

---

## 10. 离线 HTML 导出

页面右上角「⬇ 导出离线 HTML」按钮。下载一个单文件 HTML（含全部数据 + 全部 LLM 校验结果嵌入），同事拿到双击就能打开看，不需要装任何东西。

- 录音可直接播放 / 单独下载（OSS 链接 7 天有效）
- 批量打包 zip 也能用（浏览器内 JSZip 拉录音）

---

## 关键规则速记

- **真人接听**：`Hangup Reason` 是 USER_HANGUP 或 AI_HANGUP
- **意向客户**：`Structured Output.购车意向 == "是"`
- **完整转换**：4 个槽位填齐 ≥3 个
- **带车型完整转换**：完整转换 + 车型槽（品牌或型号）必填
- **有效会话**（Tab 2）：真人接听 + 至少 1 句真实 user 发言（不算系统静默 / IVR）
- **关数**：4 槽位严格线性通过（车型 → 城市 → 时间 → 姓名）
- **首句挂断**：真人接听 + `Transcript` 里 agent 只说 1 句
- **AI 校验真实成单**：原判生效 + 有误不影响（剔除"应剔除"和"灰色地带"）
