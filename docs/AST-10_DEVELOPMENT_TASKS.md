# AST-10 — 可直接领取的开发任务

本文件中的每个 task 都可独立复制、分派和执行。每个 task 均重述了自身需要的产品背景、接口、交付边界与验收标准；执行者不需要先读 AST-10 其他文档、旧 milestone 或其他 task。允许读取其明确列出的源码以保持兼容，但不得把缺失的背景留给执行者自行猜测。

目标系统是一个由本仓库拥有产品逻辑的本机闭环服务。设备、模型运行库、授权的 LLM provider 和现有本机 TTS/播放器是外部基础设施，通过仓库内适配器接入。调试期间完整保存每次监听会话收到的麦克风音频，不设 TTL、不自动清理；归档数据写在仓库外。所有轮次必须有贯穿接收语音、处理、LLM、播报和返回的结构化 logging 与时间分析。

建议实现次序：AST10-01、02、03 可并行；04 在它们的接口稳定后集成；05、06 验证整条链路。即使并行执行，每个 task 本身也包含完整背景和验收要求。

---

## AST10-01 — 全量音频归档与诊断记录

### 可直接领取的完整任务

**目标**：为 Project Astero 实现仓库内拥有的调试归档模块。调试监听会话从采集设备打开到关闭，所有收到的 PCM 帧都必须持久保存，包括静音、唤醒前音频、未形成完整语句的尾段，以及断流前已经收到的帧。当前不设置 TTL，不自动清理任何归档。

**必要背景与既有输入契约**：

- 真实 Echo 捕获输出由 `astero.audio.capture.AudioFrame` 表示：`timestamp_ns: int`（主机单调时钟纳秒）、`sample_rate: int`（目前 16000）、`channels: int`（目前 9）、`sample_format: str`（目前 `s24_3le`）、`sequence: int`、`pcm: bytes`（交错原始 PCM）。输入细节见 `astero/audio/capture.py`。
- `astero.audio.pcm.MonoFrame` 是模型侧转换结果：`timestamp` 秒、`sequence`、`sample_rate`、`sample_format`（`pcm_s16le` 或 `float32_le`）、`sample_count`、`data`。项目当前是从 9 通道数据中选取麦克风通道后转换。
- 音频归档的用途是复现误唤醒、VAD/端点截断、ASR 错误和时序问题。不能只存 VAD 返回的 utterance；必须能还原采集会话中的完整输入时间线，并能关联某次轮次。
- 本地模型和 LLM 会生成识别文字、请求和回复；这些是调试记录的一部分。密钥、Authorization 头和凭据绝不能写入归档。

**接口和实现要求**：

- 新增独立包 `astero/archive/`，至少提供 `ArchiveWriter` 和 `ArchiveReader`（或等价清晰 API）。建议接口：`begin_session(metadata) -> capture_id`、`append_frame(capture_id, AudioFrame)`、`append_event(capture_id, event)`、`close_session(capture_id, outcome)`、`read_manifest(capture_id)`、`iter_frames(capture_id)`。
- 存储根目录必须可配置，默认放在 `~/.local/share/astero/archives/` 或等价仓库外目录；创建目录和文件时限制为当前用户可读写。不得把音频或本机模型/密钥放入 Git 工作树。
- 原始字节必须无损保存。manifest 要记录容器/编码版本、设备/捕获会话信息、各帧格式、单调时间戳、序号、每帧字节数、接收总帧数和完成/异常关闭状态。归档应足以重建为正确的 9 通道 `s24_3le` 流。
- 对 `MonoFrame` 或等价转换数据提供关联归档能力，记录通道选择、增益、转换格式和源帧映射；至少能通过 `capture_id` 与 `turn_id` 关联原始音频、模型输入和结果。
- 提供安全的结构化事件记录，可容纳 stage、event、timestamp、elapsed_ms、status、retry_count 和无敏感标识属性。调试模式可以保存 transcript、LLM 请求/回复正文，但必须剥除密钥和认证头。正常 logging 不得把正文写入通用日志。
- 写入失败、磁盘空间不足和异常退出必须留下可识别状态；失败不得被静默忽略，也不得报告“归档完整”。不做配额淘汰、不做 TTL、不自动删除旧记录。
- 写入应按帧流式执行，不能把长监听会话的所有音频一直堆在 RAM 中。进程被杀后，已写入帧和 manifest 必须能识别为不完整会话，而不是误标为成功关闭。

**文件边界**：新增 `astero/archive/` 和 `tests/test_archive.py`；避免修改采集器、orchestrator、LLM 或 TTS 实现。若需要对接数据模型，在本包内提供窄适配接口。

**交付物**：可配置的归档 reader/writer、版本化 manifest、错误类型、简短包内使用示例，以及离线测试。

**验收标准**：

1. 用合成的 9 通道 `AudioFrame` 验证原始 PCM 字节逐字节相同、顺序/时间戳/格式正确，reader 可完整迭代还原。
2. 验证静音帧、唤醒前帧和异常结束前已接收帧都不会被过滤；异常后 manifest 明确标记 incomplete。
3. 验证派生 mono 数据和源 capture/turn 关联可查询。
4. 验证 transcript、LLM 正文只出现在调试归档的受限数据文件；凭据与认证头被剔除，通用日志不包含正文。
5. 验证权限、路径校验、重复 ID、无效帧、写失败和磁盘空间错误均显式失败；任何路径都不能触发自动清理。
6. 本 task 不要求真实 Echo 或联网；记录实际运行的离线测试结果，不宣称完成声学验收。

---

## AST10-02 — SQLite 会话、任务、记忆与知识存储

### 可直接领取的完整任务

**目标**：实现 Project Astero 的本机持久数据层，使 service 重启后可以恢复 session、轮次状态、记忆和知识索引。归档文件由音频归档模块管理；本数据库保存索引、结构化数据和阶段事件，不把大块 PCM 塞入 SQLite。

**必要背景与数据规则**：

- Project Astero 是语音闭环服务：Echo 麦克风音频经唤醒、VAD、ASR、LLM，再由本地 TTS/播放器播报。多个 session 必须隔离。
- 调试期全量录音由单独的仓库内归档模块放到仓库外；现在不设 TTL、不自动清理。SQLite 至少保存 `capture_id`/路径索引和完整性状态，不负责删音频。
- 记忆、知识、会话短期上下文和原始音频是不同数据类别。不可把所有对话自动提升为长期记忆。长期记忆首版只允许通过明确的写入接口保存；知识条目要有来源。
- 所有时间分析事件要有 `trace_id`、`turn_id`、`session_id`、stage、状态和耗时信息。阶段事件用 append-only 方式保留，便于调试恢复。

**接口和实现要求**：

- 新增 `astero/persistence/` SQLite 层，提供一个主入口（例如 `AsteroStore`）和 schema migrations。数据库路径可配置，默认位于 `~/.local/share/astero/astero.sqlite3` 或等价仓库外目录；创建权限限当前用户。
- 至少持久化：sessions、turns/tasks、capture archive indexes、stage events、conversation turns、memories、knowledge documents/chunks 和 schema version。
- session、turn、capture、trace 均使用稳定唯一 ID。`turn_id` 对应一次语音请求；多个阶段 event 可按单调的 sequence/created order 重放。任务终态至少支持 `completed`、`failed`、`cancelled`、`no_wake`、`no_speech`；非终态与终态必须清楚区分。
- `Memory` 记录内容、来源说明、创建/更新时间、session/user scope 和可删除 ID。`Knowledge` 记录文档来源、内容/片段、导入时间和可检索字段。提供按 scope 查询、全文检索（SQLite FTS5 可用时优先）和显式删除能力。
- 提供基于事务的 repository 方法：创建/更新 session 和 turn、追加 stage event、记录 archive index、保存/查询 conversation context、增加/删除/list/search memory、增加/删除/list/search knowledge、查询恢复未终结的 turns。
- 相同 session 的写入必须有一致性保证；不同 session 内容不可互串。数据库迁移可重复执行，旧 schema 不能被静默覆盖。
- 默认数据根目录在仓库外；不实现 TTL、自动清理、云同步、embedding 或跨机器访问。
- 日志只记 operation、ID、耗时、错误类别；不要把音频、transcript、memory/knowledge 正文、API key 或数据库内容全量写入 stderr。

**固定领域字段**：

- Stage event：`event_id, trace_id, turn_id, session_id, stage, event, occurred_at_utc, monotonic_ns, elapsed_ms, status, attempt, attributes_json`。耗时来自 monotonic clock；UTC 时间只用于人读和排序展示。迁移必须兼容没有 `monotonic_ns` 的旧记录。
- Archive index：`capture_id, turn_id nullable, manifest_path, started_at_utc, ended_at_utc nullable, frame_count, complete, error_code nullable`。
- Memory/knowledge 必须保留来源和更新时间；搜索结果包含稳定 ID，便于后续回答引用来源。

**文件边界**：新增 `astero/persistence/` 与 `tests/test_persistence.py`。避免修改 `live.py`、音频归档 writer 或 provider 实现。模块需要让未来 service 通过稳定的 Python 方法调用，不强迫调用者直接写 SQL。

**交付物**：数据库 schema 与 migration、repository API、数据类型、错误类型、仓库外路径配置、调用示例和离线测试。

**验收标准**：

1. 新数据库建表、升级、重复启动后 schema 稳定；不依赖真实 Echo、网络或模型。
2. 写入 session/turn/events 后关闭并重开数据库，数据及未终结任务可恢复。
3. 两个 session 的历史、memory、knowledge 查询互相隔离；来源和时间字段完整。
4. Memory/knowledge 查询返回稳定 ID 和来源；删除指定条目后不再被查询命中。
5. 多线程或并发 session 写入不会损坏数据库；事务失败时不留半条业务记录。
6. 验证非法路径、损坏数据库、磁盘错误或未知 schema 版本会显式报错；本层无自动清理逻辑。

---

## AST10-03 — 统一时间分析与结构化 logging

### 可直接领取的完整任务

**目标**：建立仓库内统一的 trace/logging 工具，让每次对话从“音频被服务接收”到“最终播放完成或失败”具有同一 trace、turn、session 关联，并可以计算全链路和各阶段耗时。

**必要背景与链路阶段**：

Project Astero 当前语音路径是 Echo capture → PCM/mono conversion → wake → VAD/endpoint → ASR → agent/LLM → 本地 TTS/Voice Bridge 播放。当前 `astero/orchestrator.py` 已有状态转换，`astero/live.py` 负责实机组合；未来 API/service 也要记录请求排队时间和返回时间。不能只记录一条“完成”，必须知道各阶段何时开始、何时结束、耗时及失败位置。

**固定事件 schema**：

每个事件至少具有：

```text
trace_id, turn_id, session_id, capture_id(nullable),
stage, event(started|completed|failed|retry|skipped),
timestamp_utc, monotonic_ns, elapsed_ms(nullable),
status, attempt, error_code(nullable), attributes
```

- 同一轮各阶段共用 `trace_id` 和 `turn_id`；capture 已创建后填入同一 `capture_id`。
- `monotonic_ns` 用于计算间隔和耗时，不能用墙上时钟相减；`timestamp_utc` 供人读、跨进程关联和排序展示。
- 所有阶段耗时是非负数；失败阶段也必须写终止事件。重试要增加 attempt 并单独记录。
- 必须覆盖服务入站/排队、capture frame received、PCM conversion、wake detection、endpoint/VAD、ASR、LLM 请求排队/开始/返回、TTS 请求/queued、播放终态、服务向调用方返回。持续监听的 frame received 可聚合为有界批次/统计窗口，避免每帧输出一行通用日志；全量 frame 时间线由 archive 保存。

**实现要求**：

- 新增 `astero/observability/`，至少有 `TraceContext`、`StageTimer`/span helper、结构化 `Logger` 或标准 logging adapter、可替换 clock 和 JSON-compatible formatter。
- 支持把事件同步交给调用方（用于 archive/persistence）并同时写本机结构化日志；持久化失败应通过错误状态显式上报，不能悄悄改变 trace ID。
- 记录阶段名称、elapsed_ms、状态、attempt、错误类别和适量无敏感元数据。普通日志不写 PCM、转录、LLM prompt/reply、TTS 正文、API key、Authorization 头或环境变量。调试归档正文由专门 archive 模块保存。
- HTTP/子进程适配器需能传递同一 trace id（如 request metadata/header/env 中的非秘密 correlation id），但不得让 provider 自行生成并覆盖上游 id。
- 生成每轮摘要：total_elapsed_ms 和 capture→wake、wake→speech_end、ASR、LLM、TTS enqueue→completed 等可得的阶段耗时；缺失阶段应标记 unavailable，而不是伪造 0。

**文件边界**：新增 `astero/observability/` 与 `tests/test_observability.py`；不要修改 capture、orchestrator、agent provider 或 TTS client。交付 API 需足够简单，其他模块可以经依赖注入使用。

**交付物**：上下文/计时/事件模型、JSON formatter、脱敏规则、轮次耗时汇总 helper、使用示例和离线测试。

**验收标准**：

1. 使用可控假时钟跑完整阶段序列，验证 trace ID 稳定、stage 开始/结束配对且 elapsed 精确。
2. 验证失败、重试、跳过、阶段缺失、并发 session 的关联字段正确。
3. 验证日志输出是有效结构化 JSON，不含测试用 PCM、私人文本、密钥或认证头。
4. 验证事件可以交给外部 recorder 接收；recorder 抛错时明确返回错误并保留原始诊断信息。
5. 验证服务请求/排队时间和总时间可分别计算；长时间 capture 采用聚合日志而非无界内存积累。

---

## AST10-04 — Agent 应用服务、持久上下文与知识检索

### 可直接领取的完整任务

**目标**：在本仓库提供 Agent Service 的领域层，把会话、LLM provider、持久上下文、可追溯知识和明确写入的长期记忆组合起来。Provider 只负责生成答案，不拥有持久化、session 或任务生命周期。

**必要背景**：

- 语音入口提供 ASR 转录文本和 `session_id`；现有 `astero.agent.conversation.ConversationBackend.respond(text, session_id) -> str` 是已有 provider 边界。当前 `OpenAICompatibleBackend` 的 history 只在进程内，`CodexConversation` 目前位于 `astero/live.py`。
- 本地 TTS 只播报 agent 返回的回复，不生成答案。真实 LLM 必须是明确配置并经用户授权的 provider；测试 mock 不能算真实链路验收。
- 用户要求 session、记忆和知识长期化；长期记忆不得自动吸收所有语音转录。知识和记忆要分开检索并记录来源。
- 调试归档会完整保存语音、ASR 文本、LLM 请求/回复和阶段日志，位于仓库外且不设 TTL。此 task 只负责把必要的正文/关联交给注入的 recorder，不负责音频文件 I/O。

**固定服务接口**：

```python
AgentService.submit_turn(text: str, session_id: str, *, trace_id: str, turn_id: str) -> AgentTurnResult
AgentService.get_context(session_id: str) -> ContextSnapshot
AgentService.remember(session_id: str, text: str, *, source: str) -> MemoryRecord
AgentService.forget(memory_id: str, session_id: str) -> bool
```

`AgentTurnResult` 至少包含 `turn_id, session_id, status, reply, started_at, completed_at, elapsed_ms, error_code, knowledge_sources`。状态使用 queued/running/completed/failed/cancelled 语义；同步实现可直接返回终态，但不得把 queued 当 completed。内部可以使用 Protocol 适配持久层和 provider。

**实现要求**：

- 新增 `astero/agent/service.py`（或 `astero/agent/application/`）与必要的类型；对话入口不得直接依赖 HTTP 路由或具体 SQLite SQL。
- 从持久 store 载入该 session 的有限短期上下文，按 query 检索相关 knowledge/memory，形成上下文快照。每条知识或记忆保留 source ID；service 能向调用方返回本轮参考了哪些知识来源。
- 首版知识检索可用 SQLite FTS 或注入的 repository search；不要求向量 DB。无检索命中时正常调用 LLM。
- `remember` 必须是显式方法且绑定 session/user scope/source；不可在每轮自动保存识别文本为长期记忆。`forget` 只能删除指定 scope 下的指定记忆。
- 调用 provider 前后产生 observability stage event（或注入统一 recorder）：`context_load`, `knowledge_retrieval`, `llm_request`, `llm_response`，包含耗时、状态、attempt 和相关 ID。
- 把实际送交 provider 的完整请求内容和回复传给调试 archive recorder；通用 logging 只写 ID、耗时和错误类别。秘密和认证信息不得进入 context 或 archive。
- provider 超时、空响应、非法响应、存储故障和 recorder 故障都要返回明确失败；不向用户伪报成功。

**文件边界**：主要修改/新增 `astero/agent/`；允许添加 `tests/test_agent_service.py`。不要自行实现 capture、PCM 归档、SQLite schema、HTTP 服务器或 TTS。

**交付物**：领域 service、结果/上下文类型、provider/store/recorder Protocol、记忆查看与删除方法、依赖注入示例和离线测试。

**验收标准**：

1. 两轮同 session 可以从持久层恢复短期上下文；不同 session 不共享内容。
2. 查询命中的 knowledge/memory ID 和来源出现在结果中；无匹配也能正常回答。
3. 单轮识别文本不会自动成为长期记忆；显式 remember/forget 只影响目标 scope。
4. Fake provider 验证传入 prompt、trace_id、session_id 和持久上下文；真实 provider 不在本 task 的离线验收中伪装通过。
5. 验证成功、空响应、超时、provider 错误、持久层错误、recorder 错误及并发 session 的状态和耗时事件。
6. 测试日志不包含 prompt/reply 或密钥；测试 recorder 能获得完整调试请求/回复。

---

## AST10-05 — 本机常驻服务与生命周期 API

### 可直接领取的完整任务

**目标**：提供一个可长时间运行、可健康检查、可接收对话轮次、可查询任务状态、可优雅退出和重启恢复的本机 Astero Service。此服务属于 Project Astero 仓库，产品策略和应用编排不能依赖外部 agent runtime 承担。

**必要背景与既有组件**：

- 项目使用 Python；现有入口 `astero/live.py` 可以运行一轮/持续监听，但设备和模型初始化、对话、TTS 组装在同一入口中。`astero/orchestrator.Orchestrator` 已提供注入式单轮控制，接口为 `run_once(frames, cancel=...) -> TurnResult`，负责 wake/endpoint/ASR/conversation/playback 的半双工顺序。
- 本机语音数据路径为 Echo USB/ADB 9 通道 16 kHz `s24_3le` → `AudioFrame` → `MonoFrame` → wake/VAD/ASR → agent → Voice Bridge/TTS。
- Voice Bridge `POST /announce` 返回 HTTP 202/queued 只代表排队；必须等 `completed`/`failed`。它目前没有可靠的单任务取消接口。
- 服务只面向当前机器。若提供 HTTP API，只绑定 `127.0.0.1`/loopback；不暴露无鉴权远程端口。数据库、归档文件放在仓库外。

**API 固定语义**：

- `GET /health` 返回 `status`、`ready`、依赖组件状态、服务版本和存储/归档可写状态；健康但模型未就绪要区分 `ready=false`。
- `POST /v1/turns` 接收 `session_id` 和测试/客户端标识，可选择 `mode=voice` 或 `mode=text`。`voice` 使用正在运行的采集会话；`text` 只作显式诊断并须返回 `diagnostic_only=true`，不得用于声学验收。
- 返回 `turn_id`、`trace_id` 和 queued/running 状态；`GET /v1/turns/{turn_id}` 返回阶段、最终状态、阶段耗时、错误类别和 archive completeness。仅真实完成才是 completed。
- `GET /v1/sessions/{session_id}/turns` 查询该 session 轮次；知识/记忆管理 API 可委托 AgentService，但不得绕过其 scope 检查。
- 请求必须设置合理大小/超时；错误 JSON 和异常不能泄露环境变量、API key、原始 response body 或语音正文。

**实现要求**：

- 新增 `astero/service/` 服务运行时、配置、组装/依赖工厂和本机 API adapter。HTTP 使用标准库或项目已有依赖；不新增重量级依赖，除非说明必要性。
- 将语音设备捕获的创建/关闭放在明确生命周期组件内；capture disconnect 后状态失败且可重连/重新启动，不得误报 idle turn completed。
- 服务启动时验证配置、存储和归档目录可写，并检查 capture/model/LLM/TTS readiness；单个后端故障可诊断。
- 每个请求创建/接收 trace_id、turn_id，贯穿队列、语音阶段、AgentService、TTS 终态及返回；产生完整 JSON logging 和阶段耗时，依据单调时钟计算 elapsed_ms。
- 请求正文、回复、音频绝不进入通用日志；调试内容交由 archive 处理。日志要包含 stage、state、attempt、error_code、elapsed_ms 和相关 ID。
- 宕机/重启后从持久层识别中断的 queued/running turns，标为 `interrupted`/`failed` 并保留已写入归档；不得把半成品恢复为 completed。
- 提供优雅关闭：停止接受新 turn、通知当前组件取消或在安全边界结束、关闭 capture 并恢复 Echo mixer、关闭数据库句柄。

**文件边界**：新增 `astero/service/`，入口通过 `astero/__main__.py` 或新服务 CLI 暴露。可增加 `tests/test_service.py`。尽量不重写 `orchestrator.py` 的业务状态机；用 adapter 集成。

**交付物**：可运行的 `python -m astero.service` 或等价入口、loopback API、health/readiness、配置样例、生命周期说明和离线测试。

**验收标准**：

1. 无硬件情况下使用注入替身可启动/关闭服务并验证 health/readiness；本地端口只绑定 loopback。
2. submit 请求能查到相同 turn_id/trace_id 的阶段状态和事件；queued 不会伪报完成。
3. 服务重启后 session/turn/archive 索引可查询；中断 turn 不被伪造为成功。
4. 验证 capture、LLM、TTS、SQLite、archive 故障和 HTTP 无效请求均得到明确 status/error_code。
5. 验证优雅关闭会关闭采集资源并尝试恢复 Echo mixer；测试替身确认清理错误可见。
6. 验证各阶段 elapsed_ms、全程 total_elapsed_ms 与单调时钟一致，日志不含测试 prompt/reply/key。

---

## AST10-06 — 实机语音链路组装与归档接入

### 可直接领取的完整任务

**目标**：将现有 Echo 捕获、PCM 转换、唤醒/VAD、ASR、AgentService 和 TTS/Voice Bridge 接入常驻 Astero Service，并从采集开启到退出全量归档所有收到的音频。此 task 交付可实际运行的闭环 runtime，而不是只验证注入式 mock。

**必要背景与既有组件契约**：

- `AdbCapture` (`astero/audio/capture.py`) 以 context manager 管理 Echo USB/ADB 采集。`.frames()` 产出 16 kHz、9 通道、交错 `s24_3le` 的 `AudioFrame(timestamp_ns, sample_rate, channels, sample_format, sequence, pcm)`。关闭时会删除设备临时 helper，并恢复原厂 mixer 状态。
- `convert_frame` (`astero/audio/pcm.py`) 选麦克风通道 0–6，转换成 `MonoFrame`，保持时间戳/序号；Echo 参考通道 7–8 不能作为电脑扬声器的参考信号。
- `EndpointCollector` 使用替换式 VAD 和有界预录；`Orchestrator.run_once` 完成 wake→endpoint→ASR→conversation→playback 的半双工单轮。播放期间不读取麦克风，避免回复自触发。
- `VoiceBridgeClient` 的 `/announce` queued 不是完成，需等 `completed`/`failed`。TTS/播放器是在电脑输出端；旁边的 Echo 麦克风负责听到测试 TTS。
- 现有 `astero/live.py` 含真实模型适配器与入口，模型文件位于 `~/.local/share/astero/`，不应复制或提交权重、录音、设备序列号或密钥。

**实现要求**：

- 建立清晰的 `VoiceRuntime`/factory 负责创建依赖和运行 capture；设备生命周期不能藏在 API handler 中。service start 时打开 capture 并启动持续读取，shutdown/disconnect 时释放并恢复设备。
- 在任何 wake/VAD 过滤之前，将每个 `AudioFrame` 原样送入 archive writer；不能只保留唤醒后片段。每帧需对应通道/格式/采样率/时间戳/序号。转换后的 `MonoFrame` 也归档并关联原始 capture/turn。
- 持续监听期间的帧都在同一 capture session 下保存。每轮产生独立 turn ID/trace ID 并关联 capture ID；静音、唤醒前数据、无效/空语音轮次和断流都记录。
- 使用 AST10-03 的 trace/event API 记录 PCM 转换、wake、VAD/endpoint、ASR、context retrieval、LLM 请求/响应、TTS enqueue/playback poll/final status、API 返回。需在应有的开始/结束边界计时；每轮汇总阶段耗时和总耗时。
- 将 transcript、实际 LLM 请求/回复交给调试 archive；普通 logging 只输出 ID、状态、错误类别和耗时。任何 bearer token/密钥从日志和归档中剔除。
- 调用方通过 AgentService 和 TTS adapter，不在 live runtime 中直接拼 provider 请求或写 SQL。保持播放半双工；TTS pending 或状态未知期间不得重新开启 wake listener。
- 在 `live.py` 留向后兼容 CLI 或改为 service launcher，避免存在两套行为不一致的入口。提供明确的配置项：capture helper/hash、模型目录、麦克风通道/增益、LLM provider、TTS 地址、数据目录、debug archive on/off（当前本机调试默认 on）。

**文件边界**：主要新增 `astero/runtime/` 或 `astero/service/runtime.py`，修改 `astero/live.py` 只做委托；增加集成层测试。不得改动模型权重或设备系统分区。

**交付物**：可配置 runtime assembly、捕获/归档/trace 接线、服务入口集成、配置说明与可注入集成测试。

**验收标准**：

1. 替身集成验证每个 raw AudioFrame 在 wake/VAD 前被归档一次，断流时前序帧完整保留，capture cleanup 被调用。
2. 验证无 wake、无 speech、ASR/LLM/TTS 失败、超时和取消均具有准确状态、trace 和 archive 结果。
3. 验证真实唤醒/端点/ASR/LLM/TTS 使用同一 turn/trace IDs，输出全程耗时及各阶段耗时；不把 mock 结果当真实成功。
4. 在已连接 Echo 的本机手工验收时，确认从采集打开到退出的原始帧都在仓库外可回放；Echo mixer 恢复正常。
5. 服务重启后可查询 archive/turn metadata；异常退出的 capture/turn 标为 incomplete/interrupted。
6. 不提交音频、转录、设备标识、model weights 或 secrets；本 task 的真人语音准确率不作为通过条件。

---

## AST10-07 — 本地 TTS 声学回环验收驱动器

### 可直接领取的完整任务

**目标**：实现一个可重复的本机验收驱动器：通过本地 TTS 接口播出唤醒词 “Astero” 和指定测试指令；Echo 放在电脑扬声器旁边，由真实麦克风采集发音；Astero 常驻服务处理后经本地 TTS 播报回答。驱动器收集轮次 ID、识别文本、阶段耗时、归档索引和最终播放状态。

**必要背景**：

- 自动声学回环是设备级闭环，但输入是合成语音；它验证声学链路、服务编排、ASR 和返回播放，不能冒充真人唤醒或真人听感验收。
- 本地 Voice Bridge `POST /announce` 可发送文本并播放 TTS；HTTP 202/queued 只是排队。客户端必须等待 `/announcements/{id}` 的 `completed` 或 `failed`。Voice Bridge 地址应从配置读取，默认本机 loopback。
- Echo 捕获由 Astero service 管理；驱动器不得直接操控 Echo mixer/PCM，不得绕过服务向 orchestrator 注入文本或 PCM。
- Project Astero 已要求调试期间全量保存音频和每阶段 logging；驱动器必须利用相同 trace/turn/capture 记录，而不是自行另存一个无法关联的录音。

**实现要求**：

- 新增 `astero/validation/` CLI，如 `python -m astero.validation --text 'What is two plus two?' --session astero-e2e`。
- 前置检查 Astero service readiness 与本地 TTS 健康；服务未就绪或 TTS 不健康时停止，不发起语音轮次。
- 通过配置的 TTS adapter 先播报短暂停顿可检测的 “Astero”，随后播报指令；两个播报必须可分别关联。等待可配置的 wake/endpoint 时间窗口，然后从 service API 查询同一轮的终态。
- 若 TTS 与 service 不能提供回执关联，使用驱动器生成的 correlation ID 关联本地 TTS task、service turn 和 trace；不要靠日志文本匹配。
- 每次运行输出一份结构化结果：环境摘要（不含设备序列号/密钥）、输入提示文字、TTS task IDs/status、turn/trace/capture ID、ASR transcript、LLM/provider 状态、回复摘要（完整正文只在 debug archive）、各阶段 elapsed_ms、总耗时、archive complete、错误类别。
- 支持重复固定样例和 JSON/JSONL 输出；允许调用者提供已知测试用例。默认不自动播放高音量或长文本。
- 失败时给出失败阶段；服务返回 accepted/queued 不能算完成；Bridge `completed` 代表播放器结束，真人听感由单独人工验收记录。

**文件边界**：新增 `astero/validation/` 和相应离线测试；不得改动 Echo 设备协议、模型实现或 Bridge 服务。

**交付物**：CLI、TTS/service clients、结果 schema、无设备时的接口替身测试、实机操作说明和验收记录模板。

**验收标准**：

1. 用假 TTS/service 验证健康检查、两段 TTS 提交、关联 ID、服务轮次查询、completed/failed/timeout 分支和结果 JSON。
2. 验证 TTS queued 与 service accepted 不会误报为 e2e completed。
3. 实机运行一次合成 Astero + 已知命令：Echo 麦克风获得音频、服务识别文本符合预期、真实授权 LLM 回复、TTS 返回播放 completed、archive 完整且阶段 timing 可查。
4. 结果中清楚标记 `input_kind=synthetic_tts`；不得将自动声学回环写成真人语音验收。
5. 再次运行可通过 session/trace 查询到各轮诊断，不需要人工 grep 私人正文日志。
6. 不在 Git 中保存任何录音、LLM 凭据、完整私人语音内容或设备序列号。
