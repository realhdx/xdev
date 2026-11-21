# 使用 AI 安装 xdev

[English](AI-DEPLOY.en.md) | **简体中文**

适用于 xdev Windows Pair 1.1.0-preview.13 预览版。所选助手需要能在各目标 Windows 设备的指定用户账户中运行 PowerShell。[手动安装与日常使用](README.md)

## 可直接交给助手的任务

```text
请从 https://github.com/realhdx/xdev/releases/tag/v1.1.0-preview.13-docs.1 下载 xdev 安装包和校验文件，按包内 AI 部署指南在我指定的 Windows 设备上安装并验证。
```

## 安装或升级

需要跨网收信时，按[GitHub 私有信箱指南](../design/github-mail.md)运行自助初始化与核对。接通后在两端各用一封合成信验证入箱、回执和删除。

给跨设备的聊天配对时，先核实既有 SSH 路径是否可达、主机指纹是否匹配。需要仅直连时选 `direct`；如果用户选择中继暂存，按双方约定的保留期配置，并在每端为该对端设置 `encryption: "required"`。用两个指定聊天核对原信和关联回复。[跨网络会话通信指南](../design/session-relay.md) · [Amazon SQS 可选部署](../design/managed-queue.md)

1. 取得本次预览提供的 ZIP 和同名校验文件。
2. 核对校验文件中的文件名，在解压前计算 ZIP 的 SHA-256 并比对。匹配后将同一份 ZIP 解压到各目标设备的新目录，按包内同版 `AI-DEPLOY.md` 继续。
3. 在各目标设备上读取 `hostname`、目标账户的 `%LOCALAPPDATA%` 及 64 位 CPython 3.14 路径，仅本机使用时直接进入下一步。需要跨设备传文件或执行命令时，记录相应设备对的主名和 SSH 别名，先核对主机指纹，再从发起端运行 `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes 对端SSH别名 "python --version"` 及批处理 SFTP 的 `pwd`。需要双向互通时再交换方向测试。[配对与解压步骤](README.md#安装与配对)
4. 以下 `install.py`、`migrate.py` 和 `verify.py` 命令均在解压根用 `py -3.14 -I -B -X utf8` 运行。先运行 `verify.py .`，成功后运行 `install.py status` 读取当前状态。
5. 默认使用 Windows 名并保留已有别名。已核实完整且运行时与本版一致时，沿用现有安装。**新装**运行 `install.py install --expect-current none`。单机无需 `--peer`，默认安装 CLI 和本地 MCP。仅需命令行时可加 `--cli-only`。自定义本机主名时加 `--name 本机主名`。需要配置 SSH 配对时加 `--peer 对机主名 --peer-ssh 对机SSH别名`。**升级**使用 `install.py upgrade --expect-current 当前运行时摘要`，摘要取自该机自己的 `status`。
6. 每台已安装设备均用 `%LOCALAPPDATA%\CrossDeviceCollab\xdev.cmd` 检查 `--version` 和 `who`，核对 Windows 名、本机主名及 `install.py status` 的 `active_verified`。对需要互通的设备，再从发起端运行 `who --host 对机主名` 核对对机身份键。单机演练可在安装器子命令前指定 `--root 独立空目录`。

日常命令以用户账户执行，例子中的 `$xdev` 由 `$xdev = Join-Path $env:LOCALAPPDATA 'CrossDeviceCollab\xdev.cmd'` 设置。

## 接入客户端

**配置工具调用。** 只接入用户选定的客户端。在支持本地 stdio MCP 的应用中，登记[手册中的启动命令](README.md#接入-ai-工具)。部分客户端已有配置可由包内脚本增量更新。支持的配置标识为 `vscode`、`copilot`、`cursor`、`codebuddy`、`gemini`、`kimi-code`、`lmstudio`、`antigravity`、`dsh` 和 `workbuddy`。选择实际使用的配置文件，例如 VS Code 的编辑器设置和独立 Copilot 的设置分别登记。支持带注释配置的宿主可直接使用原生设置界面。

以下以 VS Code 已有配置为例。首次检查会返回目标路径和当前 SHA-256。路径、用户配置和预期修改吻合时才执行 `apply`。保存本次返回的 `receipt_path`；若恢复返回冲突，先核对用户后续修改。

```powershell
py -3.14 -B deploy/clients.py inspect --product vscode
py -3.14 -B deploy/clients.py apply --product vscode --expect-sha256 '<上一步的sha256>'
# 需要恢复这次修改时
py -3.14 -B deploy/clients.py restore --receipt '<receipt_path>'
```

重连目标客户端的 xdev 服务，在该客户端列出工具并调用 `xdev_who`，核对设备及 `_mcp_runtime.pinned_release` 与 `active_release`。若客户端显示服务已停止，先在服务列表中启动 xdev。`deploy/clients.py verify --product 标识` 可单独验证登记的启动命令，打开的聊天是否已采用该配置需要在宿主内另查。

**按应用接入会话通信。** 每台设备配对一次，在使用的应用中接入 xdev。DeepSeek Harness 新装在安装命令加 `--with-dsh`。已有 xdev 时，先运行 `py -3.14 -B deploy/clients.py inspect --product dsh`，按返回摘要运行 `apply --product dsh --expect-sha256 '<摘要>'`。标准安装位置的常规升级会刷新已接入的 DSH。宿主按每封信的地址识别原根聊天，日常收发无需手工逐聊登记。Codex 的首次邮箱工具调用会自动登记，已有唯一可用宿主也能核对其他原聊天。OpenCode 使用原会话 ID。查看所用插件支持的即时或排队模式。[接收器列表](README.md#哪些客户端能接收原聊天通知)

既有 DSH 配置如需保留一份明确限定的名单，可用 `py -3.14 -B deploy/dsh_roots.py --session <ID> --apply`，多选时重复 `--session`。`--all-existing --apply` 会列入该配置已有的所有根聊天。需要单独刷新 MCP 时用 `--refresh-mcp`。

**Antigravity 原聊天。** 若用户选择这个入口，在该配置的 `~/.gemini/config/sidecars/xdev-receiver/sidecar.json` 中，以 Python 作为 `command`，`args` 依次填入 `-I`、`-B`、当前 xdev 运行时内 `src/adapters/antigravity.py` 的绝对路径、`--bridge`、安装根下 `state/antigravity-native` 的绝对路径、`--agentapi`、Antigravity 自带的 `language_server.exe` 绝对路径，再加 `--project-of` 和该项目的一个已有根聊天 ID。需要限定到个别聊天时改用可重复的 `--session`。在宿主的 `config.json` 中启用 `sidecars.xdev-receiver.enabled`，保留同文件中的其他设置。项目模式会逐次核对目标聊天与项目。用 `--queue` 发送一封测试信，按原 ID 核对已读与回信。[Antigravity 官方侧车配置](https://antigravity.google/docs/sidecars)

## 验收并交付

| 项目 | 如何验证 |
| --- | --- |
| 本机执行 | 运行 `& $xdev exec --host local -- hostname`，确认结果为本机 Windows 名。 |
| 跨设备传输与执行（按需） | 从发起设备向对端已有可写目录传送一份带唯一文件名的小文件，比较两端 SHA-256。执行 `& $xdev exec --host 对机主名 -- hostname` 核对目标设备。需要双向互通时，再交换方向验证。 |
| AI 工具加载 | 让每个选定客户端实际调用 `xdev_who`，记录设备及加载的运行时。遇到登录要求、额度限制或尚未启用的插件，记录达到的步骤。 |
| 聊天收发 | 两个指定原聊天先分别执行 `msg whoami`。A 发带简短主题和唯一 `--id` 的测试信给 B；B 用 `msg read 原信ID` 读出仅在正文内的测试词，再 `msg reply 原信ID --text 答复`。A 按相同的 `reply_to` 读回，随后由 B 发起反向测试。 |
| 忙时插话 | 若业务要求即时接纳，在目标原聊天忙时测试。记录设备入箱、队列或当前回合接纳、原聊天已读和关联回信。回复已结束的聊天并不能验证忙态行为。 |

**寄信身份与返回地址。** 宿主能识别当前聊天时会提供发件身份。若 `msg whoami` 无法识别，先核对本聊天的真实绑定。首信可用 `--from-client` 和 `--from-session` 提供发件身份，另用 `--return-client` 和 `--return-session` 指向可接收回信的聊天，或用 `--wait-reply 60` 在本次工具调用中等待。读取需指定身份时用 `--as-client`、`--as-session`，回信需指定身份时用 `--from-client`、`--from-session`。使用独立配置目录的聊天一并指定相应配置。纯回执会关联原信，无须唤醒模型作答。

**处理超时或格式错误。** 在原发件聊天运行 `msg wait --reply-to 原信ID --substantive-only --timeout 0` 查询实质回信。若要核对设备是否存信，用 `msg wait --host 对机主名 --device-inbox --id 原信ID --timeout 0`。提交状态未知时保留原 ID；重要回信也可用 `--id` 固定自己的回复 ID，以便查重。调用 `xdev_msg` 时 `arguments` 是字符串数组，例如 `{"action":"read","arguments":["<来信ID>"]}`。遇到数组类型错误后，用 `{"action":"whoami","arguments":[]}` 验证一次，再决定是否从同一聊天改走 CLI。

**经浏览器转交。** 使用用户选定的浏览器配置和原聊天，只转交获准发往该网站的内容。发出后用正式聊天地址重开，查找含原信 ID 的用户消息和随后对应的模型回答。草稿、临时地址和排队状态分别记录。转交助手以自己的地址执行 `msg reply 原信ID`，在回信中注明来源。若要声称能自动收件或能忙时插话，应分别测试这两项能力。

交付结果按「设备或聊天、可用能力、验证依据、待办条件」列出。为用户留下日常命令入口、实际主名、版本和必要的原信 ID。校验值、用户配置和聊天测试结果由部署方自行保管。

## 迁移与兼容选项

- **换机。** 在原设备保存 `install.py status` 中本机和各对机的主名、身份键及别名。等待在途任务完成并暂停信箱写入，然后执行 `migrate.py export --archive 备份路径`，保存回执和 SHA-256。新机首次安装用 `--identity-key 原身份键` 保留本机身份，用 `--alias` 登记旧 Windows 名及历史别名；对机相应更新主名、SSH 指向和主机指纹。再用 `migrate.py restore --archive 备份路径 --sha256 导出摘要` 将快照导入新装的空信箱。恢复后按主题读回历史来信，核对回执或群发记录，在新设备接入对应应用，再向已迁移的原聊天寄一封测试信。
- **改名后迁入。** 新装用 `--name 新主名 --alias 旧主名 --identity-key 原身份键`；对曾改名的对端，按导出记录设置 `--peer-identity-key` 和 `--peer-alias`。导入时另加 `--source-device 旧主名`。早期放在角色名下且缺少设备设置的信箱可在导出时用 `--device 原设备角色` 指定。快照包含信箱、回执、群发记录和转寄原件；正文引用的外部文件需要单独迁移。
- **旧角色地址。** 从按角色寻址的版本升级、且仍有旧客户端连接时，保留两台机原来的角色及身份键。在升级命令中使用 `--keep-legacy-clients --device 本机旧角色 --identity-key 本机旧角色 --peer 对端主名 --peer-alias 对端旧角色 --peer-identity-key 对端旧角色 --peer-ssh 对端SSH别名`。两端分别核对原信与回信路径。
- **已有依赖目录出现变更。** 默认升级会检查已有依赖。若显示 `The existing deps directory has been changed`，先比较 `release.json` 与已核验发行包的依赖摘要，核对原 `deps/.xdev-deps.json` 记录的受管文件、内容、实路径和额外文件的归属。只有在受管文件完整、额外内容属于已确认的其他应用、旧 MCP 依赖与新版相符时，才可用原升级参数加 `--cli-only` 保留共用目录，并在宿主内实际重连验证。来源或内容不符时先处理具体差异。
- **回退。** `install.py rollback --expect-current 当前摘要` 恢复上一已安装的运行时；再在客户端重连并核对版本。客户端配置单独使用该次 `apply` 生成的变更记录恢复。

## 第三方组件

xdev 维护设备配对、文件与任务操作、会话寻址、投递和回执逻辑，以及 CLI、MCP 工具和客户端适配器。基础命令使用 Python 标准库。远端文件和命令使用系统的 OpenSSH/SFTP。默认的 Python 扩展组件由发布包离线提供。

### 离线依赖

xdev 自有源码采用 MPL-2.0，许可全文和推荐引用信息分别位于安装包根目录的 `LICENSE` 和 `CITATION.cff`。Windows Pair 1.1.0-preview.13 随附的 31 个上游 Python wheel 采用各自的许可。解开 `assets/optional-deps.zip` 可查看 `wheelhouse/`、逐文件 SHA-256 清单和 `requirements.lock.txt`。wheel 本身也是 ZIP，其中的 `*.dist-info/METADATA` 记录版本与发布信息，`*.dist-info/licenses/` 和个别源码目录保存上游许可及声明。

| 上游包声明的许可 | 包名和版本 |
| --- | --- |
| MIT | annotated-types 0.8.0、anyio 4.16.0、attrs 26.1.0、h11 0.16.0、httpx-sse 0.4.3、jsonschema 4.26.0、jsonschema-specifications 2025.9.1、mcp 1.27.1、pydantic 2.14.3、pydantic_core 2.50.4、pydantic-settings 2.16.3、PyJWT 2.15.1、referencing 0.37.0、rpds-py 2026.9.1、typing-inspection 0.4.5 |
| BSD-3-Clause | click 8.5.1、httpcore 1.0.9、httpx 0.28.1、idna 3.20、pycparser 3.12、python-dotenv 1.2.4、sse-starlette 3.7.0、starlette 1.10.0、uvicorn 0.54.0 |
| Apache-2.0 | nats-py 2.14.0、python-multipart 0.0.34 |
| Apache-2.0 或 BSD-3-Clause | cryptography 50.0.2 |
| MIT-0 | cffi 2.1.1 |
| MPL-2.0 | certifi 2026.10.23 |
| PSF-2.0 | typing_extensions 4.16.0 |
| PSF，部分组件另有许可 | pywin32 312 |

模型上下文协议（MCP）的 Python SDK 提供本机工具接口。NATS Python 客户端用于可选的 JetStream 消息传输。两者均按各自项目的许可分发。[MCP Python SDK](https://pypi.org/project/mcp/1.27.1/) · [NATS Python 客户端](https://pypi.org/project/nats-py/2.14.0/)

### 许可文本和对应源码

- **nats-py 与 OpenSSL。** nats-py 的 Python 源文件保留 The NATS Authors 的版权声明；其 2.14.0 wheel 将许可标为 Apache-2.0。随包可离线阅读同一许可的完整文本，位置为 `python_multipart-0.0.34.dist-info/licenses/LICENSE.txt`，在 python-multipart 的 wheel 内。cryptography 的 Windows wheel 静态链接 OpenSSL 4.0.3，cryptography 的两种许可和 OpenSSL 的 Apache-2.0 许可分别适用。[nats-py 2.14.0 许可](https://github.com/nats-io/nats.py/blob/v2.14.0/LICENSE) · [OpenSSL 4.0.3 许可](https://github.com/openssl/openssl/blob/openssl-4.0.3/LICENSE.txt) · [OpenSSL 贡献者](https://github.com/openssl/openssl/blob/openssl-4.0.3/AUTHORS.md)
- **certifi。** wheel 内的 `certifi/cacert.pem` 保留 Mozilla 根证书数据，`*.dist-info/licenses/LICENSE` 给出来源及 MPL-2.0 声明。该版本的完整 Python 和证书包源文件可直接下载。[certifi 2026.10.23 对应源码](https://files.pythonhosted.org/packages/fd/11/b524cbd9bbdaca58608e4cf4053fd463417549e1959fa8d3aa18319f9555/certifi-2026.10.23.tar.gz) · [MPL-2.0](https://www.mozilla.org/MPL/2.0/)
- **pywin32。** 该包整体元数据标为 PSF。所含 `adodbapi/` 随 wheel 以 Python 源码提供，采用 LGPL-2.1-or-later，许可全文在 `adodbapi/license.txt`。MAPI Stub Library 的 MIT 声明在 `win32comext/mapi/NOTICE.md` 及同目录的 `MAPIStubLibrary-License.txt`。Scintilla、PythonWin、COM 等保留各自随附的文本。[pywin32 build 312 对应源码](https://github.com/mhammond/pywin32/tree/b312)
- **cffi。** cffi 自身声明 MIT-0。它的 Windows x64 构建还使用上游源码包中 `src/c/libffi_x86_x64/` 的 libffi 代码。该部分的声明列于下方。[cffi 2.1.1 对应源码](https://files.pythonhosted.org/packages/9e/ef/008a1939e372c06329a3fce4279c02f328488f3526744906eeec3da7ad5f/cffi-2.1.1.tar.gz)

<details>
<summary>libffi x86/x64 版权与许可原文</summary>

```text
ffi.c - Copyright (c) 1996, 1998, 1999, 2001  Red Hat, Inc.
Copyright (c) 2002  Ranjit Mathew
Copyright (c) 2002  Bo Thorsen
Copyright (c) 2002  Roger Sayle

libffi 2.00-beta - Copyright (c) 1996-2003  Red Hat, Inc.

ffi_common.h - Copyright (c) 1996  Red Hat, Inc.

ffitarget.h - Copyright (c) 1996-2003  Red Hat, Inc.

prep_cif.c - Copyright (c) 1996, 1998  Red Hat, Inc.

types.c - Copyright (c) 1996, 1998  Red Hat, Inc.

win32.S - Copyright (c) 1996, 1998, 2001, 2002  Red Hat, Inc.
Copyright (c) 2001  John Beniton
Copyright (c) 2002  Ranjit Mathew

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
``Software''), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED ``AS IS'', WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL CYGNUS SOLUTIONS BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.
```

</details>
