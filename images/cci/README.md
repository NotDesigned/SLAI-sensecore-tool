# 预装 SSH 的 CCI 镜像

基础镜像为 `nvcr.io/nvidia/pytorch:25.06-py3`，包含 Ubuntu 24.04、Python 3.12、CUDA 12.9.1。只额外安装 OpenSSH 服务，保留原环境和入口；不写入用户公钥、密码或主机密钥。[NVIDIA 版本说明](https://docs.nvidia.com/deeplearning/frameworks/pytorch-release-notes/rel-25-06.html)

```sh
docker buildx build --platform linux/amd64 --provenance=false --output type=docker,oci-mediatypes=false -t slai-cci-pytorch-ssh:25.06 images/cci
```

随后在 CCI 创建表单中选择此本地镜像，工具会提示目标命名空间并在提交时自动同步。命名不包含个人用户名。启动时由工具注入公钥并生成独立主机密钥，无需再执行 apt 安装。

已发布：`registry.cn-sh-01.sensecore.cn/ccr-zhicheng-02/slai-cci-pytorch-ssh:25.06-20260911`。已通过真实 CCI 公钥 SSH 登录与 AFS 读写检查。

镜像摘要：`sha256:73d41984e6948661a039da17149112b923e6705526ee5a41866e49e2cc12a8d5`。目标为私有命名空间，其他用户须具备拉取权限或将此 Dockerfile 构建并同步至自己的命名空间。
