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

同步 HTTP 和本地工具在页面挂载完成后启动 Textual worker。只读操作可返回，迟到结果不会更新已关闭页面；发出写入前检查页面仍有效并标记资源可能已变化，写入后等待结果。只有发生写入（含结果未知）才在返回列表时刷新；查看详情、连接和取消不会刷新。Docker 上传也使用同一变更标记。没有替换全局 input/stdout。

身份与资源目录在当前 Textual 会话中按账户、URL 和代理缓存 30 秒，最多 64 项；手动刷新和写入会清除缓存。实例详情和 DNAT 目标复查不缓存。普通 GET 的单次超时为 20 秒，CCR 大目录保留 120 秒，ACP 保留专用超时；界面连续只读请求共享至少 60 秒的预算，交互输入后重新计时。

列表读取时保持创建、上传及返回按钮可用；并发创建成功后会补一次刷新。资源减少时自动校正页码，翻页失败保留原页数据和页码。独立上传与创建任务时的本地镜像同步共用名称解析、认证、推送与缓存失效流程。快照首次被观察为成功时使 CCR 缓存失效；以账户隔离的本地事件记录避免反复查看同一快照清空缓存。

列表最多渲染当前 20 条。ACP 按服务端分页和筛选读取，缓存最多 12 页；CCR 使用命名空间完整快照，因为当前服务端忽略分页。CCI/DNAT 使用完整列表快照。CCR 快照持久化至 `.cache/ccr/`，账户凭据仅参与单向摘要分区，不写入缓存正文；不同账户、区域和命名空间身份隔离。

创建草稿更换资源池时清除依赖配置。规格和默认存储独立读取、最多并发 2 路，重复访问同一池使用缓存。上次配置在回填前校验账号、范围和资源身份；DNAT 只复用承载 EIP，不重放旧绑定。

服务层保存请求计划、确认目标身份、提交并复查最终状态。模板复制过滤只读字段，未知的非空字段拒绝静默丢弃。CCI 的独立 Service、ACP 的副本数转换和无文档 batchStop 路径见 [REST 接入记录](REST-MIGRATION.md)。
