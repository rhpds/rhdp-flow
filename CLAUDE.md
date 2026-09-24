# RHDP-Flow — Claude Code Project Instructions

## Project Overview

RHDP-Flow Workshop Automation tool. Automates OpenShift workshop deployment from CSV files.

- **Core CLI**: `rhdp_flow.py` (4,474 lines)
- **Backend**: FastAPI in `api/` — run with `uvicorn api.server:app --port 8000`
- **Frontend**: React + PatternFly 6 + TypeScript + Vite in `frontend/` — run with `cd frontend && npm run dev`
- **Tests**: 135 backend + 47 frontend = 182 total. All passing.
- **Git remote**: `git@github.com:rhpds/rhdp-flow.git`, branch `main`
- **Git user**: `rhjcd` / `jdisrael@redhat.com`

## Versioning

- **Single source of truth**: `VERSION` file (plain semver, e.g. `1.0.0`)
- **Bump script**: `scripts/bump-version.sh [major|minor|patch]` — updates VERSION, `frontend/package.json`, `api/server.py`
- **Release workflow**: `.github/workflows/rhdp-scheduler-release.yml` — triggers via `workflow_run` after CI passes, auto-bumps, tags `rhdp-scheduler/vX.Y.Z`, creates GitHub Release
- **Bump type detection** (priority order): PR labels (`semver:major/minor/patch`) > commit keywords (`[major]`, `[minor]`, `[patch]`) > default `patch`. Labels take precedence; keywords only checked if no label matched.
- **Infinite loop prevention**: Version bump commits contain `[skip ci]`; VERSION excluded from path triggers

## CRITICAL RULES

- **NEVER mention Claude, AI, Co-Authored-By, or any AI attribution in git commits or pushes.** All commits must be clean without AI attribution lines.
- Always run `git status` and `git diff` after any code change.
- Never push without user confirmation.

## Post-Update Protocol (Optional — requires MCP servers)

The following steps apply when Memory and Notion MCP servers are configured. They are **not required** for contributors who do not have these MCP servers set up. If the tools are unavailable, skip these steps gracefully.

**Trigger**: After ANY app update (code change, deployment, version bump, feature add) that results in a git commit.
**Timing**: Complete steps AFTER the commit is created but BEFORE reporting completion to the user.

### Step 1 — Memory MCP (`mcp__memory__*` tools, if available)

- New feature or component → `mcp__memory__create_entities` with entity name, type, and observations
- Update to existing feature → `mcp__memory__add_observations` on the existing entity
- Every observation should include: date (YYYY-MM-DD), what changed, version if applicable
- Known entities: `RHDP-Scheduler`, `RHDP-Scheduler-API`, `RHDP-Scheduler-Frontend`, `RHDP-Scheduler-Tests`, `RHDP-Scheduler-Versioning`

### Step 2 — Notion MCP (`mcp__claude_ai_Notion__*` tools, if available)

- **Page ID**: `30ab44c5-54f5-819b-91a4-f5e6457b52a4` (RHDP-Scheduler Development Log)
- **Procedure**:
  1. `notion-fetch` the page to get current block structure
  2. If version changed: update the "Current Version" table
  3. Add a new entry under "Release History" with: version, date, summary of changes
  4. Use `notion-update-page` with `insert_content_after` on the Release History section

### Checklist (when MCP servers are configured)

- [ ] Commit created successfully
- [ ] Memory MCP entity created or updated (if Memory MCP available)
- [ ] Notion Development Log updated (if Notion MCP available)
- [ ] Report completion to user

## Frontend Architecture

React 18.2 + PatternFly 6 + Vite. Components in `frontend/src/components/`:

| Component | Tab | Key features |
|-----------|-----|--------------|
| `UploadTab.tsx` | Upload & Deploy | CSV upload, schedule table, per-row redirect toggle, deploy settings, diff view |
| `DeploymentsTab.tsx` | Deployments | Results table, status cards, search/filter, auto-refresh, retry, CSV export |
| `QATab.tsx` | QA | QA1/QA2/QA3, namespace scope, showroom via QA2, results + Issues; day-2 actions → Babylon Admin Ops (Labagator) |
| `StudentsTab.tsx` | Students | Landing page URLs, copy-to-clipboard, CSV export |
| `SessionHistory.tsx` | (toolbar) | Prior session dropdown, view/back controls |
| `DiffView.tsx` | (in Upload) | Compare schedules — added/removed/changed |
| `HealthBadge.tsx` | (masthead) | Cluster connection status |

> **Note:** Flow UI no longer has an Operations tab. Lock/extend/scale/showroom-cleanup stay on the API (`/api/operations/*`) for CLI/automation; operators use **Babylon Admin Ops** (embedded in Labagator). Showroom *health* is folded into QA2.

Shared: `constants.ts`, `utils/statusColors.ts`, `services/api.ts`, `types/index.ts`, `hooks/useTheme.ts`, `hooks/useKeyboardShortcuts.ts`, `hooks/useAutoRefresh.ts`

## PatternFly 6 Selector Gotchas (for Playwright)

These were discovered through extensive debugging. Follow them exactly:

### PF6 Switch (e.g., Lock UI, White Glove, Redirect)
- The `<input>` is **hidden** with `role="switch"` — `boundingBox()` works but **click fails** because `<span class="pf-v6-c-switch__toggle">` intercepts pointer events.
- **Solution**: Use `page.getByRole('switch', { name: 'Lock UI Admin Settings' })` with `.click({ force: true })`.
- IDs: `resource-lock-switch`, `resource-pools-switch`, `white-glove-switch`, `redirect-switch` (global), `redirect-row-{i}` (per-row)

### PF6 Checkbox (e.g., Dry-Run Mode)
- Works with: `page.getByRole('checkbox', { name: 'Dry-Run Mode' })`
- ID: `globalDryRun`

### PF6 FileUpload
- `setInputFiles()` on the hidden `<input type="file">` does NOT reliably trigger PF6's `onFileInputChange` callback.
- **Working fallback**: Upload via API directly, then reload:
  ```js
  const csvContent = fs.readFileSync(CSV_PATH, 'utf8');
  await page.evaluate(async (csv) => {
    const blob = new Blob([csv], { type: 'text/csv' });
    const fd = new FormData();
    fd.append('file', blob, 'full_featured.csv');
    await fetch('/api/schedules/upload', { method: 'POST', body: fd });
  }, csvContent);
  await page.reload({ waitUntil: 'networkidle' });
  ```
- **Alternative that sometimes works**: `setInputFiles` on first `input[type="file"]` then click the Upload button. Verify table appears; if not, use API fallback.

### PF6 FormSelect (e.g., QA type)
- Renders as native `<select>` — use `select[aria-label="QA type"]`

### PF6 Table row expansion
- Expand buttons: `button:has-text("Details")` or `.pf-v6-c-table__toggle button`

### highlight() function
- Must use Playwright locator `.evaluate()`, NOT `document.querySelector()`, because `:has-text()` is a Playwright pseudo-selector not valid CSS.

### Scrolling
- The scrollable container is `.pf-v6-c-page__main-section.pf-m-fill`
- For elements below the fold, use `element.scrollIntoView({ behavior: 'smooth', block: 'center' })` via locator.evaluate()

## Demo Video Recording

### Architecture
- `videos/recording-helpers.mjs` — Shared overlay utilities (cursor, callouts, highlights, title cards, scrolling, CSV upload)
- `videos/record-demo.mjs` — Main recording script — outputs 6 chapter videos, each with own browser context
- `videos/README.md` — Viewing guide with per-chapter content descriptions

### 6 Chapter Videos (15-30s each, <2MB MP4 target)

| # | File | Content |
|---|------|---------|
| 1 | `01-upload-and-schedule` | CSV upload, schedule table, row expand |
| 2 | `02-deploy-settings` | Lock UI, White Glove, Redirect toggles, dry-run deploy |
| 3 | `03-deployments-tab` | Results, status cards, search/filter, export |
| 4 | `04-operations-tab` | _(legacy)_ Lock/Extend/Scale — now use Babylon Admin Ops in Labagator |
| 5 | `05-qa-and-students` | QA types, run QA, students, export |
| 6 | `06-extras` | Live mode, dark mode, shortcuts, diff view |

### Overlay System
- **Inline callout boxes** — positioned near relevant elements (replaced old bottom banner)
- **Title cards** — full-screen intro (2-3s) at start of each video
- **Animated cursor** — SVG arrow with click animation
- **Red highlight glow** — outline + box-shadow on focused elements
- **Section badges** — red pill in top-right showing current tab name
- **Viewport** — 1920x1120 (40px taller than 1080p to fix PF6 masthead clipping)

### Recording Commands
```bash
# Start servers
uvicorn api.server:app --port 8000 &
cd frontend && npm run dev &

# Record all 6 chapters
node videos/record-demo.mjs

# Convert all WebM to MP4
for f in videos/0*.webm; do
  ffmpeg -i "$f" -c:v libx264 -preset slow -crf 22 \
    -pix_fmt yuv420p -movflags +faststart "${f%.webm}.mp4"
done
```

## CSV Column Reference

Human-readable spec: [README.md — CSV Format](README.md#csv-format).

**Mechanics:** Case-insensitive headers; any column order; optional `Archive` ignored. Dates: `DD/MM/YYYY HH:MM`; either legacy three date columns or all three `… (UTC)`.

**Seats:** `Users` → `num_users` when >0; catalog max enforced on deploy (UI alert + API 400 + CLI fail). `Enable_workshop_interface` False → ResourceClaim only → **`Instances` unused**; use `Users`. UI True or multi-asset → `Instances` → WorkshopProvision `spec.count` (default 1 if unset). `Count` >1 → N replicated schedules—not seat count. Only `Instances` column (no `Workshop_instance_count`).

**Optional (all):** `Workshop Name`, `Multi_Asset`, `Asset_CIs`, `Multi_Workshop_Name`, `Concurrency`, `Instances`, `Salesforce IDs` (`campaign_id`), `Salesforce_Type`, `Count`, `AWS_Region`, `Redirect`, `Showroom_*`.

**Not CSV:** White Glove (Deploy Settings / config). **Redirect:** per-row CSV column; global "Redirect (all)" in Deploy Settings sets defaults for new uploads and can flip all rows—per-row table toggles still win.

## Sample Data for Demos

- `docs/examples/full_featured.csv` — 3 workshops: Virt Roadshow, Ansible Lab, OpenShift AI (with Salesforce multi-type)
- `docs/examples/basic_workshop.csv` — 2 simple workshops
- `sample-csvs/multi-asset-passwords.csv` — password CSV for multi-asset demos

## API Endpoints (for Playwright testing)

- `POST /api/schedules/upload` — upload CSV (FormData with `file` field)
- `GET /api/schedules` — current schedules
- `GET /api/health` — cluster health check
- `POST /api/deploy` — deploy workshops
- `POST /api/qa/run` — run QA checks
- `GET /api/results` — deployment results
- `GET /api/qa/results` — QA results
- `GET /api/sessions` — session history list
