"""User-facing log copy, in the language the interface is set to.

Two audiences, two languages:

- the **user log** (the lines the Transcribe page shows) is for the person
  running the job, so it follows their interface language. It used to be a
  mix -- English technical lines with Chinese sentences dropped in between --
  which read as broken rather than as bilingual.
- the **detail log** and the file log stay English: those are read by whoever
  debugs the run, and a stable single language there is worth more than
  translating them.

Keys are flat and stable; they end up in pasted bug reports, so renaming one
is a (small) compatibility break.
"""
from __future__ import annotations

from typing import Any

LANGS = ("zh", "en")

_ZH: dict[str, str] = {
    # --- media preparation -------------------------------------------------
    "media.analyzing": "正在分析音频断点（只解码音轨）",
    "media.no_pauses": "没找到可用的停顿点，改为按时长均分切块（不影响转录结果）",
    "media.plan": "切块：{n} 段，各约 {span}",
    "media.encode": "编码第 {i}/{n} 段（{span}）",
    "media.encoded": "第 {i}/{n} 段就绪（{size:.1f} MiB）",
    "media.passthrough": "文件在限制内，直接上传",
    "media.compressing": "压缩为 mp3 后上传",
    "span.secs": "{n:.0f} 秒",
    "span.mins": "{n:.1f} 分钟",
    # --- transcription -----------------------------------------------------
    "run.source": "源文件：{name}  {dur:.1f} 秒  {ch} 声道  {codec}  {size:.1f} MiB",
    "run.upload_plan": "将上传 {n} 个文件",
    "run.prep_failed": "音频准备失败：{err}",
    "run.not_signed_in": "登录失效：请先在设置页重新登录",
    "run.chunk_done": "第 {i}/{n} 段完成：{utts} 句 / {words} 词",
    "run.chunk_silent": "第 {i}/{n} 段里没有人声，已跳过，其它片段照常",
    "run.chunk_no_words": "第 {i} 段没有返回词级时间戳，已中止",
    "run.cleanup_failed": "会话清理删除失败：该转录记录仍留在 playground 上，"
                          "可稍后在网页手动删除",
    "run.no_speech": "整段音频里没有检测到人声，可能是纯音乐或静音，没有可生成的字幕",
    "run.consent_needed": "需要先接受一次生物特征通知（设置页 → 「接受生物特征通知」，"
                          "或命令行 mai2srt consent），然后重试转录",
    "run.segmented": "断句完成：{n} 行",
    "run.seg_absorb": "碎段回吸：{before} 段 → {after} 段",
    "run.silence_split": "长静默拆行：{cut} 行被切开（{before} → {after} 行）",
    "run.tiny_folded": "单字残行回吸：{n} 行",
    "run.postprocessed": "后处理完成：{n} 条（其中对话 {dialogue} 条）",
    "run.llm_missing": "没有配置大模型端点，改用确定性标点断句",
    "run.llm_active": "大模型断句：{model} @ {url}（{proto}，effort={effort}）",
    "run.wrote_json": "已写出 {path}（{words} 词）",
    "run.wrote_srt": "已写出 {path}",
    "run.llm_degraded": "大模型断句未完全生效：{err}",
    "run.auth_status": "登录状态：{status}",
    # --- biometric notice --------------------------------------------------
    "consent.no_need": "该账号无需接受生物特征通知",
    "consent.required": "playground 要求接受生物特征通知"
                        "（音频按生物特征数据处理，辖区 {jurisdiction}）",
    "consent.accepted": "已接受生物特征通知，上传已解锁",
    "login.consent_needed": "检测到该账号尚未接受生物特征通知"
                            "（音频按生物特征数据处理），首次转录前需要接受一次",
    "login.consent_accepted": "已接受生物特征通知，上传已解锁",
    "login.consent_hint": "先在设置页点「接受生物特征通知」再开始转录",
    "login.consent_check_failed": "生物特征通知状态查询失败（不影响登录）：{err}",
    # --- errors shown to the user -----------------------------------------
    # The exception still carries the technical English message (raw bodies,
    # ffmpeg stderr) for the log and the detail stream; these are the one-line
    # human summaries the UI shows instead of a protocol dump.
    "err.audio_missing": "找不到这个音频文件：{path}",
    "err.input_missing": "找不到这个文件：{path}",
    "err.not_signed_in": "登录失效：请先在设置页重新登录",
    "err.login_incomplete": "登录未完成：浏览器窗口被关闭或等待超时",
    "err.no_timestamps": "第 {i} 段没有返回词级时间戳，无法生成字幕",
    "err.bad_prepared_file": "内部错误：准备出的文件格式不对（{name}），请反馈",
    "err.upload_step_failed": "上传这一步被服务端拒绝了（HTTP {status}）",
    "err.upload_protocol": "服务端返回的上传结果格式变了，可能是 playground 更新了",
    "err.stream_failed": "转录请求被服务端拒绝（HTTP {status}）",
    "err.stream_protocol": "服务端没有返回转录结果，可能是 playground 更新了",
    "err.upload_too_big": "音频超过 25 MiB 上传上限（{size:.1f} MiB），"
                          "这是分块计算的问题，请反馈",
    "err.consent_required": "这个账号需要先接受一次 playground 生物特征通知："
                            "设置 → 文件与账号 →「接受生物特征通知」，"
                            "或命令行 mai2srt consent，然后重试",
    "err.upload_rejected": "服务端拒绝了这段音频（HTTP {status}），重试也没用",
    "err.upload_failed": "连续 {n} 次上传都失败了",
    "err.consent_http": "生物特征通知接口报错（HTTP {status}）",
    "err.consent_bad_response": "生物特征通知接口返回了无法解析的内容",
    "err.scan_failed": "音频分析失败（ffmpeg 返回 {rc}）",
    "err.chunk_too_big": "切出来的第 {i} 段有 {size:.1f} MiB，超过上传上限",
    "err.encode_failed": "音频转码失败（ffmpeg 报错）",
    "err.compress_too_big": "压缩后仍有 {size:.1f} MiB，超过上传上限",
    "err.probe_failed": "读取音频信息失败（ffprobe 报错）",
    "err.no_audio_stream": "这个文件里没有音频轨",
    "err.probe_parse": "读不懂 ffprobe 的输出",
    "err.ffmpeg_missing": "找不到 {tool}：请安装 FFmpeg 或把它加进 PATH",
    "err.bad_json": "这个 .mai.json 读不出来：可能不是本工具生成的文件，"
                    "或者文件已损坏",
    "err.file_write": "写文件失败：{path}（可能被占用或没有写权限）",
}

_EN: dict[str, str] = {
    "media.analyzing": "Analyzing the audio for pauses (audio track only)",
    "media.no_pauses": "No usable pauses found; splitting into even pieces "
                       "instead (the result is unaffected)",
    "media.plan": "{n} pieces, about {span} each",
    "media.encode": "Encoding piece {i}/{n} ({span})",
    "media.encoded": "Piece {i}/{n} ready ({size:.1f} MiB)",
    "media.passthrough": "Within the upload limits; sending as-is",
    "media.compressing": "Compressing to mp3 for upload",
    "span.secs": "{n:.0f}s",
    "span.mins": "{n:.1f} min",
    "run.source": "Source: {name}  {dur:.1f}s  {ch}ch  {codec}  {size:.1f} MiB",
    "run.upload_plan": "Uploading {n} file(s)",
    "run.prep_failed": "Media preparation failed: {err}",
    "run.not_signed_in": "Session expired — sign in again in Settings",
    "run.chunk_done": "Piece {i}/{n} done: {utts} utt(s) / {words} words",
    "run.chunk_silent": "Piece {i}/{n} had no speech; skipped, the rest is "
                        "unaffected",
    "run.chunk_no_words": "Piece {i} returned no word timestamps; aborting",
    "run.cleanup_failed": "Conversation cleanup failed: this transcript is "
                          "still on playground — delete it there later",
    "run.no_speech": "No speech detected in the whole file (it may be music "
                     "or silence) — nothing to subtitle",
    "run.consent_needed": "This account must accept the playground biometric "
                          "notice once (Settings → accept biometric notice, or "
                          "`mai2srt consent`), then retry",
    "run.segmented": "Segmented: {n} lines",
    "run.seg_absorb": "Fragments absorbed: {before} runs → {after} runs",
    "run.silence_split": "Long-silence split: {cut} line(s) cut ({before} → {after})",
    "run.tiny_folded": "One-character lines folded into neighbours: {n}",
    "run.postprocessed": "Post-processed: {n} entries ({dialogue} dialogue)",
    "run.llm_missing": "No LLM endpoint configured; using deterministic "
                       "punctuation split",
    "run.llm_active": "LLM split-point selection: {model} @ {url} "
                      "({proto}, effort={effort})",
    "run.wrote_json": "Wrote {path} ({words} words)",
    "run.wrote_srt": "Wrote {path}",
    "run.llm_degraded": "LLM split only partially applied: {err}",
    "run.auth_status": "Auth status: {status}",
    "consent.no_need": "This account needs no biometric notice",
    "consent.required": "playground requires the biometric notice "
                        "(audio is biometric data; jurisdiction {jurisdiction})",
    "consent.accepted": "Biometric notice accepted — uploads unlocked",
    "login.consent_needed": "This account has not accepted the playground "
                            "biometric notice (audio counts as biometric "
                            "data) — it is needed once before the first "
                            "transcription",
    "login.consent_accepted": "Biometric notice accepted — uploads unlocked",
    "login.consent_hint": "Accept the biometric notice in Settings, then "
                          "start the transcription",
    "login.consent_check_failed": "Could not check the biometric notice "
                                  "(login is unaffected): {err}",
    # --- errors ------------------------------------------------------------
    "err.audio_missing": "Audio file not found: {path}",
    "err.input_missing": "File not found: {path}",
    "err.not_signed_in": "Session expired — sign in again in Settings",
    "err.login_incomplete": "Sign-in did not complete: the browser window was "
                            "closed or the wait timed out",
    "err.no_timestamps": "Piece {i} came back without word timestamps, so no "
                         "subtitles can be built",
    "err.bad_prepared_file": "Internal error: the prepared file has an "
                             "unexpected format ({name}) — please report it",
    "err.upload_step_failed": "The server refused the upload step "
                              "(HTTP {status})",
    "err.upload_protocol": "The server's upload response changed shape — "
                           "playground may have been updated",
    "err.stream_failed": "The transcription request was refused "
                         "(HTTP {status})",
    "err.stream_protocol": "The server returned no transcription — playground "
                           "may have been updated",
    "err.upload_too_big": "The audio is over the 25 MiB upload limit "
                          "({size:.1f} MiB) — that is a chunking bug, please "
                          "report it",
    "err.consent_required": "This account must accept the playground "
                            "biometric notice first: Settings → Files & "
                            "accounts → accept biometric notice, or "
                            "`mai2srt consent`, then retry",
    "err.upload_rejected": "The server rejected this audio (HTTP {status}); "
                           "retrying will not help",
    "err.upload_failed": "All {n} upload attempts failed",
    "err.consent_http": "The biometric-notice endpoint returned HTTP {status}",
    "err.consent_bad_response": "The biometric-notice endpoint returned "
                                "something unreadable",
    "err.scan_failed": "Audio analysis failed (ffmpeg returned {rc})",
    "err.chunk_too_big": "Piece {i} came out at {size:.1f} MiB, over the upload "
                         "limit",
    "err.encode_failed": "Audio encoding failed (ffmpeg error)",
    "err.compress_too_big": "Still {size:.1f} MiB after compression, over the "
                            "upload limit",
    "err.probe_failed": "Could not read the audio's properties (ffprobe error)",
    "err.no_audio_stream": "This file has no audio track",
    "err.probe_parse": "Could not parse ffprobe's output",
    "err.ffmpeg_missing": "{tool} not found — install FFmpeg or add it to PATH",
    "err.bad_json": "This .mai.json cannot be read — it may not have been "
                    "produced by mai2srt, or it is damaged",
    "err.file_write": "Could not write {path} (in use, or no permission)",
}

_DICTS: dict[str, dict[str, str]] = {"zh": _ZH, "en": _EN}

#: every key the catalogue knows, in a stable order (tests assert that each
#: one carries both languages and that the English copy has no Chinese left)
TR_KEYS: tuple[str, ...] = tuple(_EN)


def normalize(lang: str | None) -> str:
    """Map anything ('zh-CN', 'EN', None) onto a supported language."""
    text = (lang or "").strip().lower()
    return "en" if text.startswith("en") else "zh"


def tr(lang: str | None, key: str, **kw: Any) -> str:
    """Translate ``key``; a missing key degrades to the key itself, never to a
    crash -- a log line must not be able to kill a job."""
    table = _DICTS[normalize(lang)]
    text = table.get(key) or _EN.get(key) or key
    if not kw:
        return text
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        return text


def user_message(exc: BaseException, lang: str | None) -> str:
    """The one-line, human, translated summary of ``exc`` for the UI.

    Exceptions carry their technical English message (raw service bodies,
    ffmpeg stderr) as ``str(exc)`` -- that is what the logs and the detail
    stream show. When they also carry a ``code``, this returns the short
    sentence the user should read instead of a protocol dump. Without a code
    the raw message is all we have, so it is shown unchanged rather than
    swallowed behind a vague "something went wrong".
    """
    code = getattr(exc, "code", None)
    if not code or code not in _EN:
        return str(exc)
    return tr(lang, code, **(getattr(exc, "params", None) or {}))
