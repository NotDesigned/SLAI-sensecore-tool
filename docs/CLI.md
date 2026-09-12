# 命令行使用

从项目目录运行 `uv run main.py --help`。每个服务、设置命令都支持 `--help`，显示帮助无需配置账户或访问云端。

- `uv run main.py`：交互终端默认打开 Textual 界面。
- `uv run main.py --text`：文本菜单。
- `uv run main.py --text 服务 操作 参数`：强制文本交互；创建和复制仍会显示可编辑配置表。
- `--plain`：打印列表后退出，适合重定向输出。输出是人类可读文本，不是 JSON。CCI/ACP/DNAT 的 CLI 列表实时查询，不复用 Textual 的会话列表缓存；CCR 可复用有效期内的镜像磁盘缓存。
- `--name`：操作目标名称；ACP 列表中表示名称前缀。CCI/ACP 创建名称在表中编辑。
- `--workspace`：CCI/ACP 工作空间；省略时用已保存的工作空间，没有默认项则询问。
- `--yes`：只跳过该命令明确支持的确认，不跳过身份核对，也不自动补齐输入。

编号菜单中回车使用默认选项，0 返回；文字输入用 q 取消。删除、解绑、停止等操作保留确认；自动化执行前请指定目标和范围。

## 设置

| 命令 | 行为 |
| --- | --- |
| `configure` | 交互配置并验证账户，首次使用引导选择工作空间 |
| `workspace` | 选择并保存默认工作空间 |
| `proxy configure` 或 `proxy` | 交互设置、修改或关闭 SOCKS5 |
| `proxy status` | 只读检查代理握手、认证 |
| `guide` | 显示使用指南 |

AccessKey、代理密码不作为命令行参数传入，按提示输入后保存在本地 `config.toml`。

## CCI

| 操作 | 行为 |
| --- | --- |
| `list [--plain]` | 本人实例列表，默认操作 |
| `create` / `create-last` | 普通创建 / 回填上次配置，编辑后提交或仅保存 |
| `describe --name NAME` | 实例详情 |
| `connect --name NAME` | 查找绑定 DNAT、检测 SSH 响应并显示命令；多入口时选择 |
| `start --name NAME` | 启动已停止实例 |
| `stop --name NAME [--yes]` | 停止实例 |
| `copy --name NAME` | 回填源配置，微调后创建副本 |
| `delete --name NAME [--yes]` | 删除实例及仍归属它的端口 Service，不删除 DNAT |
| `snapshot --name NAME` | 保存为镜像，交互选择命名空间、名称并确认 |
| `snapshots --name NAME [--plain]` | 镜像保存记录；交互模式可查看镜像地址 |

目标操作省略 `--name` 时从本人实例列表选择。停止前请把需要保留的数据写入挂载目录或保存为镜像。

```sh
uv run main.py cci list --plain
uv run main.py --text cci copy --name my-cci
uv run main.py --text cci connect --name my-cci
uv run main.py --text cci snapshot --name my-cci
```

## ACP

| 操作 | 行为 |
| --- | --- |
| `list` | 本人任务交互列表 |
| `list --plain` | 打印一页，默认第 1 页，每页 20 条 |
| `create` / `create-last` | 普通创建 / 回填上次配置 |
| `describe --name NAME` | 任务详情 |
| `copy --name NAME` | 配置表内编辑副本，不自动恢复 checkpoint |
| `stop --name NAME [--yes]` | 停止任务 |
| `delete --name NAME [--yes]` | 删除任务 |

`list --plain` 支持 `--page N`（从 1 开始）、`--page-size N`（1–500）、`--name PREFIX`、`--state STATE`。状态为 RUNNING、PENDING、SUSPENDED、SUCCEEDED、FAILED。其他操作的 `--name` 是完整名称；省略时交互选择。

```sh
uv run main.py acp list --plain --state RUNNING --page 1 --page-size 20
uv run main.py --text acp copy --name my-training-job
uv run main.py --text acp stop --name my-training-job
```

## DNAT

| 操作 | 行为 |
| --- | --- |
| `list [--plain]` | 汇总本人创建的规则 |
| `create [--name NAME] [--yes]` | 交互创建；仍需选择 EIP、填写端口等 |
| `describe --name NAME` | 规则详情 |
| `bind --name NAME` | 选择已有 CCI 并绑定；迁移原绑定仍需确认 |
| `unbind --name NAME [--yes]` | 解绑，保留规则和端口 |
| `delete --name NAME [--yes]` | 删除；若已绑定，先解绑 |

所有操作可加 `--eip EIP_NAME` 限定范围。未指定 EIP 时列表汇总全部，创建时询问；规则重名时需要限定 EIP。目标操作省略规则名称时交互选择。

```sh
uv run main.py dnat list --plain
uv run main.py --text dnat bind --name my-rule --eip my-eip
uv run main.py --text dnat unbind --name my-rule --eip my-eip
```

## CCR

- `ccr list --plain`：可访问命名空间列表。
- `ccr list --plain --namespace NAME`：该命名空间内的镜像和标签。
- `ccr upload`：选择本地 Docker 镜像、命名空间和目标名称后上传；需要可用的 Docker 服务。
- `ccr list`：交互进入命名空间，查看镜像。

列表范围是当前账号可访问，不代表本人创建。上传的命名空间在交互中选择，不能使用列表参数 `--namespace` 指定。
