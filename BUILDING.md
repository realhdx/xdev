# 从源码构建 xdev

Windows x64 上使用 64 位 CPython 3.14。基础命令用 Python 标准库即可启动。在仓库根目录运行：

```powershell
py -3.14 -B -m src.cli.main --version
py -3.14 -B -m unittest discover -s tests
py -3.14 -B -m unittest discover -s deploy
py -3.14 -B -m unittest discover -s product
```

上述测试使用临时目录和模拟的通信对端。双机 SSH、实际客户端加载和指定原聊天收信可按[产品手册](product/README.md)另行验收。

## 构建 Windows 安装包

在仓库根目录运行以下 PowerShell 命令。下载步骤需要访问 PyPI；准备好依赖后，构建和安装都可离线进行。各项目的名称、版本、文件名与来源列在[依赖清单](dependencies/windows-x64-cp314/bundle-manifest.json)。

```powershell
$deps = '.\dependencies\windows-x64-cp314'
py -3.14 -m pip --isolated download --index-url https://pypi.org/simple --only-binary=:all: --no-deps --require-hashes -r "$deps\requirements.lock.txt" -d "$deps\wheelhouse"
if ($LASTEXITCODE -ne 0) { throw '离线依赖准备失败' }
py -3.14 -I -B -X utf8 product/build.py --wheelhouse $deps --output-dir .\build
```

构建程序核对 31 个依赖文件的名称、大小和 SHA-256，并验证源码对应的运行时摘要。生成的 ZIP 和同名的 `.sha256` 在 `build` 目录内。若已解开同一依赖版本的发行包，可将构建命令的 `--wheelhouse $deps` 改为 `--dependencies '<已解包目录>\assets\optional-deps.zip'`。

最后将输出中的 ZIP 文件名和期望摘要传给校验器：

```powershell
py -3.14 -I -B -X utf8 product/verify.py '<生成的 ZIP 路径>' --sha256 '<期望的 SHA-256>'
```

正式发行版将运行时和离线依赖摘要登记在 [product/release.json](product/release.json)。同一版本使用相同的源文件字节和依赖即可重建同一 ZIP。维护新版本时，按[版本说明](product/CHANGELOG.md)更新登记信息。安装、迁移和接入客户端见[部署手册](product/AI-DEPLOY.md)。
