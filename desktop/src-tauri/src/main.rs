// Journey Forge Local — Tauri shell.
// Spawns the bundled Python sidecar (FastAPI server + distill pipeline), waits
// for it to listen, then points the native window at the local control panel.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{SocketAddr, TcpStream};
use std::process::Command;
use std::time::Duration;

use tauri::Manager;
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::UpdaterExt;

const PANEL_URL: &str = "http://127.0.0.1:8099/";
const ADDR: &str = "127.0.0.1:8099";

fn main() {
    tauri::Builder::default()
        // Single instance must be registered first: a second launch focuses the
        // existing window instead of racing for port 8099 with a second sidecar.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            // 0. On launch, check GitHub Releases for a newer version; if found,
            //    download + install + relaunch. Errors (no release / offline)
            //    are ignored so this is a no-op when there's nothing to update.
            let upd = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Ok(updater) = upd.updater() {
                    if let Ok(Some(update)) = updater.check().await {
                        if update.download_and_install(|_, _| {}, || {}).await.is_ok() {
                            upd.restart();
                        }
                    }
                }
            });

            // 1. Kill any leftover sidecar from a previous (crashed or just-updated)
            //    instance so the fresh one can bind 8099 — otherwise the new app
            //    would silently attach to the stale server (the "installed update
            //    but version didn't change" bug).
            let _ = Command::new("pkill").args(["-9", "-f", "jfl-server"]).status();
            std::thread::sleep(Duration::from_millis(300));

            // 2. Spawn the sidecar (binaries/jfl-server-<target-triple>).
            let sidecar = app.shell().sidecar("jfl-server")?;
            let (mut rx, child) = sidecar.spawn()?;
            // Keep the child alive for the app's lifetime.
            app.manage(std::sync::Mutex::new(Some(child)));

            // 2. Drain its logs to our stdout.
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    if let CommandEvent::Stdout(b) | CommandEvent::Stderr(b) = event {
                        print!("{}", String::from_utf8_lossy(&b));
                    }
                }
            });

            // 3. Wait for the server, then navigate the window to the panel.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let addr: SocketAddr = ADDR.parse().expect("addr");
                for _ in 0..80 {
                    if TcpStream::connect_timeout(&addr, Duration::from_millis(400)).is_ok() {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(300));
                }
                if let Some(win) = handle.get_webview_window("main") {
                    let _ = win.eval(&format!("window.location.replace('{PANEL_URL}')"));
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Journey Forge Local");
}
