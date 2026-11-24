# config/ —— 能力装配目录（ADR-015）

> 一句话原则：**"agent 能做什么"进 git，"agent 做了什么"进 SQLite。**
> 本目录是服务进程的唯一能力来源；进程对它**只读**（§8.7 信任边界），
> 不存在任何服务端写配置的 API、也没有能改它的应用内角色——改配置 =
> 改文件 + 代码评审 + `kill -HUP <pid>`（运维动作走 shell，见 §7）。

## 每个文件管什么

| 文件 | 管什么 | 模型（校验真源） |
|---|---|---|
| `settings.yaml` | provider（D1 只三字段+通用参数）、运行/预算限额（§8.10）、沙箱限额与网络档（D8）、开放注册护栏（§8.11）、权限模式集合 | `app/config/schema.py::Settings` |
| `tools.yaml` | 内置工具 enabled / 超时 / 结果字节上限（硬上限是代码常量，只能收紧） | `ToolConfig` |
| `permissions.yaml` | 默认模式、`policy.disallow_bypass` 硬约束、按模式的 preset、allow/ask/deny 规则（ADR-009） | `PermissionsConfig` |
| `hooks.yaml` | 事件 × matcher → command/python hook（ADR-010，装配随 T12） | `HookConfig` |
| `mcp.yaml` | MCP server 清单（ADR-007，连接随 T11）；工具目录只活在内存里 | `MCPConfig` |
| `plugins.yaml` | 插件 `enabled` / `trusted`（ADR-011，装配随 T12） | `PluginsConfig` |
| `skills/<slug>/SKILL.md` | 技能：frontmatter（name/description/when_to_use/allowed_tools/model/version）+ 正文按需注入 | `SkillFrontmatter` |
| `agents/<name>.md` | 子代理定义：frontmatter + 系统提示正文 | `AgentFrontmatter` |
| `plugins/<name>/` | 插件资源包（`.vanilla-plugin/plugin.json` + skills/agents/hooks/mcp） | T12.4 |

写文件前必读的三条规则：

1. **secret 只走 `${env:VAR}`**。配置文件（含 `*.local.yaml`）里出现明文
   密钥是事故不是风格问题——`make config-check` 有扫描（§8.6/T1.6）。
   未设置或设置为空的变量都会让加载失败并报出**变量名**（不回显值）。
2. **路径一律相对仓库根**，且不得越出 `config/`（`..`、绝对路径直接拒，
   T1.4 收束检查）。
3. **`*.local.yaml` 是部署侧覆盖**，不入库、仅当 `CONFIG_ALLOW_LOCAL=1`
   生效；语义是**整文档替换**（不做字段级深合并），未知目标名报错。

## 新增一个能力 = 改哪几个文件

以下清单与 `docs/plan.md` §5/§9 对齐；"reload" 一栏全部是
`make config-check` → 提交评审 → `kill -HUP <pid>`（SIGHUP 由进程重建
快照，在跑的 run 持旧快照不受影响，不变量 6）。

| 想加什么 | 改动 | 关联校验 |
|---|---|---|
| 启用某个内置工具（如 `write_file`） | `tools.yaml` 置 `enabled: true`；通常还要在 `permissions.yaml` 配它的审批规则 | T6.1（注册表）、T7.x（引擎） |
| 一条新权限规则 | 只在 `permissions.yaml` 加（或走前端审批后的"建议片段"人工粘贴，T7.5）——运行时永远写不了这里 | T7.1 grammar、T7.3 溯源 |
| 一个新技能 | 新建 `skills/<slug>/SKILL.md`（frontmatter 不许未知字段）；`allowed_tools` 只能引用已启用工具（收窄可以，越界在 config-check 报 error，T9.4） | T9.1–T9.4 |
| 一个新子代理 | 新建 `agents/<name>.md`；`tools`/`exclude_tools`、`permission_mode` 需 ∈ 快照允许集（T10.1） | T10.1–T10.3 |
| 一个 MCP server | `mcp.yaml` 加条目；secret 用 `${env:}`；名字会进 `mcp__<server>__<tool>` 命名空间，不得与现有冲突 | T11.1–T11.5 |
| 一个 hook | `hooks.yaml` 加条目；command target 必须是本目录内、非 world-writable 的脚本（§8.7）；python callable 仅 trusted 插件 | T12.1–T12.3、T1.7 |
| 一个插件 | 放入 `plugins/<name>/`，在 `plugins.yaml` 声明 enabled/trusted；资源同名冲突跨源报 error（T1.4） | T12.4–T12.6 |
| 放宽松箱网络（D8） | `settings.yaml` `sandbox.network_mode` 改 `internal`/`per_user_bridge`——每一次开网络都要有记录地评审（§8.4 内网可达代价） | T6.4 三条硬断言 |
| 沙箱镜像加依赖 | 改 `sandbox/image/Dockerfile` + `make sandbox-build`（运行期无网，装不了包，T6.3） | T6.3、T13.7③ |

## 自检

```bash
make config-check   # 校验 + 明文密钥扫描 + 引用完整性（T1.6 起为硬门槛）
make lint           # 代码与静态门（含 config-check）
```

改完没生效的排查顺序：`kill -HUP` 是否发给 service 进程 → 日志里的 reload
结果 → `python -m app.config dump`（T1.8）看 version 与各来源 sha256；
run 表里的 `config_version` 能回答"那一轮用的哪套能力"。

## 当前快照的缺省姿态（本目录实际值，别被"以后再说"骗了）

- `sandbox.network_mode: none`（D8）：容器运行期零网络；
- `registration.open: true`：公网监听时 config-check 会报 error（§8.11），
  部署者必须显式处置；
- 写类工具（`write_file`/`edit_file`/`delete_file`/`move_file`）关闭；
- `http_fetch` 启用但 `allowed_domains: []` = 全拒绝（fail closed）；
- `policy.disallow_bypass: true` 且 `allowed_modes` 不含 bypass。
