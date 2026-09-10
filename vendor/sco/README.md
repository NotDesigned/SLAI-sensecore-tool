# SCO 官方安装包

固定版本：SCO v2.0.2 / Registry 20260830。

本目录是仓库内置安装资源，保存完整的官方 `.tar.gz` 安装包、安装脚本和元数据，全部随 Git 分发。为兼容统一下载缓存，实际文件名使用来源 URL 的 SHA-256；下表使用原始文件名展示。点击链接下载后，可按显示名称保存。

每个数据文件旁的同名 `.json` 记录来源 URL、大小和文件 SHA-256；安装包额外记录官方清单中的 SHA-256、系统、架构和运行时。所有安装包已核对官方校验值及内部 `sco` 可执行文件的平台标识。

## 安装包

| 系统 | 架构 | 运行时 | 官方文件名 | 大小 |
| --- | --- | --- | --- | --- |
| darwin | amd64 | launcher | [sco-v2.0.2-launcher-darwin-amd64.tar.gz](3bd16db2dbe7c44f942c26c62fa7c5159d8797ca08d631d53f04da2578694925) | 34.96 MiB |
| darwin | amd64 | v1 | [sco-v2.0.2-v1-darwin-amd64.tar.gz](8ae7683392aeaf63185eb2dd80e07f22eec2e5cfda40bc9229dcbfe948ddc50c) | 34.96 MiB |
| darwin | amd64 | v2 | [sco-v2.0.2-v2-darwin-amd64.tar.gz](2677d59531ae074a3814f05975707c33bb14d5d715e6ac41d30b0f19b22b34fa) | 34.96 MiB |
| darwin | arm64 | launcher | [sco-v2.0.2-launcher-darwin-arm64.tar.gz](abd4114b09ad2f07990418fc7ec06065b2c758b2b4fb1c24c77f7dadefa73347) | 33.52 MiB |
| darwin | arm64 | v1 | [sco-v2.0.2-v1-darwin-arm64.tar.gz](e836547126f4d17000eebf664b1d6a4be20e30756611cee9242c9b56d0b1f487) | 33.52 MiB |
| darwin | arm64 | v2 | [sco-v2.0.2-v2-darwin-arm64.tar.gz](8c1285a76fc2f6d0ea5d8a90ee1b5451854f8fb2022bdef812613331d41cd212) | 33.52 MiB |
| linux | amd64 | launcher | [sco-v2.0.2-launcher-linux-amd64.tar.gz](4b639fd2f71090109707ab69c2fb4637b9efb25cb4da37b995847281bcbf3cdd) | 33.27 MiB |
| linux | amd64 | v1 | [sco-v2.0.2-v1-linux-amd64.tar.gz](420adca64b058b74574ece7d7985a8db59d725ece2d638ae430abb7f22867a25) | 33.27 MiB |
| linux | amd64 | v2 | [sco-v2.0.2-v2-linux-amd64.tar.gz](0cec3f01542a17ee9319e9379ff6b203f341cdeca8901a5f1761928ca28bf91e) | 33.27 MiB |
| linux | arm64 | launcher | [sco-v2.0.2-launcher-linux-arm64.tar.gz](f82bd9762e40805d6d0645633c53d3c0c4fd4164e7afa2199583600a2d8a94a5) | 30.69 MiB |
| linux | arm64 | v1 | [sco-v2.0.2-v1-linux-arm64.tar.gz](88ebeec444e731c54f2e31f7f1db6a7426a59758c0b0e17ac0ecbaf35109e5f4) | 30.69 MiB |
| linux | arm64 | v2 | [sco-v2.0.2-v2-linux-arm64.tar.gz](af5b1a03159faddaf1e39cb1e6bfeab48cc1fab811aca76a313e515e6d1d0705) | 30.69 MiB |

## 安装脚本与元数据

- [install.sh](85ed5e53a2194c8e6f83a6554e3696926e03d7b8cf4f2c9a7b7fb2616a098dcd)
- [manifests.tar.gz](be1301b1a5f8df43c85cf8c2c9a261e8af30863b3b84d49da59df825908ae47e)
- [meta.tar.gz](a8c413d6711ba8f2ced2cd6eaba9f846cc18bdad9a1ce98af4075940ea87b7c7)
- [product.env](9d8616110443769855fe9af279ca9215c9b24baf3f5d17e479e647ea6ac54740)
- [product.yaml](f990736fb9220c9909b0bf828b9617864b9bddf4dedf1f9a7d86597ac8a805c0)

## 使用

在仓库根目录运行 `uv run main.py`，选择 `1`。程序自动选择当前系统/架构并调用官方安装器。请保留整个目录，只有单个压缩包不足以完成官方安装流程。

已在 macOS ARM64 上验证离线安装；其他平台完成官方校验及二进制架构检查，尚未在对应系统上执行。

## Windows 安装器

另缓存官方 `https://sco.sensecore.cn/registry/install.ps1`，供 Windows x64 安装入口使用，采用相同的 URL 哈希文件名与 SHA-256 元数据。Windows runtime 尚未捆绑，安装器首次联网下载和校验 Windows 包。
