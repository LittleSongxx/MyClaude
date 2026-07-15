# MyClaude 项目深度分析与优化建议

> 分析日期：2026-07-15
>
> 分析对象：当前工作区中的 MyClaude 0.3.0，而非迁移前的旧 `HEAD` 版本
>
> 项目定位：面向秋招 AI 应用开发、AI Agent 和后端开发岗位的个人 Demo

## 1. 执行摘要

MyClaude 已经不是一个简单的 LLM API 封装，而是一个功能比较完整的终端 Coding Agent。项目包含 Textual TUI、非交互 CLI、Remote UI、三类模型协议、工具循环、权限系统、可选操作系统沙箱、上下文压缩、会话恢复、记忆、Skills、Hooks、MCP、子 Agent、Team 和 Worktree 等能力。

从求职 Demo 的目标看，当前项目的主要问题已经不是“功能不够多”，而是以下三项核心能力尚未形成足够可信的证据：

1. **Provider 协议是否完整保真**：尤其是 OpenAI Responses 的 reasoning 与 continuation state。
2. **同一个 Agent 在不同入口下是否行为一致**：尤其是记忆召回、后台任务收尾、错误与退出语义。
3. **这些复杂能力是否真的提高任务成功率**：现有测试证明了大量程序逻辑，但还没有真实模型任务评测闭环。

因此，下一阶段不应继续横向堆叠框架和基础设施，而应从“功能广度”转向四个关键词：

- **协议保真**：正确处理不同 Provider 的状态和能力边界。
- **行为一致**：TUI、Headless 和 Remote 共享同一运行语义。
- **可测量**：用可复现的 Coding Agent 任务证明设计有效。
- **可解释**：让架构、取舍、失败案例和个人贡献能够被面试官快速理解。

最值得优先投入的工作依次是：修复 Provider continuation 和子模型路由、统一三入口运行语义、建立端到端 deadline 与路径安全、实现轻量 Agent eval、再根据评测结果优化上下文和记忆。

## 2. 项目真实业务目标

MyClaude 的真实使用场景不是多租户 SaaS，也不是完全无人值守的云端执行平台，而是：

1. 单个开发者在本地仓库中输入自然语言任务。
2. Agent 读取、搜索和理解代码。
3. Agent 修改文件、执行命令与测试。
4. 用户在必要时确认敏感操作或进行中途转向。
5. 长任务可以压缩、恢复和继续执行。
6. 最终结果能够通过测试和 Git diff 被验证。

因此，项目真正应优化的指标是：

- 任务是否正确完成。
- 是否修改了正确且必要的文件。
- 是否避免破坏受保护文件和用户未要求的内容。
- 完成任务需要多少轮、多少工具调用、多少 token 和多长时间。
- 失败时是否能定位原因、恢复状态并给出稳定退出结果。

这一定位直接决定了若干工程取舍：本项目需要可靠的文件事务边界、Provider 适配、运行时限、评测和轨迹记录，但不需要微服务、消息队列、Kubernetes、生产级多租户隔离或复杂分布式工作流。

## 3. 分析范围与验证基线

本次分析覆盖当前工作区中的主要执行链：

- `myclaude/agent.py`：Agent 循环、重试、工具调度、转向消息和运行限制。
- `myclaude/client.py`：Anthropic Messages、OpenAI Responses、OpenAI-compatible Chat Completions。
- `myclaude/runtime.py` 与 `myclaude/runtime_assembler.py`：共享 Runtime 和能力装配。
- `myclaude/context/`：token 预算、工具结果落盘、压缩和恢复附件。
- `myclaude/memory/`：会话、自动记忆、召回和整合。
- `myclaude/permissions/` 与 `myclaude/sandbox/`：权限、危险命令与 OS 沙箱。
- `myclaude/tools/`：文件、命令、Skills、Agent、Team 和 Worktree 工具。
- `myclaude/mcp/`、`myclaude/hooks/`、`myclaude/teams/`：扩展和协作能力。
- TUI、Headless、Remote 三种入口以及相关测试。

当前验证结果：

- Python 测试：`645 passed, 1 skipped`。
- Ruff：通过。
- `git diff --check`：通过。
- CI 面向 Python 3.11 和 3.13。

这些结果说明项目已有较好的确定性测试基础。但大量 Provider、MCP 与 Agent 行为由 mock 驱动，所以它们主要证明“状态机按代码设计工作”，并不能证明真实模型任务的完成率、成本、延迟或轨迹质量。

## 4. 当前架构评价

### 4.1 值得保留的设计

#### 工具调用边界清晰

模型流完整结束且 stop reason 合法后才进入工具执行，避免在 Provider 输出尚未结束时产生文件或命令副作用。这比一边接收不完整 JSON、一边抢先执行工具更可靠。

#### 并发策略考虑了副作用

工具按并发安全性分批执行，写操作、命令和 Agent 工具保持受控顺序。对于本地 Coding Agent，这比追求所有工具最大并行更符合真实正确性要求。

#### 转向消息没有破坏写操作原子性

流式期间的新用户消息会进入下一轮；可取消的长命令或 Agent 任务可以响应转向，而文件写入不会被中途取消。这一取舍能够避免生成半个文件或处于不可恢复状态。

#### 文件修改具备后端工程意识

文件写入采用原子替换，Edit/Delete 依赖近期读取状态来识别外部修改。这些设计直接服务 Coding Agent 的核心风险，而不是装饰性架构。

#### 上下文溢出恢复比较务实

项目不仅依赖预估 token，还能在真实 API context overflow 后执行一次响应式压缩重试。Provider 的 token 统计可能与本地估算不同，因此保留这个兜底是合理的。

#### 安全边界与本地场景匹配

workspace trust、权限规则、危险命令硬拦截、受保护配置和可选 OS 沙箱构成了分层保护。项目也没有把普通字符串规则误称为强沙箱，这一点应继续保持。

#### 父子 Agent 共享用量账本

子 Agent、压缩、记忆和其他辅助调用共享 token/成本账本，避免只统计主 Agent 而低估整次任务开销。当前计价口径仍需修正，但共享账本方向正确。

### 4.2 架构层面的核心判断

MyClaude 的功能广度已经达到甚至超过个人 Demo 的合理上限。继续增加新的 Agent 类型、工作流框架、存储系统或消息基础设施，不会自动提高求职竞争力。

当前更有价值的工程叙事应是：

> 我实现了一个跨 Provider 的本地 Coding Agent，并用协议测试、任务评测、失败轨迹和消融实验说明其上下文、工具、安全与协作设计为什么有效。

这比“支持几十项功能”更能体现 AI 应用和后端岗位需要的判断力。

## 5. 外部调研得到的关键共识

截至 2026-07-15，主流 Agent 工程资料呈现出几项相对稳定的共识。

### 5.1 优先使用简单、可组合的 Agent 模式

Anthropic 的 Agent 工程实践强调：先使用明确的工作流和简单组合，只有在任务确实需要动态规划时才提高自主性。复杂编排本身不是目标，额外层次会增加延迟、成本和不可预测状态。

这意味着 MyClaude 不需要为了“Agent 化”而迁移到 LangGraph。当前显式 Agent loop 更容易测试，也更适合展示对流式协议、工具边界和取消语义的理解。

### 5.2 Tool interface 和 context engineering 决定实际效果

模型能力只是 Agent 效果的一部分。工具描述是否清楚、返回结果是否紧凑、错误是否可操作、上下文是否包含当前任务需要的信息，往往比增加更多工具更重要。

因此，本项目应测量 deferred tools、恢复附件、记忆召回和大工具结果对真实任务的贡献，而不是默认这些能力越多越好。

### 5.3 Agent eval 必须同时关注结果和轨迹

只评价最终文本不适合 Coding Agent。可靠评测应使用测试、Git diff、文件约束等确定性 oracle，同时分析工具错误、无效循环、重复读取、成本和时延等轨迹指标。

这与 MyClaude 的业务形态高度一致：代码是否正确、是否越界修改，远比回答是否“看起来不错”重要。

### 5.4 Provider 的会话状态不是通用聊天消息

Anthropic Messages、OpenAI Responses 和 Chat Completions 在 reasoning、tool call、缓存和 continuation 上存在真实协议差异。建立 canonical conversation 有利于 UI 与存储，但不能丢弃 Provider 要求回传的 opaque state。

### 5.5 传统后端可靠性原则仍然适用于 Agent

Deadline propagation、structured concurrency、有界并发、原子写入、明确退出码、配置校验、资源预算、审计事件和状态一致性不会因为使用了 LLM 而失效。恰恰因为模型调用慢、昂贵且不确定，这些原则更重要。

## 6. P0：必须优先修复的正确性问题

### 6.1 OpenAI Responses continuation state 丢真

#### 当前问题

[`myclaude/client.py`](myclaude/client.py) 的 OpenAI Responses 路径主要收集 reasoning summary 文本和相关 ID；[`myclaude/serialization.py`](myclaude/serialization.py) 又根据这些信息人工重建 `reasoning` item。

可读 reasoning summary 是给用户或开发者查看的摘要，不等于 Provider 原始 reasoning state。OpenAI Responses 返回的 typed output items、phase、encrypted reasoning 等状态可能影响后续 function-call continuation。手工管理状态时，不应把完整 output item 降级为文本后再伪造。

#### 影响

- 多轮 reasoning + tool calling 可能在真实 OpenAI 模型下表现异常。
- mock 测试可能通过，但 Provider 实际要求的状态没有被保留。
- 会话恢复后可能出现与在线会话不同的行为。
- 项目宣称的 OpenAI Responses 支持会因此缺少协议可信度。

#### 建议

增加一个小型、明确的 `ProviderContinuationState`：

- canonical conversation 继续保存面向 UI、压缩和跨 Provider 展示的通用消息。
- Provider continuation 单独保存其 opaque typed items，不强行翻译成通用文本。
- 活动在线会话优先使用 `previous_response_id` 或 Provider conversation state。
- 需要 stateless 或 Zero Data Retention 风格运行时，保存并回传完整 output items；需要时请求 `reasoning.encrypted_content`。
- 会话持久化对 Provider state 增加 schema/version，遇到未知版本安全降级，而不是猜测重建。

这是一项必要的协议适配，不是过度抽象。

### 6.2 子 Agent 模型路由跨 Provider 错误

#### 当前问题

内置 Explore Agent 在 [`myclaude/agents/builtins/explore.md`](myclaude/agents/builtins/explore.md) 中固定使用 `haiku`。随后 [`myclaude/tools/agent_tool.py`](myclaude/tools/agent_tool.py) 将别名映射成 Claude 模型 ID，但仍沿用父 Agent 的 Provider protocol 和 base URL。

如果父 Agent 使用 OpenAI 或其他兼容 Provider，子 Agent 可能把 Claude 模型名发送到错误的 Provider。同时，子 Agent 还可能沿用父模型的 context window、输出上限与价格，造成预算和成本统计错误。

#### 建议

- 所有内置 Agent 默认 `model: inherit`。
- 提供可选的 `model_roles.explore`、`model_roles.verify` 等配置。
- role override 必须显式绑定到某个已定义 Provider/model profile。
- 使用现有 `model_capabilities.py` 校验协议、模型、窗口、输出限制和价格。
- 配置无效时启动阶段直接报错，不在运行到子 Agent 时才失败。

本项目不需要通用模型路由平台、在线打分器或动态模型市场。一个明确的继承规则加可选 role override 已足够。

### 6.3 三种入口的核心行为不一致

#### 当前问题

相关 topic recall 只在 [`myclaude/app.py`](myclaude/app.py) 的 TUI 路径启动。Remote 与 Headless 没有相同链路。在没有 Team 时，普通 Headless 主循环完成后可能立即返回，尚未完成的后台子 Agent 随 `asyncio.run()` 关闭而被取消。

[`myclaude/__main__.py`](myclaude/__main__.py) 中存在能够收集 TaskManager 通知的逻辑，但实际绑定给 Agent 的通知函数主要读取 Team mailbox。这与工具描述中的“后台完成后自动通知”不完全一致。

#### 影响

- 同一个 prompt 在 TUI 和 `-p` 下可能因为记忆上下文不同而产生不同结果。
- Headless 自动化无法稳定判断后台任务是否已完成。
- 脚本调用方缺少稳定的 stop reason 和退出码契约。
- `RuntimeAssembler` 已经统一了组件装配，但运行生命周期仍由入口决定。

#### 建议

在现有 `runtime_assembler.py` 和 `CoreRuntime` 基础上增加共享 Delivery Adapter，收口：

- 一轮请求的启动与结束。
- 记忆召回的启动、等待预算和注入。
- 权限交互策略。
- 事件持久化和最终结果。
- 后台任务 drain/cancel 策略。
- stop reason、结构化错误和进程退出码。

三个入口只实现渲染差异：TUI 可以询问用户，Headless fail closed，Remote 通过连接发送权限事件。不要为三个入口分别维护一套 Agent 语义。

### 6.4 运行 deadline 只覆盖部分链路

#### 当前问题

[`myclaude/agent.py`](myclaude/agent.py) 建立了运行时限，但 `asyncio.timeout` 主要包住 LLM stream。工具执行、Hooks、MCP、上下文压缩、记忆、子 Agent 和 retry sleep 都可能越过总时限。

[`myclaude/mcp/client.py`](myclaude/mcp/client.py) 的 connect/call 缺少贯穿整个 run 的显式 deadline；[`myclaude/hooks/engine.py`](myclaude/hooks/engine.py) 使用未统一跟踪的后台 future。

#### 建议

引入轻量 `RunContext`：

```text
RunContext
  run_id
  started_at
  deadline
  cancel_event
  task_registry
```

- 所有外部等待使用 `asyncio.timeout_at(run_context.deadline)` 或剩余预算。
- retry backoff 先检查剩余时间，不能睡过 deadline。
- 工具和 MCP 可以设置更短的局部 timeout，但不能超过总 deadline。
- 后台任务必须登记，结束时根据入口策略 drain 或 cancel。
- 使用 `TaskGroup` 或等效受管结构传播异常和取消。
- 对并发读取工具、MCP 调用和子 Agent 设置 semaphore，形成简单背压。

这是对现有运行限制的补全，不需要 Temporal、Celery 或消息队列。

### 6.5 大工具结果文件名信任 Provider ID

#### 当前问题

[`myclaude/context/manager.py`](myclaude/context/manager.py) 使用 Provider 返回的 `tool_use_id` 构造大工具结果文件名。兼容 Provider 并不一定遵守 Anthropic/OpenAI 的 ID 格式，恶意或异常 ID 可能包含路径分隔符或 `..`。

#### 建议

- 使用 UUID 或 `sha256(provider_tool_id)` 生成内部文件名。
- 落盘前通过 `resolve()` 与 `relative_to(session_dir)` 验证最终路径。
- session/state 目录权限设为 `0700`，敏感状态文件设为 `0600`。
- 原始 Provider ID 只作为 JSON 元数据，不参与路径拼接。
- 增加 `../`、绝对路径、超长 ID 和 Unicode 分隔符测试。

这项修改规模很小，却能有效展示路径安全和信任边界意识。

## 7. P1：建立 Agent 质量评测闭环

### 7.1 为什么现有测试还不够

645 个通过的测试是重要资产，但它们主要覆盖：

- 状态转换。
- 工具输入输出。
- 权限与危险命令判断。
- 压缩、恢复、序列化和 Provider mock。
- Team、Worktree、Hooks、MCP 的程序行为。

它们尚未回答：

- 真实模型能否在一个陌生小仓库中正确修复 bug。
- 子 Agent 是否提高成功率，还是只增加 token 和时延。
- 自动记忆是否提供有用信息，还是干扰当前任务。
- 新压缩策略是否保留了关键约束。
- deferred tool schema 是否减少了整个请求的真实 token。

### 7.2 推荐的轻量评测集

在仓库中增加 `evals/`，准备 10～20 个小型 fixture repository。任务应覆盖项目真实能力，而不是追求 benchmark 数量：

1. 单文件逻辑 bug，要求定位并修复。
2. 跨文件 bug，需要搜索调用链。
3. 增加一个受现有接口约束的小功能。
4. 重构重复代码，但保证外部行为不变。
5. 修复失败测试，禁止修改测试本身。
6. 处理配置解析或序列化兼容问题。
7. 修改代码并补充测试。
8. 识别不应修改代码、只需解释原因的任务。
9. 带保护文件和 diff 白名单的安全任务。
10. 需要长上下文或会话压缩的多阶段任务。

每个任务使用独立临时 Git worktree 或复制目录，结束后通过确定性 oracle 评分：

- 目标测试是否通过。
- Git diff 是否只涉及允许路径。
- 保护文件是否保持不变。
- 是否存在意外生成物或未完成冲突。
- 必要的行为断言是否成立。

每个配置运行 3 次，以观察模型随机性和失败方差。报告成功率时同时展示 trial 数量，不把一次偶然成功包装成稳定能力。

### 7.3 应记录的轨迹指标

结果指标：

- task success。
- test pass/fail。
- diff constraint pass/fail。
- protected path violation。

效率指标：

- Agent turns。
- LLM calls。
- tool calls 与 tool errors。
- 重复文件读取次数。
- 输入、输出、cache read、cache write token。
- 估算成本。
- TTFT、总耗时和各阶段耗时。

轨迹质量指标：

- 是否在没有读取文件时直接修改。
- 是否重复执行同一失败命令而没有改变策略。
- 是否产生超大、低价值工具结果。
- 是否在任务已通过后继续无关修改。
- 最终说明是否与实际 Git diff 和测试结果一致。

### 7.4 先做消融，不先做大型 benchmark

第一批实验建议比较：

- `current baseline`。
- `no-memory`。
- `no-subagent`。
- `dynamic-compaction-budget`。
- `transient-tool-catalog`。

如果关闭某项功能后成功率不变、成本下降，应重新审视其默认开启价值。如果某项功能只对特定任务有效，就将它改成按场景触发，而不是所有任务的固定税负。

个人 Demo 没有必要先承担完整 SWE-bench 的成本和环境复杂度。一个小而真实、可复现、包含失败分析的本地 eval suite，更能证明工程判断。

### 7.5 Trace 设计

先使用本地 JSONL 即可，不部署完整可观测平台。建议定义稳定 event schema：

```text
schema_version, run_id, parent_run_id, session_id
provider, model, prompt_version, purpose
event_type, started_at, duration_ms
tool_name, tool_call_id, result_size, error_type
input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
stop_reason, limit_reason, success
```

字段命名尽量参考 OpenTelemetry GenAI semantic conventions，以便未来确实需要时再接 Collector。当前阶段最重要的是能够复现和分析失败，而不是部署 dashboard。

## 8. P2：按模型窗口重做压缩预算

### 8.1 固定预算不适配本地小窗口模型

[`myclaude/context/manager.py`](myclaude/context/manager.py) 中存在固定规模的近期消息、文件快照和 Skills 预算。对于 200k context 模型可能合理，但对用户显式配置的 8k 或 16k 本地模型，压缩恢复附件本身就可能再次溢出。

#### 建议预算公式

```text
available_input
  = context_window
  - max_output_reserve
  - reasoning_reserve
  - protocol_overhead
  - safety_margin
```

随后按比例分配：

- system、工具 schema 和必要环境信息。
- 结构化任务摘要。
- 最近原始对话。
- 必须保留的工具结果。
- 可选文件状态和 Skills 状态。

各部分都要有硬上限，且总和必须在一次恢复前被验证。

### 8.2 恢复附件不应携带大量可重读文件全文

[`myclaude/context/manager.py`](myclaude/context/manager.py) 的恢复附件会注入最近文件内容。Coding Agent 可以从磁盘重新读取这些文件，因此长期携带全文既消耗 token，又可能在文件已变化时提供过期内容。

恢复状态优先保留：

- 用户目标与不可违反的约束。
- 当前计划或未完成任务。
- 已修改文件列表。
- 已执行测试与结果。
- 失败尝试以及为什么失败。
- 未解决问题和下一步。
- 文件路径、读取范围、mtime/hash。
- 已激活 Skill 标识。

只有无法从磁盘恢复的信息才需要原样保留，例如用户纠正、关键外部返回结果和未执行的工具调用状态。

### 8.3 摘要应表示 task state，而不是聊天纪要

当前 SUMMARY_PROMPT 偏向完整时间线、全部用户消息和代码片段，容易生成冗长低信号内容。更适合本项目的是结构化 task state：

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

评测重点不是摘要“看起来完整”，而是压缩后 Agent 是否仍能完成任务、遵守约束并避免重复工作。

### 8.4 deferred tool catalog 不应不断进入历史

[`myclaude/agent.py`](myclaude/agent.py) 会把 deferred tool 名单作为新消息追加。多轮运行中，重复名单会成为永久历史，抵消延迟加载 schema 的 token 收益。

建议把它作为 transient world state：仅在 catalog version 变化时重建，并在每次 Provider 请求中位于动态上下文区域，而不是追加进 canonical conversation。

现有“schema 减少约 90%”类测试应扩展为真实请求 token 对比，包含重复目录消息、system prompt 和 Provider 序列化开销。

### 8.5 稳定 prompt 前缀以改善缓存

秒级当前时间等动态环境信息位于前缀时，会导致后续请求前缀变化，降低 Provider prompt cache 命中率。建议：

- 静态 system prompt 放最前。
- 稳定 workspace 信息和工具 schema 放在其后。
- 动态时间、运行预算、后台任务状态放在最后的 turn state。
- prompt 按 workspace、tool catalog version、Skill version 缓存构建结果。
- system prompt 使用真实 `work_dir`，避免展示的环境信息与工具边界不一致。

## 9. P2：重新约束记忆系统

### 9.1 召回入口不一致且可能错过当前回答

topic recall 主要由 TUI 启动。如果首轮回答没有工具调用，而召回尚未完成，[`myclaude/agent.py`](myclaude/agent.py) 可能只在回答结束后消费后台结果，当前回答看不到相关记忆。

#### 建议

- 召回移入共享 Runtime，而不是 UI。
- 给召回设置很短、可配置的等待预算。
- 先使用 frontmatter、scope、项目路径和关键词进行本地排序。
- 只有候选模糊时才调用小模型判断相关性。
- 超时后继续当前请求，不让记忆成为首 token 的长期阻塞项。

### 9.2 自动长期记忆不应默认成为每轮固定模型调用

当前自动记忆会在完整回答后调用模型提取内容，并可能写入跨项目用户记忆。这会增加固定成本，也让模型决定哪些信息长期保留。

更适合个人 Coding Agent 的策略：

- 默认仅保存项目会话状态。
- 长期记忆 opt-in。
- 通过显式 `/remember` 或会话结束时触发提取。
- 每条记忆保存 `source_session`、时间、workspace 和 scope。
- 用户记忆与项目记忆严格区分。
- 提供查看、删除和禁用入口。

不要为了“智能记忆”立即引入向量数据库。先用 eval 和 trace 证明关键词/frontmatter 召回不足，再决定是否增加 embedding 索引。

### 9.3 Skill 激活状态应从统一入口记录

通过模型工具 `LoadSkill` 和斜杠 Skill 命令激活时，对 recovery state 的记录路径不同。应将记录逻辑统一到 `Agent.activate_skill()` 或等效领域方法，避免 UI/工具入口决定核心状态。

## 10. P2：修正用量与成本口径

### 10.1 当前问题

[`myclaude/usage.py`](myclaude/usage.py) 将普通输入、cache read 和 cache write 使用同一 input 单价计算；OpenAI 路径对 cache creation 的记录也不完整。

不同 Provider、不同模型以及不同缓存周期可能采用不同价格。以 Anthropic 当前公开方式为例，普通输入、5 分钟 cache write、1 小时 cache write 和 cache read 并非同一倍率。OpenAI 的模型和用量字段也需要按实际响应解析，而不是套用 Anthropic 口径。

### 10.2 建议

`ModelCapabilities` 或单独的 pricing profile 应明确区分：

```text
uncached_input_cost_per_million
cache_read_cost_per_million
cache_write_cost_per_million
output_cost_per_million
pricing_source
pricing_updated_at
```

- Provider adapter 把原始 usage 转成统一但不丢字段的结构。
- 无法识别的 token 类型保留在 provider-specific metadata 中。
- 配置覆盖价格时明确优先级。
- UI 将金额标注为“估算成本”。
- `max_cost_usd` 在账单字段可能延迟、流式 usage 不完整时不能宣称严格硬边界。

个人 Demo 不需要自动联网同步价格表。显式配置、集中默认值和更新时间已经足够可靠，也更容易测试。

## 11. P3：高级能力的合理收口

### 11.1 MCP

[`myclaude/mcp/tool_wrapper.py`](myclaude/mcp/tool_wrapper.py) 应完整处理 `structuredContent`、`outputSchema` 和可操作错误；MCP connect/call 应遵守 RunContext deadline。

优先级较低的内容包括完整 OAuth 平台、MCP Tasks、多模态音频/图像资源和生产级远程 Server 管理。这些能力只有在项目出现真实演示用例时才值得增加。

### 11.2 Hooks

异步 Hook 不应使用无人跟踪的 fire-and-forget future。应进入统一 task registry，记录异常，并在 Runtime 退出时 drain 或 cancel。Hook 还应具备独立超时和输出大小限制，避免外部脚本拖住整个 Agent。

### 11.3 Team

Team 的共享 JSON 状态存在跨进程 read-modify-write lost update 风险。如果 Team 继续作为重点展示能力，应增加：

- 临时文件 + 原子替换。
- 文件锁或带版本号的 compare-and-swap。
- 明确的任务状态转换校验。
- 并发进程测试。

如果短期内没有时间保证这些语义，应在 README 中将 Team 标为 experimental，并默认隐藏相关入口。保留一个不可靠的大功能，不如展示一个边界清楚的小实现。

### 11.4 Remote UI

Remote UI 对本地 Demo 已经足够。继续保持 loopback 默认绑定和随机 token，文档明确其没有 TLS、不能直接暴露公网。无需建设用户系统、多租户 RBAC、数据库会话或生产部署控制面。

## 12. 必要的后端工程化设计

以下设计不是生产级过度建设，而是能直接改善当前功能并体现后端能力的最小集合。

| 设计 | 解决的真实问题 | 建议规模 |
| --- | --- | --- |
| `ProviderContinuationState` | Responses reasoning/tool continuation 丢真 | 一个版本化数据结构和 Provider adapter 接口 |
| `RunContext` | deadline、取消和后台任务生命周期分散 | 一个上下文对象，贯穿 LLM、工具、MCP、Hook |
| Delivery Adapter | TUI、Headless、Remote 行为差异 | 共享一轮执行语义，入口只负责 IO |
| Stable Trace Schema | 无法比较成功率、成本和失败轨迹 | 本地 JSONL 和 schema version |
| Model/Profile Validation | 子 Agent 路由、窗口和价格不一致 | 复用 `model_capabilities.py`，启动时校验 |
| Atomic Shared State | Team 并发 lost update | 原子写 + 文件锁/CAS，仅用于确实共享的 JSON |
| Bounded Concurrency | 子 Agent/MCP/读取工具无背压 | 少量 semaphore 配置，不建调度平台 |
| Explicit Exit Contract | Headless 自动化难以判断结果 | stop reason、结构化错误、稳定非零退出码 |

这些抽象都有明确的当前问题作为驱动力。除此之外，不应为了“架构完整”再创建 repository/service/domain 等没有实际复杂度收益的层次。

## 13. 安全设计的合理边界

MyClaude 的威胁模型应清楚写成：用户主动在本地运行 Agent，并可能允许它修改当前仓库或执行命令；不可信输入可能来自仓库文件、网页、MCP 工具结果和模型输出。

最值得补强的安全项：

1. Provider ID 和外部名称永远不能直接变成文件路径。
2. 所有外部调用都有 deadline、大小限制和错误分类。
3. 项目配置、Hooks 和 stdio MCP 只能在 workspace trusted 后加载。
4. Headless 权限请求保持 fail closed。
5. 工具返回的网页/MCP 内容明确标为非可信数据，不把其中指令提升为 system 指令。
6. 日志、trace、会话和记忆写入前过滤 API key 等敏感内容。
7. OS 沙箱不可用时明确告知，不静默退化后仍宣称沙箱保护。

没有必要在个人 Demo 中实现生产级租户隔离、Secret Manager、企业审计中心或复杂网络策略。安全设计应围绕本地 Agent 的真实数据流展开。

## 14. 求职展示与项目可信度

### 14.1 补齐来源和许可

项目需要明确 LICENSE、NOTICE、参考材料和个人新增贡献。源码中的教学来源说明与 README 中的“独立项目”“自主开发”等表述应保持一致且可验证。

这不是形式问题。面试官会关心哪些设计是本人完成、哪些来自教程或参考项目。准确说明来源比模糊扩大原创范围更有可信度。

### 14.2 推荐展示材料

准备三段短录屏或可复现脚本：

1. 修复一个真实 bug，展示搜索、编辑、测试和最终 diff。
2. 在危险操作前触发权限/沙箱，并展示保护边界。
3. 长任务触发压缩或子 Agent，并展示 trace 与用量报告。

同时提供：

- 一张核心 Agent loop 时序图。
- 一张三入口共享 Runtime 架构图。
- 一张 Anthropic/OpenAI/兼容协议差异表。
- 一份 10～20 个 eval task 的 baseline 与消融报告。
- 一个失败案例及其根因分析。

### 14.3 面试时最值得讲的技术点

- 为什么工具只能在完整 stop reason 后执行。
- 为什么写工具和只读工具使用不同的取消/并发策略。
- canonical conversation 与 Provider opaque state 为什么必须分开。
- context overflow、摘要和可重读文件之间如何取舍。
- 权限规则、workspace trust 与 OS 沙箱分别解决什么问题。
- 如何使用 outcome oracle 和 trajectory metrics 评估 Agent。

这些问题能同时体现 AI Agent、Python 异步编程和传统后端工程能力。

## 15. 明确不建议投入的方向

### 15.1 不使用 LangGraph 重写

当前显式循环可以测试 stop reason、工具批次、压缩、转向和权限边界。重写会带来迁移成本和新的隐式状态，但不会自动提高成功率。除非未来出现大量持久化分支工作流且现有状态机无法管理，否则没有必要。

### 15.2 不拆微服务

本项目是单用户本地应用，LLM、工具和 UI 在同一进程中反而容易传递取消、权限和上下文。拆服务会引入部署、RPC、认证和分布式状态，却没有独立扩缩容需求。

### 15.3 不引入 Redis、Kafka 或数据库集群

本地 JSONL、原子文件和少量文件锁足以满足会话、trace 和 Team Demo。只有出现跨机器、高吞吐或多用户共享状态时，这些基础设施才有合理性。

### 15.4 不建设全量向量 RAG

记忆规模和召回效果尚未测量。frontmatter、scope 和关键词足够构建第一版可解释召回。向量库应由 eval 中的召回失败驱动，而不是由技术清单驱动。

### 15.5 不追求生产级云平台能力

Kubernetes、多租户、TLS 终止、企业 OAuth、计费系统和远程执行集群都不属于当前真实业务。README 清楚声明本地 Demo 边界即可。

### 15.6 不把完整 SWE-bench 排名作为第一目标

完整 benchmark 成本高、环境复杂，且不一定能解释项目内部设计的贡献。先建立小型、可复现、有消融的本地评测集，后续有预算时再选取 SWE-bench 子集验证外部有效性。

## 16. 推荐实施路线

### 阶段一：协议和运行正确性

目标：消除会导致真实 Provider 或入口行为错误的问题。

- 修复 OpenAI Responses continuation state。
- 内置子 Agent 默认继承父模型，增加 Provider-aware role override。
- 修复大工具结果路径安全。
- 统一 Headless 后台任务收尾、stop reason 和退出码。
- 增加对应的协议、路径与生命周期测试。

验收标准：确定性测试通过；真实 OpenAI Responses tool-calling 多轮 smoke test 可恢复；三个入口对同一无交互任务产生同类最终状态。

### 阶段二：统一生命周期和可观测性

目标：让 deadline、取消、后台任务和错误能够被一致追踪。

- 引入 `RunContext`。
- MCP、Hooks、工具、压缩、记忆和 retry 继承 deadline。
- 建立受管 task registry 和有界并发。
- 定义版本化 trace JSONL。

验收标准：任何外部等待不会越过总 deadline；后台异常可见；Headless 能稳定返回结构化 stop reason 和非零错误码。

### 阶段三：建立 eval baseline

目标：从“功能存在”转向“效果可证明”。

- 新增 10～20 个 fixture tasks。
- 每个任务建立测试、diff 和保护路径 oracle。
- 每项配置运行 3 次。
- 输出 baseline、成本、延迟和失败轨迹报告。

验收标准：任何人按照文档能复现实验；报告同时展示成功和失败结果；模型、prompt 与代码版本均可追踪。

### 阶段四：数据驱动优化上下文和记忆

目标：只保留能够提高任务质量或降低成本的复杂能力。

- 动态压缩预算。
- 结构化 task-state summary。
- transient tool catalog。
- 稳定 prompt cache 前缀。
- 统一记忆召回并将长期记忆改为 opt-in。
- 修正 cache token 和成本统计。

验收标准：通过消融证明至少一项成功率、成本或时延指标有明确改善，并记录没有改善而被删除或降级的设计。

### 阶段五：求职材料收口

目标：让面试官在较短时间内理解项目价值和个人能力。

- 补 LICENSE/NOTICE 和贡献说明。
- 绘制架构图与 Provider 时序图。
- 准备三段真实工作流演示。
- 发布 eval 报告和一个失败案例复盘。
- 将 Team/MCP 高级能力按稳定程度标记为 stable 或 experimental。

## 17. 优先级总表

| 优先级 | 项目 | 业务收益 | 实现成本 | 是否适合个人 Demo |
| --- | --- | --- | --- | --- |
| P0 | Responses continuation 保真 | 避免真实 Provider 多轮错误 | 中 | 必须 |
| P0 | 子 Agent Provider-aware 路由 | 避免跨 Provider 错模型 | 低 | 必须 |
| P0 | 路径安全与状态文件权限 | 关闭明确安全缺口 | 低 | 必须 |
| P0 | 三入口结束与退出契约 | 支持可靠 CLI 自动化 | 中 | 必须 |
| P1 | RunContext 与端到端 deadline | 防止运行限制失效 | 中 | 很适合 |
| P1 | 本地 eval 与 trace | 证明 Agent 实际效果 | 中 | 核心亮点 |
| P2 | 动态压缩和 task-state summary | 支持不同模型窗口 | 中 | 由 eval 驱动 |
| P2 | 记忆收口与 opt-in | 降低成本和错误污染 | 中 | 由 eval 驱动 |
| P2 | 精确 cache 成本统计 | 提高预算可信度 | 低 | 适合 |
| P3 | MCP structured content | 提升协议完整性 | 低至中 | 有真实用例再做 |
| P3 | Team 文件锁/CAS | 修复并发状态风险 | 中 | 展示 Team 才做 |
| 不做 | 微服务/Kafka/Kubernetes | 无当前业务收益 | 高 | 不适合 |
| 不做 | LangGraph 重写 | 无证据提升效果 | 高 | 不适合 |
| 不做 | 全量向量 RAG | 召回问题尚未被证明 | 中至高 | 暂不适合 |

## 18. 最终结论

MyClaude 已经具有明显超过普通课程 Demo 的实现深度，尤其是工具副作用边界、文件一致性、上下文恢复、权限和多入口能力。它目前最大的风险不是“不够复杂”，而是复杂能力缺少统一语义和效果证据。

下一阶段最合理的方向是冻结横向扩展，将精力集中在：

1. 修复跨 Provider 和跨入口的真实正确性问题。
2. 建立端到端 deadline、取消、退出与 trace 契约。
3. 用小型真实 Coding 任务评测成功率、成本和轨迹。
4. 根据消融结果决定压缩、记忆、子 Agent 和 deferred tools 的保留方式。
5. 清楚展示来源、个人贡献、架构取舍和失败分析。

做到这些后，MyClaude 的求职价值将不再依赖功能数量，而会体现为一套可运行、可验证、边界清楚的 AI Agent 工程实践。

## 19. 调研资料

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
- [Agent safety](https://developers.openai.com/api/docs/guides/agent-builder-safety)

### MCP、后端工程与安全

- [MCP 2025-11-25: Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [MCP security best practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)
- [Python 3.11 asyncio TaskGroup and timeout](https://docs.python.org/3.11/library/asyncio-task.html)
- [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai)
- [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
- [SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering](https://arxiv.org/abs/2405.15793)

---

本报告的建议以当前 MyClaude 代码和个人求职 Demo 的投入产出比为约束。任何新增设计都应由真实失败案例或评测数据驱动，而不应仅因为某项技术流行而引入。
