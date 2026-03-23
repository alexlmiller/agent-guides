# Self-Hosted Google Drive Sync

Automated Google Drive backup to local storage or Seafile using rclone with OAuth2 in a single Docker container. Designed for AI coding agents.

## Acceptance Criteria

1. rclone container is running (`docker ps --filter name=drive-sync` shows healthy)
2. OAuth token is valid and can list Drive contents (`rclone ls gdrive: --max-depth 1`)
3. Files sync to correct destination (local directory or Seafile library)
4. Google Workspace files are converted (Docs -> docx, Sheets -> xlsx, Slides -> pptx)
5. Sync runs on schedule (default: every 6 hours)
6. Dangling shortcuts are skipped without error (`--drive-skip-dangling-shortcuts`)

## Architecture

```
Google Drive ──→ rclone (OAuth2, read-only) ──→ Local directory / Seafile
```

After setup, you have a continuously updated local mirror of your entire Google Drive. Google Workspace files are exported to standard Office formats. Subsequent syncs are incremental — only changed files are transferred.

## Stack

| Component | Image/Tool | Role |
|-----------|------------|------|
| rclone | `rclone/rclone:1.68` | Syncs Google Drive to local dir or Seafile via OAuth2 |

## Prerequisites

- A Linux host with Docker and Docker Compose
- Persistent storage for the synced files
- A Google Cloud account with OAuth credentials (reuse from contact-sync/gmail-sync, or create new)
- Python 3.x on your local machine (for the one-time OAuth flow)

## Gotchas

1. **"can't read dangling shortcut"** — Google Drive shortcuts pointing to deleted or inaccessible files. Handled automatically — `--drive-skip-dangling-shortcuts` logs a NOTICE and continues.

2. **"Duplicate directory/object found in source"** — Google Drive allows duplicate file names in the same folder. rclone logs a NOTICE and picks one. No action needed.

3. **Google Workspace file conversion** — Docs, Sheets, and Slides are exported to docx/xlsx/pptx. Drawings and other Workspace types are skipped (they have no export format). This is expected behavior.

4. **Token refresh is automatic** — rclone refreshes the access token using the refresh_token and writes it back to the config file. Since the config is copied to `/tmp/rclone.conf` at startup, the refreshed token survives within a sync cycle but is reset on container restart. This is fine — rclone re-refreshes on every start.

5. **Google Workspace accounts** must have the OAuth app whitelisted by the domain admin, or you'll get an authorization error.

6. **Shared Drives** (formerly Team Drives) are NOT synced by default. To include them, add `--drive-shared-with-me` or configure a separate `[gdrive-shared]` remote with `team_drive = <team-drive-id>`.

7. **Google Drive API rate limits** — large syncs with many small files may hit rate limits. rclone handles retries automatically, but the sync may take longer than expected. The `--transfers 4 --checkers 8` settings are conservative defaults.

8. **Seafile password obscuring** — rclone requires the Seafile password to be obscured (not plaintext). Always run `rclone obscure` before putting it in the config.

9. **Initial sync of large Drives** (100GB+) may take many hours. rclone is resumable — if interrupted, the next run picks up where it left off.

## Steps

### Step 1: Google Cloud Project

If you already have a GCP project with OAuth credentials (e.g., from the contact-sync or gmail-sync setup), reuse it. Just enable the Drive API:

```bash
gcloud services enable drive.googleapis.com
```

If starting fresh:

```bash
gcloud projects create my-drive-sync --name="Drive Sync" 2>/dev/null || true
gcloud config set project my-drive-sync
gcloud services enable drive.googleapis.com
```

Then create OAuth credentials in the GCP Console:
1. Go to https://console.cloud.google.com/apis/credentials
2. Configure OAuth consent screen (External, minimal info)
3. Create Credentials -> **OAuth client ID** -> **Desktop app**
4. Copy the **Client ID** and **Client Secret**

#### Google Workspace accounts

Workspace-managed accounts (e.g., `user@company.com`) block unverified OAuth apps by default. The domain admin must whitelist the OAuth client ID in admin.google.com -> Security -> API controls -> Add app by client ID. Personal Gmail accounts can click through the "unverified app" warning.

### Step 2: Get the OAuth Token

The Drive read-only scope is `https://www.googleapis.com/auth/drive.readonly`. Run this locally where you have a browser.

**Save this as `oauth_flow.py`:**

```python
#!/usr/bin/env python3
"""Generate OAuth tokens for rclone Google Drive sync."""
import http.server, json, sys, urllib.parse, urllib.request, webbrowser

CLIENT_ID = "<your-client-id>.apps.googleusercontent.com"
CLIENT_SECRET = "<your-client-secret>"
PORT = 8085
SCOPE = "https://www.googleapis.com/auth/drive.readonly"

auth_code = None

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Success! Close this tab.</h1>")
        else:
            self.send_response(400)
            self.end_headers()
    def log_message(self, *a): pass

def main():
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID, "redirect_uri": f"http://localhost:{PORT}",
        "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent",
    })
    print(f"\n=== OAuth for Google Drive (read-only) ===")
    webbrowser.open(url)

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    print(f"Waiting on localhost:{PORT}...")
    while auth_code is None:
        server.handle_request()
    server.server_close()

    print("Exchanging code for tokens...")
    data = urllib.parse.urlencode({
        "code": auth_code, "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": f"http://localhost:{PORT}",
        "grant_type": "authorization_code",
    }).encode()
    resp = urllib.request.urlopen(urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data))
    tokens = json.loads(resp.read())

    # rclone expects this exact JSON format in the token field of rclone.conf.
    # The "expiry" field tells rclone when to refresh; set it to the past to
    # force an immediate refresh on first run.
    token_json = {
        "access_token": tokens["access_token"],
        "token_type": "Bearer",
        "refresh_token": tokens.get("refresh_token"),
        "expiry": "2024-01-01T00:00:00.000000Z",
    }
    print(f"\nrclone token (paste this into rclone.conf):")
    print(json.dumps(token_json))
    print(f"\nrefresh_token present: {'yes' if tokens.get('refresh_token') else 'NO — re-run with prompt=consent'}")

if __name__ == "__main__":
    main()
```

Run it:
```bash
python3 oauth_flow.py
# Browser opens — sign in with the Google account whose Drive you want to sync
# Copy the JSON token from the output
```

### Step 3: Choose a Storage Backend

rclone can sync to a local directory or to a Seafile library. Pick the one that fits your setup.

#### Option A: Local directory (simplest)

Sync to a directory on disk. Good for NAS storage, NFS mounts, or any persistent path.

#### Option B: Seafile library

Sync into a Seafile library for versioning, dedup, and web access. Requires a Seafile instance and a service account. You'll need:

- Seafile server URL
- A Seafile user account (dedicated service account recommended — no 2FA)
- A library shared with that account
- The password obscured with rclone: `docker run --rm -it rclone/rclone obscure <password>`

### Step 4: Create the Docker Stack

#### Directory structure

```
drive-sync/
├── compose.yaml
└── rclone.conf
```

#### compose.yaml

The rclone config is mounted read-only and copied to a writable location at startup. This allows rclone to persist refreshed OAuth tokens in-memory (the refreshed token is written back to the working copy at `/tmp/rclone.conf`).

Note: `$$` is Docker Compose's escape for a literal `$` in command strings.

```yaml
services:
  drive-sync:
    image: rclone/rclone:1.68
    container_name: drive-sync
    restart: unless-stopped
    entrypoint: /bin/sh
    command:
      - -c
      - |
        cp /config/rclone.conf /tmp/rclone.conf || { echo "FATAL: Failed to copy rclone config" >&2; exit 1; }
        echo "drive-sync: starting (interval=6h)"
        while true; do
          echo "$$(date +%F\ %T) Starting Google Drive sync"
          rclone sync gdrive: local:/backup/google-drive \
            --config /tmp/rclone.conf \
            --drive-export-formats docx,xlsx,pptx \
            --drive-acknowledge-abuse \
            --drive-skip-dangling-shortcuts \
            --transfers 4 \
            --checkers 8 \
            --log-level INFO \
            --stats-one-line \
            --stats 30s \
            2>&1
          rc=$$?
          if [ "$$rc" -eq 0 ]; then
            echo "$$(date +%F\ %T) Sync complete"
          else
            echo "$$(date +%F\ %T) Sync failed (exit code $$rc)"
          fi
          echo "$$(date +%F\ %T) Sleeping 6h until next run"
          sleep 6h
        done
    volumes:
      - ./rclone.conf:/config/rclone.conf:ro
      - /path/to/persistent/storage:/backup
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: "256m"
    cpus: "0.5"
    pids_limit: 64
```

**If using Seafile instead of local storage**, change the `rclone sync` line:
```bash
rclone sync gdrive: seafile:"Google Drive" \
```
And remove the `/path/to/persistent/storage:/backup` volume mount (Seafile is accessed over the network).

#### rclone.conf — Local storage

```ini
[gdrive]
type = drive
client_id = <your-client-id>.apps.googleusercontent.com
client_secret = <your-client-secret>
scope = drive.readonly
token = <paste-the-json-token-from-step-2-on-one-line>
```

The `token` value is the single-line JSON from Step 2, e.g.:
```
token = {"access_token":"ya29.xxx","token_type":"Bearer","refresh_token":"1//xxx","expiry":"2024-01-01T00:00:00.000000Z"}
```

#### rclone.conf — Seafile storage

```ini
[gdrive]
type = drive
client_id = <your-client-id>.apps.googleusercontent.com
client_secret = <your-client-secret>
scope = drive.readonly
token = <paste-the-json-token-from-step-2-on-one-line>

[seafile]
type = seafile
url = https://<your-seafile-hostname>
user = <seafile-username>
pass = <rclone-obscured-password>
library = <library-name>
```

To obscure the Seafile password:
```bash
docker run --rm -it rclone/rclone obscure '<your-seafile-password>'
```

**Important:** `rclone.conf` contains OAuth secrets. Set permissions to 0600:
```bash
chmod 600 rclone.conf
```

### Step 5: Start and Verify

```bash
cd drive-sync
docker compose up -d

# Watch the initial sync
docker logs -f drive-sync
```

The first sync of a large Drive (50GB+) can take hours depending on bandwidth. Subsequent syncs are incremental and much faster.

## Verification

Dry run (recommended before first real sync):

```bash
# Local storage
docker exec drive-sync rclone sync gdrive: local:/backup/google-drive \
  --config /tmp/rclone.conf \
  --drive-export-formats docx,xlsx,pptx \
  --drive-skip-dangling-shortcuts \
  --dry-run 2>&1

# Seafile
docker exec drive-sync rclone sync gdrive: seafile:"Google Drive" \
  --config /tmp/rclone.conf \
  --drive-export-formats docx,xlsx,pptx \
  --drive-skip-dangling-shortcuts \
  --dry-run 2>&1
```

Verify synced files:

```bash
# Local storage — check file count and size
du -sh /path/to/persistent/storage/google-drive/
find /path/to/persistent/storage/google-drive/ -type f | wc -l

# Seafile — check via rclone
docker exec drive-sync rclone ls seafile:"Google Drive" --config /tmp/rclone.conf | wc -l
```

## Operations

**Monitoring:**

```bash
# Check last sync result
docker logs drive-sync --tail 5

# Check if currently syncing
docker logs drive-sync --tail 1
# If it says "Sleeping" — last sync is done
# If it says transfer stats — sync in progress

# Check container status
docker ps --filter name=drive-sync
```

**Triggering an immediate sync:**

```bash
# Restart the container — it starts a sync immediately
docker restart drive-sync
```

**Refreshing the OAuth token** (if revoked or consent reset):

1. Re-run `oauth_flow.py` (Step 2)
2. Replace the `token = ` line in `rclone.conf` with the new JSON
3. Restart: `docker compose up -d`

**Rebuilding after changes:**

```bash
docker compose up -d
```

No `--build` needed since this uses the stock `rclone/rclone` image (no custom Dockerfile).

## File Locations

| What | Where |
|------|-------|
| Synced files (local) | `/path/to/storage/google-drive/` |
| rclone config | `drive-sync/rclone.conf` (contains secrets — 0600) |
| Compose config | `drive-sync/compose.yaml` |
| Working rclone config (in-container) | `/tmp/rclone.conf` (writable copy for token refresh) |
