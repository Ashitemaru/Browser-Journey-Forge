# In-app auto-update (GitHub Releases + Tauri updater)

Standard path: CI publishes a signed macOS bundle + `latest.json` to a GitHub
Release; the app's Tauri updater polls that, verifies the signature with a baked-in
public key, then downloads / installs / relaunches.

The **client-side** code changes below are intentionally NOT applied yet — they
touch Rust (`Cargo.toml`, `main.rs`) which can't be compiled in this dev box, and
enabling them on the current GitLab pipeline risks breaking the working build.
Apply them as part of the GitHub migration, then iterate live in Actions.

## Keys (already generated — `.updater-keys/`)
- **Public key** → goes in `tauri.conf.json` (below).
- **Private key** → GitHub repo secret `TAURI_SIGNING_PRIVATE_KEY`
  (`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` = empty).

## Changes to apply during migration

### 1. `desktop/src-tauri/Cargo.toml`
```toml
[dependencies]
tauri = { version = "2", features = [] }
tauri-plugin-shell = "2"
tauri-plugin-updater = "2"   # add
```

### 2. `desktop/src-tauri/tauri.conf.json`
Add a `plugins` block (set OWNER/REPO to the GitHub repo):
```json
"plugins": {
  "updater": {
    "pubkey": "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEM1OTdFM0UwM0ZDRkZEOEYKUldTUC9jOC80T09YeFFmSWQvalcydHgrYlFZTjdFUFZjV2N3eEdLN3NPOWNSMWZTUFJGRjg3NU4K",
    "endpoints": ["https://github.com/OWNER/REPO/releases/latest/download/latest.json"]
  }
}
```
(Do NOT add `bundle.createUpdaterArtifacts` here — the release workflow turns it
on per-build via `--config`, so local/GitLab builds stay unaffected.)

### 3. `desktop/src-tauri/capabilities/default.json`
Add to `permissions`:
```json
"updater:default"
```

### 4. `desktop/src-tauri/src/main.rs`
Register the plugin and check for updates once the app is up (auto download +
install + relaunch; errors are ignored so a missing/unreachable release is a
no-op):
```rust
use tauri_plugin_updater::UpdaterExt;

// inside tauri::Builder::default()
.plugin(tauri_plugin_updater::Builder::new().build())

// inside .setup(|app| { ... }) — spawn alongside the sidecar wait:
let upd_handle = app.handle().clone();
tauri::async_runtime::spawn(async move {
    if let Ok(updater) = upd_handle.updater() {
        if let Ok(Some(update)) = updater.check().await {
            if update.download_and_install(|_, _| {}, || {}).await.is_ok() {
                upd_handle.restart();
            }
        }
    }
});
```

## CI / Release
- `.github/workflows/release.yml` (added) builds the sidecar, pre-signs it
  (`disable-library-validation`), then `tauri-action` builds + signs the updater
  artifacts and publishes the Release with `latest.json`.
- Trigger: push a tag `vX.Y.Z`.

## One live thing to verify on the first GitHub release
macOS hardened-runtime + the embedded Python.framework: we pre-sign the sidecar
binary before `tauri build`. Confirm the **updater-installed** app still launches
the sidecar (i.e. the `.app.tar.gz` carries the right signature). If Gatekeeper/
library-validation blocks it, move the re-sign to operate on the bundle the
updater packages (adjust the workflow). This is the one step that needs real
Actions logs to nail down.

## User steps (post-migration)
1. Add secrets `TAURI_SIGNING_PRIVATE_KEY` (+ empty `..._PASSWORD`).
2. Set `OWNER/REPO` in the updater endpoint.
3. Apply changes 1–4 above; push a tag `v0.1.1`.
4. Old app on launch → sees the release → updates itself. No more manual dmg.
