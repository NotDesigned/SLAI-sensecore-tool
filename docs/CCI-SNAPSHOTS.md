# CCI 保存为镜像

入口：CCI 列表 → 运行中的 CCI → 保存为镜像。只有一个运行中主容器时自动选择，否则选择具体副本与容器；然后选择 CCR 命名空间和镜像名称。平台生成版本标签，提交后在“已保存的镜像”中刷新，成功后复制完整地址。

选择保存记录后会重查状态。平台返回地址时显示“复制快照地址”按钮，只复制完整镜像地址，不包含状态、名称等描述。保存中或保存失败也可能已有目标地址，此时页面会明确提示镜像尚未确认可用；没有地址时不显示复制按钮。

保存会暂停源容器，完成后自动恢复。控制台提示镜像容量须在 100 GB 内。镜像可能包含文件系统中的密钥、配置和软件；挂载存储应另行备份，不能用镜像保存进程内存或训练执行状态。默认 SSH 启动脚本按实例 hostname 区分主机密钥，新实例会生成新的主机密钥，并覆盖为当前所选的登录公钥；未重建的容器再次执行启动脚本时复用密钥，容器重建会生成新密钥。自定义启动脚本需自行处理。

## REST 合约

当前控制台前端使用以下接口，现有 AK/SK REST 客户端已完成实际读写验证。此接口未列在当前公开 CCI OpenAPI 导航中，路径与字段来自控制台实现，平台更新后需要复测。

基础路径：

```text
https://cci.{region}.sensecore.cn/compute/cci/data/v2/subscriptions/{subscription}/resourceGroups/{group}/zones/{zone}/workspaces/{workspace}/apps/{app}
```

- `GET /instances`：读取副本的 `name`、`uid`、`state` 与 `container_infos`。只选择运行中的 `MAIN` 容器。
- `POST /snapshots?client_type=0`：创建快照，正文包含 `name`、`display_name`、`ccr_namespace`、`container_name`、`instance_uuid`。**instance_uuid 实际传实例的 name，不是实例 uid 或应用 uid。**
- `GET /snapshots`：支持 `page_size`、`page_token`，返回 `snapshots`、`next_page_token`、`total_size`。
- 记录字段包括 `uid`、`state`、`reason`、`uri`、`image_tag`、`created_time`。状态包括 `CREATING`、`SUCCESS`、`FAIL`、`INVALID`、`UNKNOWN`；仅成功状态显示可使用的镜像地址。

提交前重新检查应用归属和 UID、实例 UID、容器状态及命名空间。已有同名同命名空间快照时拒绝重复提交；网络超时不会自动重试写入，需先查列表确认。快照成功后清除对应 CCR 镜像列表缓存。

## 实测边界

验证记录见 [snapshot-validation.json](snapshot-validation.json)。临时实例使用 2 vCPU、4 GiB、0 加速卡，没有挂载用户 AFS。测试通过 SSH 安装/确认 sshd 并写入独立标记文件，保存后使用镜像创建新 CCI，再以 SSH 读取标记。测试不证明 AFS 内容被纳入镜像，也没有测试大于 100 GB 的镜像。

参考：[官方 CCI 用户指南](https://console.sensecore.cn/micro/help/docs/cloud-foundation/compute/cci/)。

2026-09-12 的完整 MNIST 流程使用官方 `lepton-trainingjob/ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04` 保存成功。预装 SSH 镜像 `slai-cci-pytorch-ssh:25.06-20260911` 在两个命名空间的尝试均返回 `PUSHFAILED`，原因尚未由平台日志确认，因此没有将该镜像的保存标记为验证通过。详情见 [完整流程记录](mnist-workflow-validation.json)。
