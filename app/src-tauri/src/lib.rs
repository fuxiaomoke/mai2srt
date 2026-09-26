/// mai2srt Tauri shell: thin glue only -- all real logic lives in the
/// Python sidecar (dev: dev.mjs spawns `mai2srt serve`; release: the
/// bundled sidecar binary below).
///
/// Release-mode sidecar contract:
///  - spawned once at setup with `serve --port 47613`; the frontend's
///    withBackendRetry covers the bind window, so no readiness gate here
///  - killed on app exit: a stray backend would hold the port AND the
///    user's data dir, and Windows would then refuse to overwrite the
///    exe on the next install/upgrade
///  - spawn failure is logged, not fatal: the UI surfaces "backend
///    offline" and the user gets a readable error instead of a crash
#[tauri::command]
fn ping() -> &'static str {
    "mai2srt-shell"
}

use tauri::Manager;

/// Retire the splash card and bring up the real window. The main window is
/// created HIDDEN (tauri.conf.json), so its taskbar button is born exactly
/// here -- icon and window appear together, with content already painted.
fn reveal(app: &tauri::AppHandle) {
    let splash = app.get_webview_window("splash");
    let main = app.get_webview_window("main");
    eprintln!(
        "[shell] reveal: splash={} main={}",
        splash.is_some(),
        main.is_some()
    );
    if let Some(splash) = splash {
        match splash.close() {
            Ok(()) => eprintln!("[shell] splash.close() ok"),
            Err(e) => eprintln!("[shell] splash.close() failed: {e}"),
        }
    }
    if let Some(main) = main {
        match main.is_visible() {
            Ok(v) => eprintln!("[shell] main.is_visible() = {v}"),
            Err(e) => eprintln!("[shell] main.is_visible() failed: {e}"),
        }
        match main.show() {
            Ok(()) => eprintln!("[shell] main.show() ok"),
            Err(e) => eprintln!("[shell] main.show() failed: {e}"),
        }
        match main.set_focus() {
            Ok(()) => eprintln!("[shell] main.set_focus() ok"),
            Err(e) => eprintln!("[shell] main.set_focus() failed: {e}"),
        }
    }
}

/// Boot handshake from the frontend: the shell has painted and the startup
/// prefetch has settled, so the window can take over from the splash card.
#[tauri::command]
fn app_ready(app: tauri::AppHandle) {
    eprintln!("[shell] app_ready invoked");
    reveal(&app);
}

/// The failure path: a webview that never calls `app_ready` (crash, blocked
/// script, dead dev server) must not leave the user staring at the splash
/// forever, so force the handover after a grace period. The frontend's own
/// gate gives up at ~15s, so this only fires when something is really wrong.
fn show_watchdog(app: tauri::AppHandle) {
    tauri::async_runtime::spawn(async move {
        std::thread::sleep(std::time::Duration::from_secs(20));
        match app.get_webview_window("main") {
            Some(main) => {
                // Log what the shell believed, but never GATE on it: a window
                // can report itself visible while the OS still has WS_VISIBLE
                // off (seen once when the rAF-gated boot never signalled).
                // reveal() is idempotent, so just do it.
                eprintln!(
                    "[shell] watchdog fired: main.is_visible() = {}",
                    main.is_visible().unwrap_or(false)
                );
                reveal(&app);
            }
            None => eprintln!("[shell] watchdog fired: no window labelled main"),
        }
    });
}

#[cfg(not(debug_assertions))]
mod sidecar {
    use std::sync::Mutex;
    use tauri::AppHandle;
    use tauri_plugin_shell::process::CommandChild;
    use tauri_plugin_shell::ShellExt;

    static CHILD: Mutex<Option<CommandChild>> = Mutex::new(None);

    pub fn spawn(app: &AppHandle) {
        let cmd = match app.shell().sidecar("mai2srt-backend") {
            Ok(c) => c,
            Err(e) => {
                eprintln!("sidecar resolve failed: {e}");
                return;
            }
        };
        match cmd.args(["serve", "--port", "47613"]).spawn() {
            Ok((mut rx, child)) => {
                *CHILD.lock().unwrap() = Some(child);
                // drain the event stream: the child redirects its own
                // output to the log file, so these pipes carry nothing --
                // but an unread pipe is a deadlock hazard if that ever
                // changes, so consume and discard regardless
                tauri::async_runtime::spawn(async move {
                    while rx.recv().await.is_some() {}
                });
            }
            Err(e) => eprintln!("sidecar spawn failed: {e}"),
        }
    }

    pub fn kill() {
        if let Some(child) = CHILD.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![ping, app_ready])
        .setup(|app| {
            #[cfg(not(debug_assertions))]
            sidecar::spawn(app.handle());
            // closing the splash by hand (Alt+F4 on the card) or losing it
            // to a crash must hand over at once instead of waiting for the
            // watchdog -- the splash exists to cover a wait, not to gate it
            let handover = app.handle().clone();
            if let Some(splash) = app.get_webview_window("splash") {
                splash.on_window_event(move |e| {
                    if let tauri::WindowEvent::Destroyed = e {
                        reveal(&handover);
                    }
                });
            }
            show_watchdog(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running tauri application");

    app.run(|_app, _event| {
        #[cfg(not(debug_assertions))]
        if let tauri::RunEvent::Exit = _event {
            sidecar::kill();
        }
    });
}
