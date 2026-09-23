# 安装与设备重新绑定

工作台采用“代码从 Git 重建、项目从迁移包恢复、凭证在每台设备重新绑定”的模式。不要复制虚拟环境、`node_modules`、Cookie 或 API Key。

## 系统要求

- Windows 11 或支持 PowerShell 7 的 Windows 环境
- Python 3.12.x
- Node.js 22.13 或更高版本
- FFmpeg 与 FFprobe，均需在 `PATH` 中
- 使用链接下载时需要 yt-dlp；只导入本地视频时可暂不安装

本地 Qwen3-TTS、Kokoro、CosyVoice 受 GPU、CUDA 和 Torch 版本影响，不纳入核心环境。只使用云端翻译与 MiniMax 语音时不需要安装这些模型。

## 可复现安装

在仓库根目录执行：

```powershell
.\scripts\bootstrap.ps1
```

默认安装：

- `requirements-dev.lock` 中固定的 Python 3.12 依赖；
- `package-lock.json` 中固定的 Node 依赖；
- 本地 `.venv`，不会进入 Git。

只安装运行依赖可使用 `-Profile core`；需要 CPU 媒体分析依赖可使用 `-Profile media`。GPU TTS 引擎仍需按设备单独建立环境。

## 设备级设置

`.env.example` 只列出变量名，不会被自动加载。真实值应由操作系统凭证管理器、受限的进程环境或未来的设置界面保存。

核心绑定：

- `DUB_PROJECT_CONFIG`：当前项目的本机配置 JSON；
- `DUB_WORKBENCH_LIBRARY_DIR`：可选的视频项目资料库根目录；
- `DUB_WORKBENCH_STATE_DIR`：设备本地状态目录；
- `DUB_WORKBENCH_DB`：可选的 SQLite 数据库路径；
- `DUB_WORKBENCH_PYTHON`：可选的 Python 可执行文件；未设置时依次使用 `.venv` 和 `PATH`。
- `DUB_CREDENTIAL_BACKEND`：留空时必须使用系统密钥链；仅在明确接受服务重启后重输 Key 时设置为 `memory`。

供应商凭证：

- `OPENAI_API_KEY`
- `MINIMAX_API_KEY`

本地模型：

- `DUB_QWEN_MODEL_DIR`
- `DUB_KOKORO_MODEL_DIR`
- `DUB_COSYVOICE_MODEL_DIR`
- `DUB_COSYVOICE_REPO_DIR`
- `DUB_QWEN_PYTHON`
- `DUB_KOKORO_PYTHON`
- `DUB_COSYVOICE_PYTHON`

API Key 和 Cookie 不得写入项目配置、日志、迁移包或 Git。凭证必须通过系统密钥链、设置页或当前进程环境提供；通用启动脚本不会读取仓库旁的 Key 文本文件。

## 诊断

基础诊断：

```powershell
.\scripts\diagnose.ps1 -Strict
```

迁移项目正式接管前，检查项目绑定和指定供应商：

```powershell
.\scripts\diagnose.ps1 -RequireProject -Provider minimax -Strict
python -m backend.portability.cli rebind <项目目录> --workbench-root . --provider minimax
```

诊断报告只显示“已配置／未配置”和“存在／缺失”，不会输出凭证值或文件系统路径。`rebind` 通过后会在项目的 `.dub-workbench` 目录写入本机回执；该目录不会再次进入迁移包。

## 启动

```powershell
.\start_workbench.ps1 -ProjectConfig <设备本地项目配置.json>
```

前后端就绪后访问 `http://127.0.0.1:3000/`。关闭时运行：

```powershell
.\stop_workbench.ps1
```

如果依赖或路径变更，先重新运行诊断。不要通过复制另一台机器的虚拟环境来修复。
