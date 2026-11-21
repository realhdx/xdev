# 独立浏览器试验 · Isolated browser experiment

用一份合成表单检验一个具体问题：两张页面标题相同、控件同名，能否把一条批注准确交给指定页？示例另起无窗口的 Chrome 或 Edge，在离线浏览器上下文中生成两页，等控件加载，填写并提交 A 页，核对 B 页仍为空，再关闭 A 并检查 B 仍可读取。只在屏幕上打印结果。它使用临时浏览器配置，无需 OpenCode Browse。

这是可单独运行的实验。发行版的 `xdev surface` 网页入口、已登录标签的授权和跨聊天投递见[产品手册](../../product/README.md#读取网页与应用窗口)。

在一处单独的工作目录中复制 `check.cjs`。准备 Node.js 20 或更高版本及已安装的 Chrome 或 Edge，运行以下命令。安装依赖时需要访问 npm。选择与本机浏览器对应的一行测试命令即可。

This example tests whether a note reaches the intended page when two pages have the same title and controls. It starts a separate headless browser with a temporary profile and creates two offline forms. It waits for the controls, submits a note on A, checks that B is untouched, then closes A and reads B again. It prints the result and closes its own browser. The experiment runs independently of OpenCode Browse. See the [product guide](../../product/README.en.md#read-web-pages-and-application-windows) for the released browser interface, authorized tabs and chat handoffs.

Copy `check.cjs` to a separate working directory. With Node.js 20 or later and a local installation of Chrome or Edge, install the pinned dependency from npm, then run the check for your browser.

```powershell
npm install --prefix . --no-save --package-lock=false --ignore-scripts playwright-core@1.65.1
node check.cjs msedge
node check.cjs chrome
```

成功时退出码为 0，输出包含 `page B stayed ready and open`。A 页操作或 B 页隔离检查未通过时会报告错误并以非零退出码结束。

On success, the script exits with code 0 and prints `page B stayed ready and open`. A failed interaction or isolation check produces an error and a nonzero exit code.
