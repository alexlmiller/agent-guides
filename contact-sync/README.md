# Self-Hosted Contact Sync & Dedup

Self-hosted CardDAV contact synchronization with intelligent deduplication across Google and iCloud accounts. Designed for AI coding agents.

## Acceptance Criteria

1. Radicale is running, healthy, and accessible at `http://localhost:5232/.web/`
2. vdirsyncer discovers all configured pairs (Google accounts + iCloud) without errors
3. Initial sync pulls contacts from all accounts into Radicale collections
4. Dedup dry-run produces no obviously wrong merges (no clusters with mismatched names)
5. Dedup apply merges duplicates and fans the unified contact set to all collections
6. Contact counts match across all collections after dedup (true unification, not just dedup)
7. Second dedup run is idempotent (0 duplicates found, 0 unified)
8. Bidirectional sync works: changes made in Google/iCloud propagate to Radicale and vice versa
9. vdirsyncer cron runs sync every 15 minutes automatically
10. contact-dedup runs at :15 past the hour automatically

## Architecture

```
[Google Acct 1] ──┐                         ┌── [Google Acct 1]
[Google Acct 2] ──┤── vdirsyncer ──→ Radicale ──→ dedup ──→ vdirsyncer ──┤── [Google Acct 2]
[iCloud]        ──┘    (pull)        (hub)      (merge)      (push)      └── [iCloud]
```

A three-container Docker stack that:

1. **Radicale** — CardDAV server, central hub for all contacts
2. **vdirsyncer** — Pulls/pushes contacts between Radicale and Google/iCloud on an hourly schedule
3. **contact-dedup** — Multi-signal fuzzy deduplication that runs after each sync, merging duplicates and fanning out the unified set to all collections

After setup, all accounts converge to the same unified contact set. New contacts added anywhere propagate everywhere. Duplicates are automatically merged. Contacts that only exist in one account are fanned out to all others (true unification, not just dedup).

## Stack

| Component | Image/Tool | Role |
|-----------|-----------|------|
| Radicale | `tomsquest/docker-radicale:3.6.1.0` | CardDAV server, central contact hub |
| vdirsyncer | `bleala/vdirsyncer:2.6.1` | Syncs contacts between Radicale and Google/iCloud |
| contact-dedup | Custom (`python:3.12-alpine` + `vobject`) | Multi-signal fuzzy dedup, merge, and full fan-out |

## Prerequisites

- A Linux host with Docker and Docker Compose
- A persistent storage path for contact data (ideally with snapshots/backups — if something goes wrong during the first dedup, you want a rollback point)
- A Google Cloud account (for OAuth credentials)
- Python 3.x on the machine running the setup (for the OAuth flow script — this runs locally where you have a browser, not on the Docker host)

## Gotchas

1. **Google Workspace accounts** must have the OAuth app whitelisted in the domain's admin console, or users will see **"This app is blocked"** with no way to proceed. Personal Gmail shows a bypassable warning instead.

2. **Shared family/couple emails** (e.g., `mike.kathy@gmail.com`) — the dedup correctly avoids merging contacts with different given names that share an email. Unnamed stubs can still bridge family members in rare cases, but the cluster name-group validation catches most of these.

3. **Office/shared phones** — contacts sharing an office phone are NOT merged. Phone match requires name similarity > 0.7. This was a hard-won lesson from real data where coworkers at the same company shared a main office number.

4. **OAuth token refresh** — vdirsyncer handles token refresh automatically using the refresh_token. But if the GCP project's OAuth consent is revoked or the token becomes invalid, you'll need to re-run the OAuth flow script.

5. **Initial sync of large address books** (5000+ contacts) may take 10+ minutes and could hit Google API rate limits. Sync one account at a time and wait between them if you encounter errors.

6. **Token file format** — vdirsyncer uses `aiohttp_oauthlib`, which expects standard OAuth2 response format (`access_token`, `token_type`, `refresh_token`). The `google-auth` library uses a different format (`token` instead of `access_token`). If tokens don't work, check the JSON key names.

7. **Radicale web UI** is extremely minimal — it shows collection names and contact counts, but has no contact browser or editor. To inspect individual contacts, look at the VCF files on disk or use a CardDAV client.

8. **CardDAV API vs People API** — vdirsyncer uses CardDAV (not the People/Contacts REST API), which is a separate Google API that must be explicitly enabled (`carddav.googleapis.com`).

9. **vdirsyncer status directory ownership** — vdirsyncer runs as UID 1000 inside the container. The entire status directory must be writable by this user, not just the token files.

10. **`$$` in compose.yaml** — `$$` is Docker Compose's escape for a literal `$` in command strings. If adapting for a non-Compose context, use single `$`.

11. **Stop vdirsyncer before dedup** — after initial sync, stop vdirsyncer to prevent it from syncing back before you've verified the dedup results. The hourly cron could fire and push unreviewed changes.

## Steps

### Step 1: Google Cloud Project

#### Create project and enable APIs

```bash
# Create project (or use an existing one)
gcloud projects create my-contact-sync --name="Contact Sync" 2>/dev/null || true
gcloud config set project my-contact-sync

# IMPORTANT: Enable the CardDAV API specifically — the People API is NOT sufficient.
# vdirsyncer uses CardDAV (not the People/Contacts REST API), which is a separate
# Google API that must be explicitly enabled.
gcloud services enable carddav.googleapis.com
```

#### Create OAuth credentials

This step must be done in the GCP Console (no CLI equivalent):

1. Go to: https://console.cloud.google.com/apis/credentials
2. If prompted, configure the OAuth consent screen:
   - User type: **External**
   - App name: anything (e.g., "Contact Sync")
   - Support email: your email
   - Developer contact: your email
   - Leave everything else blank, save
3. Create Credentials → **OAuth client ID**
   - Application type: **Desktop app**
   - Name: anything
4. Copy the **Client ID** and **Client Secret**

A single OAuth client ID works for all your Google accounts — the scopes are requested at authorization time, not configured on the client.

#### Google Workspace accounts

If syncing contacts from Google Workspace accounts (not just personal Gmail), each Workspace domain's admin must whitelist the OAuth app:

1. Sign in to `admin.google.com` for each domain
2. Security → Access and data control → API controls
3. Manage Third-Party App Access → Add app → OAuth App Client ID
4. Enter your client ID, set access to **Trusted**

Personal Gmail accounts don't need this — they'll see an "unverified app" warning but can click through via Advanced → "Go to app (unsafe)."

Without whitelisting, Workspace users will see **"This app is blocked"** with no way to proceed.

### Step 2: iCloud App Password

If syncing iCloud contacts:

1. Go to https://appleid.apple.com
2. Sign-In & Security → App-Specific Passwords
3. Generate a password, label it "contact-sync"
4. Save the password

Your iCloud username is your Apple ID email (which may be a Gmail address — that's fine).

### Step 3: Generate Radicale Password

```bash
# Generate a random password for the Radicale sync user
RADICALE_PASSWORD=$(openssl rand -base64 24)
echo "Radicale password: $RADICALE_PASSWORD"

# Generate the bcrypt htpasswd entry (option A: if htpasswd is installed)
htpasswd -nbB sync "$RADICALE_PASSWORD"

# Option B: if htpasswd is not available, use Python
python3 -c "
import bcrypt
password = '$RADICALE_PASSWORD'
hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
print(f'sync:{hashed}')
"

# Save the output line (sync:$2y$...) — you'll need it for the users file
```

### Step 4: Create the Docker Stack

#### Directory structure

```
contact-sync/
├── compose.yaml
├── .env
├── radicale-config
├── vdirsyncer-config
├── Dockerfile.dedup
└── contact-dedup.py
```

#### compose.yaml

Adjust paths to match your storage setup. The key requirement: Radicale data must be on persistent storage (not an ephemeral Docker volume).

Note: `$$` is Docker Compose's escape for a literal `$` in command strings. If adapting this for a non-Compose context, use single `$`.

```yaml
services:
  radicale:
    image: tomsquest/docker-radicale:3.6.1.0
    container_name: radicale
    restart: unless-stopped
    ports:
      - "127.0.0.1:5232:5232"
      # Add your Tailscale/VPN IP here if you want remote access:
      # - "100.x.x.x:5232:5232"
    environment:
      - TAKE_FILE_OWNERSHIP=true
    volumes:
      - /path/to/persistent/storage/radicale:/data
      - ./radicale-config:/config/config:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5232/.web/"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - SETUID
      - SETGID
      - DAC_OVERRIDE
    security_opt:
      - no-new-privileges:true
    mem_limit: "256m"

  vdirsyncer:
    image: bleala/vdirsyncer:2.6.1
    container_name: vdirsyncer
    restart: unless-stopped
    environment:
      - VDIRSYNCER_CONFIG=/vdirsyncer/config
      - AUTODISCOVER=false
      - AUTOSYNC=true
    env_file: .env
    volumes:
      - /path/to/persistent/storage/vdirsyncer:/vdirsyncer/status
      - ./vdirsyncer-config:/vdirsyncer/config:ro
    depends_on:
      radicale:
        condition: service_healthy
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: "128m"

  contact-dedup:
    build:
      context: .
      dockerfile: Dockerfile.dedup
    container_name: contact-dedup
    restart: unless-stopped
    entrypoint: /bin/sh
    command:
      - -c
      - |
        echo "contact-dedup: waiting for first run at :15 past the hour"
        while true; do
          min=$$(date +%M)
          sec=$$(date +%S)
          if [ "$$min" -lt 15 ]; then
            wait_sec=$$(( (15 - $$min) * 60 - $$sec ))
          else
            wait_sec=$$(( (75 - $$min) * 60 - $$sec ))
          fi
          echo "$$(date +%F\ %T) Sleeping $${wait_sec}s until next dedup run"
          sleep "$$wait_sec"
          echo "$$(date +%F\ %T) Starting contact dedup"
          python3 /app/contact-dedup.py --data /data --apply 2>&1
          echo "$$(date +%F\ %T) Dedup complete"
        done
    volumes:
      - /path/to/persistent/storage/radicale:/data
    depends_on:
      radicale:
        condition: service_healthy
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: "128m"
```

#### .env

```bash
GOOGLE_CLIENT_ID=<your-client-id>.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=<your-client-secret>
ICLOUD_APP_PASSWORD=<your-icloud-app-password>
RADICALE_PASSWORD=<your-generated-password>
```

#### radicale-config

```ini
[server]
hosts = 0.0.0.0:5232

[auth]
type = htpasswd
htpasswd_filename = /data/users
htpasswd_encryption = bcrypt

[storage]
type = multifilesystem
filesystem_folder = /data/collections

[rights]
type = authenticated

[logging]
level = info
```

#### vdirsyncer-config

Adjust the number of Google accounts and the iCloud email. Each Google account gets a numbered pair (google1, google2, etc.).

`conflict_resolution = "a wins"` means external sources (Google/iCloud) win on conflict. This is correct for the initial import and ongoing operation because: (1) humans edit contacts in Google/iCloud, not Radicale directly, and (2) the dedup service handles unification after sync. If you want Radicale to be the authoritative editor, change to `"b wins"`.

```ini
[general]
status_path = "/vdirsyncer/status/"

# --- Google Account 1 ---
[pair google1_pair]
a = "google1_contacts"
b = "radicale_google1"
collections = ["from a"]
conflict_resolution = "a wins"
metadata = ["displayname"]

[storage google1_contacts]
type = "google_contacts"
token_file = "/vdirsyncer/status/google1_token"
client_id.fetch = ["command", "sh", "-c", "echo -n $GOOGLE_CLIENT_ID"]
client_secret.fetch = ["command", "sh", "-c", "echo -n $GOOGLE_CLIENT_SECRET"]

[storage radicale_google1]
type = "carddav"
url = "http://radicale:5232/sync/google1/"
username = "sync"
password.fetch = ["command", "sh", "-c", "echo -n $RADICALE_PASSWORD"]

# --- Repeat for additional Google accounts (google2, google3, etc.) ---
# Copy the pair + two storage blocks above, incrementing the number.

# --- iCloud ---
[pair icloud_pair]
a = "icloud_contacts"
b = "radicale_icloud"
collections = ["from a"]
conflict_resolution = "a wins"
metadata = ["displayname"]

[storage icloud_contacts]
type = "carddav"
url = "https://contacts.icloud.com/"
username = "<your-apple-id-email>"
password.fetch = ["command", "sh", "-c", "echo -n $ICLOUD_APP_PASSWORD"]

[storage radicale_icloud]
type = "carddav"
url = "http://radicale:5232/sync/icloud/"
username = "sync"
password.fetch = ["command", "sh", "-c", "echo -n $RADICALE_PASSWORD"]
```

#### Sync schedule

With `AUTOSYNC=true`, the bleala/vdirsyncer image runs its built-in supercronic cron every 15 minutes, executing `vdirsyncer metasync && vdirsyncer sync` (discover + sync). No custom crontab is needed.

#### Dockerfile.dedup

```dockerfile
FROM python:3.12-alpine
RUN pip install --no-cache-dir vobject
COPY contact-dedup.py /app/contact-dedup.py
ENTRYPOINT ["python3", "/app/contact-dedup.py"]
```

#### contact-dedup.py

This is an ~800-line Python script that handles multi-signal fuzzy matching, merge, and full fan-out. It is the core intelligence of the system. The full source is included in the `contact-dedup-script.py` file alongside this guide.

If you don't have that file, the latest version lives at:
https://github.com/alexlmiller/infra/blob/main/roles/docker_stacks/templates/contact-sync/contact-dedup.py.j2

### Step 5: Create Radicale Users File and Data Directories

Before starting the stack:

```bash
# Create the data directories
mkdir -p /path/to/persistent/storage/radicale
mkdir -p /path/to/persistent/storage/vdirsyncer

# Write the htpasswd file (use the output from Step 3)
echo 'sync:$2y$05$...<your-bcrypt-hash>...' > /path/to/persistent/storage/radicale/users

# IMPORTANT: vdirsyncer runs as UID 1000 inside the container.
# The entire status directory must be writable by this user,
# not just the token files.
chown -R 1000:1000 /path/to/persistent/storage/vdirsyncer
```

### Step 6: Start Radicale and Verify

```bash
cd contact-sync
docker compose up -d radicale

# Wait for healthy
sleep 10
docker ps --filter name=radicale
# Should show "healthy"

# Test the web UI: http://localhost:5232/.web/
# Login: sync / <your-radicale-password>
# Note: Radicale's web UI is very minimal — it shows collections
# but has no contact browser or editor. All the real work happens
# through vdirsyncer and the dedup service.
```

### Step 7: OAuth Token Generation

vdirsyncer's OAuth flow runs inside the container and starts a local HTTP server to receive the browser redirect. This doesn't work when the container is on a remote host (the redirect goes to `localhost` on your machine, not inside the container). Use this local Python script instead.

**Save this as `oauth_flow.py` on the machine with a web browser:**

```python
#!/usr/bin/env python3
"""Generate OAuth tokens for vdirsyncer Google Contacts sync."""
import http.server, json, sys, urllib.parse, urllib.request, webbrowser

CLIENT_ID = "<your-client-id>.apps.googleusercontent.com"
CLIENT_SECRET = "<your-client-secret>"
PORT = 8085
SCOPE = "https://www.googleapis.com/auth/carddav"

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
    label = sys.argv[1] if len(sys.argv) > 1 else "google"
    out = sys.argv[2] if len(sys.argv) > 2 else f"/tmp/{label}_token.json"

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID, "redirect_uri": f"http://localhost:{PORT}",
        "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent",
    })

    print(f"\n=== OAuth for: {label} ===")
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

    # CRITICAL: vdirsyncer expects this exact JSON format.
    # Do NOT use google-auth format (which uses "token" instead of "access_token").
    # vdirsyncer uses aiohttp_oauthlib which requires the standard OAuth2 response format.
    token_file = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "token_type": "Bearer",
        "expires_in": tokens.get("expires_in", 3599),
    }
    with open(out, "w") as f:
        json.dump(token_file, f, indent=2)
    print(f"Token saved to: {out}")
    print(f"  refresh_token: {'yes' if tokens.get('refresh_token') else 'NO!'}")

if __name__ == "__main__":
    main()
```

**Run it for each Google account:**

```bash
python3 oauth_flow.py "google1" /tmp/google1_token.json
# Browser opens — sign in with the correct Google account
# Repeat for google2, google3, etc.
```

**Copy tokens to the vdirsyncer container:**

```bash
# Option A: Direct copy if you have write access to the storage path
cp /tmp/google1_token.json /path/to/persistent/storage/vdirsyncer/google1_token
chown 1000:1000 /path/to/persistent/storage/vdirsyncer/google1_token

# Option B: Pipe into container via docker exec (works when agent can't write
# to the storage path directly — useful for remote hosts or restricted permissions)
cat /tmp/google1_token.json | docker exec -i vdirsyncer tee /vdirsyncer/status/google1_token > /dev/null
```

### Step 8: Discover and Initial Sync

#### Start vdirsyncer

```bash
docker compose up -d vdirsyncer
```

#### Run discovery for each pair

Discovery sets up the collection mappings. Pipe `y` to auto-accept collection creation.

```bash
# Google accounts
echo "y" | docker exec -i vdirsyncer vdirsyncer discover google1_pair
echo "y" | docker exec -i vdirsyncer vdirsyncer discover google2_pair
# ... repeat for each account

# iCloud (no OAuth needed — uses app password)
echo "y" | docker exec -i vdirsyncer vdirsyncer discover icloud_pair
```

#### Initial sync (one-way pull into empty Radicale)

```bash
# Sync each pair — this pulls contacts into Radicale
docker exec vdirsyncer vdirsyncer sync google1_pair
docker exec vdirsyncer vdirsyncer sync google2_pair
docker exec vdirsyncer vdirsyncer sync icloud_pair
```

This may take several minutes for large address books (1000+ contacts).

#### Stop vdirsyncer before dedup

**Important**: Stop vdirsyncer now to prevent it from syncing back before you've verified the dedup results. The hourly cron could fire and push unreviewed changes.

```bash
docker stop vdirsyncer
```

#### Verify the pull

```bash
# Count contacts per collection
for dir in $(docker exec radicale find /data/collections/collection-root/sync -maxdepth 1 -type d | tail -n +2); do
  name=$(basename $dir)
  count=$(docker exec radicale sh -c "ls $dir/*.vcf 2>/dev/null | wc -l")
  echo "$name: $count contacts"
done
```

### Step 9: Run Initial Dedup

Build the dedup container and run it manually first:

```bash
# Build and start dedup container
docker compose up -d --build contact-dedup

# Run a dry-run first to see what would be merged
docker exec contact-dedup python3 /app/contact-dedup.py --data /data
# Review the JSON output — check the sample_clusters for any obviously wrong merges.
# Look for clusters where contacts with different names are grouped together.

# If satisfied, apply
docker exec contact-dedup python3 /app/contact-dedup.py --data /data --apply

# Verify idempotency — should find 0 duplicates AND 0 unified
docker exec contact-dedup python3 /app/contact-dedup.py --data /data --apply
```

#### What the dedup does

**Matching signals** (in order of strength):

| Signal | Score | Requirements |
|--------|-------|-------------|
| Shared email (both named) | 0.95 | Given names must be compatible (Jaro-Winkler > 0.6) to prevent family email merging |
| Shared email (one unnamed) | 0.90 | Unnamed stub matched to a named contact |
| FullContact cluster ID | 0.92 | Name similarity > 0.6 required (FC sometimes misclusters) |
| Org name + phone (businesses) | 0.92 | Both contacts unnamed, org name > 0.9 similar |
| Shared phone (both named) | 0.85 | Name similarity > 0.7 required (prevents office phone chaining) |

Name similarity alone is **never** sufficient — it always requires a hard identifier (email, phone, or FC cluster ID).

**Safety mechanisms**:
- Cluster validation: after union-find grouping, each cluster is verified by checking all members against an anchor contact directly. Transitive chains (A matches B, B matches C, but A doesn't match C) are broken apart.
- Name group validation: if a cluster contains named contacts with incompatible given names (e.g., "James" and "Zem" from the same family), they're split into separate clusters.
- Same-UID skip: contacts fanned out across collections share a UID and are never re-matched as duplicates.

**Full unification**: After dedup, ALL contacts are fanned out to ALL collections — not just contacts that had duplicates. A contact that only existed in Google will appear in iCloud, and vice versa. This is what makes it true unification, not just dedup.

### Step 10: Verify and Go Live

After the dedup looks correct:

```bash
# Both collections should have approximately the same contact count
for dir in $(docker exec radicale find /data/collections/collection-root/sync -maxdepth 1 -type d | tail -n +2); do
  name=$(basename $dir)
  count=$(docker exec radicale sh -c "ls $dir/*.vcf 2>/dev/null | wc -l")
  echo "$name: $count contacts"
done

# Re-enable vdirsyncer to start bidirectional sync
docker start vdirsyncer
```

At this point:
- Radicale has the deduplicated, unified contact set
- vdirsyncer's built-in cron syncs every 15 minutes
- contact-dedup merges new duplicates and fans out at :15
- All accounts will converge to the same unified set

## Verification

Confirm each acceptance criterion:

```bash
# 1. Radicale healthy
docker ps --filter name=radicale --format '{{.Status}}'
# Should show "(healthy)"

# 2. Discovery succeeded (no errors in logs)
docker logs vdirsyncer 2>&1 | grep -i error

# 3-6. Contact counts match across all collections
for dir in $(docker exec radicale find /data/collections/collection-root/sync -maxdepth 1 -type d | tail -n +2); do
  name=$(basename $dir)
  count=$(docker exec radicale sh -c "ls $dir/*.vcf 2>/dev/null | wc -l")
  echo "$name: $count contacts"
done

# 7. Dedup idempotency (0 duplicates, 0 unified)
docker exec contact-dedup python3 /app/contact-dedup.py --data /data

# 8. Bidirectional sync — add a test contact in Google, wait 15+ minutes,
#    verify it appears in other collections

# 9-10. Cron running
docker logs vdirsyncer --tail 5
docker logs contact-dedup --tail 5
```

## Operations

**Monitoring logs:**

```bash
docker logs vdirsyncer --tail 20    # sync logs
docker logs contact-dedup --tail 20 # dedup logs
docker logs radicale --tail 20      # access logs
```

**Triggering a manual sync:**

```bash
docker exec vdirsyncer vdirsyncer sync
```

**Triggering a manual dedup:**

```bash
# Dry-run
docker exec contact-dedup python3 /app/contact-dedup.py --data /data
# Apply
docker exec contact-dedup python3 /app/contact-dedup.py --data /data --apply
```

**Stopping bidirectional sync** (to inspect/modify contacts in Radicale before syncing changes back):

```bash
docker stop vdirsyncer
# Make changes in Radicale...
docker start vdirsyncer
```

**Rebuilding after changes** (e.g., modifying `contact-dedup.py`):

```bash
docker compose up -d --build contact-dedup
```

**OAuth token refresh** — vdirsyncer handles this automatically. If a token becomes invalid (consent revoked, etc.), re-run `oauth_flow.py` for the affected account and copy the new token file into the vdirsyncer status directory.

## File Locations

| What | Where |
|------|-------|
| Contact VCF files | `/path/to/storage/radicale/collections/collection-root/sync/` |
| vdirsyncer tokens | `/path/to/storage/vdirsyncer/google1_token` etc. |
| vdirsyncer status | `/path/to/storage/vdirsyncer/` (must be owned by UID 1000) |
| Radicale users | `/path/to/storage/radicale/users` |
| Compose config | `contact-sync/compose.yaml` |
