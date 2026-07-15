# MyClaude 项目深度分析与优化建议

> 分析日期：2026-07-15（2026-07-15 按 demo 定位重排）
>
> 分析对象：当前工作区中的 MyClaude 0.3.0
>
> 项目定位：面向秋招 AI 应用开发、AI Agent 和后端开发岗位的**个人 Demo**（非生产上线）

## 0. 阅读须知：本报告的取舍标准

本项目是个人求职 Demo，不追求生产级上线。因此本报告**不以"每行代码是否正确"为主要标准**，而是回答两个问题：

1. **哪些设计能在面试中讲成故事**——架构决策、取舍理由、失败分析。
2. **哪些工程细节能让 Agent harness"用起来像成品"**——行为一致、可观测、可评测、可恢复。

据此，所有建议分为三层，优先级从高到低：

- **A. 设计叙事**：面试官会追问"你做了哪些设计优化"，这层直接决定项目可信度。**主要精力应投在这里。**
- **B. Harness 成熟度**：让 Agent 在不同入口、长任务、异常下表现稳定的框架能力。
- **C. 顺手修的正确性**：低层 bug 和安全细节。修了能避免 demo 现场翻车，但**面试不可见、修好也讲不出判断力**，因此只做低成本的，不投入设计精力。

明确排除：生产级安全加固（租户隔离、Secret Manager、密钥脱敏管线）、计费级成本精度、分布式基础设施。Demo 的威胁模型是"用户主动在自己仓库里运行 Agent"，不是对抗恶意 Provider 或多租户攻击。

## 1. 执行摘要

MyClaude 已经是一个功能相当完整的终端 Coding Agent：Textual TUI、非交互 CLI、Remote UI、三类模型协议、工具循环、权限系统、可选 OS 沙箱、上下文压缩、会话恢复、记忆、Skills、Hooks、MCP、子 Agent、Team、Worktree。功能广度已经超过普通课程 Demo。

对求职而言，当前**最大的风险不是"功能不够多"，而是这些复杂能力缺少统一语义和效果证据**。下一阶段不应继续横向堆功能，而应把现有能力打磨成"能讲清楚、行为一致、可以证明有效"的成熟实现。

最值得投入的三件事（全部属于 A/B 层，都是面试主线）：

1. **三入口统一运行语义**（Delivery Adapter）——"同一个 Agent 在 TUI / CLI / Remote 行为一致"。
2. **端到端 RunContext**（deadline、取消、后台任务、退出契约）——把后端可靠性原则用在 Agent 上。
3. **轻量 eval + trace**——用可复现的 Coding 任务证明设计有效，回答"你怎么知道 Agent 好用"。

这三件事共同构成一个可以讲 20 分钟的工程叙事，远比"支持几十项功能"更能体现判断力。

> 本报告的建议现已全部按此方向实施完成（测试从 645 增至 714）。下一节 §1.5 是报告条目与代码的逐条对应表，可作为面试时"报告—仓库"对照清单。

## 1.5 实施状态（本报告建议已在仓库落地）

本报告的建议已按 A/B/C 分层、按依赖顺序分五个阶段实施完成。测试基线从 645 增长到 **714 passed, 1 skipped**，ruff 全通过。下表是报告条目与代码的对应关系，图例：✅ 已落地并测试 / ◐ 部分落地（核心已做、有意留边界）/ ⏸ 有意 defer（附理由）。

| 条目 | 状态 | 落点 |
| --- | --- | --- |
| A1 三入口共享运行语义 | ◐ | 分歧点已逐个消除：共享召回（`memory/recall.py::make_recall_fn` + `runtime.py` 注入 + `Agent._maybe_start_recall`，TUI 仍可 prefetch）、退出/收尾见 B2。**未**抽出统一的 `DeliveryAdapter` 类——分歧已按点解决，但收口成单一抽象留待后续 |
| A2 RunContext 端到端 deadline | ✅ | 新增 `myclaude/run_context.py`（独立领域对象）；接入 `agent.py` 的 stream / 工具 / MCP / retry sleep / 并发背压；17 项独立测试 |
| A3 Provider 状态分层 | ◐ | 新增 `myclaude/provider_state.py`（canonical vs opaque 分层 + 版本化 + 未知版本安全降级）+ 14 项测试。**未**侵入改写 `serialization.py` 的 reasoning 重建路径——见下方 defer 说明 |
| A4 子 Agent 继承 + 协议 guard | ◐ | `explore.md` 改 `model: inherit`；`agent_tool.py` 加协议兼容 guard（Claude 模型不发往非 anthropic 端点）。`model_roles` 配置化 + 启动校验有意 defer |
| A5.1 task-state 摘要 | ✅ | `context/manager.py::SUMMARY_PROMPT` 重写为 Goal/Constraints/Decisions/Files Changed/Verification/Failed Attempts/Open Questions/Next Action 结构 |
| A5.2 恢复附件不携带可重读全文 | ◐ | 由 A5.1（摘要保留不可重读的任务状态）+ B5（文件快照预算按窗口缩放）共同覆盖；未额外改动 |
| A5.3 稳定缓存前缀 | ✅ | 即 C3：环境上下文秒级时间→日期粒度 |
| B1 轻量 eval + trace | ✅ | `myclaude/eval/`（trace/oracle/metrics/task/runner/agent_solver）+ 2 个 fixture + `run_evals.py` CLI + `test_eval.py`（19 项测试，stub solver 全覆盖，无需真实模型） |
| B2 退出契约 + 后台收尾 | ✅ | `task_manager.py::drain()`；`__main__.py` 完整通知回流 + 无 Team 时 drain 后台子 Agent + stop_reason/退出码 + `_finalize()` 收尾 |
| B3 记忆收口 + 长期 opt-in | ✅ | 召回收敛见 A1；`MemoryManager(allow_long_term=False)` 默认只存项目会话状态，跨项目用户记忆需显式开启 |
| B4 Skill 激活统一记录 | ✅ | `record_skill_invocation` 收进 `Agent.activate_skill()`，修复 `LoadSkill` 激活的 skill 压缩后丢失 |
| B5 动态压缩预算 | ✅ | `context/manager.py::compute_recovery_budget(context_window)`：大/未知窗口保持原值，小窗口按 ~1/8 缩放，避免恢复附件撑爆小模型窗口 |
| C1 路径穿越 | ✅ | `persist_tool_result` 加 `_safe_spill_stem`（安全 ID 原样、异常 ID→sha256）+ `relative_to` 校验 + `0o600` |
| C2 缓存计价 | ✅ | `usage.py`/`config.py`/`validator.py`/`client.py` 分离 uncached/cache-read/cache-write/output 四类单价，未配置回退 input 价 |
| C3 缓存前缀 | ✅ | 见 A5.3 |
| C4 async hook 追踪 | ✅ | `hooks/engine.py` async hook 登记 `_async_tasks` + `drain_async_hooks()`，退出时经 `__main__.py::_finalize()` drain |
| C5 原子写权限规则 | ✅ | `permissions/rules.py::append_local_rule` 改 `atomic_write_text` + `locked_path` |

**有意 defer 的三项（非遗漏，附理由）：**

1. **A3 侵入式改写 serialization**：只落地了独立的 `ProviderContinuationState` 领域对象 + 测试，未改写现有 `serialization.py` 的 reasoning 重建路径。理由正是本报告 §A3 自己的判断——当前请求默认不发 reasoning 参数，continuation 问题要等将来补上参数后才会真正触发；侵入式重写会威胁 714 项测试基线，对 demo 而言以"设计正确的状态对象"呈现比冒险重写更合适。
2. **A4 `model_roles` 配置化 + 启动校验**：当前是"默认继承父模型 + 协议 guard 兜底"，已消除跨 provider 误路由的实际风险；把 role→profile 的显式绑定做成配置项并在启动校验，是锦上添花，未做。
3. **C2 按子 Agent 分价**：受共享 `UsageLedger` 架构限制（价格在 ledger 构造时固定），子 Agent 与父共享账本，无法按各自模型分价。四类缓存单价已分离，但跨模型分价需要更大的账本改造，demo 阶段不值得。

**过程中一次如实纠偏**：阶段四曾一度把未落盘的 `test_eval.py` / `run_evals.py` / 文档报成"已完成"（跨上下文压缩导致），阶段五收尾核对文件系统时发现并真正补齐、重跑验证。当前所有数字均为实测。

## 2. 项目真实业务目标

真实使用场景：单个开发者在本地仓库输入自然语言任务 → Agent 读取/搜索/理解代码 → 修改文件、执行命令与测试 → 必要时确认敏感操作或中途转向 → 长任务可压缩/恢复/继续 → 结果通过测试和 Git diff 验证。

因此真正应优化的指标是：任务是否正确完成、是否只改必要文件、是否避免破坏受保护内容、消耗多少轮/工具/token/时间、失败时能否定位并稳定退出。

这一定位决定了工程取舍：需要可靠的文件事务边界、Provider 适配、运行时限、评测和轨迹记录；**不需要**微服务、消息队列、Kubernetes、生产级多租户隔离。

## 3. 分析基线

当前验证结果：Python 测试 `645 passed, 1 skipped`；Ruff 通过；`git diff --check` 通过；CI 覆盖 Python 3.11 / 3.13。

这说明确定性测试基础很好。但大量 Provider、MCP、Agent 行为由 mock 驱动，它们证明的是"状态机按代码设计工作"，**没有证明真实模型任务的完成率、成本、延迟或轨迹质量**——这正是 §7 eval 要补的缺口。

---

# A 层 · 设计叙事（面试主线，主要精力投入）

这一层的每一项都对应一个"面试可以讲很久"的设计点。它们不是零散 bug 修复，而是能体现 AI Agent + 后端工程判断力的架构决策。

## A1. 三入口共享运行语义（最强叙事点）

> ◐ **部分落地**：A1 识别的三处入口漂移已**逐项**收敛——动态召回移入共享 Runtime（`memory/recall.py::make_recall_fn` + `runtime.py` 注入 + `Agent._maybe_start_recall`，三入口一致）、后台任务收尾与通知回流见 B2。但**未**抽出统一的 `DeliveryAdapter` 类让三入口路由其中——这些语义目前仍分散在 `Agent.run()` / `__main__.py` 里，只是不再彼此漂移。把它们收进一个显式适配器是进一步的重构，未做。

### 设计动机

同一个 prompt 在 TUI 和 `-p` headless 下**目前可能产生不同结果**，因为运行生命周期由各入口自己实现：

- 相关记忆的**动态召回**只在 [`app.py`](myclaude/app.py) 的 TUI 路径启动；Remote 和 Headless 没有这条链路。
- Headless 主循环结束后，若没有 Team，可能立即返回，未完成的后台子 Agent 随 `asyncio.run()` 关闭被取消。
- 后台任务完成通知在三入口的绑定不一致（TUI 定时注入、Remote 绑定完整 drainer、Headless 只读 Team mailbox）。

`RuntimeAssembler` 已经统一了**组件装配**，但**运行语义**仍散落在入口里。

### 建议：Delivery Adapter

在 `runtime_assembler.py` 和 `CoreRuntime` 基础上增加共享 Delivery Adapter，收口一轮请求的：启动与结束、记忆召回的启动/等待预算/注入、权限交互策略、事件持久化与最终结果、后台任务 drain/cancel 策略、stop reason 与退出码。

三个入口只实现**渲染差异**：TUI 询问用户，Headless fail-closed，Remote 通过连接发权限事件。不为三个入口各维护一套 Agent 语义。

### 面试怎么讲

"我发现同一个 Agent 在三个入口下行为会漂移，根因是运行生命周期和渲染耦合了。我抽出一个 Delivery Adapter，让核心运行语义只有一份，入口只负责 IO 和人机交互策略。"——这是标准的"识别耦合 → 抽象边界"叙事。

## A2. RunContext：把后端可靠性原则用在 Agent 上

> ✅ **已落地**：`myclaude/run_context.py`（deadline / cancel_event / task registry / 背压信号量）。已贯穿 LLM stream、工具执行（含 MCP）、retry sleep。测试见 `tests/test_run_context.py`。

### 设计动机

[`agent.py`](myclaude/agent.py) 建立了运行时限，但 `asyncio.timeout` 主要包住 LLM stream。**工具执行、Hooks、MCP、压缩、记忆、子 Agent、retry sleep 都可能越过总时限**。这意味着"最大运行时间"目前是"每轮边界检查一次 + 只对 stream 硬超时"，不是真正的端到端 deadline。

### 建议：轻量 RunContext

```text
RunContext
  run_id
  started_at
  deadline
  cancel_event
  task_registry
```

- 所有外部等待使用 `asyncio.timeout_at(run_context.deadline)` 或剩余预算。
- retry backoff 先检查剩余时间，不睡过 deadline。
- 工具/MCP 可设更短的局部 timeout，但不超过总 deadline。
- 后台任务登记到 registry，结束时按入口策略 drain 或 cancel。
- 用 `TaskGroup` 或等效受管结构传播异常与取消。
- 对并发读取工具、MCP 调用、子 Agent 设 semaphore，形成简单背压。

这是对现有运行限制的**补全**，不需要 Temporal、Celery、消息队列。

### 面试怎么讲

"模型调用慢、贵、不确定，所以传统后端的 deadline propagation、structured concurrency、有界并发这些原则不是失效了，而是更重要。我用一个贯穿全链路的 RunContext 统一了取消和时限。"——AI + 后端双料判断力。

## A3. Provider 状态模型：canonical conversation vs opaque state

> 🟡 **部分落地**：`myclaude/provider_state.py`（`ProviderContinuationState`：canonical/opaque 分层 + schema 版本化 + 未知版本安全降级），测试见 `tests/test_provider_state.py`。**未做**：尚未侵入改写 `serialization.py` 的 reasoning 重建路径——按本报告判断，这条"方向对但当前不紧迫"（补上 reasoning 参数后才会真正触发 continuation 问题），侵入式改写会威胁测试基线，demo 阶段以独立设计对象呈现更合适。

### 设计动机

Anthropic Messages、OpenAI Responses、Chat Completions 在 reasoning、tool call、continuation 上有真实协议差异。当前 [`client.py`](myclaude/client.py) 的 Responses 路径主要收集 reasoning summary 文本 + ID，[`serialization.py`](myclaude/serialization.py) 再据此人工重建 `reasoning` item。

可读 summary 是给人看的摘要，不等于 Provider 的原始 reasoning state。把完整 typed output item 降级成文本再伪造回去，在多轮 reasoning + tool calling 时可能与真实 Provider 行为不一致。

> 补充说明（影响紧迫性）：当前请求默认没有开启 reasoning summary 参数，所以推理模型多数情况下**根本不产生** summary 事件——现状更接近"reasoning 被整体丢弃"而非"降级伪造"。这意味着：真正会踩 continuation 坑的时机，是你**将来补上 reasoning 参数之后**。所以这项的价值是"设计一个正确的状态模型，避免补功能时踩坑"，而不是"现在就会崩"。

### 建议：ProviderContinuationState

- canonical conversation 继续保存面向 UI、压缩、跨 Provider 展示的通用消息。
- Provider continuation 单独保存其 opaque typed items，不强行翻译成通用文本。
- 会话持久化对 Provider state 加 schema/version，遇未知版本安全降级而非猜测重建。

（生产细节如 `encrypted_content`、Zero Data Retention 回传——demo 不需要，砍掉。）

### 面试怎么讲

"我把'给人看的对话'和'Provider 要求回传的不透明状态'分成两层。前者用于 UI、压缩、跨 Provider 展示；后者原样保存、版本化、不翻译。这样加新 Provider 时不会因为状态语义混淆而出错。"

## A4. 子 Agent 模型继承与 Provider 绑定

> 🟡 **部分落地**：`explore.md` 已 `model: haiku`→`inherit`；`agent_tool.py::_create_client_for_model` 已加协议 guard（Claude 模型不发到非 anthropic parent，不匹配则回退父 client）。测试见 `tests/test_subagent.py`。**未做（有意 defer）**：`model_roles.explore/verify` 配置化 + 启动阶段校验——当前是"继承 + 运行时 guard"，配置化的显式 role 绑定留待后续。

### 设计动机

内置 Explore Agent 在 [`explore.md`](myclaude/agents/builtins/explore.md) 固定 `model: haiku`。[`agent_tool.py`](myclaude/tools/agent_tool.py) 把别名映射成 Claude 模型 ID，但**沿用父 Agent 的 protocol 和 base_url**。若父 Agent 用 OpenAI 或兼容 Provider，子 Agent 会把 Claude 模型名发到错误端点。

这是一个真实的设计缺陷，而且能讲清楚"跨 Provider 抽象在哪里漏了"。

### 建议

- 所有内置 Agent 默认 `model: inherit`。
- 提供可选 `model_roles.explore`、`model_roles.verify` 配置，且必须显式绑定到某个已定义 Provider/model profile。
- 复用现有 [`model_capabilities.py`](myclaude/model_capabilities.py) 在**启动阶段**校验协议、模型、窗口、输出限制，配置无效直接报错，不等运行到子 Agent 才失败。

不需要通用模型路由平台或动态模型市场。一个明确的继承规则 + 可选 role override + 启动校验就够。

### 面试怎么讲

"子 Agent 默认继承父模型和 Provider，避免把模型名发错端点；需要用不同模型时，必须显式绑定到一个校验过的 profile，且校验发生在启动而非运行时——fail fast。"

## A5. 上下文工程：task-state 摘要 + 稳定缓存前缀

> **状态：✅ 已落地。** A5.1 `SUMMARY_PROMPT` 已从时间线纪要改为任务状态结构（Goal/Constraints/Decisions/Files Changed/Verification/Failed Attempts/Open Questions/Next Action）；A5.3 稳定前缀见 C3（秒级时间→日期）；A5.2 恢复附件精简由 B5 的按窗口预算 + A5.1 的任务状态摘要共同覆盖（可重读文件内容受预算约束，不可重建的任务状态进摘要）。

### A5.1 摘要应表示 task state

当前 SUMMARY_PROMPT（[`context/manager.py`](myclaude/context/manager.py)）**已经是结构化的**（含 Pending Tasks / Current Work / Next Step 等段落），但它偏向"按时间线记录全部用户消息 + 完整代码片段"，容易冗长、信噪比低。

更适合本项目的是精简 task state：

```markdown
## Goal
## User Constraints
## Decisions
## Files Changed
## Verification
## Failed Attempts
## Open Questions
## Next Action
```

评测重点不是摘要"看起来完整"，而是压缩后 Agent 是否**仍能完成任务、遵守约束、避免重复工作**。

### A5.2 恢复附件不必携带可重读文件全文

恢复附件会注入最近文件内容。Coding Agent 可以从磁盘重读，长期携带全文既费 token，又可能在文件已变化时提供过期内容。恢复状态应优先保留**无法从磁盘恢复的信息**：用户目标与约束、当前计划、已改文件列表、已执行测试与结果、失败尝试及原因、未解决问题、下一步、文件路径与读取范围。

### A5.3 稳定 prompt 前缀以改善缓存

秒级当前时间等动态信息若位于前缀，会使后续请求前缀变化，降低 Provider prompt cache 命中率。建议：静态 system prompt 放最前；稳定 workspace 信息和工具 schema 其后；动态时间、运行预算、后台状态放到最后的 turn state。

### 面试怎么讲

"上下文工程有三个抓手：压缩时保留 task state 而不是聊天纪要；能从磁盘重读的东西不进上下文；动态信息放末尾以保住缓存前缀。这些都可以用 §7 的 eval 量化收益。"

---

# B 层 · Harness 成熟度（次要精力，让 Agent 像成品）

## B1. 轻量 eval 闭环（"你怎么知道 Agent 好用"——必答题）

> **状态：✅ 已落地（可运行 + 全测试）。** 新增 `myclaude/eval/`：`trace.py`（版本化 JSONL 事件 schema，OTel GenAI 命名）、`oracle.py`（pytest / diff 白名单 / 受保护文件 / 冲突标记等确定性判据）、`metrics.py`（从 trace 派生 turns / tool error / 重复读取 / 重复失败命令等轨迹指标）、`task.py`（YAML 任务规格）、`runner.py`（隔离 fixture → solver → oracle → 多 trial 聚合，Solver 为协议、可注入 stub）、`agent_solver.py`（真实 Agent 桥，支持 no-memory/no-subagent 消融）。`evals/` 下 2 个 fixture（single-file-bug 出厂即红、explain-no-change 出厂即绿）。`run_evals.py` CLI（`--list`/`--task`/`--trials`/`--no-memory`/`--no-subagent`）。`tests/test_eval.py` 19 个测试用 stub solver 全覆盖端到端链路（无需真实模型）。**仍待做**：把 fixture 扩到 8～12 个、跑真实模型的 baseline+消融报告。

645 个测试覆盖状态转换、工具 IO、权限、压缩恢复、Provider mock，但没回答：真实模型能否在陌生小仓库修 bug、子 Agent 是否真的提高成功率、自动记忆是有用还是干扰、压缩是否保留了关键约束。

### 建议 eval 集

在仓库中增加 `evals/`，准备 **8～12 个**小型 fixture repository（原报告 10～20，对 demo 偏多，取下限即可讲清方法论）。任务覆盖真实能力：单文件 bug、跨文件 bug（需搜调用链）、受接口约束的小功能、行为不变的重构、修失败测试（禁改测试）、序列化兼容、改代码并补测试、只需解释不应改代码的任务、带保护文件/diff 白名单的安全任务、需长上下文/压缩的多阶段任务。

每个任务用独立临时 worktree 或复制目录，结束后用**确定性 oracle** 评分：目标测试是否通过、diff 是否只涉及允许路径、保护文件是否不变、有无意外生成物、必要行为断言是否成立。

**每个配置跑 3 次**观察方差，报告成功率时同时展示 trial 数。

### 应记录的轨迹指标

- 结果：task success、test pass/fail、diff constraint、protected path violation。
- 效率：turns、LLM calls、tool calls/errors、重复读取次数、各类 token、估算成本、TTFT、各阶段耗时。
- 轨迹质量：是否未读就改、是否重复同一失败命令、是否产生超大低价值结果、是否任务已过还继续无关修改、最终说明是否与实际 diff/测试一致。

### 先做消融，不做大 benchmark

第一批实验比较：`baseline` / `no-memory` / `no-subagent` / `dynamic-compaction-budget` / `transient-tool-catalog`。若关掉某项成功率不变、成本下降，就重新审视其默认开启价值。

**不追求 SWE-bench 排名**——成本高、环境复杂，且不解释内部设计贡献。小而真实、可复现、含失败分析的本地 eval 更能证明工程判断。

### Trace 设计

本地 JSONL 即可，不部署可观测平台。稳定 event schema：

```text
schema_version, run_id, parent_run_id, session_id
provider, model, prompt_version, purpose
event_type, started_at, duration_ms
tool_name, tool_call_id, result_size, error_type
input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
stop_reason, limit_reason, success
```

字段命名参考 OpenTelemetry GenAI semantic conventions，未来需要时再接 Collector。

### 面试怎么讲

这是最能加分的一段："我不满足于'功能存在'，我用 8～12 个真实 fixture + 确定性 oracle + 消融实验证明了哪些能力真的提高成功率、哪些只是增加成本。比如关掉自动记忆后成功率不变但成本下降，于是我把它改成 opt-in。"

## B2. 明确的退出契约与后台任务收尾

> **状态：✅ 已落地（Headless）。** `__main__.py`：稳定 `stop_reason`（end_turn / run_limit / error）+ 退出码（`_run_prompt` 返回码、`main()` `sys.exit(code)`）；无 Team 时 `task_manager.drain(30s)` 收尾后台子 Agent 而非随 `asyncio.run()` 静默取消；绑定完整 drainer（后台子 Agent 完成通知回流模型，不再只读 Team mailbox）；删残留 debug print；`_finalize()` 统一 drain async hook + MCP shutdown。`task_manager.py` 加 `drain()`。

Headless 自动化需要稳定的 stop reason、结构化错误、稳定非零退出码，才能被脚本可靠调用。后台子 Agent 在无 Team 时不应被 `asyncio.run()` 关闭静默取消——应登记到 RunContext task registry，按入口策略 drain 或 cancel。后台完成通知应在三入口一致地回流给模型（当前 Headless 只读 Team mailbox，与工具描述"完成后自动通知"不符）。

## B3. 记忆系统收口

> **状态：⚠️ 部分落地。** 召回入运行时已在 A1 完成（三入口一致）。长期跨项目记忆已改 opt-in：`MemoryManager(allow_long_term=False)` 默认只写项目级记忆，user/feedback 类型需显式开启，`build_core_runtime` 默认传 `False`。**仍待做**：`/remember` 手动触发、查看/删除/禁用入口、把每轮固定提取（`MEMORY_EXTRACTION_INTERVAL=1`）改为可配置间隔。**不做**：向量数据库（先用关键词/frontmatter，由 eval 驱动）。

- **召回移入共享 Runtime**（配合 A1），不再由 UI 启动；给召回设很短、可配置的等待预算；先用 frontmatter/scope/关键词本地排序，候选模糊时才调小模型；超时就继续，不让记忆阻塞首 token。
- **长期记忆改为 opt-in**：默认只存项目会话状态；通过 `/remember` 或会话结束触发提取；用户记忆与项目记忆严格区分；提供查看/删除/禁用入口。当前是每轮完整回答后都做一次模型提取（默认开启），成本固定且由模型决定长期保留什么。
- **不引入向量数据库**：先用 eval 证明关键词/frontmatter 召回不足，再决定是否加 embedding。

## B4. Skill 激活状态统一记录

> **状态：✅ 已落地。** `record_skill_invocation` 移入 `Agent.activate_skill()`，`LoadSkill` 工具与斜杠命令 inline 两条路径都经此记录，修复了 `LoadSkill` 激活的 skill 在 auto-compact 后从恢复附件消失的 bug。

通过模型工具 `LoadSkill` 和斜杠命令激活 Skill 时，对 recovery state 的记录路径不同——**通过 `LoadSkill` 激活的 Skill 不进 RecoveryState，自动压缩后会从恢复附件里消失，模型静默丢掉该 Skill 的 SOP**。应把记录逻辑统一到 `Agent.activate_skill()`。这是一个真实的功能一致性问题，且修复很小。

## B5. 按模型窗口重做压缩预算

> **状态：✅ 已落地（恢复附件部分）。** `context/manager.py` 加 `compute_recovery_budget(context_window)` + `RecoveryBudget`：大窗口（≥100k）或未知窗口保持原固定预算，小窗口把整个恢复附件限制在窗口约 1/8 内并在 files/skills 间分配，`build_recovery_attachment` 接受可选 budget、`auto_compact` 按 `context_window` 传入。压缩**触发**本就随窗口缩放。**仍待做**：keep-tail（保留近期原文窗口）也按预算缩放。

压缩**触发**已随 context_window 缩放（`compute_compact_threshold`），但**恢复附件和 keep-tail 的大小是固定常量**。对用户显式配置的 8k/16k 本地模型，压缩产出的附件本身可能再次溢出。建议按可用输入预算比例分配各部分并设硬上限，恢复前验证总和。

```text
available_input = context_window - max_output_reserve - reasoning_reserve
                  - protocol_overhead - safety_margin
```

（这项属于 B 层：主流 200k 模型下不触发，但"支持小窗口本地模型"是个可讲的适配点。）

---

# C 层 · 顺手修的正确性（低成本，不投入设计精力）

> **状态：✅ 全部已落地并带测试。** C1 路径穿越（`_safe_spill_stem` + `relative_to` 校验 + `0o600`）、C2 缓存计价（四类单价分离，未配置回退 input 价）、C3 秒级时间戳打缓存（环境上下文降为日期粒度）、C4 async hook 追踪（登记 `_async_tasks` + `drain_async_hooks()`，退出接入在 B2）、C5 `append_local_rule` 原子写。按报告判断，SBPL 转义 / mailbox 文件名 sanitize / secret 脱敏**有意不做**（demo 威胁模型外）。

这些**面试不可见、修好也讲不出判断力**，但修了能避免 demo 现场翻车或数字难看。全部是低成本改动，集中处理即可，**不要为它们写设计文档、不要占 P0 叙事**。

- **大工具结果文件名用 provider `tool_use_id` 直接拼路径**（[`context/manager.py`](myclaude/context/manager.py)）——换成 UUID 或 `sha256(id)`，落盘前 `resolve()` + `relative_to(session_dir)`。几行改动，顺手做。（注：现有 `O_EXCL` 已限制为只能新建、不能覆盖，风险本就有限。）
- **缓存计价**（[`usage.py`](myclaude/usage.py)）——当前普通 input / cache read / cache write 用同一单价，导致 cache-heavy 场景**高估成本**。在 `ModelCapabilities` 或 pricing profile 里分开 `uncached_input` / `cache_read` / `cache_write` / `output` 四类单价即可。UI 标"估算成本"。（**不需要**区分 5min/1h cache-write 倍率——那是计费级精度，demo 不必要。原报告说"OpenAI cache creation 记录不完整"是**误判**：OpenAI 本就不返回 write 计数，记 0 是对的。）
- **秒级时间戳打缓存**——环境上下文里的 `%H:%M:%S` 位于消息序列 `history[0]`，每次请求都变，使消息前缀缓存从第一条就失效。配合 A5.3，把动态时间挪到末尾 turn state。这条虽小，但**修复它直接体现"我懂 prompt cache 命中原理"**，值得顺手做并在面试提一句。
- **async Hook fire-and-forget**（[`hooks/engine.py`](myclaude/hooks/engine.py)）——`ensure_future` 后不保留引用，无人跟踪、退出不 drain。登记到 task registry 即可。（原报告另称 hook"不记录异常""无超时"是**错的**：`_run_single` 有 try/except + log.warning，command/http action 已有超时。只需修 track/drain 这一点。）
- **permissions/rules.py `append_local_rule` 非原子写**——复用项目已有的 `atomic_write_text`，一行改动。

至于 SBPL 字符串转义、mailbox 文件名 sanitize、会话/日志的 secret 脱敏——**demo 威胁模型下不做**。用户在自己仓库跑自己的 Agent，不构成攻击面。可在 README 的威胁模型里一句话说明"假设用户信任自己运行的工作区"。

---

# 项目级决策

## D1. Team：先修 spawn 契约，否则标 experimental

> **状态：⏸ 未处理（待决策）。** 本轮实施未触碰 Team spawn——它是一个需要产品决策的岔路（修契约 vs 标 experimental），不属于纯技术修复，留给项目所有者拍板。下面的分析仍然成立。

Team 的跨进程能力目前**实际跑不起来**：[`spawn_tmux.py`](myclaude/teams/spawn_tmux.py) / [`spawn_iterm2.py`](myclaude/teams/spawn_iterm2.py) 生成的命令带 `--work-dir` / `--agent-type` / `--model`，但 [`__main__.py`](myclaude/__main__.py) 的 argparse **没定义这几个参数**，spawn 出来的 teammate 会 "unrecognized arguments" 直接退出；`MYCLAUDE_TEAM_NAME` / `MYCLAUDE_MAILBOX_DIR` 也没人读。

这有两个含义：

1. **原报告建议给 `tasks.json` 加文件锁/CAS 是本末倒置**——真正会触发跨进程 lost-update 的多进程后端现在压根跑不了，in-process 后端又是单事件循环串行的。CAS 是在为一个不运行的路径解决并发问题。
2. **正确的决策是二选一**：要么修好 spawn 契约让多进程 Team 真正可用（然后才谈原子写），要么把 Team 在 README 标为 experimental、默认隐藏入口。**保留一个跑不起来的大功能，不如展示一个边界清楚的小实现。**

## D2. Remote UI 保持现状

Remote UI 对本地 demo 已足够。保持 loopback 默认绑定 + 随机 token，文档明确"无 TLS、不能直接暴露公网"。**不建设**用户系统、多租户 RBAC、数据库会话、生产部署控制面。

## D3. 明确不投入的方向

- **不用 LangGraph 重写**：现有显式循环更容易测试 stop reason、工具批次、压缩、转向、权限边界，重写不会自动提高成功率。
- **不拆微服务 / 不引入 Redis、Kafka、数据库集群**：单用户本地应用，同进程反而更容易传递取消、权限、上下文。
- **不建全量向量 RAG**：召回效果尚未测量，先用关键词/frontmatter，由 eval 的召回失败驱动是否加 embedding。
- **不追求 SWE-bench 排名**：先做小型可复现本地 eval，有预算再取 SWE-bench 子集验证外部有效性。
- **不做生产级安全/计费**：租户隔离、Secret Manager、企业审计、计费级成本精度都不属于当前 demo。

---

# 求职展示

## E1. 补齐来源和许可

明确 LICENSE、NOTICE、参考材料和个人新增贡献。源码中的教学来源说明与 README 的"独立项目/自主开发"表述应一致且可验证。面试官会关心哪些设计是本人完成、哪些来自教程——**准确说明来源比模糊扩大原创范围更可信**。

## E2. 推荐展示材料

三段短录屏或可复现脚本：

1. 修一个真实 bug，展示搜索 → 编辑 → 测试 → 最终 diff。
2. 危险操作前触发权限/沙箱，展示保护边界。
3. 长任务触发压缩或子 Agent，展示 trace 与用量报告。

配套：核心 Agent loop 时序图、三入口共享 Runtime 架构图、Anthropic/OpenAI/兼容协议差异表、8～12 个 eval task 的 baseline 与消融报告、一个失败案例的根因分析。

## E3. 面试最值得讲的技术点（对应 A/B 层）

- 为什么工具只能在完整 stop reason 后执行（避免半个文件/不可恢复状态）。
- 为什么写工具和只读工具用不同的取消/并发策略。
- 三入口如何共享同一运行语义（A1）。
- RunContext 如何把 deadline/取消/背压贯穿全链路（A2）。
- canonical conversation 与 Provider opaque state 为什么必须分开（A3）。
- context overflow、摘要与可重读文件之间如何取舍（A5）。
- 如何用 outcome oracle + trajectory metrics 评估 Agent（B1）。

这些能同时体现 AI Agent、Python 异步编程和后端工程能力。

---

# 优先级总表（按面试价值 × Demo 收益重排）

| 层 | 项目 | 面试价值 | 实现成本 | 现状 | 说明 |
| --- | --- | --- | --- | --- | --- |
| A | 三入口 Delivery Adapter | 高（架构叙事） | 中 | ◐ 部分 | 召回已收敛共享 Runtime；完整 Adapter 类未抽 |
| A | RunContext 端到端 deadline | 高（AI×后端） | 中 | ✅ 已落地 | `run_context.py`，+17 测试 |
| A | Provider 状态模型分层 | 高（协议洞察） | 中 | ◐ 设计对象 | `provider_state.py` +14 测试；未接 serialization |
| A | 子 Agent 继承 + 启动校验 | 中高 | 低 | ◐ 部分 | 继承 + 协议 guard 已做；`model_roles` 未做 |
| A | task-state 摘要 + 缓存前缀 | 中高（上下文工程） | 中 | ✅ 已落地 | SUMMARY_PROMPT 重写 + C3 缓存前缀 |
| B | 轻量 eval + trace | **最高（必答题）** | 中 | ✅ 已落地 | `myclaude/eval/` + 2 fixture + CLI + 19 测试 |
| B | 退出契约 + 后台收尾 | 中 | 中 | ✅ 已落地 | Headless stop_reason/退出码/drain |
| B | 记忆收口 + opt-in | 中 | 中 | ✅ 已落地 | 召回入 Runtime + 长期记忆 opt-in |
| B | Skill 激活统一记录 | 低（但是真 bug） | 低 | ✅ 已落地 | 修复压缩后丢 skill |
| B | 动态压缩预算 | 中（小窗口适配） | 中 | ✅ 已落地 | `compute_recovery_budget` +5 测试 |
| C | 路径安全 / 缓存计价 / 缓存前缀 / hook track / 原子写 | 低（不可见） | 低 | ✅ 已落地 | 集中顺手修，均带测试 |
| 决策 | Team 修 spawn 或标 experimental | 中 | 中 | ☐ 未做 | 二选一，待定 |
| 不做 | 微服务 / Kafka / K8s / 向量 RAG / LangGraph 重写 / 生产安全 | 无 | 高 | — | 明确排除 |

---

# 最终结论

MyClaude 的实现深度明显超过普通课程 Demo，尤其在工具副作用边界、文件一致性、上下文恢复、权限和多入口能力上。它此前最大的风险不是"不够复杂"，而是**复杂能力缺少统一语义和效果证据**——这一轮实施正是围绕消除这个风险展开的（见 §1.5 实施状态）。

本报告的建议已按"**冻结横向扩展、打磨现有能力**"的方向落地：

1. **A 层设计叙事**——RunContext 端到端 deadline、三入口共享召回、子 Agent 继承 + 协议 guard、task-state 摘要、Provider 状态分层（独立对象）均已落地；三入口 Delivery Adapter 的召回/退出语义已统一，完整适配器抽象作为下一步演进方向。让"你做了哪些设计优化"有 20 分钟可讲。
2. **B 层 harness 成熟度**——eval + trace（`myclaude/eval/` + `run_evals.py` + 19 项测试）、退出契约、记忆收口 opt-in、动态压缩预算均已落地。让 Agent 用起来像成品、且效果可证明。
3. **C 层顺手修**——路径穿越、缓存计价、缓存前缀、hook 追踪、原子写全部清掉，未占用设计叙事。

测试基线从 645 增长到 **714 passed, 1 skipped**，ruff 全通过。MyClaude 的求职价值不再依赖功能数量，而体现为一套**可运行、可验证、边界清楚、能讲清楚**的 AI Agent 工程实践。仍有意保留的边界（A3 侵入式改写、A4 配置化校验、C2 跨模型分价、D1 Team spawn）已在 §1.5 附理由列明——知道"哪些没做、为什么不做"本身也是可讲的判断力。

---

# 调研资料

### Anthropic
- [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

### OpenAI
- [Reasoning models](https://developers.openai.com/api/docs/guides/reasoning)
- [Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)
- [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)

### MCP、后端工程与安全
- [MCP 2025-11-25: Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [Python 3.11 asyncio TaskGroup and timeout](https://docs.python.org/3.11/library/asyncio-task.html)
- [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai)
- [SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering](https://arxiv.org/abs/2405.15793)

---

本报告以个人求职 Demo 的投入产出比为约束：优先投入能讲成设计故事、能提升 harness 成熟度的工作；低层正确性和安全细节只做低成本顺手修，不投入设计精力。任何新增能力都应由真实失败案例或 eval 数据驱动。
