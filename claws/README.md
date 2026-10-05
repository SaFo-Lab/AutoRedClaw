# Claw Docker environments

这些 Dockerfile 以原 RedClaw 的评测镜像配方为基准。原来的 adapter 启动参数、workspace、系统提示、工具权限、provider 路由和评测流程保持原样。

## 复用原实验环境

要与已有实验比较，优先复用相同的镜像和冻结数据集。先检查实际镜像及 CLI 版本设置：

```bash
python -m redclaw runtime verify
# 或只检查要评测的 claw，并保存环境身份记录。
python -m redclaw runtime verify --claw nanobot codex --output output/runtime.json
```

参考镜像 ID 和平台见 [runtime-baseline.json](runtime-baseline.json)。基准来自当前保留的旧实验镜像，均为 `linux/arm64`；不代表每一批历史实验都使用过这些镜像。校验命令检查实际镜像 ID，而不是仅检查同名标签；不匹配时返回非零状态。

不要为了复现实验而先强制重建镜像。

## 从源码构建

新机器没有原镜像时，可以一键构建：

```bash
docker compose -f claws/compose.yaml --parallel 1 build
# 或仅构建所需 claw。
docker compose -f claws/compose.yaml build nanobot seclaw
```

每个 `claws/<name>/` 都是独立构建目录。构建时获取固定 revision，无需预先下载上游仓库或在宿主机安装 Node/Rust。构建完成后由评测适配器创建和回收容器，通常不需要手动 `compose up`。

**从源码重建不等同于复用原镜像。** 已锁定源码、关键基础镜像和可恢复的依赖；仍有系统包、构建依赖和平台差异无法仅凭源码配方消除。严格复用请使用原镜像导出/导入流程。构建会更新相应 `latest` 标签，先保留需要复用的原镜像。

## 对齐内容

| Claw | 源码 / 客户端 | 构建和运行设置 |
| --- | --- | --- |
| nanobot | Git `172ec4d4c4a9` | 恢复 uv + Python 3.12.12、Node 20.20.2、系统级 Python 安装、原 entrypoint 和用户；Python 约束及 bridge 锁文件取自旧镜像 |
| openclaw | Git `083377adb8bb` + 本地源码补丁 | 恢复原多阶段源码构建、Node 24 的固定摘要、pnpm 锁文件及原运行镜像配置；不再替换为官方发行镜像 |
| seclaw | Git `5a96f6abb4f5` | 恢复原单阶段 `npm install`、编译及 prune 流程，Node 固定为旧镜像的 22.23.2 |
| zeroclaw | Git `1a4ba770c2a4` | 恢复原 Rust 1.91、单编译 worker、ci profile 和原运行层 |
| nemoclaw | NemoClaw `v0.0.109` + OpenShell `0.0.101` | 原 controller 配方、Compose 和启动脚本；保留原 sandbox base 版本，并校验其实际镜像 ID |
| codex | CLI `0.156.1` | 对齐当前旧镜像实际版本；此前代码默认 `0.150.1`，复现更早的实验时可显式设置 `CODEX_CLI_VERSION` |
| claudecode | CLI `2.1.236` | 保留原 Dockerfile、工具子进程 wrapper 和权限设置 |

完整来源与构建文件哈希见 [sources.json](sources.json)。OpenClaw 的 `source.patch.gz` 记录当前原始 checkout 相对其 HEAD 的全部 tracked 修改，构建时校验 SHA256 后应用；该补丁主要调整源码 import 的 `.js` / `.ts` 路径。它是构建输入，不是运行日志。`context-files.txt.gz` 记录经原 Docker 忽略规则筛选后的 17,536 个 tracked 输入文件，源码阶段按该清单恢复构建上下文，避免额外带入原构建排除的文件。

Nanobot 使用原镜像的 Python 已安装版本作为 constraints。Bridge 改用捕获的锁文件执行 `npm ci`，锁住原来 `npm install` 实际产生的依赖图；这一步有意固定安装结果。若历史包已不可下载，构建应失败，不静默升级依赖。

ZeroClaw 的原生 workspace 模板随代码提供于 `redclaw/resources/workspaces/`，与原源码文件内容一致。NemoClaw 仍由 controller 管理独立 OpenShell sandbox；controller 镜像本身不是 agent sandbox。
