# 指标定义 · 数据字典

> Dashboard 上每个数字怎么算出来的。**改口径前先读这里**——所有 funnel / chart / 导出都依赖以下字段和派生标志。

代码位置：[`scripts/build_dashboard.py`](scripts/build_dashboard.py)（`enrich` / `is_*` / `funnel_counts` / `slice_data` 等函数）

---

## 1. 输入数据：CSV / XLSX 原始列

Agora ConvoAI Console 默认导出格式。脚本只读以下列，其他列被忽略：

| 列名 | 类型 | 用途 |
|---|---|---|
| `Call ID` | string | 通话唯一标识，导出时作为主键 |
| `Agent ID` | string | Agent 唯一标识 |
| `Agent Name` | string | 批次/版本标识。Dashboard 顶部下拉框就是按它分组。空值会被填为 `"(unknown)"` |
| `Duration (seconds)` | number | 通话时长。空值解析为 0。**用作 Duration 分布的横轴 & 派生 `_answered`** |
| `Transcript` | JSON array | 每条 turn 形如 `{"turn_id": int, "role": "assistant"\|"user", "content": "..."}`。空数组 `[]` / 解析失败 → 当作空 list |
| `Structured Output` | JSON object | agent 收集到的结构化字段。**所有 conversion 类指标都从它派生**。详见 §4 |
| `Hangup Reason` | enum string | 挂断原因。值见 §3.2 |
| `Audio Record File Download URL` | string | 录音 URL（OSS 签名链接）。仅导出录音时使用，不进任何指标 |

---

## 2. 派生字段（每行加一组下划线开头的列）

`enrich(df)` 在加载 CSV 后立刻给每行算这些字段。后续所有指标都直接读它们，不再回看原始 JSON。

| 派生字段 | 来源 | 计算方式 |
|---|---|---|
| `_transcript` | `Transcript` | `json.loads()` 后保证是 list；解析失败 → `[]` |
| `_structured` | `Structured Output` | `json.loads()` 后保证是 dict；解析失败/空 → `None` |
| `_assistant_turns` | `_transcript` | `len([t for t in _transcript if t["role"] == "assistant"])` |
| `_max_turn_id` | `_transcript` | `max(t["turn_id"] for t in _transcript)`，无值时 0 |
| `_answered` | `Duration (seconds)` | `Duration > 0` |
| `_human` | `Hangup Reason` | `Hangup Reason ∈ {USER_HANGUP, AI_HANGUP}` |
| `_filled_slots` | `_structured` | 见 §4，4 个槽位中哪些非空 |
| `_field_count` | `_filled_slots` | `len(_filled_slots)`，0..4 |
| `_full` | `_field_count` | `_field_count >= 3` |
| `_full_with_model` | `_full` + `_filled_slots` | `_full AND "车型" in _filled_slots` |
| `_intent` | `_structured` | `_structured["购车意向"] == "是"` |

---

## 3. Funnel 5 层定义

```
拨打总数 → 真人接听 → 完整转换 → 带车型完整转换 → 意向客户
```

注意：**完整转换 / 带车型完整转换 / 意向客户 是并列分支**，不是严格嵌套（一通电话可以同时是意向客户 + 完整转换；意向客户 ⊄ 完整转换）。Funnel 视觉上把它们按降序排，但语义上它们都从"真人接听"里独立切出。

### 3.1 各层 SQL-like 表达

| 层 | 表达 | 注 |
|---|---|---|
| 拨打总数 | `len(df)` | 一行一通 |
| 真人接听 | `df[_human]` | `Hangup Reason in {USER_HANGUP, AI_HANGUP}` |
| 完整转换 | `df[_full]` | `_field_count >= 3` （详见 §4） |
| 带车型完整转换 | `df[_full_with_model]` | `_full AND 车型槽已填` |
| 意向客户 | `df[_intent]` | `Structured Output 的 "购车意向" == "是"` |

### 3.2 「接听」为什么没有

早期版本有 `接听 = Duration > 0` 层，但实测各批 接听 ≈ 拨打总数（>99%），信息量为 0，2026-05-15 移除。`_answered` 派生字段仍保留，给 Duration 分布图的 X 轴上限用。

### 3.3 `Hangup Reason` 枚举

| 值 | 算真人接听？ | 说明 |
|---|---|---|
| `USER_HANGUP` | ✓ | 用户手动挂断（最强真人信号） |
| `AI_HANGUP` | ✓ | AI 主动挂断（一般是任务完成或对话自然结束） |
| `AI_ASSISTANT_HANGUP` | ✗ | 客户端在 agent 还在说时挂掉。常见于客户切话 |
| `SILENCE_HANGUP` | ✗ | 静默超时 |
| `CALL_TIMEOUT` | ✗ | 系统超时 |
| `VOICE_MAIL` | ✗ | 转语音信箱 |
| `NO_ANSWER` | ✗ | 没接 |
| `BUSY` | ✗ | 占线 |
| `TEMPORARY_FAILURE` | ✗ | 链路故障 |

---

## 4. Structured Output 与"槽位"

### 4.1 当前 Schema（汽车外呼）

```json
{
  "购车品牌": "...",
  "购车型号": "...",
  "购车城市": "...",
  "购车姓名": "...",
  "购车意向": "...",
  "购车时间": "..."
}
```

每个字段可能是字符串值，也可能是 JSON 的 `null`，或者空串 `""`。

### 4.2 4 个 Conversion 槽位

完整转换的判定基于 4 个**槽位（slot）**，不是 6 个字段：

| 槽位名 | 满足条件 |
|---|---|
| **车型** | `购车品牌` **或** `购车型号` 任一非 null/非空 |
| **时间** | `购车时间` 非 null/非空 |
| **城市** | `购车城市` 非 null/非空 |
| **姓名** | `购车姓名` 非 null/非空 |

注：
- **`购车意向` 不是槽位**——它是独立的"客户意向"标签，对应 funnel 的「意向客户」层。
- **车型槽特殊**：`购车品牌` 和 `购车型号` 任一即可。语义上"奔驰"和"奔驰E300"对 4S 店都是足够的线索（4S 店回访时能问到型号）。

代码常量：

```python
CONVERSION_SLOT_NAMES = ["车型", "时间", "城市", "姓名"]
CONVERSION_SLOT_FIELDS = {
    "车型": ["购车品牌", "购车型号"],   # 任一非 null 即填齐
    "时间": ["购车时间"],
    "城市": ["购车城市"],
    "姓名": ["购车姓名"],
}
FULL_CONVERSION_MIN = 3   # ≥ 3 of 4 slots = 完整转换
```

### 4.3 例子

| Structured Output | 填齐槽位 | _field_count | _full | _full_with_model | _intent |
|---|---|---|---|---|---|
| `{品牌: 奔驰, 型号: null, 城市: 广州, 姓名: null, 意向: 是, 时间: 1月}` | 车型 / 城市 / 时间 | 3 | ✓ | ✓ | ✓ |
| `{品牌: null, 型号: E300, 城市: null, 姓名: 张, 意向: 是, 时间: null}` | 车型 / 姓名 | 2 | ✗ | ✗ | ✓ |
| `{品牌: 路虎, 型号: 极光, 城市: 上海, 姓名: 李, 意向: 是, 时间: 半年}` | 4/4 | 4 | ✓ | ✓ | ✓ |
| `{品牌: null, 型号: null, 城市: 北京, 姓名: 王, 意向: 否, 时间: 1月}` | 时间 / 城市 / 姓名 | 3 | ✓ | **✗（缺车型）** | ✗（意向=否） |

### 4.4 `_intent` 为什么是 `== "是"` 而不是"非 null"

实测：`购车意向` 字段在大部分通话里填 `null`（agent 没问到），少数填 `"是"`（约 4%），还有相当部分填 `"否"`（客户拒绝，约 20%）。

如果用"非 null"判定，会把 `"否"` 算成意向客户，逻辑反了。所以坚持 `== "是"`。LLM 真伪分析（§7）会进一步把 `"是"` 拆成真意向 / 假意向 / 模糊。

### 4.5 加新业务场景的 schema

如果以后跑保险/教育/招聘场景，schema 会变。只需修改 `CONVERSION_SLOT_FIELDS` 把字段映射换掉，`is_intent` 改为对应的意向标记字段（如 `投保意向`），其他指标计算逻辑不动。

---

## 5. Hero KPI 卡的百分比口径

每张卡显示 1-3 个百分比分母，**老板锁定**：

| 卡 | 分母配置 |
|---|---|
| 拨打总数 | (无百分比) |
| 真人接听 | `占总` |
| 完整转换 | `占总` · `占真人` |
| 带车型完整转换 | `占总` · `占真人` · `占完整` |
| 意向客户 | `占总` · `占真人` |

JS 配置：

```js
const SHOW = [
  [],                              // 拨打总数
  ['total'],                       // 真人接听
  ['total', 'human'],              // 完整转换
  ['total', 'human', 'full'],      // 带车型完整转换
  ['total', 'human'],              // 意向客户
];
```

---

## 6. 各图表对应的数据源

### 6.1 漏斗图（section 1）
- 数据：`funnel_counts(df)` 返回 5 个数
- 点任一层 → 导出该子集（用 `_human / _full / _full_with_model / _intent` 过滤）

### 6.2 轮次分布（section 2，3 张子图）

| 子图 | 数据集 | X 轴 | Y 轴 |
|---|---|---|---|
| 真人接听 (全部) | `df[_human]` | `_max_turn_id` 1..N | 通话数 |
| 完整转换 | `df[_human & _full]` | 同上 | 同上 |
| 意向客户 | `df[_human & _intent]` | 同上 | 同上 |

- X 轴范围全局对齐（用全部 `_human` 行的最大 `_max_turn_id`），方便跨 Agent 比较
- 每个柱子上方标百分比（相对该子集总数）
- 右侧环形图：同数据可视化为占比
- 点柱可导出对应子集

### 6.3 Duration 分布（section 3）
- 数据：`df[_human]` 的 `Duration (seconds)`
- X 轴：每秒一柱
- 底部 dataZoom 滑块 + 滚轮可缩放
- 点柱导出该秒数的真人接听通话

### 6.4 完整转换槽位分布（section 4）

- 柱图：横轴 0..4 是 `_field_count` 取值，纵轴是真人接听内该字段数的通话数
- 环形图：同数据，中心显示真人接听总数
- 下钻面板：
  - **4/4 全齐**：`df[_human & _full & _field_count == 4]` 通数
  - **仅 3/4**：`df[_human & _full & _field_count == 3]` 通数
  - **仅 3/4 里缺哪个槽位**：对每通缺的那个槽位计数

### 6.5 早期挂断（section 5）

| 桶 | 条件 |
|---|---|
| 首句挂断 | `_human & _assistant_turns == 1` |
| 2 句挂断 | `_human & _assistant_turns == 2` |
| 3 句挂断 | `_human & _assistant_turns == 3` |
| 4 句挂断 | `_human & _assistant_turns == 4` |
| 5 句挂断 | `_human & _assistant_turns == 5` |

**互斥分桶**——2 句挂断不包含 1 句挂断。

### 6.6 首句挂断 Duration 分布（section 5 右）

- 基础数据：`df[_human & _assistant_turns == 1]` 的 `Duration (seconds)`
- Toggle 切换：
  - **全部**：所有首句挂断
  - **短挂断 (< 15 秒)**：进一步 filter `_duration < 15`
- 点柱可导出该秒数 + 当前 toggle 视角的子集

---

## 7. LLM 意向真伪分析

### 7.1 输入选样
- 数据集：`df[_intent]`（购车意向 == "是" 的全部通话）
- 每通送 LLM：
  - `transcript`：渲染成 `role: content\n...` 的纯文本（不送 turn_id / metadata）
  - `structured`：完整的 Structured Output dict
  - 超 3000 字符截断（开头）

### 7.2 模型与 prompt
- 默认模型：`qwen3.6-plus`（DashScope OpenAI-compatible 接口）
- API key 来源（先后顺序）：
  - 环境变量 `DASHSCOPE_API_KEY`
  - 文件 `~/.config/agora-outbound-call-analysis/env` 里 `DASHSCOPE_API_KEY=...`
- System prompt（精简）：
  ```
  你是外呼通话质检员。判断"购车意向=是"的客户是真的有意向还是只是客气敷衍。
  - 真意向：主动给具体信息（品牌/车型/时间/城市/预算/手机尾号）/ 问价格 / 同意接 4S 店回电
  - 假意向：只有"好的""考虑一下""有需要会联系"等敷衍话术，没具体动作
  - 模糊：两者之间难判
  返回 JSON {"verdict": "真意向"|"假意向"|"模糊", "reason": "≤30字", "evidence": "客户原话证据 ≤ 50字"}
  ```
- `temperature: 0.1`，`response_format: json_object`

### 7.3 并发与触发
- 服务端启动时自动跑（不需要用户点按钮）
- ThreadPoolExecutor 16 并发，DashScope qwen3.6-plus 单条 1-5 秒
- 34 通约 90 秒；67 通约 2-3 分钟
- 前端模态打开后 5 秒轮询 `/llm-intent-status`，结束自动停

### 7.4 输出 schema
```json
{
  "status": "idle | running | done | error | skipped",
  "total": 34,
  "done": 34,
  "elapsed_s": 92.0,
  "model": "qwen3.6-plus",
  "results": [
    {"call_id": "...", "verdict": "真意向", "reason": "...", "evidence": "..."},
    {"call_id": "...", "error": "HTTP 429: rate limit"},
    ...
  ]
}
```

---

## 8. 导出 Excel 的列

任意点击导出，xlsx 都含这 12 列：

| 列 | 来源 |
|---|---|
| Call ID | 原始 `Call ID` |
| Agent ID | 原始 `Agent ID` |
| Agent Name | 原始 `Agent Name` |
| Duration (s) | 原始 `Duration (seconds)`（int） |
| Hangup Reason | 原始 `Hangup Reason` |
| Max turn_id | 派生 `_max_turn_id` |
| Assistant turns | 派生 `_assistant_turns` |
| Is Human Answered | `_human` |
| Is Full Conversion | `_full` |
| Is Intent | `_intent` |
| Transcript | 渲染成 `role: content\n...` 多行文本 |
| Audio URL | 原始 `Audio Record File Download URL` |

LLM 分析结果的 xlsx 多 4 列：`LLM Verdict / LLM Reason / LLM Evidence / LLM Error`。

---

## 9. 边界情况和注意

- **空 Structured Output**：解析失败/空 → `_structured = None`，所有派生指标都是 0/False（包括 `_intent`）
- **Agent Name 缺失**：填 `"(unknown)"`。下拉框会看到这一组
- **跨 Agent 比较的 X 轴**：`max_turn_id` 和 Duration 分布的 X 轴范围用全数据集计算，切换 Agent 时 X 轴不动，方便横向比较
- **意向客户不计入完整转换分母**：funnel 是并列分支
- **录音 URL 失效**：OSS 签名 URL 有效期 7 天。下载失败会写进 `failed_downloads.txt` 但不影响 xlsx 导出

---

## 10. Change Log

- 2026-05-15: 删除 funnel 的"接听"层（与拨打基本相等，信息量低）
- 2026-05-15: 车型槽放宽——`购车品牌` 或 `购车型号` 任一非空即满足
- 2026-05-15: 完整转换改为 ≥3 of 4 槽位（旧口径是 6 字段全填）
- 2026-05-15: 新增"带车型完整转换"层（完整转换 ∩ 车型槽已填）
- 2026-05-15: 新增 LLM 真伪意向分析（qwen3.6-plus，服务端启动自动跑）
- 2026-05-15: 新增完整转换下钻面板（4/4 vs 3/4 + 缺槽位分布）
