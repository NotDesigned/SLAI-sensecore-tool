# CCI 开发 → 保存镜像 → ACP 训练 → CCI 查看结果

这是一次完整的服务集成示例，使用真实 MNIST 训练验证环境保存、批量任务和共享存储，不作为模型性能基准。使用 2 vCPU、4 GiB 内存、0 加速卡，完整训练集 60,000 张、测试集 10,000 张，默认训练两轮。

## 1. 创建开发 CCI，准备代码和数据

在 CCI 列表点击“创建 CCI”，选择官方标准镜像 `registry.cn-sh-01.sensecore.cn/lepton-trainingjob/ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04`、CPU 规格，保留默认 AFS `/data` 挂载和 SSH，附加 DNAT。实例运行后选择“连接”，复制 SSH 命令登录，也可用 VS Code Remote-SSH。

此标准镜像首次启动时，工具会安装并启动 sshd，可能需要等待几分钟。

通过 SSH / VS Code 把本目录的 [train.py](train.py) 放到容器的 `/opt/slai-mnist/train.py`。代码放在容器文件系统，数据和输出放在共享存储；这样保存镜像会带上代码，ACP 与之后的 CCI 都能访问共享数据。

在 CCI 中执行以下命令。为每次运行选择一个新的目录；下文以 `/data/mnist-demo-001` 为例：

```sh
mkdir /data/mnist-demo-001
python /opt/slai-mnist/train.py \
  --prepare-only --data-dir /data/mnist-demo-001/dataset
```

若云内下载慢，可以在本机准备同一数据集后，通过 SSH / VS Code 上传 `dataset/MNIST/raw` 到上述共享目录，再执行准备命令校验。本次实测使用 [CVDF 提供的 Google Cloud 镜像](https://github.com/cvdfoundation/mnist)，并校验四个压缩包的 MNIST MD5。

数据由 [Torchvision MNIST](https://docs.pytorch.org/vision/stable/generated/torchvision.datasets.MNIST.html) 下载并校验。训练时不再下载；不要求 ACP 访问互联网。

## 2. 保存为镜像

返回 CCI 列表，选中该实例 → **保存为镜像**，选择 CCR 命名空间并填写镜像名称。确认后容器会短暂暂停，完成后自动恢复。在“镜像快照”刷新到“已保存”，复制完整镜像地址。

不需要把数据集和训练日志打入镜像，也不要把账户密钥写入容器。保存限制及 SSH 主机密钥处理见 [镜像保存说明](../../docs/CCI-SNAPSHOTS.md)。

## 3. 用 ACP 运行训练

ACP → 创建任务，选择计算资源池与 2 vCPU / 4 GiB / 0 加速卡规格，配额使用“闲时资源”。镜像填写上一步的地址，使用与开发 CCI 相同的 AFS 和子目录，挂载到 `/data`。

不使用镜像入口程序，启动命令填写：

```sh
set -euC
python -u /opt/slai-mnist/train.py \
  --data-dir /data/mnist-demo-001/dataset \
  --output-dir /data/mnist-demo-001/output \
  --epochs 2 > /data/mnist-demo-001/train.log 2>&1
```

保存成功后，开发 CCI 此时可以停止。实测停止再启动保留 CCI 名称和配置，但容器内临时文件不保留；需要保存的代码应先放入镜像或 AFS。ACP 使用保存的镜像运行独立进程；训练成功后任务状态应为 `SUCCEEDED`。脚本拒绝复用非空输出目录，防止覆盖以前的结果。

## 4. 新建 CCI 查看日志和模型

可以在 CCI 列表选中已停止的实例，点击“启动”原地使用，也可以新建另一台 CCI。本例用另一台相同官方标准镜像的 CCI 验证共享存储，挂载同一个 AFS 子目录到 `/data`，通过 SSH 执行：

```sh
cat /data/mnist-demo-001/train.log
cat /data/mnist-demo-001/output/result.json
cat /data/mnist-demo-001/output/metrics.jsonl
ls -lh /data/mnist-demo-001/output/model.pt
```

结果包括训练前准确率、每轮损失与准确率、运行时长、训练代码与模型文件的 SHA-256。`model.pt` 是 PyTorch 模型权重。查看这些文件不需要原开发 CCI 继续运行，也不需要查看日志的 CCI 使用保存后的镜像。

实测数据见 [mnist-workflow-validation.json](../../docs/mnist-workflow-validation.json)。测试的临时云资源会清理；共享目录中的代码、日志和模型保留，方便继续检查。
