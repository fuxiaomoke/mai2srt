; mai2srt custom uninstall hooks (loaded by the Tauri NSIS template via
; bundle.windows.nsis.installerHooks in tauri.conf.json).
;
; The stock confirm-page checkbox (删除应用程序数据) only clears WebView2
; storage (theme/glass/wallpaper preferences) -- it never touches the
; three things users actually care about at uninstall time. This hook
; makes that checkbox the OPT-IN GATE for the prompts below: left
; unchecked (the default) an uninstall asks nothing and keeps every
; byte of user data; checked, the three prompts -- ordered by blast
; radius ascending, the irreplaceable one last and loudest -- decide:
;
;   1. sessions + config   ~/.mai2srt  minus audio-cache  (cookies, LLM
;      API keys, browser profile, logs -- privacy-relevant leftovers)
;   2. audio-extract cache ~/.mai2srt/audio-cache        (regenerable)
;   3. project library     Documents\mai2srt              (mai.json
;      transcripts + edit records -- media and exported SRTs live next
;      to the source audio, outside the library. IRREPLACEABLE work)
;
; Gating, in order: not silent/passive (a /S or /P uninstall must never
; block on dialogs), not UpdateMode (an over-install must never shred
; data), and the checkbox actually checked. The macro runs at the top
; of Section Uninstall -- AFTER un.ConfirmLeave stored the checkbox
; state, so $DeleteAppDataCheckboxState is reliable; silent runs never
; show the confirm page, so the var stays empty there and the gate
; holds. A prompt whose target directory does not exist is skipped
; (asking about nothing is noise).
;
; NOTE: keep this file UTF-8 (NSIS 3 unicode installers read it as-is);
; data paths use $PROFILE / $DOCUMENTS so per-user data is honored no
; matter where the app itself was installed (e.g. a custom D:\ dir).
;
; MessageBox uses the IDNO-skip pattern: NO jumps past the deletes,
; YES falls through them. /SD IDNO is a second silent-mode belt in case
; the ${IfNot} ${Silent} guard is ever relaxed.

!macro NSIS_HOOK_PREUNINSTALL
  ${IfNot} ${Silent}
  ${AndIf} $UpdateMode <> 1
  ${AndIf} $DeleteAppDataCheckboxState = 1

    ; ---- 1. sessions + config (privacy-sensitive leftovers) -----------
    ${If} ${FileExists} "$PROFILE\.mai2srt\*.*"
      MessageBox MB_YESNO|MB_ICONQUESTION \
        "同时删除登录会话与应用设置？$\n$\n包含：登录会话、大模型 API key、浏览器配置、日志（位于 C:\Users\<你>\.mai2srt）$\n删除后重装需重新登录并重新配置大模型。$\n$\n[是] 删除    [否] 保留" \
        /SD IDNO IDNO skip_sessions
      RMDir /r "$PROFILE\.mai2srt\accounts"
      RMDir /r "$PROFILE\.mai2srt\browser-profile"
      RMDir /r "$PROFILE\.mai2srt\logs"
      Delete "$PROFILE\.mai2srt\cookies.json"
      Delete "$PROFILE\.mai2srt\config.json"
      RMDir  "$PROFILE\.mai2srt"
      skip_sessions:
    ${EndIf}

    ; ---- 2. audio-extract cache (regenerable) -------------------------
    ${If} ${FileExists} "$PROFILE\.mai2srt\audio-cache\*.*"
      MessageBox MB_YESNO|MB_ICONQUESTION \
        "同时删除音频提取缓存？$\n$\n视频音轨的提取缓存（可随时重新提取，不影响任何数据）。$\n$\n[是] 删除    [否] 保留" \
        /SD IDNO IDNO skip_cache
      RMDir /r "$PROFILE\.mai2srt\audio-cache"
      skip_cache:
    ${EndIf}

    ; ---- 3. project library (IRREPLACEABLE work) ----------------------
    ${If} ${FileExists} "$DOCUMENTS\mai2srt\*.*"
      MessageBox MB_YESNO|MB_ICONEXCLAMATION|MB_DEFBUTTON2 \
        "同时删除字幕项目库？$\n$\n文档\mai2srt 内的转录项目文件（词级时间戳 mai.json 与精修编辑记录）将被永久删除，无法恢复！$\n媒体与导出的 .srt 通常在源音频旁，不在库内、不受影响。$\n$\n[是] 永久删除    [否] 保留（推荐）" \
        /SD IDNO IDNO skip_lib
      RMDir /r "$DOCUMENTS\mai2srt"
      skip_lib:
    ${EndIf}

  ${EndIf}
!macroend
