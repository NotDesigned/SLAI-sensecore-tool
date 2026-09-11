# 代码结构

`main.py` 进入 `scripts/cli.py`，配置只读取根 `config.toml`。云业务只通过 REST 请求。

| 模块 | 职责 |
| --- | --- |
| onboarding.py | 首次账户/工作空间引导和服务用途说明，从根配置判断进度 |
| cli.py | 主入口、账户验证、配置的原子保存、菜单标题 |
| ui.py / tui.py / tui.tcss | Textual 与文本界面、选择、编辑、分页及后台请求 |
| forms.py | 创建草稿、依赖校验、CCI/ACP 上次选项回填 |
| cloud.py | 工作空间、关联池、规格、AFS/VPC/EIP 等 REST 查询与操作内缓存 |
| rest.py | HMAC、HTTP/SOCKS5 传输、错误脱敏、分页完整性与身份校验 |
| templates.py / schemas/ | 按公开请求 schema 保留可写字段，拒绝不明确的模板字段 |
| cci.py / cci_api.py / cci_service.py | CCI 模板、应用/Service 生命周期、列表操作 |
| cci_network.py / dnat.py | DNAT 创建、绑定、迁移、解绑、删除与复查 |
| acp.py | ACP 模板、分页、详情、创建、复制和单目标批量控制 |
| ccr.py / docker_registry.py | 镜像浏览、搜索和 Docker 上传 |
| ccr_cache.py | CCR 私有磁盘快照、5 分钟有效期、并发读取合并和上传后失效 |
| listing.py | 本地快照与统一页结果 |
| workspace.py | 默认工作空间选择与持久化 |
| cci_ssh.py / ssh_probe.py | SSH 启动配置、就绪与入口检查、连接命令 |
| proxy_settings.py | 顶部 SOCKS5 配置、隐藏密码输入、限时握手与认证检测 |
| network.py / ncat_proxy.py / commands.py | SOCKS5、Ncat 转发与跨平台命令格式化 |
| plans.py / clipboard.py | 私有计划文件、文本导出与系统剪贴板 |

同步 HTTP 和本地工具在 Textual worker 中运行。关闭只读页面后忽略迟到结果；写入操作等待结果，不把退出页面当作撤销云请求。没有替换全局 input/stdout。

列表最多渲染当前 20 条。ACP 按服务端分页和筛选读取，缓存最多 12 页；CCR 使用命名空间完整快照，因为当前服务端忽略分页。CCI/DNAT 使用完整列表快照。CCR 快照持久化至 `.cache/ccr/`，账户凭据仅参与单向摘要分区，不写入缓存正文；不同账户、区域和命名空间身份隔离。

创建草稿更换资源池时清除依赖配置。规格和默认存储独立读取、最多并发 2 路，重复访问同一池使用缓存。上次配置在回填前校验账号、范围和资源身份；DNAT 只复用承载 EIP，不重放旧绑定。

服务层保存请求计划、确认目标身份、提交并复查最终状态。模板复制过滤只读字段，未知的非空字段拒绝静默丢弃。CCI 的独立 Service、ACP 的副本数转换和无文档 batchStop 路径见 [REST 接入记录](REST-MIGRATION.md)。
