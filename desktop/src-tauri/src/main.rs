#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Emitter, Manager, State};
use tauri_plugin_global_shortcut::{Builder as GsBuilder, ShortcutState};

struct BrainProcess(pub Mutex<Option<Child>>);

// ---------------------------------------------------------------------------
// What the brain actually said
//
// `start_brain` gives the child piped stdout and stderr. Nothing used to
// read them, which cost two separate things:
//
//   * The brain's own startup failure was invisible. The shell could
//     report what it RESOLVED (`brain_runtime_info`: which feral-core,
//     which interpreter, which URL) but never what the process SAID, so
//     a traceback, an `OperationalError: no such module: fts5`, or
//     "address already in use" all looked identical from the outside:
//     a health probe that never went green.
//   * A pipe nobody drains has a fixed OS buffer (64KiB on macOS). A
//     chatty brain fills it and then blocks on its next write, forever.
//     Piping output and not reading it is strictly worse than inheriting
//     the parent's stdio.
//
// So both streams are drained on their own threads into this ring, which
// keeps the last BRAIN_LOG_LINES lines and drops the oldest. Bounded on
// both axes (line count and line length) because the input is a process
// that may be printing in a loop, and a diagnostic buffer must not be a
// way to exhaust memory.
// ---------------------------------------------------------------------------

/// Lines of brain output retained. Enough for a Python traceback plus the
/// uvicorn banner around it, which is what a failure actually looks like.
const BRAIN_LOG_LINES: usize = 300;

/// Longest single retained line, in bytes. A brain that prints a 10MB
/// blob on one line still costs one line of the ring.
const BRAIN_LOG_MAX_LINE: usize = 2_000;

/// Truncate on a char boundary, so a clipped multi-byte character cannot
/// produce a panic or invalid UTF-8.
fn clip_line(s: &str, max: usize) -> String {
    if s.len() <= max {
        return s.to_string();
    }
    let mut end = max;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}... <line truncated>", &s[..end])
}

/// Bounded, shared, last-N-lines view of the brain's stdout and stderr.
#[derive(Clone)]
struct BrainLog(Arc<Mutex<VecDeque<String>>>);

impl BrainLog {
    fn new() -> Self {
        BrainLog(Arc::new(Mutex::new(VecDeque::with_capacity(BRAIN_LOG_LINES))))
    }

    /// Drop everything. Called when a new brain is spawned, so the screen
    /// never shows the previous process's traceback next to this one's
    /// pid.
    fn clear(&self) {
        if let Ok(mut buf) = self.0.lock() {
            buf.clear();
        }
    }

    fn push(&self, stream: &str, line: &str) {
        let text = clip_line(line.trim_end_matches(['\n', '\r']), BRAIN_LOG_MAX_LINE);
        if let Ok(mut buf) = self.0.lock() {
            buf.push_back(format!("[{stream}] {text}"));
            while buf.len() > BRAIN_LOG_LINES {
                buf.pop_front();
            }
        }
    }

    fn tail(&self) -> String {
        match self.0.lock() {
            Ok(buf) => buf.iter().cloned().collect::<Vec<_>>().join("\n"),
            Err(_) => "<brain output unavailable: the log lock is poisoned>".to_string(),
        }
    }
}

/// Read one pipe to EOF on its own thread, appending to the ring.
///
/// Reads bytes rather than `BufRead::lines()`: the brain's output is not
/// guaranteed to be valid UTF-8 (a library printing raw bytes is enough),
/// and a decode error must not silently kill the drain thread and
/// re-create the blocked-pipe problem this exists to remove.
fn drain_stream<R: Read + Send + 'static>(reader: R, stream: &'static str, log: BrainLog) {
    std::thread::spawn(move || {
        let mut buffered = BufReader::new(reader);
        let mut raw = Vec::new();
        loop {
            raw.clear();
            match buffered.read_until(b'\n', &mut raw) {
                Ok(0) => break,
                Ok(_) => log.push(stream, &String::from_utf8_lossy(&raw)),
                Err(e) => {
                    log.push(stream, &format!("<could not read this stream: {e}>"));
                    break;
                }
            }
        }
        log.push(stream, "<end of stream: the brain closed it or exited>");
    });
}

// ---------------------------------------------------------------------------
// Brain process helpers
//
// Everything in this section exists because the previous implementation
// could only start the brain on the machine that compiled it:
//
//   * `resolve_feral_core_dir()` was built on `env!("CARGO_MANIFEST_DIR")`,
//     which is expanded by rustc at COMPILE time. The shipped binary
//     therefore carried the build machine's absolute source path, e.g.
//     /Users/<builder>/…/desktop/src-tauri, and `.canonicalize()` on it
//     returned Err on every other machine. `start_brain` returned
//     "feral-core path: No such file or directory" and the app was inert.
//   * `python_bin()` returned the bare string "python3", so the brain ran
//     under whatever `python3` PATH happened to resolve to. On the
//     maintainer's own machine that is pyenv 3.11.11, which has no
//     loadable SQLite extensions; on a machine whose python3 has no FTS5
//     the brain aborts at MemoryStore construction. A GUI app that spawns
//     an unknown interpreter from the user's shell PATH cannot make any
//     promise about what it just started.
//
// Both are now resolved at RUN time, relative to the running executable
// and the app's bundled resources, and the interpreter is capability
// checked before it is used.
// ---------------------------------------------------------------------------

/// Relative path, inside `feral-core`, of a file that must exist for a
/// directory to actually be feral-core. Checking for the directory alone
/// matched empty leftovers.
const CORE_SENTINEL: &str = "api/server.py";

fn is_feral_core_dir(candidate: &Path) -> bool {
    candidate.join(CORE_SENTINEL).is_file()
}

/// Walk up from `start`, returning the first ancestor containing a
/// `feral-core/api/server.py`.
///
/// This is what makes a dev build work without any environment variable:
/// the binary sits at `desktop/src-tauri/target/debug/feral-desktop`, so
/// the repo root is four levels up, and that number changes between
/// debug, release and `cargo run` layouts. Searching is stable across all
/// of them where a hard-coded `../../..` is not.
///
/// Bounded to 8 levels so a binary installed at `/usr/local/bin` cannot
/// walk to `/` and adopt an unrelated directory.
fn find_core_dir_upwards(start: &Path) -> Option<PathBuf> {
    let mut dir = Some(start);
    for _ in 0..8 {
        let d = dir?;
        let candidate = d.join("feral-core");
        if is_feral_core_dir(&candidate) {
            return Some(candidate);
        }
        dir = d.parent();
    }
    None
}

/// Every place a `feral-core` could legitimately live, most explicit first.
///
/// `resource_dir` is `None` only if Tauri cannot resolve the app's own
/// resource directory, which should not happen but is not worth panicking
/// over.
fn core_dir_candidates(resource_dir: Option<&Path>, exe_dir: Option<&Path>) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(dir) = std::env::var("FERAL_CORE_DIR") {
        if !dir.is_empty() {
            out.push(PathBuf::from(dir));
        }
    }
    if let Some(res) = resource_dir {
        // Where `bundle.resources` in tauri.conf.json puts a staged copy.
        // On macOS this is FERAL.app/Contents/Resources.
        out.push(res.join("feral-core"));
        // A sibling layout, for anyone unpacking the payload by hand.
        if let Some(parent) = res.parent() {
            out.push(parent.join("feral-core"));
        }
    }
    if let Some(exe) = exe_dir {
        if let Some(found) = find_core_dir_upwards(exe) {
            out.push(found);
        }
    }
    out
}

fn resolve_feral_core_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let resource_dir = app.path().resource_dir().ok();
    let exe = std::env::current_exe().ok();
    let exe_dir = exe.as_deref().and_then(Path::parent);

    let candidates = core_dir_candidates(resource_dir.as_deref(), exe_dir);
    for candidate in &candidates {
        if is_feral_core_dir(candidate) {
            // canonicalize only after the sentinel matched, so a failure
            // to canonicalize an irrelevant candidate cannot abort the
            // whole search the way the old implementation did.
            return candidate
                .canonicalize()
                .map_err(|e| format!("feral-core at {} is unreadable: {e}", candidate.display()));
        }
    }
    Err(format!(
        "could not locate feral-core (looked for {CORE_SENTINEL} under: {}). \
         Set FERAL_CORE_DIR to the feral-core directory.",
        render_paths(&candidates)
    ))
}

/// Every place an interpreter able to run the brain could live, most
/// explicit first.
fn python_candidates(resource_dir: Option<&Path>, core_dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(p) = std::env::var("FERAL_PYTHON") {
        if !p.is_empty() {
            out.push(PathBuf::from(p));
        }
    }
    // The interpreter staged by desktop/scripts/stage_bundle.sh. This is
    // the only candidate whose SQLite build we control, so it is first
    // after the explicit override.
    if let Some(res) = resource_dir {
        out.push(res.join("python").join(python_relative_exe()));
    }
    // Dev machines: the repo's pinned .venv, built by `make dev` from
    // .python-pin. Without this a `cargo run` in the repo would fall
    // through to PATH and start the brain on an interpreter the repo
    // deliberately does not use.
    if let Some(repo_root) = core_dir.parent() {
        out.push(repo_root.join(".venv").join(venv_relative_exe()));
    }
    out
}

fn python_relative_exe() -> PathBuf {
    if cfg!(windows) {
        PathBuf::from("python.exe")
    } else {
        PathBuf::from("bin").join("python3")
    }
}

fn venv_relative_exe() -> PathBuf {
    if cfg!(windows) {
        PathBuf::from("Scripts").join("python.exe")
    } else {
        PathBuf::from("bin").join("python")
    }
}

/// Ask an interpreter whether it can actually run FERAL's memory store.
///
/// Not a version check. The two things that matter are SQLite build
/// features, and they are invisible from the version number: pyenv
/// 3.11.11 has FTS5 and no loadable extensions, python-build-standalone
/// 3.11.13 has loadable extensions and no FTS5 (verified: SQLite 3.49.1,
/// `CREATE VIRTUAL TABLE ... USING fts5` raises "no such module: fts5"),
/// and 3.11.15 has both. Starting the brain on the 3.11.13 kind produces
/// a process that dies during MemoryStore construction, which the desktop
/// app previously surfaced as nothing at all: `start_brain` returned Ok
/// with a pid, and the health probe just never went green.
///
/// Exit code 0 means usable. The probe costs one short-lived process.
fn interpreter_is_usable(python: &Path) -> Result<(), String> {
    if !python.is_file() {
        return Err("not present".to_string());
    }
    let probe = "import sqlite3,sys\n\
                 c=sqlite3.connect(':memory:')\n\
                 try:\n    c.execute('CREATE VIRTUAL TABLE t USING fts5(x)')\n\
                 except Exception as e:\n    sys.exit('no SQLite FTS5 (%s); the memory store cannot start' % e)\n";
    let out = Command::new(python)
        .args(["-c", probe])
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("could not run it: {e}"))?;
    if out.status.success() {
        return Ok(());
    }
    let detail = String::from_utf8_lossy(&out.stderr).trim().to_string();
    Err(if detail.is_empty() {
        format!("probe exited {}", out.status)
    } else {
        detail
    })
}

fn resolve_python(app: &tauri::AppHandle, core_dir: &Path) -> Result<PathBuf, String> {
    let resource_dir = app.path().resource_dir().ok();
    let candidates = python_candidates(resource_dir.as_deref(), core_dir);

    let mut rejected = Vec::new();
    for candidate in &candidates {
        match interpreter_is_usable(candidate) {
            Ok(()) => return Ok(candidate.clone()),
            Err(why) => rejected.push(format!("{} ({why})", candidate.display())),
        }
    }
    Err(format!(
        "no usable Python interpreter found. Tried: {}. \
         FERAL needs an interpreter whose SQLite has FTS5. Install the app's \
         bundled runtime, or set FERAL_PYTHON to such an interpreter (the repo's \
         `make dev` builds one at .venv/bin/python).",
        rejected.join("; ")
    ))
}

fn render_paths(paths: &[PathBuf]) -> String {
    if paths.is_empty() {
        return "<no candidates>".to_string();
    }
    paths
        .iter()
        .map(|p| p.display().to_string())
        .collect::<Vec<_>>()
        .join(", ")
}

fn brain_base_url() -> String {
    std::env::var("FERAL_PUBLIC_BASE_URL")
        .or_else(|_| std::env::var("FERAL_BRAIN_URL"))
        .unwrap_or_else(|_| "http://localhost:9090".to_string())
}

// ---------------------------------------------------------------------------
// Tauri commands — brain lifecycle
// ---------------------------------------------------------------------------

#[tauri::command]
fn start_brain(
    app: tauri::AppHandle,
    state: State<'_, BrainProcess>,
    log: State<'_, BrainLog>,
) -> Result<u32, String> {
    let mut guard = state.0.lock().map_err(|e| format!("lock: {e}"))?;
    if let Some(mut existing) = guard.take() {
        let _ = existing.kill();
        let _ = existing.wait();
    }
    let dir = resolve_feral_core_dir(&app)?;
    let python = resolve_python(&app, &dir)?;
    let mut cmd = Command::new(&python);
    cmd.current_dir(&dir)
        .args(["-m", "api.server"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd.spawn().map_err(|e| {
        format!(
            "failed to spawn {} -m api.server in {}: {e}",
            python.display(),
            dir.display()
        )
    })?;
    let pid = child.id();

    // This process's output only. Whatever the previous brain printed
    // describes a pid that no longer exists.
    let log = log.inner().clone();
    log.clear();
    log.push(
        "shell",
        &format!("started {} -m api.server (pid {pid})", python.display()),
    );
    if let Some(out) = child.stdout.take() {
        drain_stream(out, "stdout", log.clone());
    }
    if let Some(err) = child.stderr.take() {
        drain_stream(err, "stderr", log);
    }

    *guard = Some(child);
    Ok(pid)
}

/// What the app resolved, as text, so the operator can be shown it
/// instead of guessing. Reachable from the UI's troubleshooting panel,
/// which previously could only tell people to set FERAL_CORE_DIR.
#[tauri::command]
fn brain_runtime_info(app: tauri::AppHandle) -> String {
    let core = match resolve_feral_core_dir(&app) {
        Ok(dir) => {
            let python = match resolve_python(&app, &dir) {
                Ok(p) => p.display().to_string(),
                Err(e) => format!("UNRESOLVED: {e}"),
            };
            format!("feral-core: {}\npython: {python}", dir.display())
        }
        Err(e) => format!("feral-core: UNRESOLVED: {e}"),
    };
    format!("{core}\nbrain url: {}", brain_base_url())
}

/// The tail of what the brain itself printed, as text.
///
/// Companion to `brain_runtime_info`: that one reports what the shell
/// RESOLVED, this one reports what the process SAID. The difference is
/// "the interpreter was wrong" versus the traceback that proves it.
/// Bounded to the last `BRAIN_LOG_LINES` lines by the ring buffer, so
/// this is safe to call from a UI on a timer.
#[tauri::command]
fn brain_output_tail(log: State<'_, BrainLog>) -> String {
    let tail = log.inner().tail();
    if tail.is_empty() {
        "<no output captured: the brain has not been started, or it printed nothing>".to_string()
    } else {
        tail
    }
}

#[tauri::command]
fn stop_brain(state: State<'_, BrainProcess>, pid: u32) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|e| format!("lock: {e}"))?;
    if let Some(mut child) = guard.take() {
        if child.id() == pid {
            let _ = child.kill();
            let _ = child.wait();
            return Ok(());
        }
        *guard = Some(child);
    }
    kill_pid(pid).map_err(|e| e.to_string())
}

fn kill_pid(pid: u32) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        std::process::Command::new("kill")
            .args(["-TERM", &pid.to_string()])
            .status()?;
    }
    #[cfg(windows)]
    {
        std::process::Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/F"])
            .status()?;
    }
    Ok(())
}

fn brain_health_probe() -> (bool, String) {
    let url = format!("{}/health", brain_base_url().trim_end_matches('/'));
    match reqwest::blocking::get(url) {
        Ok(resp) => {
            let code = resp.status().as_u16();
            let ok = resp.status().is_success();
            (ok, format!("HTTP {code}"))
        }
        Err(e) => (false, format!("unreachable: {e}")),
    }
}

#[tauri::command]
fn check_brain_health() -> Result<String, String> {
    let (_ok, status) = brain_health_probe();
    Ok(status)
}

#[tauri::command]
fn get_brain_url() -> String {
    brain_base_url()
}

// ---------------------------------------------------------------------------
// Graceful brain shutdown
// ---------------------------------------------------------------------------

fn shutdown_brain(state: &BrainProcess) {
    if let Ok(mut guard) = state.0.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

fn toggle_floating_window(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("floating") {
        if w.is_visible().unwrap_or(false) {
            let _ = w.hide();
        } else {
            let _ = w.center();
            let _ = w.show();
            let _ = w.set_focus();
        }
    }
}

fn main() {
    let voice_shortcut: &[&str] = if cfg!(target_os = "macos") {
        &["cmd+shift+t"]
    } else {
        &["ctrl+shift+t"]
    };
    let floating_shortcut: &[&str] = if cfg!(target_os = "macos") {
        &["cmd+shift+f"]
    } else {
        &["ctrl+shift+f"]
    };

    let all_shortcuts: Vec<&str> = voice_shortcut
        .iter()
        .chain(floating_shortcut.iter())
        .copied()
        .collect();

    let floating_key = floating_shortcut[0].to_string();
    let global_shortcut = GsBuilder::new()
        .with_shortcuts(all_shortcuts)
        .expect("register global shortcut definitions")
        .with_handler(move |app, shortcut, event| {
            if event.state == ShortcutState::Pressed {
                let key = shortcut.to_string().to_lowercase();
                if key.contains(&floating_key.to_lowercase()) {
                    toggle_floating_window(app);
                } else {
                    let _ = app.emit("voice-activation", ());
                }
            }
        })
        .build();

    tauri::Builder::default()
        .plugin(global_shortcut)
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec!["--minimized"]),
        ))
        .manage(BrainProcess(Mutex::new(None)))
        .manage(BrainLog::new())
        .invoke_handler(tauri::generate_handler![
            start_brain,
            stop_brain,
            check_brain_health,
            get_brain_url,
            brain_runtime_info,
            brain_output_tail,
        ])
        .setup(|app| {
            // ---- System tray menu ----------------------------------------
            let show_hide = MenuItem::with_id(
                app,
                "show_hide",
                "Show / Hide FERAL",
                true,
                None::<&str>,
            )?;
            let spotlight = MenuItem::with_id(
                app,
                "spotlight",
                "Spotlight Chat  (Cmd+Shift+F)",
                true,
                None::<&str>,
            )?;
            let quick_chat = MenuItem::with_id(
                app,
                "quick_chat",
                "Quick Chat",
                true,
                None::<&str>,
            )?;
            let quit =
                MenuItem::with_id(app, "quit", "Quit FERAL", true, None::<&str>)?;

            let menu = Menu::with_items(
                app,
                &[
                    &show_hide,
                    &spotlight,
                    &PredefinedMenuItem::separator(app)?,
                    &quick_chat,
                    &PredefinedMenuItem::separator(app)?,
                    &quit,
                ],
            )?;

            let tray = TrayIconBuilder::with_id("feral-tray")
                .title("FERAL")
                .tooltip("FERAL — starting…")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show_hide" => {
                        if let Some(w) = app.get_webview_window("main") {
                            if w.is_visible().unwrap_or(false) {
                                let _ = w.hide();
                            } else {
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                    }
                    "spotlight" => {
                        toggle_floating_window(app);
                    }
                    "quick_chat" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                            let _ = app.emit("voice-activation", ());
                        }
                    }
                    "quit" => {
                        if let Some(bp) = app.try_state::<BrainProcess>() {
                            shutdown_brain(bp.inner());
                        }
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                })
                .build(app)?;

            // ---- Background health tooltip loop --------------------------
            let tray_bg = tray.clone();
            std::thread::spawn(move || loop {
                let (ok, detail) = brain_health_probe();
                let dot = if ok { "🟢" } else { "🔴" };
                let tip = format!("FERAL — {dot} {detail}");
                let _ = tray_bg.set_tooltip(Some(tip.as_str()));
                std::thread::sleep(Duration::from_secs(2));
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(bp) = window.app_handle().try_state::<BrainProcess>() {
                    shutdown_brain(bp.inner());
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running FERAL Desktop");
}

// ---------------------------------------------------------------------------
// Tests
//
// Cover the pure halves of the resolution logic. `resolve_feral_core_dir`
// and `resolve_python` themselves need a tauri::AppHandle, so the parts
// that decide WHERE to look and WHETHER a candidate qualifies are split
// out into functions that take plain paths, and those are what is tested.
// The defect being locked out is not subtle logic, it is that the old
// code baked a build-machine path into the binary, so the tests assert
// the resolution is driven by paths supplied at run time.
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// Build a fake repo: <root>/feral-core/api/server.py
    fn make_core(root: &Path) -> PathBuf {
        let core = root.join("feral-core");
        fs::create_dir_all(core.join("api")).unwrap();
        fs::write(core.join(CORE_SENTINEL), b"# fake\n").unwrap();
        core
    }

    fn tempdir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "feral-desktop-test-{tag}-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_directory_without_the_sentinel_is_not_feral_core() {
        let root = tempdir("sentinel");
        let empty = root.join("feral-core");
        fs::create_dir_all(&empty).unwrap();
        assert!(
            !is_feral_core_dir(&empty),
            "an empty leftover directory named feral-core must not be adopted"
        );
        make_core(&root);
        assert!(is_feral_core_dir(&empty));
    }

    #[test]
    fn core_is_found_by_walking_up_from_the_executable() {
        // The dev layout: desktop/src-tauri/target/debug/<binary>.
        let root = tempdir("upwards");
        let core = make_core(&root);
        let exe_dir = root.join("desktop/src-tauri/target/debug");
        fs::create_dir_all(&exe_dir).unwrap();

        assert_eq!(find_core_dir_upwards(&exe_dir), Some(core));
    }

    #[test]
    fn the_upward_walk_is_bounded() {
        // A binary installed outside any checkout must not keep climbing
        // until it finds some unrelated `feral-core` near the filesystem
        // root.
        let root = tempdir("bounded");
        make_core(&root);
        let deep = root.join("a/b/c/d/e/f/g/h/i/j");
        fs::create_dir_all(&deep).unwrap();

        assert_eq!(find_core_dir_upwards(&deep), None);
    }

    #[test]
    fn bundled_resources_are_preferred_over_the_source_tree() {
        // The shipped app must run its own staged copy, not whatever
        // checkout happens to sit above the install location.
        let root = tempdir("resources");
        make_core(&root);
        let resources = root.join("FERAL.app/Contents/Resources");
        fs::create_dir_all(&resources).unwrap();
        let exe_dir = root.join("FERAL.app/Contents/MacOS");
        fs::create_dir_all(&exe_dir).unwrap();

        let candidates = core_dir_candidates(Some(&resources), Some(&exe_dir));
        assert_eq!(candidates.first().unwrap(), &resources.join("feral-core"));
    }

    #[test]
    fn resolution_uses_only_runtime_paths() {
        // The regression guard. The old implementation was
        // `env!("CARGO_MANIFEST_DIR").join("../../feral-core")`, so the
        // answer was fixed at compile time and identical for every input.
        // Two different runtime layouts must produce two different
        // answers.
        let a = tempdir("runtime-a");
        let b = tempdir("runtime-b");
        make_core(&a);
        make_core(&b);

        let from_a = core_dir_candidates(None, Some(&a.join("bin")));
        let from_b = core_dir_candidates(None, Some(&b.join("bin")));

        assert_eq!(from_a.len(), 1);
        assert_eq!(from_b.len(), 1);
        assert_ne!(from_a[0], from_b[0]);
        assert!(from_a[0].starts_with(&a));
        assert!(from_b[0].starts_with(&b));
    }

    #[test]
    fn python_candidates_prefer_bundled_then_repo_venv_and_never_bare_path() {
        let root = tempdir("python");
        let core = make_core(&root);
        let resources = root.join("Resources");

        let candidates = python_candidates(Some(&resources), &core);

        assert_eq!(
            candidates[0],
            resources.join("python").join(python_relative_exe())
        );
        assert_eq!(candidates[1], root.join(".venv").join(venv_relative_exe()));
        // The whole point: nothing in the list is a bare name that the OS
        // would resolve through the user's PATH.
        for c in &candidates {
            assert!(
                c.is_absolute() || c.components().count() > 1,
                "{} would be resolved through PATH",
                c.display()
            );
        }
    }

    #[test]
    fn an_absent_interpreter_is_rejected_with_a_reason() {
        let root = tempdir("absent");
        let err = interpreter_is_usable(&root.join("no-such-python")).unwrap_err();
        assert!(err.contains("not present"), "{err}");
    }

    #[test]
    fn the_running_interpreter_probe_accepts_a_python_with_fts5() {
        // Uses whatever python3 the test machine has, and skips rather
        // than fails if it has none: this asserts the probe's contract,
        // not the CI image's contents.
        let Ok(python) = which_python() else {
            eprintln!("skipped: no python3 on PATH");
            return;
        };
        match interpreter_is_usable(&python) {
            Ok(()) => {}
            Err(why) => {
                // A machine whose python3 genuinely lacks FTS5 is exactly
                // the case this probe exists for, so that is also a pass.
                assert!(why.contains("FTS5"), "unexpected rejection: {why}");
            }
        }
    }

    #[test]
    fn the_brain_log_keeps_only_the_last_n_lines() {
        // The buffer exists to survive a brain that prints in a loop, so
        // the bound is the property under test, not the content.
        let log = BrainLog::new();
        for i in 0..(BRAIN_LOG_LINES * 3) {
            log.push("stdout", &format!("line {i}"));
        }
        let tail = log.tail();
        let lines: Vec<&str> = tail.lines().collect();
        assert_eq!(lines.len(), BRAIN_LOG_LINES);
        // Oldest dropped, newest kept: a traceback printed just before the
        // process died must be the part that survives.
        assert!(!tail.contains("line 0\n"));
        assert!(lines.last().unwrap().contains(&format!("line {}", BRAIN_LOG_LINES * 3 - 1)));
    }

    #[test]
    fn a_single_enormous_line_is_clipped_not_kept_whole() {
        let log = BrainLog::new();
        log.push("stderr", &"x".repeat(BRAIN_LOG_MAX_LINE * 4));
        let tail = log.tail();
        assert!(tail.len() < BRAIN_LOG_MAX_LINE * 2, "clip did not apply");
        assert!(tail.contains("line truncated"), "{tail}");
    }

    #[test]
    fn clipping_does_not_split_a_multibyte_character() {
        // Truncating at a byte offset inside a UTF-8 sequence would panic
        // on the slice. Every char here is 2 bytes, so max lands mid-char.
        let s = "é".repeat(BRAIN_LOG_MAX_LINE);
        let clipped = clip_line(&s, BRAIN_LOG_MAX_LINE + 1);
        assert!(clipped.starts_with('é'));
        assert!(clipped.ends_with("<line truncated>"));
    }

    #[test]
    fn starting_a_new_brain_drops_the_previous_ones_output() {
        let log = BrainLog::new();
        log.push("stderr", "traceback from the process that already died");
        log.clear();
        assert_eq!(log.tail(), "");
    }

    #[test]
    fn a_drained_stream_reaches_the_ring() {
        // The whole point of the piped stdio: what the process wrote is
        // readable afterwards. A Cursor stands in for the child's pipe.
        let log = BrainLog::new();
        let payload = b"Traceback (most recent call last):\nsqlite3.OperationalError: no such module: fts5\n";
        drain_stream(std::io::Cursor::new(payload.to_vec()), "stderr", log.clone());

        // The drain runs on its own thread; poll rather than sleep a fixed
        // amount, so this is not a timing bet.
        for _ in 0..200 {
            if log.tail().contains("fts5") {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        let tail = log.tail();
        assert!(tail.contains("no such module: fts5"), "{tail}");
        assert!(tail.contains("[stderr]"), "{tail}");
        assert!(tail.contains("end of stream"), "{tail}");
    }

    fn which_python() -> Result<PathBuf, ()> {
        let out = Command::new("sh")
            .args(["-c", "command -v python3"])
            .output()
            .map_err(|_| ())?;
        if !out.status.success() {
            return Err(());
        }
        let p = String::from_utf8_lossy(&out.stdout).trim().to_string();
        if p.is_empty() {
            Err(())
        } else {
            Ok(PathBuf::from(p))
        }
    }
}
