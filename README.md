# bulk-image-generation — Claude Code skill

Orchestrated bulk AI image generation for [Claude Code](https://claude.com/claude-code) via [Pollinations.ai](https://pollinations.ai/). Built for seeding demo datasets (patient avatars, product photos, dermatology references, etc.) without juggling manual downloads, URLs, or browser tabs.

Claude asks you for a prompt (or drafts a professional one from your description), confirms the parameters, then runs a Python helper that:

- hits Pollinations sequentially with polling on transient errors
- writes every image to a user-specified output directory
- serves a **live HTML dashboard** you open via `file://` that auto-refreshes every 2s while the run is in progress
- emits JSON-lines events to stdout so Claude Code's `Monitor` tool can stream progress in chat
- persists a `run.json` next to the images with all the parameters, for later recovery
- optionally uses **your own Pollinations API key** (stored locally, entered via a one-shot local web form — never pasted into chat)

Stdlib-only Python. No `pip install` anything.

## Install

### Via Claude Code plugin marketplace

```text
/plugin marketplace add christianpasinrey/bulk-image-generation-skill
/plugin install bulk-image-generation@bulk-image-generation-marketplace
```

### Manual (drop-in)

1. Clone or download the repo.
2. Copy the entire `skills/bulk-image-generation/` directory to `~/.claude/skills/`:
   ```bash
   cp -r skills/bulk-image-generation ~/.claude/skills/
   ```
   Windows:
   ```
   xcopy /E /I skills\bulk-image-generation %USERPROFILE%\.claude\skills\bulk-image-generation
   ```
3. Make sure Python 3.10+ is on your PATH.
4. Open a fresh Claude Code session. The skill is auto-detected.

## Prerequisites

- **Claude Code** (any recent version with skill support)
- **Python 3.10+** (`python --version`)
- No pip dependencies — everything uses stdlib (`urllib`, `http.server`, `json`, `pathlib`, …)

## Usage

### From Claude Code (natural)

> "Necesito 20 retratos de pacientes masculinos para el seeder demo en `storage/app/demo/avatars/male`"

Claude detects the trigger, asks whether to use the public API or your own key, confirms the prompt, launches the Python helper in the background, gives you the dashboard URL to open in your browser, and reports the final count when it finishes.

### Direct CLI usage

The Python helper works standalone too — no Claude Code needed.

```bash
# Fresh generation run
python ~/.claude/skills/bulk-image-generation/generate.py \
  --prompt "simple red apple on plain white background, product photography" \
  --count 10 \
  --model flux \
  --width 768 --height 768 \
  --output ~/bulk-test
```

Output layout:

```
~/bulk-test/
├── 001_flux_12345.jpg     # generated image
├── 002_flux_12346.jpg
├── …
├── dashboard.html         # open with your browser (auto-refreshes during the run)
└── run.json               # full run metadata + per-seed status
```

## Recovery mode

Pollinations caches every image by URL hash (`prompt + model + seed + width + height`). Requesting the same URL again returns the cached bytes in ~1 second without re-generating. Two ways to tap the cache:

**A. Known seed base**

```bash
python ~/.claude/skills/bulk-image-generation/generate.py \
  --recover \
  --prompt "THE ORIGINAL PROMPT" \
  --seed 12345 \
  --count 20 \
  --model flux \
  --width 1024 --height 1024 \
  --output /new/destination
```

The prompt must be byte-identical to the original run. The script's URL encoder matches JavaScript's `encodeURIComponent()` exactly so runs seeded from other tools (web-based bulk generators, etc.) hit the same cache entries.

**B. From a previous `run.json`**

```bash
python ~/.claude/skills/bulk-image-generation/generate.py \
  --from /path/to/previous/run.json \
  --output /new/destination
```

`--from` pulls prompt, model, dimensions, seed base and count from the JSON and implies `--recover`.

## Using your own Pollinations API key

The public API is free but rate-limited and shared across every anonymous caller. If you have a Pollinations token (free tier available at [auth.pollinations.ai](https://auth.pollinations.ai)) you can use it for higher limits and priority queue.

**Setup once:**

```bash
python ~/.claude/skills/bulk-image-generation/generate.py --set-token
```

The script starts a tiny HTTP server on `127.0.0.1`, picks a free port, and emits a `token-server-ready` event with the URL. Open it in your browser, paste your token, click Save. The token lands in `~/.bulk-image-generation/config.json` (mode `0600` where supported) and the server shuts itself down.

**Use it on runs:**

```bash
python generate.py --prompt "…" --count 10 --output /out --auth
```

**Check / clear / inspect:**

```bash
python generate.py --show-config    # is a token saved?
python generate.py --clear-token    # delete the saved token
```

When invoked from Claude Code, the skill walks you through this flow automatically: before any generation it asks whether to use the public API or your key, and if you chose your key with none saved it launches `--set-token` and hands you the URL.

## Models

| Model | Speed | Quality | Notes |
|---|---|---|---|
| `flux` | Medium | High | Default. Best balance. |
| `turbo` | Fast | Medium | Good for rapid iteration. |
| `gptimage` | Medium | High | DALL-E style. |
| `seedream` | Medium | High | Realistic. |

## Troubleshooting

**HTTP 500 on recovery mode but I know the image was generated**
- The prompt hash differs even by one character. Check for trailing spaces, different line endings, or markdown bolding (`**…**`) that were stripped from the original.
- Copy the prompt literally from wherever the original run lived. If it came from a web tool, check the browser's network tab for the exact URL the tool sent.

**Rate limit (HTTP 429) very frequent**
- Pollinations throttles per IP. If you're running two bulk tools in parallel from the same connection, you're throttling yourself.
- Bump `--poll-wait` (default 20s) to 30-45s and `--delay` (default 5s between images) to 10s.
- Or register a token and use `--auth` for higher limits.

**Token setup URL opens but the form POST fails**
- The server binds `127.0.0.1` only. Don't try to access it via LAN IP — browser must be on the same machine.
- If the port is in use by something else, re-run `--set-token` and it will pick a new free port.

**The skill doesn't appear in Claude Code**
- Verify the folder is exactly `~/.claude/skills/bulk-image-generation/` with `SKILL.md` + `generate.py` inside.
- Start a fresh session — some Claude Code versions cache the skill list at boot.

## Repository layout

```
bulk-image-generation-skill/
├── .claude-plugin/
│   ├── plugin.json          # plugin manifest
│   └── marketplace.json     # marketplace catalog (single-plugin)
├── skills/
│   └── bulk-image-generation/
│       ├── SKILL.md         # workflow + trigger metadata consumed by Claude Code
│       └── generate.py      # the actual worker, stdlib-only
└── README.md                # this file
```

## Contributing

Issues and PRs welcome. Keep in mind:

- Python stdlib only — no new dependencies.
- Any new flag should be covered in both `SKILL.md` (workflow instructions) and this `README.md` (user reference).
- Events emitted to stdout are a public interface: don't rename existing event names without bumping the major version.

## Third-party

Uses [Pollinations.ai](https://pollinations.ai/) as the image backend — free public image endpoint. Review their terms and content policy before generating anything sensitive.
