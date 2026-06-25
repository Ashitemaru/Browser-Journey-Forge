# Building the native macOS app (Tauri + frozen Python sidecar)

A real `.app`: a native Tauri window whose WebView shows the control panel, with
the Python brain (FastAPI server + distill pipeline) frozen by PyInstaller into a
**sidecar binary** that the shell spawns. Double-click → native window. No
browser, no terminal, no venv, no system Python required by the end user.

> **Must be built on a Mac.** PyInstaller and Tauri produce native artifacts and
> cannot be cross-built from Linux, and there's no macOS CI runner. The repo has
> everything authored; you run one script on your Mac.

## Architecture

```
Journey-Forge Local.app
└─ Tauri shell (Rust, native window)        desktop/src-tauri/
     └─ spawns sidecar  binaries/jfl-server-<triple>   (PyInstaller onefile)
            = packaging/sidecar_main.py
              → runs server/server.py (FastAPI) in-process on 127.0.0.1:8099
              → pipeline runs in-process (no subprocess) — harness/
     window waits for :8099, then loads the panel (app/dist/index.html, served
     by the sidecar). Writable state → ~/Library/Application Support/JourneyForgeLocal.
```

## Prerequisites (on the Mac, one-time)

- **Xcode Command Line Tools**: `xcode-select --install`
- **Rust**: `curl https://sh.rustup.rs -sSf | sh`
- **Node + pnpm**: Node 20+, `corepack enable` (or `npm i -g pnpm`)
- **Python 3.10+** with `venv` (system python3 is fine — only used to *build* the
  sidecar; end users don't need Python)
- App icons (Tauri needs them to bundle): generate once from any square PNG:
  ```
  cd desktop && npm install && npm run tauri icon /path/to/icon-1024.png
  ```

## Build

```
bash packaging/build-native-mac.sh
```

Output: `desktop/src-tauri/target/release/bundle/macos/Journey-Forge Local.app`

First launch (unsigned): right-click → **Open** (or
`xattr -dr com.apple.quarantine "Journey-Forge Local.app"`). Set your LLM key in
the app's Settings; recordings then distill into per-site capability skills.

## Likely first-build fixes (I authored this blind — can't compile Rust/PyInstaller from Linux)

Expect 1–3 iterations. The usual suspects, with fixes:

1. **PyInstaller missing module** at sidecar runtime (e.g. `ModuleNotFoundError`
   for a uvicorn/anyio/click submodule). Add `--hidden-import <mod>` or
   `--collect-submodules <pkg>` to the `pyinstaller` line in
   `packaging/build-native-mac.sh`. Test the frozen sidecar alone first:
   `./dist/jfl-server` then open http://127.0.0.1:8099/.
2. **Tauri shell permission denied spawning the sidecar.** Adjust
   `desktop/src-tauri/capabilities/default.json` (the `shell:allow-execute`
   sidecar scope). Tauri v2 permission identifiers occasionally differ by CLI
   version — `npm run tauri permission ls` lists valid ones.
3. **Icons missing** → run the `tauri icon` step above, then rebuild.
4. **Window stays on the loading spinner.** The sidecar didn't come up on :8099;
   run `./dist/jfl-server` standalone and check its output, or look at
   `~/Library/Application Support/JourneyForgeLocal/`.
5. **Sidecar binary name.** Tauri expects `binaries/jfl-server-<target-triple>`
   (e.g. `-aarch64-apple-darwin`). The build script derives the triple via
   `rustc -Vv`; if it mismatches, copy/rename accordingly.

Paste any build/runtime error and I'll patch the scaffold.

## Not included (yet)

- Code signing / notarization (needs an Apple Developer cert). Out of scope for
  now — `.app` only, opened via right-click → Open.
- `.dmg` packaging (add `"dmg"` to `bundle.targets` later).
