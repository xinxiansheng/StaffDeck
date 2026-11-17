# 平台构建与增量说明

## 最新：会话分叉与平台图片输入（2026-11-17）

基于 `55a3ce05`，新增两项 Public API 能力，继续保留之前的隔离、原件导入、拒绝审计与 SOP 失败处理。平台业务界面和开关由平台团队接入，本轮未部署。

- `POST /api/v1/agents/{agentId}/sessions/{sessionId}:fork`：携带 `Idempotency-Key` 和 `{"beforeMessageId":"用户消息ID"}`。从该消息之前的真实检查点复制消息、槽位、技能栈、任务进度与工作区，返回 `sessionId`、来源和 `copied`。原分支不变；外部动作、运行租约、回调与长期记忆不复制。人工 / 外部等待需在新分支重新确认。源会话执行或排队时拒绝，同键重试返回同一新会话，恢复失败不暴露半个会话。
- `POST /api/v1/agents/{agentId}/images`：multipart `files`，PNG / JPEG / WebP，每张最多 4 MiB，一次最多 8 张。返回 `pimg_...` 引用；创建运行时传 `attachments:[{"id":"pimg_..."}]`。引用绑定租户、用户和员工，7 天过期；执行前再验证。原图进入模型和本次工作区，模型明确拒图时失败为 `IMAGE_INPUT_UNSUPPORTED`，不再删除图片后按文本成功返回。

两接口使用现有 `sessions:write`，运行使用 `runs:create`；平台委托还需 `platform:delegate` 和既有 `X-Platform-Subject`。无通用 URL 抓取或网络放宽。图片暂存到期不删除已使用的会话 / 工作区副本。

StaffDeck 启动时自动创建 SQLite 表 `harness_session_checkpoints`，不改旧消息，不为旧消息伪造历史。保留 30 天、最近 50 个检查点、每会话 512 MiB，先到为准；单份 64 MiB（状态 JSON 最多 8 MiB），最多 2,000 条工作区路径。过期、超限、无历史或不安全文件均返回明确错误；聊天本身仍可继续。标准每小时 Public API 维护清理过期快照与图片暂存；后台维护被关闭时须另行安排。SQLite 空闲页供复用，数据库文件不会因此立即缩小。

**789 项 StaffDeck + 1 项真实 Java HTTP 本机验收通过**，零失败 / 错误 / 跳过。真实 Node 验证了分叉后旧文件 / 旧 SOP 进度继续执行（包括重启引擎）、新旧分支隔离，以及图片真正进入规划 / 执行请求和拒图明确失败。使用脚本模型；测试服模型能否看图、真实业务交付和目标 Linux 镜像仍待现场验收。

继续使用 `Dockerfile.platform` 从固定源码构建，无新增运行依赖。升级按既有流程备份 SQLite 和数据卷，保留 APP_SECRET。回退到 `55a3ce05` 前关闭新入口、排空运行，保留新增表和数据；旧版本不会维护新式检查点。平台契约、示意图与完整交接位于平台仓库 `docs/contracts/PRD-007/staffdeck-runtime-extensions.md` 和 `docs/features/PRD-007/F-I3/runtime-extensions.md`。

## 前一增量：SOP 失败状态与真实错误回传（2026-11-17）

基于 `7dc871b3`，继续保留原件导入、拒绝审计和 member 网关授权修复。本次只改 StaffDeck 运行处理；不改测试服工具白名单、SOP 路由、模型或数据库结构。

- 模型提交 `submit_step_result status=failed` 时，保留本步失败及实际工具错误；不因前一步已经完成而改成 `action_budget` 或复用前一步过渡回复。没有工具错误时使用 `TASK_EXECUTION_FAILED`。
- 不允许的转人工继续拒绝为 `HANDOFF_NOT_ALLOWED`。`harness_turns`、任务帧、逻辑 AgentLoop 与 Public API job 均记录失败；保留本步生成的回复。
- Public API 在失败流没有 `complete` 事件时，只读取同租户、同会话、同 job 的持久化终态回执。`error_json` / 运行详情的 `error` 带 `code`、`message`、`reply`、`causes`；转人工被拒时可同时查到先前工具的 `NOT_ALLOWED`。确定性拒绝不可重试；同一回合重放不会重复执行。
- 回归中修复上一增量引入的参数修正问题：存在审计回执时，仅 `denied`、明确 `executed=false` 且没有写声明的参数错误允许修正。已执行或结果不确定的写调用不能据此重试。

旧基线两条真实 Public API → member → SOP → Node 引擎用例均复现失败；修复后统一本机验收 **646 项 StaffDeck + 1 项 Java → StaffDeck HTTP** 通过，零失败 / 错误 / 跳过。模型决策为脚本数据；没有调用线上法务服务或验证真实 Word 文件。目标 Linux 镜像及测试服需构建后复测。

失败运行请读取 `GET /runs/{run_id}` 的 `error.reply` 和 `error.causes`，或 `run.failed` 事件中的同名字段；`/runs/{run_id}/result` 对失败运行仍返回 409。现存失败记录不自动回填，应用于升级后新运行。复用 `Dockerfile.platform`，无需新增配置或迁移；本次运行状态修复可独立更新 StaffDeck。两仓库提交及复测步骤见平台交接 `docs/features/PRD-007/F-I3/sop-run-status-fix.md`。

## 前一增量：委托运行原件导入与拒绝审计（2026-11-17）

基于 `289126c3`。委托沙箱继续 `network_mode=deny`、`allowed_domains=[]`。已授权的 `workbench_file_original` 返回一次性链接后，由 StaffDeck 服务端按部署配置下载，核对大小和 SHA-256，放入当前任务的 `attachments/originals/`，返回 `workspace_path`。不依赖 curl、Perl 模块、shell 变量或重定向，不修改 SRT 策略。

运行容器新增环境变量（必须与平台返回的完整前缀一致）：

```text
STAFFDECK_PLATFORM_ORIGINAL_BASE_URL=http://172.17.0.1:18082/workbench/originals/
```

不配置时拒绝原件下载。只接受指定 scheme / host / port / 路径后追加 43 位 token；不接受其他地址、重定向或编码穿越，不使用环境代理。平台继续负责 300 秒过期、一次性消费和当前运行绑定。下载最多 12 MiB，下载前后重新验证授权；文件大小、哈希或安全目录检查失败则清理下载文件。模型和调用结果不保留一次性链接，输入原件不自动发布为成果。

拒绝审计补齐：被平台策略过滤的原快照资源进入 `unavailable_references`；前置拒绝 / 运行中撤权进入调用账本和 `tool_calls`。确定未进入执行的网关拒绝记 `failed`，已进入执行或不确定的写调用仍保留 `outcome_unknown`。此分类需要平台同时提供 `structuredContent.error={code:PLATFORM_GATEWAY_REJECTED,executed:false}`；旧平台的纯文本拒绝继续保守处理，不能仅凭文案断定未执行。普通 MCP 不能用同名字段获得平台网关待遇。

复用 `Dockerfile.platform` 构建本次分支固定版本，不叠补丁、不提升 member、不迁移数据库。原件下载可先只更新 StaffDeck 并配置地址；要完成新的拒绝状态分类，必须同时更新平台网关生产者。详细契约、测试数量和两仓库准确 SHA 见平台仓库 `docs/features/PRD-007/F-I3/original-delivery-fix.md` 及交付 manifest。本轮验证为本机合成业务服务、真实 API / Node / SRT，不是目标 Linux 镜像或线上业务验收。

## 前一增量：平台委托 member 调用员工网关

基于 `30d945d9679d57745001c9a0f98c6b3fa4c36f8c` 修复：员工只绑定 MCP 工具、未单独绑定 MCP 服务器时，member 在工具清单和执行阶段被服务器 PEP 拒绝。

现在两阶段共用 `mcp_server_for_tool_ref`：当前员工已有效绑定且可见的工具，可以在该工具调用内使用其同租户、启用的 MCP 服务器。此权限仅是工具连接依赖，不产生服务器绑定记录，不授予直接使用 / 管理服务器或调用其他工具的权限。有显式服务器绑定时保持原规则，停用 / 删除记录不会被继承逻辑覆盖。调用前刷新工具、服务器和绑定状态，避免继续使用撤权前的缓存。没有提升影子用户角色，也没有数据库迁移。

**本机 492 项 StaffDeck 回归 + 1 项 Java → 实际 StaffDeck HTTP 验收通过**，0 失败 / 错误 / 跳过。新增验收通过真正的委托 API 创建 member 影子身份，经真实 Node 引擎、授权层与 MCP HTTP 客户端访问本机合成网关；`web_search`、`workbench_file_original`、`group_dispatch`、法务工具名分别收到一次调用。未绑定同服务器工具、其他员工网关以及本轮运行中撤权后的调用均被拒绝。不是线上搜索、文件、群派单或法务业务服务验收。

隔离复跑脚本仍为平台仓库 `scripts/verify-staffdeck-isolation.py`。目标环境应构建本分支最新固定提交，再用平台委托 member 复测工具可见、一次正常调用及撤权后拒绝。不需要重新同步员工配置或手工补绑定。本轮未部署、未构建新 Docker 镜像，构建入口仍为 `Dockerfile.platform`，原安全运行配置不变。

## 前一增量：三项构建反馈修复（2026-11-16）

基于 `98a4aa34483c40fa8a5ec7b0510c0300654a8954`，在原 `feature/20261116_platformIsolation_huangya_fintech` 分支修复三项反馈。af1 / af2 和既有主体隔离继续保留，不再额外叠补丁。

## 改动

1. **Linux 沙箱依赖**：`Dockerfile.platform` 的最终运行镜像安装 `ripgrep`、`socat`，保留 Bubblewrap、SRT 和非 root 运行；构建时以服务用户执行 `rg --version`、`socat -V`、`bwrap --version`。没有取消 SRT 或降低隔离。仅清除 `STAFFDECK_SRT_RUNTIME` 不可靠，因为 `resolve_srt()` 还会自动发现源码目录的运行时。
2. **隐藏路径不作为成果**：V2 / V3 工作目录自动发现共用的过滤规则，排除任意路径段以 `.` 开头的文件和目录，包括 `.cache/a.json`、`reports/.cache/a.json`、`.env`、`reports/.draft.md`。原附件源、技能过程目录、缓存和临时后缀过滤保留；普通报告、多段扩展名和 `.py` 交付物保留。历史已发布成果不会被自动清理。
3. **坏密钥不拖垮模型列表**：`GET /api/enterprise/model-configs` 遇到当前 `APP_SECRET` 无法解密的单行配置，仅跳过该行；全部不可解密时返回空列表。只捕获专门的 `SecretDecryptionError`，不吞掉任意程序错误。告警只写租户 ID 和配置 ID，不写密文、明文或异常栈。不删除配置、不切换默认模型、不改变执行时的租户、启用与验证检查；坏凭据本身仍需单独修复。

## 验证

- 修复前，新增和扩展回归复现 8 项失败；修复后通过。
- 最终 **438 项 StaffDeck 测试通过，0 失败 / 错误 / 跳过**；包括真实目录规则、真实 HTTP 模型列表、损坏密文 / 旧密钥 / 非 UTF-8、全坏列表、租户隔离及既有引擎隔离用例。
- **1 项 Java → 实际 StaffDeck HTTP 验收通过**。通过平台仓库的 `scripts/verify-staffdeck-isolation.py` 统一复跑，新增模型列表、V2 成果与沙箱模块套件已纳入。
- 扩大检查发现的两个旧模块测试在基线也失败；根据既有 `SandboxFacade.execute` 契约改读 `error.details`，并检查命令失败结果的 `exit_code == 3`。没有修改生产失败行为来迎合测试。
- 测试使用本机临时数据库、合成身份、匹配源码的真实 Node 引擎和脚本模型；不是目标 Linux 容器验收。

## 令子重建及复测

从本分支的新提交直接构建：

```sh
git rev-parse HEAD
docker build -f Dockerfile.platform --target app -t staffdeck-platform:feedback-20261116 .
```

本机本轮重试构建仍在拉取 `node:22-bookworm-slim` 时因 Docker 守护进程代理连接被拒绝而中断，未进入依赖安装，因此没有本轮镜像摘要或 Linux 容器通过结论。

请在原受控运行配置下重建，核对普通服务用户能找到 `rg`、`socat`、`bwrap`；再验证工作目录内命令成功、工作目录外文件与禁止联网均被拦截。工具存在或诊断显示 ready 不能替代这些实际执行检查。不需要修改 APP_SECRET、迁移数据库或给容器 privileged 权限。

追加冒烟：生成普通报告及 `.cache` / 嵌套隐藏文件，确认只发布普通报告；在隔离测试数据中混入旧密钥加密的模型配置，确认列表仍返回可解密的同租户模型，并产生仅含 ID 的告警。
