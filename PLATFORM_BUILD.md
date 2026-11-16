# 平台构建反馈修复（2026-11-16）

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
