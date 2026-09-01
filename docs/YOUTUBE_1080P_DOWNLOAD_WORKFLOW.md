# YouTube 高清视频下载 SOP（Windows / 剪映工作流）

本文记录可跨设备复用的 YouTube 完整视频下载流程。目标是取得高清原视频、正确的原语言音频，并完成无重编码合并和完整性验收。

本流程只应用于自己拥有下载、编辑和发布权限的内容。

## 1. 最终结论

稳定流程如下：

```text
在目标 YouTube 视频页手动导出 Cookie
→ 用 yt-dlp 查询该视频的全部格式
→ 人工选择最高 H.264/MP4 画面和正确语言的 AAC/M4A 音频
→ 获取两条短期有效直链（只放在 PowerShell 内存变量中）
→ 分块并行下载画面和音频
→ FFmpeg 无重编码合并为 MP4
→ ffprobe 检查参数
→ FFmpeg 全片解码验收
→ 计算 SHA-256
```

> 注意：格式编号只对当前视频和当前查询结果有效。每条新视频都必须重新运行 `-F` 查询，不得硬编码沿用。

## 2. 哪些步骤必须每条视频手动完成

### 2.1 Cookie 手动导出

Cookie 本质上属于 Google/YouTube 登录会话，并不严格绑定某个视频。但 YouTube 会轮换会话信息，而且 Windows 新版 Chrome 的 App-Bound Encryption 使 `--cookies-from-browser chrome` 无法稳定解密 Cookie。

因此本工作流采用保守规则：

- 每下载一条新视频，都在该视频页面重新导出一次 Cookie。
- 下载前确认该视频能在 Chrome 中正常播放。
- Cookie 仅保存在本机临时文件，不复制到聊天、文档、代码或 Git 仓库。

### 2.2 格式选择

每条视频的最高分辨率、编码、音轨语言和格式编号都可能不同，必须人工检查：

- 是否真的提供 1080p、1440p 或 4K；
- 最高画质是 H.264、VP9 还是 AV1；
- 是否有多个语言音轨；
- 哪条音轨标记为 `original (default)`；
- 剪映兼容性和最高画质之间如何取舍。

### 2.3 临时直链

`--get-url` 得到的 `googlevideo.com` 地址带短期签名，会过期，而且每个视频、每个格式都不同。

- 不要把直链写进脚本或文档。
- 不要把直链提交到 Git。
- 地址失效或返回 403 时，重新运行 `--get-url`。

## 3. 已验证的本机环境

参考环境（版本不必完全相同，但应记录实际版本）：

| 工具 | 版本 | 用途 |
|---|---:|---|
| Windows | Windows 11 | 运行环境 |
| Python | 3.12.10 | yt-dlp 和并行下载脚本 |
| yt-dlp | 2026.08.19 | 解析 YouTube、查询格式、取得媒体直链 |
| requests | 2.32.5 | HTTP Range 并行下载 |
| Node.js | 24.14.0 | YouTube EJS 挑战求解 |
| FFmpeg / ffprobe | 8.0.1 | 合并、探测和全片解码验收 |

检查命令：

```powershell
python --version
python -c "import yt_dlp, requests; print('yt-dlp', yt_dlp.version.__version__); print('requests', requests.__version__)"
node --version
ffmpeg -version
ffprobe -version
```

缺少 Python 包时：

```powershell
python -m pip install -U yt-dlp requests
```

## 4. 手动获取 YouTube Cookie

### 4.1 安装正确的 Chrome 扩展

扩展名称必须是：

```text
Get cookies.txt LOCALLY
```

Chrome 商店地址：

```text
https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
```

不要安装名称相近但不带 `LOCALLY` 的旧扩展。

### 4.2 导出步骤

1. 在 Chrome 登录 YouTube。
2. 打开要下载的目标视频，并播放几秒确认可正常访问。
3. 保持目标 YouTube 视频标签页为当前活动页。
4. 点击 `Get cookies.txt LOCALLY` 扩展。
5. 选择 Netscape 格式。
6. 点击导出文件，不要复制 Cookie 内容到剪贴板或聊天。
7. 保存到本机下载目录，例如：

```text
$env:USERPROFILE\Downloads\www.youtube.com_cookies.txt
```

### 4.3 防止从错误标签页导出

如果当前标签页是 Chrome 应用商店，导出的文件可能叫：

```text
chromewebstore.google.com_cookies.txt
```

该文件不包含 YouTube 登录会话，不能用于下载。

可以只检查域名、不显示 Cookie 值：

```powershell
$cookieFile = Join-Path $env:USERPROFILE "Downloads\www.youtube.com_cookies.txt"

Get-Content -LiteralPath $cookieFile |
  Where-Object { $_ -and -not $_.StartsWith('#') } |
  ForEach-Object { ($_ -split "`t")[0] } |
  Sort-Object -Unique
```

正确文件至少应看到：

```text
.youtube.com
```

### 4.4 Cookie 安全规则

- Cookie 等同于临时登录凭证，不要把内容粘贴到聊天。
- 不要放在工作区、项目目录或 Git 仓库里。
- 不要通过网盘、邮件或即时通信传输。
- 下载完成后删除本地 Cookie 文件。
- 如果 Cookie 曾被粘贴、上传或泄露，仅删除文件不够；应在 Google 账户安全设置中注销相应会话并重新登录。

## 5. 为新视频设置本次变量

以下命令在 PowerShell 中执行。每条视频只需要修改开头几项：

```powershell
$workspaceRoot = (Get-Location).Path
$videoUrl = "https://www.youtube.com/watch?v=替换为视频ID"
$videoId = "替换为视频ID"
$cookieFile = Join-Path $env:USERPROFILE "Downloads\www.youtube.com_cookies.txt"
$sourceDir = Join-Path $workspaceRoot "$($videoId)_run\source"

New-Item -ItemType Directory -Force -Path $sourceDir | Out-Null
```

如果新项目没有采用 `$videoId` 命名目录，可直接把 `$sourceDir` 指向实际项目的 `source` 文件夹。

## 6. 查询该视频的可用格式

```powershell
Set-Location $workspaceRoot

python -m yt_dlp `
  --cookies $cookieFile `
  --js-runtimes node `
  --remote-components ejs:github `
  --no-playlist `
  -F `
  $videoUrl
```

### 6.1 剪映优先选择规则

画面优先顺序：

1. 达到目标分辨率；
2. `EXT` 为 `mp4`；
3. `VCODEC` 以 `avc1` 开头，即 H.264；
4. 标记为 `video only`。

音频优先顺序：

1. 语言是所需原声；
2. 标记为 `original (default)`；
3. `EXT` 为 `m4a`；
4. `ACODEC` 为 `mp4a.40.2`，即 AAC；
5. 标记为 `audio only`。

格式表示例（仅说明字段，不可直接照搬编号）：

```text
<video_format>  mp4  1920x1080  avc1...    video only
<audio_format>  m4a  audio only  mp4a...    <source language> original
```

设置本次格式编号：

```powershell
$videoFormat = "<本次查询得到的视频格式编号>"
$audioFormat = "<本次查询得到的音频格式编号>"
$audioLanguage = "<ISO 639-2 语言代码>"
```

如果目标视频提供 1440p/4K，但只有 VP9 或 AV1，可先下载最高画质，确认剪映能否直接解码；不能时再单独转码为 H.264。不要为了容器扩展名是 MP4 就假定内部编码一定是 H.264。

## 7. 简单下载方式

网络速度正常时，可让 yt-dlp 自动完成下载和合并：

```powershell
$finalFile = Join-Path $sourceDir "youtube_high_quality.mp4"
$formatChoice = "$videoFormat+$audioFormat"

python -m yt_dlp `
  --cookies $cookieFile `
  --js-runtimes node `
  --remote-components ejs:github `
  --no-playlist `
  --continue `
  --retries 10 `
  --fragment-retries 10 `
  -f $formatChoice `
  --merge-output-format mp4 `
  -o $finalFile `
  $videoUrl
```

## 8. 分别下载画面与原语言音频

默认让 yt-dlp 管理临时直链，不把直链写入脚本、日志或仓库。网络较慢时可安装 `aria2c`，并保留下面的 `--downloader` 参数；未安装时删掉这两行即可。

### 8.1 下载画面

```powershell
$videoOnly = Join-Path $sourceDir "youtube_1080p_video_only.mp4"

python -m yt_dlp `
  --cookies $cookieFile `
  --js-runtimes node `
  --remote-components ejs:github `
  --no-playlist `
  --continue `
  --retries 10 `
  --downloader aria2c `
  --downloader-args "aria2c:-x 8 -s 8 -k 1M" `
  -f $videoFormat `
  -o $videoOnly `
  $videoUrl
```

### 8.2 下载原语言音频

```powershell
$audioOnly = Join-Path $sourceDir "youtube_original_audio.m4a"

python -m yt_dlp `
  --cookies $cookieFile `
  --js-runtimes node `
  --remote-components ejs:github `
  --no-playlist `
  --continue `
  --retries 10 `
  --downloader aria2c `
  --downloader-args "aria2c:-x 8 -s 8 -k 1M" `
  -f $audioFormat `
  -o $audioOnly `
  $videoUrl
```

## 9. 无重编码合并为剪映用 MP4

```powershell
$finalFile = Join-Path $sourceDir "youtube_1080p_h264_original_audio.mp4"

ffmpeg -y -v warning `
  -i $videoOnly `
  -i $audioOnly `
  -map 0:v:0 `
  -map 1:a:0 `
  -c copy `
  -metadata:s:a:0 language=$audioLanguage `
  -movflags +faststart `
  $finalFile

if ($LASTEXITCODE -ne 0) {
  throw "FFmpeg 合并失败，退出码 $LASTEXITCODE"
}
```

`-c copy` 表示不重新压缩画面和声音，因此速度快，也不会引入二次画质损失。

## 10. 完整性验收

### 10.1 检查封装参数

```powershell
ffprobe -v error `
  -show_entries "format=filename,duration,size,bit_rate:stream=index,codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels,duration,nb_frames:stream_tags=language" `
  -of json `
  $finalFile
```

必须确认：

- 存在一条视频流和一条音频流；
- 分辨率符合目标；
- 视频、音频时长接近完整节目时长；
- 视频编码符合剪映要求；
- 音频语言正确；
- 高画质文件不是只有画面没有声音。

音频和视频尾部相差几十毫秒通常是正常封装尾差；明显偏差必须调查。

### 10.2 全片解码

```powershell
ffmpeg -v error `
  -i $finalFile `
  -map 0:v:0 `
  -map 0:a:0 `
  -f null NUL

if ($LASTEXITCODE -ne 0) {
  throw "全片解码失败，退出码 $LASTEXITCODE"
}

Write-Output "FULL_DECODE_OK"
```

该步骤会从头到尾读取并解码所有音视频数据，比只看文件大小可靠。

### 10.3 计算文件哈希

```powershell
Get-FileHash -Algorithm SHA256 $finalFile
```

## 11. 验收记录

把容器、编解码器、分辨率、帧率、帧数、音频语言、各流时长、文件大小、SHA-256 和全片解码结果写入当前项目的版本化 QA JSON。媒体文件和临时下载地址只留在本机运行目录，不进入 Git。

## 12. 常见故障和处理方法

### `Sign in to confirm you're not a bot`

原因通常是 Cookie 缺失、过期或从错误页面导出。

处理：

1. 在 Chrome 中确认目标视频能播放；
2. 保持目标视频为活动标签页；
3. 重新导出 Netscape Cookie；
4. 确认文件域名含 `.youtube.com`；
5. 重新运行 `-F`。

### `Could not copy Chrome cookie database`

Chrome 正在占用 Cookie 数据库。关闭 Chrome 有时可以解决文件锁问题，但新版 Windows Chrome 还可能遇到下一项 DPAPI 问题。

### `Failed to decrypt with DPAPI`

这是 Windows Chrome 新版 App-Bound Encryption 导致的限制。不要关闭系统安全功能，也不要修改注册表绕过。直接使用 `Get cookies.txt LOCALLY` 在 YouTube 页面手动导出。

### 格式列表只有 360p

可能原因：

- 未正确加载登录 Cookie；
- Cookie 过期；
- YouTube 当前只返回兼容流；
- 视频源本身没有更高分辨率。

重新导出 Cookie 后再次运行 `-F`。如果仍无高画质，则不要凭空放大为“原生 1080p”。

### 高画质文件没有声音

YouTube 的 1080p 及以上通常把画面和音频分开提供。必须分别下载 video-only 和 audio-only，再用 FFmpeg 合并。

### 下载速度只有几百 KiB/s

先尝试标准 yt-dlp。如果单连接长期被限速，可使用第 8 节的本地 HTTP Range 并行下载器。

### 并行下载返回 403 或地址过期

重新运行对应格式的 `--get-url`，立即开始下载。不要复用旧直链。

### 视频有多个音轨

不要只看格式编号。检查 `-F` 输出中的语言、`original (default)` 和编码。本次视频同时返回英语和西班牙语音轨，最终选择的是西班牙语 `140-1`。

## 13. 每条视频的完成清单

- [ ] 在目标 YouTube 视频页面重新导出 Cookie
- [ ] Cookie 文件含 `.youtube.com`
- [ ] Cookie 内容未粘贴、上传或写入项目
- [ ] 运行 `-F` 查询全部格式
- [ ] 确认真实最高分辨率
- [ ] 确认视频编码和剪映兼容性
- [ ] 确认原声音轨语言
- [ ] 分别下载画面和音频
- [ ] 并行下载器报告 `progress=100%` 和 `complete`
- [ ] FFmpeg 使用 `-c copy` 合并成功
- [ ] ffprobe 参数正确
- [ ] 全片音视频解码通过
- [ ] 记录 SHA-256
- [ ] 删除 Cookie 文件
- [ ] 如果 Cookie 曾暴露，注销对应 Google 会话并重新登录
