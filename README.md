# Journey-Forge Local

Record your own browser tasks → each website accumulates a **bucket of
capabilities**, and every capability distills into one reusable skill → use them
in **Claude Desktop** (and Claude Code). A single-user, local product. You bring
your own LLM API key. Pure Python at runtime (no Node needed). Not a benchmark.

```
extension (record free-form task)
   └─► localhost server :8099   /v1/traces/init → chunks → finalize
          └─ assemble → data/traces/<id>/trace.json  (intent + events)
          └─ background harness pipeline:
               atomize → classify (capability) → bucket (per domain+capability)
               → distill bucket → SKILL.md + TRACE_GUIDE.md  → install
                 ├─ Claude Code:    ~/.claude/skills/<domain>-<capability>/SKILL.md (auto)
                 └─ Claude Desktop: <name>.zip → upload via Settings → Skills
   /api/buckets  exposes per-site capability buckets + skills
```

Each website is a folder of capability buckets, e.g.
`github.com/{login-with-credentials, signup-with-email, …}` — a new recording is
atomized into segments, each segment classified into a capability, and segments
of the same capability pooled into one bucket that distills into one skill.

## Layout

| Path | What |
|---|---|
| `entry/main.py` | Desktop entry — starts the server, opens the panel in your browser |
| `app/dist/index.html` | Control panel (zero-build, served by the server) |
| `extension/` | The recorder (product fork: points at localhost, free-form only) |
| `server/server.py` | Local ingestion + control API |
| `harness/` | Distillation pipeline: atomize / classify / bucket / distill / install (pure Python) |
| `harness/main.py` | CLI: `ingest` / `distill` / `status` / `consolidate` / `query` |
| `scripts/start.sh` | Headless dev launcher |
| `docs/` | Trace schema + Claude Desktop setup |

State lives under `data/harness/`: `buckets.json`, `registry.json`, and
`skills/<domain>/<capability>/{SKILL.md,TRACE_GUIDE.md,meta.json,evidence.jsonl}`.

## Quick start

1. **Configure & run the server**
   ```bash
   cp config.example.env .env.local      # then set SF_LLM_KEY=sk-ant-...
   ./scripts/start.sh                     # or: python entry/main.py  (native window)
   ```
   The panel is at <http://127.0.0.1:8099/>. The server seeds a default key
   (`jfl-local-dev-key`) the extension ships with, so it connects automatically.

2. **Build & load the extension**
   ```bash
   cd extension && pnpm install && pnpm build
   ```
   Chrome → `chrome://extensions` → enable Developer mode → **Load unpacked** →
   select `extension/dist/chrome-mv3`. It's pre-pointed at the local server.

3. **Record → finalize.** Record a short task with the extension, stop, label,
   upload. With auto-distill on, a skill appears within ~1–3 min:
   - **Claude Code**: already installed under your skills root.
   - **Claude Desktop**: download the `.zip` from the panel (Trajectories) and
     upload it in Settings → Skills. See [`docs/claude-desktop-setup.md`](docs/claude-desktop-setup.md).

4. **(Optional) real browser execution.** Panel → *Browser execution* →
   *Configure Playwright MCP* so Claude Desktop can actually click/type/navigate.
   Restart Claude Desktop after.

## Notes

- **Skills don't grant tools.** A `SKILL.md` is injected instructions. To
  *execute* its steps in a browser you must configure a browser MCP (Playwright)
  separately — the panel automates this for Claude Desktop.
- **LLM:** the distiller speaks the Anthropic Messages API natively (default
  `claude-opus-4-8`). Point `SF_LLM_BASE` at an OpenAI-compatible gateway to use
  that path instead.
- Data lives under `data/` (git-ignored). Nothing leaves your machine except
  the distillation calls to your configured LLM.
