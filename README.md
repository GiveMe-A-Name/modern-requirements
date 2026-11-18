# modern-requirements

用结构化 JSON 向 `web-infra-dev/modern.js` 提交功能需求。CLI 将需求保存为 GitHub Issue，使用 Modern.js 的功能建议标题、正文格式和标签。编辑直接更新 Issue，审批和进度记录在标签中，验收证据记录在状态流转评论中。

## 安装

项目需要 Python 3.12 或更高版本。运行 GitHub 操作还需要安装 `gh` 并登录 `github.com`。

在源码仓库中运行以下命令，把 CLI 安装到 uv 管理的独立环境。安装后可在任意目录运行 `modern-requirements`。

```sh
uv tool install .
gh auth login --hostname github.com
modern-requirements --help
```

要把工具交给另一台机器，先用 `uv build --wheel` 构建 wheel，再在目标机器上安装这个文件。macOS、Linux 和 Windows 可使用同一个 wheel。

```sh
uv tool install path/to/modern_requirements-0.1.1-py3-none-any.whl
```

## 提交需求

`create --help` 提供待审批需求和已批准需求的 JSON 示例。保存示例并换成真实需求内容后，可以提交一个待审批的功能建议。

```sh
modern-requirements create --help
modern-requirements create --input idea.json --registration-source human --decision pending
modern-requirements list --decision pending
```

创建和修改需求需要 Modern.js 仓库的写权限。第一次提交会添加缺少的工作流标签。提交成功时返回 Issue 编号和链接。用 `get` 查看详情和评论，用 `edit` 更新或审批需求，用 `transition` 流转状态。

## 开发

```sh
uv sync --locked
uv run --locked modern-requirements --help
uv run --locked python -m unittest discover -v
```
