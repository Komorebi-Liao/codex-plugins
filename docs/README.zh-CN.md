# Session Guardian 中文指南

Session Guardian 用于防止 Codex 长任务持续携带过大的会话上下文，导致每次请求上传量增大、响应变慢或出现多条大流量连接。

## 原理

Codex 在每次提交用户提示和回合结束时调用插件 Hook。插件只读取 Codex 提供的 `transcript_path`，用文件字节数衡量会话体积；它不依赖不稳定的 JSONL 内部格式，也不解析或保存会话内容。

为降低误报，默认需要同时满足：

1. 会话日志达到 16 MiB；
2. 当前任务至少提交过 6 次提示。

达到条件后，插件会在当前回合结束后执行：

1. 通过本地 Codex App Server 让旧任务生成结构化交接摘要；
2. 新建一个任务，写入目标、已完成项、决策、文件变更、验证结果和待办项；
3. 确认新任务已准备好；
4. 最后才归档旧任务。

任何一步失败都不会归档旧任务。归档是可恢复的，可以在 Codex 的已归档任务中找回。

## 安装

```bash
codex plugin marketplace add Komorebi-Liao/codex-plugins --ref main
codex plugin add session-guardian@komorebi-codex-plugins
```

安装后重启 Codex 或新建任务。Codex 会要求审查并信任插件 Hook；在 CLI 中可使用 `/hooks` 查看。

## 使用

在 Codex 中可以说：

- “检查当前会话是否过长”
- “现在就交接到新任务”
- “Session Guardian 改为只提醒，不自动归档”
- “把自动交接阈值改为 24 MiB”
- “取消已排队的会话交接”

也可以显式调用 `$session-rollover`。

## 模式

- `auto`：达到条件后自动交接，默认。
- `warn`：只提醒，不新建或归档任务。
- `off`：关闭检测。

配置和运行状态保存在 Codex 分配给插件的私有数据目录，不会进入代码仓库。
