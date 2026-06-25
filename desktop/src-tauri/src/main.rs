// Journey-Forge Local — Tauri shell.
// Spawns the bundled Python sidecar (FastAPI server + distill pipeline), waits
// for it to listen, then points the native window at the local control panel.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

use tauri::Manager;
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

const PANEL_URL: &str = "http://127.0.0.1:8099/";
const ADDR: &str = "127.0.0.1:8099";

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            // 1. Spawn the sidecar (binaries/jfl-server-<target-triple>).
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
        .expect("error while running Journey-Forge Local");
}
