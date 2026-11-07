# XM_Studio

本机短剧制作工具，围绕单集制作提供从剧本到成片的工作流程：

**项目 → 单集 → 剧本 → 拆解 → 资产 → 视频 → 成片**

项目与测试记录保存在本机。制作团队可在各自电脑上记录提示词、资产图片和视频单元的修改，并导出记录供离线汇总。

## 项目资料

- [制作团队测试说明](docs/production-mvp/team-testing.md)

## Windows 下载与运行

ZIP 是源码包，首次运行需要安装依赖并构建页面。以下命令使用 **Windows PowerShell**，每条命令完成且没有报错后再执行下一条。

### 1. 安装运行工具

安装 [Node.js](https://nodejs.org/)（24.12 或以上版本）和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。Python 3.12 会在后续安装依赖时由 uv 按需下载，无需单独安装。

也可以在 PowerShell 中通过 WinGet 安装：

```powershell
winget install --exact --id OpenJS.NodeJS.LTS --source winget
winget install --exact --id astral-sh.uv --source winget
```

安装完成后关闭并重新打开 PowerShell，检查工具是否可用：

```powershell
node --version
npm.cmd --version
uv --version
```

### 2. 下载并解压

在[仓库首页](https://github.com/5JjiaLin/xm-studio)点击 **Code → Download ZIP**，右键 ZIP 选择「全部解压」。在解压后的 `xm-studio-main` 文件夹内打开 PowerShell；该文件夹应包含 `pyproject.toml`、`frontend` 和 `server`。

也可在 PowerShell 中进入文件夹，路径按实际解压位置修改：

```powershell
Set-Location "$HOME\Downloads\xm-studio-main"
```

### 3. 首次安装与构建

在项目根目录逐条执行，需要联网下载依赖：

```powershell
uv sync --python 3.12 --no-dev
npm.cmd install --global pnpm@10.33.2
Set-Location frontend
pnpm.cmd install --frozen-lockfile
pnpm.cmd build
Set-Location ..
```

看到构建成功，并回到包含 `pyproject.toml` 的项目根目录后，再启动服务。

### 4. 启动并打开页面

每次启动都在项目根目录执行以下命令：

```powershell
$env:AUTH_ENABLED = "false"
$env:ARCREEL_DATA_DIR = "projects"
$env:ARCREEL_PROFILE_DIR = "agent_runtime_profile"
uv run --no-dev uvicorn server.app:app --host 127.0.0.1 --port 1242
```

出现 `Application startup complete` 后，在浏览器打开：

**[http://127.0.0.1:1242/app/projects](http://127.0.0.1:1242/app/projects)**

此启动方式仅监听本机，免登录。运行期间保持 PowerShell 窗口打开；按 `Ctrl+C` 停止服务。以后启动只需进入项目根目录，重复本步骤，无需每次重新安装和构建。

### 5. 配置模型并开始制作

1. 进入「模型配置」，填写供应商、API Key、接口地址和模型，测试连接；按制作需要配置文本、图像和视频能力。
2. 新建项目和单集，粘贴或上传剧本，按「剧本 → 拆解 → 资产 → 视频 → 成片」操作。
3. 检查并保存完整提示词，再明确点击生成或重新生成。模型调用使用所配置供应商的额度。
4. 已保存的制作修改会自动记录。完成后，在项目管理页点击「导出修改记录」，选择项目并下载 ZIP，交给负责人汇总。详见[制作团队测试说明](docs/production-mvp/team-testing.md)。

项目素材和修改记录保存在本机 `projects/` 目录，API Key 通过本机模型配置保存。保留整个项目目录以便下次继续制作；换电脑或更新源码前备份本机配置和 `projects/`。

### 常见问题

| 情况 | 处理方式 |
| --- | --- |
| 找不到 `node`、`npm.cmd` 或 `uv` | 安装对应工具后重新打开 PowerShell，再检查版本。 |
| 提示禁止运行 `npm.ps1` 或 `pnpm.ps1` | 使用本文的 `npm.cmd`、`pnpm.cmd` 命令，无需修改系统执行策略。 |
| 页面无法访问，或缺少前端页面 | 先确认 `pnpm.cmd build` 成功且 `frontend/dist/index.html` 存在，再重新启动服务。 |
| 1242 端口被占用 | 先关闭此前启动的 XM_Studio；也可把启动命令端口改为 1243，并打开 `http://127.0.0.1:1243/app/projects`。 |
| 下载依赖失败 | 检查网络连接后重试失败的安装命令。 |

本说明已按仓库依赖和启动入口核对；Windows 真机完整制作流程仍待测试团队验证。

## 在剪映中继续剪辑

确认采用的视频后，在「成片」页保存片段顺序、裁剪、音量和字幕，然后点击 **「在剪映建立草稿」**。草稿保留分段视频和独立字幕，方便继续编辑。

1. 首次展开「本机剪映设置」，确认剪映应用位置、草稿根目录，点击「保存并启用」。若找不到草稿索引，先在剪映建立一个空草稿并退出，再检查路径。
2. 点击「在剪映建立草稿」后，XM 按已保存的时间线复制素材并准备工程。剪映仍在运行时会等待；请保存并正常退出剪映，再点击「继续登记 / 重试」。
3. 登记成功后点击「打开剪映」，在首页按 XM 显示的草稿名称打开，继续剪辑并导出。
4. XM 内修改时间线后，可点击「建立新版本」。新版本独立建立，已交付草稿中的人工修改会保留。进入页面、刷新或视频生成完成都不会自动登记。

MP4 渲染和剪映草稿交接互相独立。当前已实测 Apple Silicon Mac 上的剪映 **11.7.0**；Windows 已提供适配及自动测试，**真机打开、编辑和导出仍待验收**。XM 与剪映需运行在同一台电脑才能登记本机草稿箱；远程部署仅提供含素材的草稿包，迁移后需调整素材路径并自行登记。

## 许可证

许可证及第三方组件说明见 [LICENSE](LICENSE)、[NOTICE](NOTICE) 和 [LICENSES](LICENSES/)。
