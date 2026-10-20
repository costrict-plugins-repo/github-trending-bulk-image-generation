#!/usr/bin/env python3
"""
Bulk image generator for Claude Code's `bulk-image-generation` skill.

Talks to the free Pollinations.ai image endpoint:

    https://image.pollinations.ai/prompt/<url-encoded prompt>
        ?width=W&height=H&seed=N&nologo=true&model=M[&enhance=true]

Sequential, with polling on transient failures (JSON "generating" response,
HTTP 500, timeout). Saves images to the output directory and keeps a live
HTML dashboard at `<output>/dashboard.html` that auto-refreshes every 2s
while the run is in progress. Emits JSON-lines events to stdout so the
caller (e.g. Claude Code's Monitor tool) can stream progress.

Stdlib only — no pip installs required.
"""

from __future__ import annotations

import argparse
import html
import http.server
import json
import os
import random
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError

API_BASE = "https://image.pollinations.ai/prompt"
VALID_MODELS = ("flux", "turbo", "gptimage", "seedream")

# Persistent config: stores the user's Pollinations API token so the skill
# can call authenticated endpoints without re-asking on every run.
CONFIG_DIR = Path.home() / ".bulk-image-generation"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        # Windows + some filesystems don't support POSIX perms — best effort
        pass


def get_saved_token() -> str | None:
    return load_config().get("pollinations_token") or None


# HTML served by the local token-setup server. The form posts to /save and
# the server shuts itself down once a non-empty token is persisted.
TOKEN_FORM_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Pollinations API key</title>
<style>
:root{--bg:#0a0a0f;--card:#1a1a24;--bd:rgba(255,255,255,.08);--text:#e8e8ed;--muted:#6b6b80;--accent:#6c5ce7}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:32px;max-width:560px;width:100%}
h1{font-size:22px;font-weight:700;background:linear-gradient(135deg,#e8e8ed,#6c5ce7);-webkit-background-clip:text;background-clip:text;color:transparent}
p{color:var(--muted);font-size:13px;line-height:1.6;margin-top:10px}
p a{color:var(--accent);text-decoration:none}
form{margin-top:24px;display:flex;flex-direction:column;gap:14px}
label{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
input{padding:12px 14px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;background:#0a0a0f;border:1px solid var(--bd);border-radius:10px;color:var(--text);outline:none}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(108,92,231,.25)}
button{padding:12px 24px;font-size:14px;font-weight:600;background:var(--accent);color:#fff;border:none;border-radius:10px;cursor:pointer;margin-top:6px}
button:hover{filter:brightness(1.1)}
.note{margin-top:18px;padding:12px 14px;background:#0a0a0f;border:1px solid var(--bd);border-radius:10px;font-size:12px;color:var(--muted);line-height:1.6}
</style></head><body>
<div class="card">
  <h1>Pollinations API key</h1>
  <p>Paste your Pollinations.ai token below. It will be stored locally at <code>~/.bulk-image-generation/config.json</code> and used for authenticated requests (higher rate limits, priority queue). Nothing is sent anywhere except Pollinations itself when you generate.</p>
  <p>Don't have one? Get it at <a href="https://auth.pollinations.ai" target="_blank">auth.pollinations.ai</a>.</p>
  <form method="POST" action="/save">
    <label for="token">Token</label>
    <input id="token" name="token" type="text" autocomplete="off" autofocus placeholder="poll_...">
    <button type="submit">Save</button>
  </form>
  <div class="note">This window will close automatically after saving. You can safely leave it open in the background while Claude Code runs the generation — the token is persisted to disk once you click Save.</div>
</div>
</body></html>
"""

TOKEN_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Token saved</title>
<style>
body{font-family:system-ui,sans-serif;background:#0a0a0f;color:#e8e8ed;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:#1a1a24;border:1px solid #00b894;border-radius:16px;padding:32px;max-width:480px;text-align:center}
h1{font-size:22px;color:#00b894}
p{color:#6b6b80;font-size:13px;margin-top:10px}
</style></head><body>
<div class="card">
<h1>✓ Token saved</h1>
<p>You can close this tab. Claude Code will resume the generation with your authenticated key.</p>
</div>
<script>setTimeout(()=>window.close(),1500)</script>
</body></html>
"""


def run_token_server() -> int:
    """Serve a tiny form on localhost where the user pastes their Pollinations
    token, then persist it to the config file and shut down.

    Emits `token-server-ready` with the URL before blocking, and
    `token-saved` right after the config is written so the caller can resume.
    """
    # Pick a free port dynamically so we don't collide with whatever else is
    # bound on the user's machine.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    saved_flag = {"ok": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def _html(self, status: int, body: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def do_GET(self):  # noqa: N802 (http.server naming)
            if self.path in ("/", "/index.html"):
                self._html(200, TOKEN_FORM_HTML)
            else:
                self.send_error(404)

        def do_POST(self):  # noqa: N802
            if self.path != "/save":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            params = urllib.parse.parse_qs(raw)
            token = (params.get("token") or [""])[0].strip()
            if not token:
                self._html(400, "<p>Empty token — go back and try again.</p>")
                return
            cfg = load_config()
            cfg["pollinations_token"] = token
            cfg["saved_at"] = datetime.now().isoformat(timespec="seconds")
            save_config(cfg)
            saved_flag["ok"] = True
            self._html(200, TOKEN_SUCCESS_HTML)
            # Schedule shutdown after the response is flushed to the client.
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, format, *args):  # silence access log noise
            return

    server = socketserver.TCPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    emit("token-server-ready", url=url, config_file=str(CONFIG_FILE))
    try:
        server.serve_forever()
    finally:
        server.server_close()
    if saved_flag["ok"]:
        emit("token-saved", config_file=str(CONFIG_FILE))
        return 0
    emit("token-setup-cancelled")
    return 1


def emit(event: str, **fields) -> None:
    """Print a JSON event line so the Monitor tool can pick it up."""
    payload = {"event": event, "ts": datetime.now().isoformat(timespec="seconds")}
    payload.update(fields)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def build_url(
    prompt: str,
    seed: int,
    model: str,
    width: int,
    height: int,
    enhance: bool,
    token: str | None = None,
) -> str:
    params = {
        "width": width,
        "height": height,
        "seed": seed,
        "nologo": "true",
        "model": model,
    }
    if enhance:
        params["enhance"] = "true"
    if token:
        params["token"] = token
    qs = urllib.parse.urlencode(params)
    # Must match JavaScript's `encodeURIComponent()` exactly so URLs hash
    # identically to those produced by the HTML bulk generator tool. Python's
    # default `quote(safe='')` escapes `!*'()` but JS leaves them as-is; this
    # breaks Pollinations cache recovery from sessions seeded by the HTML tool.
    encoded_prompt = urllib.parse.quote(prompt, safe="!*'()")
    return f"{API_BASE}/{encoded_prompt}?{qs}"


def fetch_image(url: str, timeout_s: int) -> tuple[bytes | None, str | None, int]:
    """Return (image_bytes, error_msg, status).

    - If response is an image, returns (bytes, None, status).
    - If response is JSON or an error page, returns (None, error_msg, status).
    - Timeout and network errors become (None, msg, 0).
    """
    req = urllib.request.Request(url, headers={"User-Agent": "ClaudeCodeBulkImages/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = resp.status
            content_type = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read()
            if content_type.startswith("image/") and len(body) >= 500:
                return body, None, status
            if content_type.startswith("application/json"):
                try:
                    data = json.loads(body.decode("utf-8", "replace"))
                    msg = data.get("message") or data.get("error") or f"HTTP {status} JSON"
                    return None, msg, status
                except Exception:
                    return None, f"HTTP {status} (unparseable json)", status
            if len(body) < 500:
                return None, f"tiny response {len(body)}B", status
            return None, f"unexpected content-type: {content_type}", status
    except HTTPError as e:
        return None, f"HTTP {e.code}", e.code
    except URLError as e:
        return None, f"network: {e.reason}", 0
    except TimeoutError:
        return None, f"timeout {timeout_s}s", 0
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", 0


def process_one(
    idx: int,
    total: int,
    seed: int,
    prompt: str,
    model: str,
    width: int,
    height: int,
    enhance: bool,
    output_dir: Path,
    max_polls: int,
    poll_wait_s: int,
    fetch_timeout_s: int,
    state: dict,
    token: str | None = None,
) -> None:
    url = build_url(prompt, seed, model, width, height, enhance, token=token)
    item = state["items"][idx]
    item["status"] = "fetching"
    item["attempts"] = 1
    write_dashboard(state, output_dir)
    emit("fetch-start", index=idx + 1, total=total, seed=seed, url=url)

    blob, err, status = fetch_image(url, fetch_timeout_s)
    attempt = 1

    while blob is None and attempt <= max_polls:
        if err and err.startswith("HTTP 429"):
            wait_s = poll_wait_s + 10
            emit("rate-limited", index=idx + 1, seed=seed, wait_s=wait_s, attempt=attempt)
            item["status"] = "rate-limited"
            item["attempts"] = attempt
            write_dashboard(state, output_dir)
            time.sleep(wait_s)
        else:
            emit(
                "polling",
                index=idx + 1,
                seed=seed,
                reason=err or f"status {status}",
                attempt=attempt,
                max_polls=max_polls,
                wait_s=poll_wait_s,
            )
            item["status"] = f"polling {attempt}/{max_polls}"
            item["attempts"] = attempt
            write_dashboard(state, output_dir)
            time.sleep(poll_wait_s)

        attempt += 1
        blob, err, status = fetch_image(url, fetch_timeout_s)

    if blob is not None:
        filename = f"{idx + 1:03d}_{model}_{seed}.jpg"
        (output_dir / filename).write_bytes(blob)
        item["status"] = "done"
        item["file"] = filename
        item["size_kb"] = round(len(blob) / 1024)
        item["attempts"] = attempt
        state["done"] += 1
        emit(
            "done",
            index=idx + 1,
            seed=seed,
            file=filename,
            size_kb=item["size_kb"],
            attempts=attempt,
        )
    else:
        item["status"] = "failed"
        item["error"] = err or "unknown"
        item["attempts"] = attempt
        state["failed"] += 1
        emit("failed", index=idx + 1, seed=seed, error=err or "unknown", attempts=attempt)

    write_dashboard(state, output_dir)


DASHBOARD_CSS = """
:root{
  --bg:#0a0a0f;--bg-card:#1a1a24;--bg-elevated:#22222e;
  --border:rgba(255,255,255,.06);--text:#e8e8ed;--muted:#6b6b80;--dim:#44445a;
  --accent:#6c5ce7;--success:#00b894;--error:#ff6b6b;--warning:#fdcb6e;--polling:#74b9ff;
  --mono:'JetBrains Mono',ui-monospace,Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);
     min-height:100vh;padding:32px 24px}
.wrap{max-width:1200px;margin:0 auto}
h1{font-size:26px;font-weight:700;background:linear-gradient(135deg,#e8e8ed,#6c5ce7);
   -webkit-background-clip:text;background-clip:text;color:transparent}
.sub{font-size:12px;color:var(--muted);margin-top:4px}
.prompt-box{margin-top:16px;padding:14px 18px;background:var(--bg-card);border:1px solid var(--border);
            border-radius:10px;font-size:13px;line-height:1.5;color:var(--muted);word-break:break-word}
.banner{margin-top:20px;padding:14px 18px;background:var(--bg-card);border:1px solid var(--border);
        border-radius:10px}
.banner .row{display:flex;justify-content:space-between;align-items:center;font-size:13px}
.banner .state{font-weight:600;font-size:14px;color:var(--text)}
.banner .pct{font-family:var(--mono);font-size:13px;color:var(--accent)}
.banner .track{height:4px;background:var(--bg-elevated);border-radius:2px;margin-top:10px;overflow:hidden}
.banner .fill{height:100%;background:linear-gradient(90deg,var(--accent),#a29bfe);transition:width .4s}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;margin-top:18px}
.card{position:relative;aspect-ratio:1;border-radius:10px;overflow:hidden;background:var(--bg-card);
      border:2px solid var(--border);transition:border-color .25s}
.card.done{border-color:var(--success)}
.card.failed{border-color:var(--error)}
.card.fetching,.card.polling,.card[class*="polling"]{border-color:var(--polling)}
.card.rate-limited{border-color:var(--warning)}
.card img{width:100%;height:100%;object-fit:cover;display:block}
.card .ci{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
          justify-content:center;gap:4px;padding:8px;text-align:center}
.card .seed{font-family:var(--mono);font-size:10px;color:var(--dim)}
.card .label{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
.card .num{font-family:var(--mono);font-size:18px;color:var(--dim)}
.card.done .ci{display:none}
.card.failed .ci .label{color:var(--error)}
.card.failed .ci .err{font-size:9px;color:var(--error);max-width:100%;overflow-wrap:break-word}
.card .badge{position:absolute;bottom:4px;right:4px;padding:2px 6px;background:rgba(0,0,0,.7);
             color:rgba(255,255,255,.7);font-size:9px;font-family:var(--mono);border-radius:3px}
.log{margin-top:20px;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;
     max-height:260px;overflow-y:auto;font-family:var(--mono);font-size:11px;line-height:1.7;padding:12px 16px}
.log .l{display:flex;gap:8px}.log .l .t{color:var(--dim);min-width:70px;flex-shrink:0}
.log .l.ok .m{color:var(--success)}.log .l.err .m{color:var(--error)}
.log .l.warn .m{color:var(--warning)}.log .l.info .m{color:var(--muted)}
.log .l.poll .m{color:var(--polling)}.log .l.req .m{color:#a29bfe}
.done-badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:11px;font-weight:600}
.done-badge.ok{background:var(--success);color:#fff}
.done-badge.running{background:var(--accent);color:#fff}
.done-badge.mixed{background:var(--warning);color:#000}
"""


def render_card(item: dict) -> str:
    status = item.get("status", "queued")
    idx = item["index"]
    seed = item["seed"]
    class_name = status.split()[0]  # "polling 2/8" -> "polling"
    if status == "done":
        src = html.escape(item["file"])
        size = item.get("size_kb", "?")
        return f'''<div class="card done"><img src="{src}" alt=""><span class="badge">{seed} · {size}KB</span></div>'''
    if status == "failed":
        err = html.escape(str(item.get("error", "")))[:40]
        return f'''<div class="card failed"><div class="ci"><div class="num">#{idx}</div><div class="label">FAILED</div><div class="err">{err}</div></div><span class="badge">{seed}</span></div>'''
    label = html.escape(status.upper())
    return f'''<div class="card {class_name}"><div class="ci"><div class="num">#{idx}</div><div class="label">{label}</div></div><span class="badge">{seed}</span></div>'''


def render_log(events: list[dict]) -> str:
    lines = []
    for e in events[-80:]:
        ev = e.get("event", "")
        ts = e.get("ts", "").split("T")[-1][:8]
        css = {
            "fetch-start": "req",
            "done": "ok",
            "failed": "err",
            "polling": "poll",
            "rate-limited": "warn",
            "start": "info",
            "complete": "ok",
        }.get(ev, "info")
        msg_parts = []
        if "index" in e and "total" in e:
            msg_parts.append(f"[{e['index']}/{e['total']}]")
        elif "index" in e:
            msg_parts.append(f"[{e['index']}]")
        msg_parts.append(ev.upper())
        for k in ("seed", "reason", "attempt", "size_kb", "wait_s", "error"):
            if k in e:
                msg_parts.append(f"{k}={e[k]}")
        msg = " ".join(str(p) for p in msg_parts)
        lines.append(f'<div class="l {css}"><span class="t">{ts}</span><span class="m">{html.escape(msg)}</span></div>')
    return "\n".join(lines) or '<div class="l info"><span class="m">No events yet</span></div>'


def write_dashboard(state: dict, output_dir: Path) -> None:
    total = state["total"]
    done = state["done"]
    failed = state["failed"]
    finished = done + failed >= total
    pct = round((done / total) * 100) if total else 0
    refresh_tag = "" if finished else '<meta http-equiv="refresh" content="2">'
    badge_class = "ok" if (finished and failed == 0) else ("mixed" if finished else "running")
    badge_text = "COMPLETE" if finished else "RUNNING"
    if finished and failed > 0:
        badge_text = f"DONE · {failed} FAILED"

    cards_html = "\n".join(render_card(item) for item in state["items"])
    log_html = render_log(state["events"])
    prompt_html = html.escape(state["prompt"])
    sub_html = html.escape(
        f"{state['model']} · {state['width']}x{state['height']} · seed base {state['seed_base']} · {total} images"
    )

    page = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
{refresh_tag}
<title>Bulk Image Generation · {done}/{total}</title>
<style>{DASHBOARD_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>Bulk Image Generation <span class="done-badge {badge_class}">{badge_text}</span></h1>
  <div class="sub">{sub_html}</div>
  <div class="prompt-box">{prompt_html}</div>
  <div class="banner">
    <div class="row">
      <div class="state">{done}/{total} done · {failed} failed</div>
      <div class="pct">{pct}%</div>
    </div>
    <div class="track"><div class="fill" style="width:{pct}%"></div></div>
  </div>
  <div class="grid">
    {cards_html}
  </div>
  <div class="log">
    {log_html}
  </div>
</div>
</body>
</html>
'''
    (output_dir / "dashboard.html").write_text(page, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk image generator using Pollinations.ai")
    parser.add_argument("--prompt", help="Image prompt (required unless --from is used)")
    parser.add_argument("--count", type=int, default=10, help="Number of images (1-50)")
    parser.add_argument("--model", default="flux", choices=VALID_MODELS)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--seed", type=int, default=None, help="Base seed (random if omitted)")
    parser.add_argument(
        "--output",
        help="Output directory for images + dashboard (required for generate/recover modes)",
    )
    parser.add_argument("--delay", type=int, default=5, help="Seconds between images")
    parser.add_argument("--max-polls", type=int, default=8)
    parser.add_argument("--poll-wait", type=int, default=20, help="Seconds between polls on failure")
    parser.add_argument("--fetch-timeout", type=int, default=180, help="Per-request timeout seconds")
    parser.add_argument("--enhance", action="store_true", help="Enable prompt enhancement")
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Recovery mode: fetch cached images from Pollinations using a known seed base "
        "(short timeout, no polling, fast pace)",
    )
    parser.add_argument(
        "--from",
        dest="from_file",
        metavar="RUN.JSON",
        help="Load prompt/model/width/height/seed_base/count from a previous run.json "
        "(implies --recover when not otherwise specified)",
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Use the saved Pollinations API token for this run (higher rate limits, "
        "authenticated requests). Run `--set-token` first to store one.",
    )
    parser.add_argument(
        "--set-token",
        action="store_true",
        help="Open a local web form to save your Pollinations API token to "
        "`~/.bulk-image-generation/config.json`, then exit.",
    )
    parser.add_argument(
        "--clear-token",
        action="store_true",
        help="Delete the saved Pollinations API token and exit.",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print the config file path and whether a token is currently stored, then exit.",
    )
    args = parser.parse_args()

    # Standalone config-management commands: they don't need --output or --prompt.
    if args.set_token:
        return run_token_server()
    if args.clear_token:
        cfg = load_config()
        had_token = bool(cfg.pop("pollinations_token", None))
        cfg.pop("saved_at", None)
        if CONFIG_FILE.exists():
            save_config(cfg)
        emit("token-cleared", had_token=had_token, config_file=str(CONFIG_FILE))
        return 0
    if args.show_config:
        emit(
            "config",
            config_file=str(CONFIG_FILE),
            has_token=get_saved_token() is not None,
            saved_at=load_config().get("saved_at"),
        )
        return 0

    # Resolve the token to pass through to build_url. `--auth` is strict:
    # if the user asks for authenticated mode but no key is saved, surface
    # the error loudly so Claude Code can route the user to `--set-token`.
    token: str | None = None
    if args.auth:
        token = get_saved_token()
        if not token:
            parser.error(
                "--auth requested but no token saved. "
                "Run `--set-token` first to store one."
            )

    # Load metadata from a previous run if requested
    if args.from_file:
        meta_path = Path(args.from_file).expanduser().resolve()
        if not meta_path.exists():
            parser.error(f"run metadata not found: {meta_path}")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            parser.error(f"failed to parse {meta_path}: {e}")
        # Only fill fields the user didn't explicitly pass — argparse defaults
        # are used as "not provided" signals here.
        if not args.prompt:
            args.prompt = meta.get("prompt")
        if meta.get("model") and args.model == "flux":
            args.model = meta["model"]
        if meta.get("width") and args.width == 768:
            args.width = meta["width"]
        if meta.get("height") and args.height == 768:
            args.height = meta["height"]
        if args.seed is None and meta.get("seed_base") is not None:
            args.seed = int(meta["seed_base"])
        if args.count == 10 and meta.get("total"):
            args.count = int(meta["total"])
        # `--from` defaults to recovery mode because its normal use-case is
        # re-hydrating a previous session. Can be overridden by passing
        # `--recover` explicitly (which is a no-op here — the flag stays true)
        # or by ... well, if you want to rerun fresh you'd omit --from and pass
        # the params manually.
        args.recover = True

    if not args.prompt:
        parser.error("--prompt is required unless --from is used")

    if not args.output:
        parser.error("--output is required for generate and recover modes")

    if args.recover and args.seed is None:
        parser.error("--recover requires --seed (the base seed of the original run)")

    # Recovery mode short-circuits the polling/timeout defaults because
    # Pollinations cache hits return in ~1s. Anything slower than ~15s
    # means the cache is cold and the server is generating fresh, which
    # defeats the point of recovery — treat that as a miss and move on.
    if args.recover:
        fetch_timeout = 15
        poll_wait = 0
        max_polls = 0
        delay = 2
    else:
        fetch_timeout = args.fetch_timeout
        poll_wait = args.poll_wait
        max_polls = args.max_polls
        delay = args.delay

    count = max(1, min(50, args.count))
    seed_base = args.seed if args.seed is not None else random.randint(1, 99999)
    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    state = {
        "prompt": args.prompt,
        "model": args.model,
        "width": args.width,
        "height": args.height,
        "seed_base": seed_base,
        "total": count,
        "done": 0,
        "failed": 0,
        "items": [
            {"index": i + 1, "seed": seed_base + i, "status": "queued"} for i in range(count)
        ],
        "events": [],
    }

    # Mirror stdout events into state.events so the dashboard shows them too.
    def logged_emit(event: str, **fields):
        payload = {"event": event, "ts": datetime.now().isoformat(timespec="seconds")}
        payload.update(fields)
        state["events"].append(payload)
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    # Rebind the module-level `emit` so nested calls use the logged one.
    # `process_one` imports `emit` from the module namespace at call time.
    globals()["emit"] = logged_emit

    dashboard_path = output_dir / "dashboard.html"
    state["mode"] = "recover" if args.recover else "generate"
    state["auth"] = token is not None
    started_at = datetime.now().isoformat(timespec="seconds")
    state["started_at"] = started_at
    logged_emit(
        "start",
        mode=state["mode"],
        auth=token is not None,
        total=count,
        model=args.model,
        width=args.width,
        height=args.height,
        seed_base=seed_base,
        output=str(output_dir),
        dashboard=dashboard_path.as_uri(),
    )
    write_dashboard(state, output_dir)

    started = time.time()
    try:
        for i in range(count):
            process_one(
                i,
                count,
                seed_base + i,
                args.prompt,
                args.model,
                args.width,
                args.height,
                args.enhance,
                output_dir,
                max_polls,
                poll_wait,
                fetch_timeout,
                state,
                token=token,
            )
            if i < count - 1 and delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        logged_emit("interrupted")

    elapsed = round(time.time() - started)
    logged_emit("complete", done=state["done"], failed=state["failed"], elapsed_s=elapsed)
    write_dashboard(state, output_dir)

    # Persist run metadata so it can be recovered later via `--from run.json`.
    run_meta = {
        "prompt": args.prompt,
        "model": args.model,
        "width": args.width,
        "height": args.height,
        "seed_base": seed_base,
        "total": count,
        "mode": state["mode"],
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": elapsed,
        "done": state["done"],
        "failed": state["failed"],
        "items": [
            {
                "seed": it["seed"],
                "status": it.get("status"),
                "file": it.get("file"),
                "error": it.get("error"),
            }
            for it in state["items"]
        ],
    }
    (output_dir / "run.json").write_text(
        json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return 0 if state["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
