# Self-Hosted Gmail Backup

Automated Gmail backup to local Maildir format using offlineimap3 with OAuth2 in Docker. Designed for AI coding agents.

## Acceptance Criteria

1. offlineimap3 container is running and healthy (`docker ps` shows `gmail-sync` as `Up`)
2. OAuth token authenticates successfully via XOAUTH2 (no auth errors in logs)
3. All configured Gmail folders sync to local Maildir (`[Gmail]/All Mail`, `[Gmail]/Sent Mail`, `[Gmail]/Trash`, `INBOX`)
4. New emails appear on subsequent sync runs (incremental sync works)
5. Sync runs on schedule (default: every 4 hours)

## Architecture

```
Gmail IMAP ──→ offlineimap3 (XOAUTH2) ──→ Maildir on disk
```

After setup, you have a continuously updated local backup of all Gmail messages in standard Maildir format — readable by any email client, rsync-friendly, one file per message.

## Stack

| Component | Image/Tool | Role |
|-----------|-----------|------|
| offlineimap3 | `python:3.11-alpine` + offlineimap3 from GitHub | IMAP sync engine with OAuth2 support |

## Prerequisites

- A Linux host with Docker and Docker Compose
- Persistent storage for Maildir (ideally with snapshots/backups)
- A Google Cloud project with OAuth credentials (see the contact-sync guide or create a new one)
- Python 3.x on your local machine (for the one-time OAuth flow)

## Gotchas

1. **Initial sync is slow** — 100K+ messages takes hours. This is normal. Don't interrupt it. Subsequent runs are fast (incremental).

2. **offlineimap3 is not on PyPI** — must install from GitHub. The Dockerfile handles this.

3. **Python 3.12+ may not work** — offlineimap3 uses some deprecated modules. Stick with Python 3.11.

4. **OAuth eval sandbox is restricted** — offlineimap's `_eval` config options don't have `os` or `import` available. Template secrets directly into the config instead of using environment variables.

5. **Google Workspace accounts** must have the OAuth app whitelisted by the domain admin, or you'll get "This app is blocked."

6. **Token refresh is automatic** — offlineimap handles it using the refresh_token. But if the token is revoked (password change, manual revocation), you'll need to re-run the OAuth flow.

7. **Maildir format** — one file per message, standard format readable by mutt, Thunderbird, notmuch, etc. rsync-friendly for backup replication.

## Steps

### Step 1: Google Cloud Project

If you already have a GCP project with OAuth credentials (e.g., from the contact-sync setup), reuse it. Just enable the Gmail API:

```bash
gcloud services enable gmail.googleapis.com
```

If starting fresh:

```bash
gcloud projects create my-gmail-backup --name="Gmail Backup" 2>/dev/null || true
gcloud config set project my-gmail-backup
gcloud services enable gmail.googleapis.com
```

Then create OAuth credentials in the GCP Console:
1. Go to https://console.cloud.google.com/apis/credentials
2. Configure OAuth consent screen (External, minimal info)
3. Create Credentials → **OAuth client ID** → **Desktop app**
4. Copy the **Client ID** and **Client Secret**

### Step 2: Get the OAuth Refresh Token

The Gmail IMAP scope is `https://mail.google.com/`. Run this locally where you have a browser:

```python
#!/usr/bin/env python3
"""Generate OAuth refresh token for Gmail IMAP access."""
import http.server, json, sys, urllib.parse, urllib.request, webbrowser

CLIENT_ID = "<your-client-id>.apps.googleusercontent.com"
CLIENT_SECRET = "<your-client-secret>"
PORT = 8085
SCOPE = "https://mail.google.com/"

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
    print(f"\n=== OAuth for Gmail IMAP ===")
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
    print(f"\nRefresh token (save this):\n{tokens.get('refresh_token', 'ERROR: no refresh token!')}")

if __name__ == "__main__":
    main()
```

Run it:
```bash
python3 oauth_flow.py
# Browser opens — sign in with the Gmail account you want to back up
# Copy the refresh token from the output
```

#### Google Workspace accounts

Workspace-managed accounts (e.g., `user@company.com`) block unverified OAuth apps by default. The domain admin must whitelist the OAuth client ID in admin.google.com → Security → API controls → Add app by client ID. Personal Gmail accounts can click through the "unverified app" warning.

### Step 3: Create the Docker Stack

#### Directory structure

```
gmail-sync/
├── compose.yaml
├── Dockerfile.gmail-sync
└── offlineimaprc
```

#### compose.yaml

```yaml
services:
  gmail-sync:
    build:
      context: .
      dockerfile: Dockerfile.gmail-sync
    container_name: gmail-sync
    restart: unless-stopped
    entrypoint: /bin/sh
    command:
      - -c
      - |
        mkdir -p /backup/gmail
        echo "gmail-sync: starting (interval=4h)"
        while true; do
          echo "$$(date +%F\ %T) Starting Gmail backup"
          offlineimap -c /etc/offlineimaprc 2>&1
          rc=$$?
          if [ "$$rc" -eq 0 ]; then
            echo "$$(date +%F\ %T) Backup complete"
          else
            echo "$$(date +%F\ %T) Backup failed (exit code $$rc)"
          fi
          echo "$$(date +%F\ %T) Sleeping 4h until next run"
          sleep 4h
        done
    volumes:
      - /path/to/persistent/storage:/backup
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: "256m"
```

#### Dockerfile.gmail-sync

```dockerfile
FROM python:3.11-alpine
RUN apk add --no-cache git gcc musl-dev libffi-dev && \
    pip install --no-cache-dir git+https://github.com/OfflineIMAP/offlineimap3.git && \
    apk del git gcc musl-dev libffi-dev
COPY offlineimaprc /etc/offlineimaprc
ENTRYPOINT ["/bin/sh"]
```

**Why Python 3.11?** offlineimap3 is not on PyPI and may have compatibility issues with Python 3.12+.

**Why install from GitHub?** The PyPI package doesn't exist; the project distributes via GitHub releases.

#### offlineimaprc

```ini
[general]
accounts = Gmail

[Account Gmail]
localrepository = Gmail-Local
remoterepository = Gmail-Remote

[Repository Gmail-Local]
type = Maildir
localfolders = /backup/gmail

[Repository Gmail-Remote]
type = Gmail
remoteuser = <your-gmail-address>
auth_mechanisms = XOAUTH2
oauth2_client_id = <your-client-id>.apps.googleusercontent.com
oauth2_client_secret = <your-client-secret>
oauth2_refresh_token = <your-refresh-token-from-step-2>
sslcacertfile = /etc/ssl/certs/ca-certificates.crt
# Only sync All Mail (every message) plus key folders
# Exclude label folders to avoid duplicate storage
folderfilter = lambda folder: folder in ['[Gmail]/All Mail', '[Gmail]/Sent Mail', '[Gmail]/Trash', 'INBOX']
```

**Important:** This file contains OAuth secrets. Set permissions to 0600.

**Why `[Gmail]/All Mail` only?** Gmail stores every message in All Mail. Label folders are just views — syncing them would download the same messages multiple times. All Mail + Sent + Trash + INBOX covers everything.

### Step 4: Start and Verify

```bash
cd gmail-sync
docker compose up -d --build

# Watch the initial sync
docker logs -f gmail-sync
```

The first sync of a large account (100K+ messages) can take hours. Subsequent syncs are incremental.

## Verification

```bash
# Check container is running
docker ps --filter name=gmail-sync

# Check last sync result
docker logs gmail-sync --tail 5

# Check if currently syncing
docker logs gmail-sync --tail 1
# If it says "Sleeping" — last sync is done
# If it says "Copy message" — sync in progress

# Check message counts per folder
for d in /path/to/storage/gmail/*/; do
  echo "$(basename $d): $(ls "$d/cur/" 2>/dev/null | wc -l) messages"
done

# Check disk usage
du -sh /path/to/storage/gmail/
```

## Operations

**Monitoring:** Check `docker logs gmail-sync --tail 5` for last sync status. "Sleeping" means idle; "Copy message" means sync in progress; non-zero exit codes indicate failure.

**Triggering a manual sync:** Restart the container to skip the sleep timer:
```bash
docker restart gmail-sync
```

**Rebuilding after changes:**
```bash
docker compose up -d --build gmail-sync
```

**Token refresh:** Automatic via offlineimap using the refresh_token. If the token is revoked (password change, manual revocation in Google account), re-run the OAuth flow from Step 2 and update `offlineimaprc`.

## File Locations

| Logical Name | Path |
|-------------|------|
| Compose file | `gmail-sync/compose.yaml` |
| Dockerfile | `gmail-sync/Dockerfile.gmail-sync` |
| offlineimap config | `gmail-sync/offlineimaprc` |
| Maildir output | `/path/to/persistent/storage/gmail/` (mapped to `/backup/gmail` inside container) |
