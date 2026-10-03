<div align="center">

<img src="app/public/app-icon.png" alt="mai2srt" width="108" />

# mai2srt

### MAI-Transcribe-2 的桌面客户端

把音频或视频拖进窗口，得到词级时间戳的转录，按自己的习惯精修断句，导出标准 SRT。

转录免费 · 不需要 GPU · Windows 10 / 11

<br />

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](./LICENSE) [![Platform: Windows 10 / 11](https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-0078D6.svg?logo=windows11&logoColor=white)](#系统要求) [![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/downloads/) [![FFmpeg 9.0.2 bundled](https://img.shields.io/badge/FFmpeg-9.0.2%20bundled-007808.svg?logo=ffmpeg&logoColor=white)](https://ffmpeg.org/) [![Tauri 2.11](https://img.shields.io/badge/Tauri-2.11-24C8DB.svg?logo=tauri&logoColor=white)](https://tauri.app/)

<br />

**[下载安装包](../../releases/latest)** · [使用方法](#使用方法) · [从源码运行](#从源码运行) · [命令行](#命令行) · [English](README.en.md)

<br />

<img src="docs/image/readme/transcribe.webp" alt="mai2srt 转录页（转录进行中）" width="94%" />

<br />

**转录页：把音频或视频拖进来，点「开始转录」，进度、分块与日志都实时可见。**

</div>

---

## 它解决什么问题

[Microsoft AI Playground](https://playground.microsoft.ai/) 上的 MAI-Transcribe-2 转录免费、给词级时间戳、还能区分说话人，但它是网页：

- 单次上传限制 25 MiB / 约 60 分钟，超了得自己切
- 断句由服务端决定，切得不合适也改不了

mai2srt 把这些补齐：超限文件自动压缩、按静音分块，词级时间戳拼回完整时间轴；断句可以用大模型帮忙，也可以用纯规则；断句结果能在精修页逐行改，改完导出标准 SRT。

<table>
<tr>

<td width="25%" valign="top">

### 音视频通吃

常见音频（mp3 / wav / m4a / flac / ogg / opus / aac / wma）与视频（mp4 / mkv / webm / mov / avi 等）直接拖进来。

超过站点限制的文件自动压缩、按静音切块，时间戳拼回。

</td>

<td width="25%" valign="top">

### 说话人分离

转录时区分出的说话人会保留下来。几个人的话挨得太近、分不开时，会并成一条多行字幕，每行一个 `- ` 开头的短句。

</td>

<td width="25%" valign="top">

### 精修工作台

逐行改文字、拆分 / 合并 / 删除，听单句、点行跳播、跟随播放高亮，撤销重做整套都在。

</td>

<td width="25%" valign="top">

### 可选 LLM 断句

内置 DeepSeek / OpenAI / Claude / Gemini / OpenRouter / Ollama 预设，也可以填任意兼容端点。

不配也能出字幕。

</td>

</tr>
</table>

---

## 工作流程

```text
音频 / 视频
     │
     │  超出站点限制（25 MiB / 约 60 分钟）时：压缩 + 按静音分块
     ▼
转录       playground MAI-Transcribe-2  →  词级时间戳 + 说话人
     │
     ▼
断句       按标点和停顿切；用不用大模型都行，用了只是让它挑更合适的位置
     │
     ▼
精修       改文本 / 拆分 / 合并 / 删除 / 试听 / 点行跳播
     │
     ▼
导出       <音频名>.srt（精修另存 <音频名>_精修.srt）
```

## 外观

玫瑰（明亮）/ 余烬（暗黑）两套主题，各配 **液态玻璃** 与 **毛玻璃** 两种材质，壁纸也能换成自己的。主题可以跟随系统，也可以锁死明或暗。

<img src="docs/image/readme/settings-appearance.webp" alt="mai2srt 外观设置页（余烬 / 暗黑主题）" width="94%" />

---

## 精修页

这里是改字幕的地方。左栏是断句参数，改完可以重新断句；右栏逐行显示结果——点文字直接改，剪刀拆分、合并相邻行、删除多余行，点任意一行播放器就跳到那一句，可以边听边改。整套编辑都能撤销重做；「保存编辑结果」会把编辑记录一起存进项目（下次打开自动恢复，手动改过的行会标出来），满意之后「保存 .srt」导出。

<img src="docs/image/readme/refine.webp" alt="mai2srt 精修页（玫瑰 / 明亮主题）" width="94%" />

---

## 系统要求

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10 / 11（64 位） |
| 浏览器 | Microsoft Edge（系统自带）或 Chrome——登录与转录都经由它完成 |
| 账号 | 一个微软账号（playground 免费服务需要登录） |
| 网络 | 能访问 playground.microsoft.ai |

FFmpeg 已随安装包内置，**不需要**自行安装。

---

## 安装

1. 到 [Releases](../../releases) 下载最新的 `mai2srt_x.y.z_x64-setup.exe`
2. 双击运行。首次安装可能出现 Windows SmartScreen 蓝窗提示 → 点「更多信息」→「仍要运行」
3. 默认装到当前用户目录（`C:\Users\<你>\AppData\Local\mai2srt`），想换地方就在安装向导里改；装完开始菜单中会出现 mai2srt

> [!NOTE]
> 安装后第一次启动要等一下：后端进程首次拉起，杀毒软件扫描新文件时会更慢——窗口出现前会先显示一张「启动中」的卡片。之后启动很快。

---

## 使用方法

1. 点击启动后，去 **设置 → 文件与账号 → playground 账号**，随便输个名字，登录一个自己的微软账号。此处会弹出浏览器窗口，正常登录即可，之后登录状态会自动保持
2. 需要大模型帮忙断句的话，去 **设置 → 大模型 → 添加服务商**，选一个服务商、填上 API key，再把它设为当前模型。不配也行，只按标点和停顿来断，字幕照样出

    <img src="docs/image/readme/settings-llm.webp" alt="mai2srt 大模型设置页（余烬 / 暗黑主题）" width="94%" />

3. 去 **转录**页，把音频或视频文件拖进窗口（或点击选择），决定「LLM 断句」开关要不要开，点「开始转录」，耐心等结果
4. 转录结束后会得到两个文件：`.mai.json`（词级时间戳，存在项目库 `文档\mai2srt`）和 `.srt`（按断句结果生成，存在源文件旁）
5. 想让断句更精细一些，点「去精修」进 **精修**页，按需要编辑、合并、删除、拆分。调整字幕时可以边听边改，点任意一行会跳到那一句开头
6. 改完点「保存 .srt」，会在原音频 / 视频所在路径生成 `<原名>_精修.srt`（不影响上一步那个 `.srt`）

拖进来的如果是 `.mai.json` 而不是音视频，按钮会变成「生成字幕」：跳过转录、只重跑断句，不消耗转录额度，适合反复调参。

| 精修页快捷键 | 作用 |
| --- | --- |
| Ctrl + Z / Ctrl + Shift + Z（或 Ctrl + Y） | 撤销 / 重做 |
| Ctrl + S | 保存编辑结果（存档） |
| 编辑框内 Ctrl + Enter / Esc | 提交 / 取消本次文本编辑 |

---

## 数据位置与隐私

登录会话、项目文件与缓存全部保存在本机：

| 内容 | 位置 |
| --- | --- |
| 登录会话（cookie） | `~/.mai2srt/accounts/` |
| LLM 端点配置 | `~/.mai2srt/config.json` |
| 项目文件（mai.json 转录数据 + 精修记录） | `文档\mai2srt\`（可在设置页改位置） |
| 导出的 .srt | 源音频 / 视频旁（精修导出为 `<音频名>_精修.srt`；源文件不在时落在项目文件旁） |
| 音频提取缓存 | `~/.mai2srt/audio-cache/`（设置页可清理） |
| 日志 | `~/.mai2srt/logs/` |

音频只上传给 playground 的转录接口（受其服务条款约束）；设置页可开启「转录完成即删除 playground 云端会话记录」，其余一切都在本机。

多账号：`~/.mai2srt/accounts/` 下每个账号有独立的会话与浏览器配置，可在设置页切换。

---

## 卸载

Windows 设置 → 应用 → 已安装的应用 → mai2srt → 卸载（或在开始菜单右键卸载）。

卸载确认页上的「删除应用程序数据」勾选框（默认不勾）决定数据去留：

- **不勾选**：只卸载程序本身，界面偏好与全部用户数据原样保留，重装后直接可用
- **勾选**：界面偏好（主题 / 玻璃 / 壁纸 / 播放器设置）一并清除，并依次弹出三问，逐项决定用户数据去留（默认全保留）：
  1. **登录会话与应用设置**（`C:\Users\<你>\.mai2srt`：登录会话、大模型 API key、浏览器配置、日志）——关心隐私残留就选删
  2. **音频提取缓存**（视频音轨缓存，可随时重新提取，删了零损失）
  3. **字幕项目库**（`文档\mai2srt`：mai.json 转录数据与精修记录，**删了不可恢复**，默认推荐保留；媒体与导出的 .srt 在源文件旁，不受影响）

覆盖安装升级与静默卸载（`/S`）时，勾选框与三问均不生效，不删除任何数据。

---

## 从源码运行

需要 Python ≥ 3.10、Node ≥ 20、pnpm、Rust 工具链，以及 PATH 里的 FFmpeg——然后：

```powershell
git clone https://github.com/fuxiaomoke/mai2srt.git mai2srt
cd mai2srt
pip install -e .[dev]          # 后端 + mai2srt 命令（含 playwright；有 Edge/Chrome 即可，无需 playwright install）
cd app && pnpm install && cd ..
.\dev.ps1                      # 一条命令拉起 后端 + vite + tauri dev
```

要出安装包就跑 `scripts\release.ps1`（ffmpeg → 后端 sidecar → NSIS 安装包 → dist\）。加 `-SkipBackend` 只复用已冻结的后端产物，适用于前端 / NSIS / 配置迭代；**改动 `src/mai2srt/**` 的 Python 代码必须完整跑一次**，否则打出来的包仍是旧后端。

---

## 命令行

GUI 覆盖全部常用场景；CLI 适合写进脚本做自动化——一次一个文件，要批量可以写个循环去运行。`mai2srt` 命令由 Python 包提供（见上方「从源码运行」），安装包不会把它加进 PATH。日志同时输出到终端与 `~/.mai2srt/logs/mai2srt.log`，加 `-v` 得到调试级输出。

```powershell
# 登录：弹出浏览器完成微软账号登录，会话持久化（之后长期免登录）
mai2srt login
mai2srt login --account work     # 多账号：--account 后接任意名字，首次使用自动创建；各账号独立会话与浏览器配置，指定名字即切换

# 接受生物特征通知：playground 视音频为生物特征数据（BIPA），账号须先接受一次才能上传。
# 转录报 HTTP 451 / biometric-consent-required 时运行；新账号 login 后与转录遇到该错误时都会当场询问
mai2srt consent

# 转录：音频/视频 -> 词级时间戳 JSON（默认输出 <音频名>.mai.json，在源文件旁）
mai2srt transcribe "abc.wav"
mai2srt transcribe "abc.wav" --out D:\out\x.mai.json   # 指定输出
mai2srt transcribe "abc.wav" --keep-conversation       # 转录完保留云端会话（默认跟随应用内开关，默认=删除）
mai2srt transcribe "abc.wav" --include-raw             # JSON 内附带原始响应（调试用）

# 断句/后处理：mai.json -> SRT。纯离线，不重新转录、不消耗转录额度——调参迭代用它
mai2srt process "abc.mai.json"
mai2srt process "abc.mai.json" --out D:\out\abc.srt
mai2srt process "abc.mai.json" --no-llm                # 跳过 LLM，只用标点和停顿断句（无需配 key）

# 一条龙：音频/视频 -> 转录 + 断句 + SRT（默认输出 <音频名>.srt 和 .mai.json，都在源文件旁）
mai2srt run "abc.wav"
mai2srt run "abc.wav" --out D:\out\abc.srt --json D:\out\abc.mai.json
```

另有 `mai2srt serve --port 47613`：GUI 的后端服务进程，应用自己会拉起，一般无需手动运行（开发调试除外）。

<details>
<summary><strong>断句调参（process 与 run 共用，GUI 精修页的滑杆就是这些）</strong></summary>

<br />

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--max-duration` / `--max-chars` | 12 s / 60 | 行上限（时长或字数超限即拆分） |
| `--min-duration` / `--min-chars` | 1.2 s / 5 | 行下限（过短合并；`--min-chars 0` 禁用） |
| `--split-pause` | 0.8 s | 词间停顿达到该值即为候选切点 |
| `--merge-gap` | 0.8 s | 后处理智能合并可跨越的最大空隙 |
| `--tolerance` | 0.2 s | 多近算「同时说话」（用来把对白并成一条） |
| `--expand` | 0.25 s | 时间戳往外扩一点，让字幕别贴得太紧 |

</details>

<details>
<summary><strong>CLI 的 LLM 断句配置</strong></summary>

<br />

读取 `~/.mai2srt/config.json` 的 `llm` 段——在 GUI 设置页把某个模型设为「当前模型」后会自动写入，CLI 与 GUI 因此共用同一配置；也可手工编辑：

```jsonc
// ~/.mai2srt/config.json
{
  "llm": {
    "base_url": "https://api.deepseek.com",
    "api_key": "sk-...",
    "model": "deepseek-flash",
    "temperature": 0.0
  }
}
```

大模型只决定**在哪里断句**，不改动任何文字，也不动时间戳；没配或调用失败时自动退回纯标点断句，流程不会卡住。

</details>

---

## 常见问题

**Q：转录要花钱吗？**
A：不花。走的是 Microsoft AI Playground 的免费模型；不过它是微软的预览服务，随时可能变动或下线。

**Q：会上传我的文件到哪里？**
A：音频只上传给 playground 的转录接口（服务条款的约束对象）；可在设置页开启「转录完成即删除云端会话记录」。其余一切都在本机。

**Q：支持多长 / 多大的文件？**
A：站点原生限制 25 MiB / 60 分钟；超限由本工具自动压缩、按静音点分块处理，实际支持数小时级文件。

**Q：转录报 HTTP 451 / biometric-consent-required？**
A：playground 把音频视为生物特征数据（BIPA），账号需要先接受一次「生物特征通知」才能上传音频——网页端首次上传会弹同意框，本工具走无头通道看不到它，于是被 451 拒绝。处理方式：**设置 → 文件与账号 → 「接受生物特征通知」**（GUI；新账号登录完成时会自动检测并高亮提示该按钮），或运行 `mai2srt consent`（CLI；新账号登录后与转录遇到该错误时都会当场询问）。接受记录保存在微软账号上，一次生效，之后转录恢复正常。

**Q：安装包报毒？**
A：未签名的安装包偶尔会被杀软误报，属于常见现象；每个 Release 都附 `SHA256SUMS.txt`，可以校验下载是否完整。

**Q：断句一定要配大模型吗？**
A：不用。不配置也能出字幕，只按标点和停顿来断；配上以后，大模型只是帮你挑更合适的断句位置，其余流程一点不依赖它。

**Q：支持哪些大模型端点？**
A：OpenAI 兼容、Anthropic、Gemini 三种格式都能用。同一家换个格式，地址会自动跟着换，也能手填任何兼容地址。

---

## 友情链接

[Linux.Do](https://linux.do/) — 新的理想型社区

---

## 声明

本工具为个人学习与低频使用目的编写。playground 是 Microsoft 的免费预览服务，请遵守其服务条款，自行评估使用风险。

---

## 开源协议

mai2srt 以 [GNU 通用公共许可证第 3 版](./LICENSE) 发布。安装包内的第三方组件保留各自许可证，见 [THIRD-PARTY-NOTICES.md](./THIRD-PARTY-NOTICES.md)。

---

<div align="center">

<img src="app/public/app-icon.png" alt="mai2srt" width="72" />

### mai2srt

Windows 10 / 11 · 转录免费 · 不需要 GPU

<br />

**[下载安装包](../../releases/latest)** · [使用方法](#使用方法) · [回到顶部](#mai2srt)

</div>
