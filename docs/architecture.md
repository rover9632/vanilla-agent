# vanilla-agent 架构

> **本文与 `plan.md` 的分工（drift 规则）**：`docs/plan.md` 是决策记录与进度的唯一事实来源（ADR、里程碑、TODO 状态）；本文是**系统稳定描述** —— 架构形态、契约、协议、不变量、配置面。冲突时：决策依据以 plan 的 ADR/§13 为准，字段级实现细节以代码与生成的 schema 为准，本文只允许总结、不允许引入新决策；契约/协议变更必须先改 plan，再同步本文。配置字段的权威清单在 plan §5 与 `app/config/schema.py`（T1.1），本文只述角色与合并规则。
>
> 提取自 plan r3.6（§3 / §4 / §5 / §6 / §7 / §8）；各条决策的理由见 plan §2 与 §13。

---

## 1. 定位与形态

一个 **framework-free** 的轻量 AI agent 服务：不引入 LangChain / LlamaIndex 等任何 agent 框架，直接在 provider SDK（OpenAI 兼容 Chat Completions，ADR-001）之上实现多轮 tool-use 主循环，以小 FastAPI 服务的形态暴露。

三条形态约束（ADR-014 / ADR-015 / §8.4）：

- **单进程 asyncio**：run 监督在进程内；重启靠 `event_log` + `interrupt` 恢复；启动检测 `--workers>1` 即拒绝。
- **config-as-code**：装配面（tools/skills/agents/MCP/plugins/permissions/hooks）全部是随项目版本管理的仓库文件；服务端不存在配置写入路径，没有 HTTP reload 端点，没有 admin 角色。一句话原则：**"agent 能做什么"进 git，"agent 做了什么"进 SQLite。**
- **service 跑宿主机**：service 经 `docker` CLI 驱动沙箱容器；`docker-compose.yml` 只装 web。**不挂载 `/var/run/docker.sock`**（socket == 宿主 root，service 被攻陷即可起特权容器读走全部用户数据）。

v1 明确不做（防范围漂移，完整清单见 plan §1.2）：水平扩展、向量记忆/RAG、web search、IDE/CLI/移动端、k8s、gVisor/Firecracker 级强隔离、plan-mode worktree 隔离、per-user 能力定制、管理控制台。

## 2. 总览

```
仓库版本管理（运维/评审唯一写入路径）              运行时进程（只读）
┌───────────────────────────────────┐   ┌──────────────────────────────────────────┐
│ config/                           │   │ ConfigLoader → ConfigSnapshot (immutable) │
│   settings.yaml   tools.yaml      │──▶│   version, sources[sha256], 合并后视图     │
│   permissions.yaml hooks.yaml     │   │   ├ ToolRegistry      ├ SkillIndex        │
│   mcp.yaml        plugins.yaml    │   │   ├ AgentIndex        ├ HookBus           │
│   skills/<slug>/SKILL.md          │   │   ├ MCPPlan(+已连工具) ├ PermissionsPolicy│
│   agents/<name>.md                │   │   └ 原子替换；run 持有旧引用 → 不撕裂      │
│   plugins/<name>/.vanilla-plugin/ │   └──────────────────────────────────────────┘
└───────────────────────────────────┘                     ▲ kill -HUP <pid>（运维触发，无 HTTP 端点）

                         ┌────────────────────────── web/ (Next.js) ─────────────────────────┐
                         │  /login  /chat(thread)  [ToolCard] [ApprovalDialog] [TracePanel]  │
                         └───────▲───────────────────────────────────────────┬───────────────┘
                    REST/JSON    │        SSE (event stream, Last-Event-ID)   │ resolve(once/session)
┌────────────────────────────────┴───────────────────────────────────────────▼────────────────────────┐
│ service/ (FastAPI, 单进程 asyncio)                                                                     │
│  api/  auth(register,login) │ threads │ runs │ messages │ stream │ interrupts │ usage │ capabilities │
│    └───────────────────────────────────────┬─────────────────────────────────────────────────────────│
│  agent runtime (RunManager → RunTask)      │                                                         │
│    loop: ContextBuilder(snapshot) → ModelClient → 解析流 → tool_calls?                                 │
│              │                     │             │                                                   │
│              │                     │             ▼                                                   │
│   ┌──────────┴────────┐   ┌────────┴────────┐  ToolDispatcher                                        │
│   │ ContextBuilder    │   │ ModelClient     │   ├ PermissionEngine ← snapshot.permissions            │
│   │  system prompt    │   │  (openai SDK)   │   │   + SessionGrantStore(内存 TTL)                    │
│   │  skills index     │   │  + Scripted fake│   ├ HookBus ← snapshot.hooks                           │
│   │  tool catalog     │   └────────┬────────┘   └ ToolRegistry ← snapshot.tools                      │
│   │  compaction       │            │ deltas                │ results/artifacts                       │
│   └───────────────────┘            ▼                       ▼                                         │
│                             EventRecorder ──→ event_log(SQLite) ──→ EventStream(SSE fan-out)          │
│                                    ▲                                                                │
│                    interrupt 表 ───┘  (awaiting_input；跨重启可 resolve)                               │
│  workspace/ (per-user 目录 + 路径约束)      sandbox/ (docker exec | local | fake)                     │
└──────────┬────────────────────────────────────────────────────────────────────────────┬─────────────┘
           │ SQLite (WAL)：只存运行时事实                                                  │ docker CLI
   ./data/vanilla.db, ./data/artifacts/             container per workspace（默认无网络）
                          -v ./data/workspaces/<user>:/work  --network none（放宽才建 va-net-<user>）
```

## 3. 主循环不变量

每条都有对应测试（plan §10）；这是全系统行为的骨架约束。

1. **单一 writer**：run 的消息与事件只能由该 run 的 task 产生；subagent 事件带 `source_run_id` 汇入父 thread 流。
2. **事件单调递增 seq**：`event_log.id`（全局 rowid，SSE cursor）+ `(run_id, seq)`（run 内有序，重放用）。
3. **可取消**：`cancel_requested` 检查点在每个 await 边界；模型流与 `docker exec` 都要响应 `CancelledError`。
4. **中断不丢状态**：`awaiting_input` 的 run 只依赖 DB 行，进程重启后仍可被 resolve 并继续。
5. **输出不可信**：tool 结果、`http_fetch`、MCP 返回一律标注 untrusted 并做注入加固。
6. **配置快照不可变且被 run 引用**：run 全程使用启动时的 `ConfigSnapshot`；reload 只影响新 run。不存在"运行中发现配置不一致"的分支。
7. **服务端零配置写入**：不存在写 `config/` 的代码路径；进程对 `config/` 只读挂载或属主非服务账号。

## 4. 配置面与装配

### 4.1 文件与角色

| 路径 | 角色 |
|---|---|
| `config/settings.yaml` | provider（`base_url`/`api_key`/`model_name` + 通用参数）、limits（`max_turns`、预算、`global_hourly_tokens`）、sandbox 限额与网络档、registration 开关、permissions 默认 mode |
| `config/tools.yaml` | 内置工具 enabled、timeout、`max_result_bytes`、参数上限、`http_fetch` 域名 allowlist、bash 硬超时 |
| `config/permissions.yaml` | `default_mode` + `policy`（硬约束，如 disallow bypass）+ mode preset 覆写 + `rules[]` |
| `config/hooks.yaml` | event → matcher → command/python、timeout、`on_timeout` |
| `config/mcp.yaml` | MCP server 清单（stdio / streamable HTTP）、`requires_approval`、`enabled`、connect 超时 |
| `config/plugins.yaml` | 插件 name/path/`enabled`/`trusted`（enable 与 trust 分离） |
| `config/skills/<slug>/SKILL.md` | frontmatter（name/description/when_to_use/allowed_tools/model/version）+ 正文；附属文件同目录 |
| `config/agents/<name>.md` | subagent 定义（tools/exclude_tools/model/max_turns/permission_mode/timeout_s）+ 系统提示正文 |
| `config/plugins/<name>/` | manifest `.vanilla-plugin/plugin.json` 声明贡献的 skills/agents/mcp/hooks/tools/pythonEntryPoints |
| `config/*.local.yaml` | 本地部署覆盖（可含明文 secret），不入库，仅 `CONFIG_ALLOW_LOCAL=1` 生效 |

### 4.2 加载与合并规则

- `${env:VAR}` 展开：secret/path 一律经环境变量引用；配置文件不得含明文密钥（`make config-check` 扫描，含未跟踪的 `*.local.yaml`）。禁止 `${file:}`。
- 路径全部相对 `REPO_ROOT` 解析，禁止越出 `config/`。
- 校验是**收集式**的：全部问题一次报全（`ConfigIssue`），不在第一条 fail-fast；`error` 非空拒绝启动（除非 dev 开关）。
- 合并优先级：`settings.yaml` 默认 < 插件贡献 < `*.local.yaml` 覆盖（门控）。permission 规则取**所有来源并集，deny 永远赢**；插件不得声明 `bypass`、不得覆写 `permissions.yaml` 的 policy 段。同名冲突（skill slug / tool name / mcp server / hook matcher）一律 error，不允许静默覆盖。
- 部署面 env（`OPENAI_API_KEY`、`DATA_DIR`、`CONFIG_DIR`、`REPO_ROOT`、`SANDBOX_IMAGE`、dev 开关等）收口在 `app/core/env.py` 白名单；**能力参数一律不从 env 读**（ADR-015 / plan T0.2）。

### 4.3 快照与 reload

`ConfigSnapshot`：单调 `version` + `sources[]`（每个来源文件 sha256）+ 合并后视图 + issues。`ConfigStore.swap()` 先构建并校验新快照、全绿才**原子替换**；失败保留旧快照。run 记录 `config_version` 以便复现。

reload 触发方式：**`kill -HUP <pid>` 或重启**。没有 HTTP reload 端点（开放注册下任意登录用户可触发全局快照替换 = churn/DoS 面）。运维出口：`python -m app.config check|dump|report`、`scripts/*.py`；变更配置 = 改文件 + 代码评审 + reload。

## 5. 数据模型边界

SQLite（WAL + `busy_timeout` + 单写者 executor，ADR-003），手写迁移、stdlib `sqlite3`，14 张表。**只存运行时事实**（users/sessions/workspace/thread/message/message_part/run/event_log/tool_call/interrupt/artifact/usage/compaction/audit_log）；不存在任何配置类表、不存在 at-rest 加密主密钥。表级列清单见 plan §6。

关键语义：
- `event_log.id` = 全局游标（SSE `Last-Event-ID`）；`(run_id, seq)` 提供 run 内重放。
- `interrupt`：中断的**事实**入库（含跨重启可 resolve）；中断的**效果**不入库 —— `session` 授权只存在于进程内 TTL 表（`SessionGrantStore`），重启/reload 即失效，因此"运行时写规则"不会变成隐式配置。
- `tool_call.rule_source` 记 `file:line` 只作审计证据，不指回可写规则行。

## 6. 事件与流式协议

### 6.1 Event 信封

所有对外流式（SSE、subagent 转发、trace inspector、调试）共用同一模型（ADR-002 事件溯源）：

```python
class Event(BaseModel):
    v: Literal[1]
    seq: int                # run 内单调
    cursor: int             # event_log.id = SSE Last-Event-ID
    ts: datetime
    thread_id: UUID
    run_id: UUID
    source_run_id: UUID | None   # subagent 事件转发时指向子 run
    type: EventType
    payload: dict           # 全部过 secret redactor（§8.6）
```

### 6.2 EventType

值为点分小写，即 SSE 的 `event:` 字段；**发布后不改名**（前端 reducer 按 value 匹配，TS 类型由后端 schema 生成）。必须写成 `MEMBER = "value"` 的 StrEnum（代码生成的 schema 依赖 value）。

| 组 | 值 |
|---|---|
| run | `run.started` `run.status` `run.finished` `run.failed` `run.cancelled` |
| 消息 | `message.started` `text.delta` `thinking.delta` `message.completed` |
| 工具 | `tool_call.started` `tool_call.args_delta` `tool_call.completed` `tool_call.failed` |
| 权限/中断 | `permission.requested` `permission.resolved` `interrupt.raised` `interrupt.resolved` |
| hook | `hook.started` `hook.finished` `hook.blocked` |
| subagent | `subagent.started` `subagent.finished` |
| 其他 | `context.compacted` `usage` `error` `heartbeat` |

没有 `CONFIG_CHANGED` 一类事件 —— 配置变更走 SIGHUP，快照版本可从 `run.config_version` 与运维 CLI 对账。

### 6.3 SSE 机制

- `GET /threads/{id}/stream?run_id=`：`event:` / `id: <cursor>` / `data: <Event JSON>`；连接时按 `Last-Event-ID`（或 `?after=`）先重放缺口再进入 live fan-out（每 thread 一个 EventHub，多订阅者）。
- 15s `: ping` 心跳；`Cache-Control: no-cache`、`X-Accel-Buffering: no`。
- **断开 ≠ 取消**：客户端断线不中止 run；取消必须显式走 `POST /runs/{id}/cancel`（幂等）。慢客户端的丢弃策略不得反压阻塞 run。
- `POST messages` 立即返回 202 + `run_id`（`client_msg_id` 幂等）：run 生命周期与流解耦，这是 HITL 长等待与断线重挂的前提。
- 前端按 `(run_id, seq)` 去重（cursor 重放语义的对称要求）。

### 6.4 模块边界契约（plan §4 摘要；规范性代码形态在 plan，实现落地后以代码为准）

- **ModelClient**：`async def stream(req: ModelRequest) -> AsyncIterator[ModelEvent]`；`ModelEvent` = TextDelta | ReasoningDelta | ToolCallDelta | UsageDelta | Stop(reason)。实现：OpenAI 兼容 client（chunk 归一化，T4.2）+ `ScriptedModelClient`（注入 delta/错误/取消，使 loop 完全离线可测）。
- **ToolSpec**（frozen）：name（全局唯一；mcp/plugin 带命名空间前缀）/description/input_schema（发给模型的 JSON Schema）/args_model/namespace ∈ {builtin, mcp, plugin}/kind/read_only/destructive/concurrency_safe（可并行）/timeout_s（`tools.yaml` 只能收紧、不能突破硬上限）/max_result_bytes/permission_target(args) → 权限模式串/handler → `ToolResult{ok, content, artifact_id?, metadata}`。
- **HookOutcome**：action ∈ {continue, block, modify, inject} + stop + updated_input/additional_context；合并规则 block 优先、modify 串行；无 hook 时零开销。
- **Sandbox protocol**：`ensure(ws) / exec(argv, cwd, env, timeout_s) / release(ws)`；实现 DockerSandbox（生产）/ LocalSandbox（仅 dev，显式 env 开关）/ FakeSandbox（测试断言 argv/env/timeout）。
- **PermissionResult**：decision(allow|ask|deny) + rule + `rule_source(file:line)` + mode + reason + `suggested_grant(once|session)` + `suggested_rule_yaml`（文本建议，人工提交评审 —— 系统不自动写配置）。
- **Interrupt**：type ∈ {permission, question, plan_approval, elicitation}；prompt 为前端渲染所需结构化内容；timeout_at + status(pending|resolved|expired|cancelled)。
- **MCP 命名**：工具注册为 `mcp__<server>__<tool>`；只用官方 SDK 的 client 面，不自建 MCP server；elicitation 映射为 interrupt，sampling 明确 unsupported（返回清晰错误而非静默）。

## 7. HTTP API

前缀 `/api/v1`。**鉴权豁免是显式白名单**：免 session 的端点只有 `POST /auth/login`、`POST /auth/register`（开放注册，受 per-IP 限速；`registration.open=false` 可关）、`GET /healthz`。其余全部要求 session cookie，**包括 `/auth/me`、`POST /auth/logout`、`POST /auth/password`**。

**没有 `/admin/*`**（无 admin 角色，ADR-004）：运维动作全部走 shell（SIGHUP / `python -m app.config` / `scripts/*.py`）；用户侧完整配置面不外泄，只留脱敏视图。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` `/auth/login` `/auth/logout` `/auth/password` | 注册开放、无首账号特权；`status` 仅运维 CLI 可改 |
| GET | `/auth/me` | 当前用户 + 能力位（也是 CSRF token 签发点之一） |
| POST | `/threads` | `model`/`permission_mode` 必须 ∈ snapshot 允许集合 |
| GET | `/threads` `/threads/{id}` `/threads/{id}/messages` | 列表（游标分页）/详情/历史 |
| POST | `/threads/{id}/messages` | 202 + `{run_id}`；`client_msg_id` 幂等 |
| GET | `/threads/{id}/stream?run_id=` | SSE（§6.3） |
| GET | `/runs/{id}` `/runs/{id}/events?after_seq=&limit=` | trace inspector 分页读 |
| POST | `/runs/{id}/cancel` | 置 `cancel_requested`；幂等 |
| POST | `/threads/{id}/steer` | 运行中追加引导，下一 turn 边界注入 |
| POST | `/threads/{id}/compact` | 强制压缩上下文 |
| GET | `/interrupts?status=pending` | 待办审批角标 |
| POST | `/interrupts/{iid}/resolve` | `{response, scope?: once|session}`；resolve 必须带用户身份 |
| GET | `/capabilities` | 用户视角：可调用的 tools/skills/agents（名称+描述+是否需审批）；**不含**来源 hash、MCP 命令行、hook matcher、插件路径 |
| GET | `/mcp/status` | 已脱敏：server 名称、连接状态、工具数、last_error |
| GET | `/usage/summary` | 当前用户用量与预算余量（无 `?user_id=` 变体 —— 没有能看别人数据的角色） |
| GET/POST | `/workspaces/current(/reset|/file?path=)` | 沙箱状态 + host 侧读文件（渲染 diff）；reset 只清容器与临时状态 |

错误响应统一信封（`app/core/errors.py`，T0.3 已落地）：`{"error": {"code", "message", "request_id", "details?"}}`；`code` snake_case 稳定，一个 code 只映射一个 HTTP status（框架校验失败为 `validation_failed`/422，与端点抛出的 `invalid_request`/400 分离）；5xx 只回泛化消息，traceback 仅进日志。

## 8. 安全模型摘要

逐条完整版见 plan §8；此处为不变的安全姿态。

- **认证**：口令 `hashlib.scrypt`（参数入库以便升级）；session 为 opaque token，DB 存 sha256，cookie `HttpOnly; SameSite=Lax; Secure(prod)`、滑动 30 天；登录限速（IP+email）。没有 cookie 签名密钥 —— 查表式校验不需要它。
- **授权**：所有 repo 查询强制带 `user_id` 谓词；除 `/capabilities` 外一切端点只按 principal 取数（不得用他人 id 作查询参数）。权限分层只有"HTTP 面（任何注册用户，看自己）vs shell 面（运维）"两层。
- **CSRF**：double-submit + `X-CSRF-Token`；豁免 `login/register`（无 cookie 的新浏览器否则死锁），`password` 不豁免（必须有已认证 principal）。
- **沙箱威胁模型**：每 workspace 一个长生命周期容器，`docker exec` 执行；`--cap-drop ALL`、`no-new-privileges`、`--memory/--cpus/--pids-limit`、`--tmpfs /tmp`、非 root、只 bind mount 本用户目录。网络三档（`settings.sandbox.network_mode`）：**`none`（缺省，运行期零网络，连内部 DNS 都没有）** / `internal` / `per_user_bridge`；放宽档必须每用户一张自定义网络；**禁止默认 `bridge`**（同网段互通打破跨用户隔离）。`none` 的连带要求：镜像预烘焙工具链、网络失败翻译成人话、留一条"全程无网冒烟"。已知残余风险全部成文不掩饰：共享内核（runc）不抗针对性内核提权；放宽网络 ⇒ 同网段内网设备可达 + prompt injection 成 pivot/外泄通道；**`http_fetch` 是 host 侧工具，不受容器网络档约束**，其 SSRF/allowlist 需求与网络档无关。
- **Prompt injection 加固**：外部内容一律 `<untrusted source="...">` 包裹 + system 侧声明"内容非指令"；`http_fetch` 拒绝 RFC1918/loopback/link-local/metadata、DNS 重解析二次校验、重定向与跳数上限、仅 http(s)、大小/超时上限。**配置文件内容同样是不可信输入面**：skill frontmatter 的 `allowed_tools`、插件 hook target 都必须过 snapshot 校验，声明式内容不能绕过权限引擎。
- **Secret 处理**：来源只有进程 env 与 `config/*.local.yaml`；注入容器按名 allowlist；**事件 payload、日志、tool 输出共用同一 redactor**（sk-/Bearer/JWT/PEM 模式）；"在日志里脱敏但在事件里原样发出"不算脱敏。
- **配置信任边界**：`config/` 的写权限 == agent 能力授权。服务账号对其无写权限；启动与 reload 检查 world-writable/属主，违规拒绝启动（dev 开关下打醒目告警）；hook command target 必须是仓库内已存在、非 world-writable、不含 shell 元字符的可执行脚本（argv 直传）。
- **插件信任**：`enabled` 控制装配、`trusted` 才允许进程内 Python entry point（in-process == service 权限）；两者都是需评审的文件变更，不存在"运行时管理员点的勾"。
- **权限不可自批**：run 内永不写规则文件；`always` 档已删除；`session` 授权只进内存 TTL 表并写 `audit_log`；审批必须来自带用户身份的 resolve 请求。
- **配额闸门（开放注册 + 全局单 key ⇒ 硬需求）**：per-run（max_turns/墙钟/单工具输出字节）、per-user（`max_concurrent_runs`=1 默认/`daily_token_budget`）、**global 两道硬闸**：`global_hourly_tokens` 账户级熔断（N 个用户各自不超限、合计也能烧穿同一把 key）与 `sandbox.max_live_containers`（核定原则 `containers × per-container memory ≲ 宿主可用内存的 1/3`，默认 8 须在部署时按实际资源复核）。超限返回 429 排队提示，不静默丢弃。
- **开放注册护栏**：注册 per-IP 限速 + 每日上限、`registration.open` 一键关、新账号取最保守预算档且异常用量写审计可 CLI 一键 disable、`/capabilities` 不外泄配置结构。**残余风险由部署者显式确认**：公网监听 + `open=true` 时 `config-check` 报 error。

## 9. HITL 与权限语义（运行面）

- 统一 `interrupt` 机制（权限审批 / `ask_user` / plan 审批 / MCP elicitation 共用）：写库 → `INTERRUPT_RAISED` → 进程内 Future → resolve 唤醒；timeout→expired；崩溃重启后 pending 仍可 resolve。
- 权限决策效果优先（deny > ask > allow，不看特异性）× mode preset × 工具 `read_only`/`destructive` 属性；`PermissionResult` 携带 `rule_source(file:line)`、`reason`、`suggested_grant(once|session)` 与 `suggested_rule_yaml`（供人工提交评审 —— 系统不自动写配置）。
- `session` 授权存活边界 = 同一 thread 内、进程重启或 reload 即失效（无额外 TTL）。
