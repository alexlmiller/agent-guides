# Paperless-ngx Smart Ingestion: Signal + Email + AI Classification

Three-channel document ingestion (Signal, email, manual upload) with AI-powered classification into Paperless-ngx. Designed for AI coding agents.

## Acceptance Criteria

1. Paperless-ngx is running with Tika + Gotenberg sidecars (`.eml` and Office doc support functional)
2. AI classification script processes every consumed document and sets title, correspondent, document type, tags, AI summary, and expiration date
3. Signal bot receives photos/documents in a designated group and routes them to Paperless API with ingestion context
4. Cloudflare Email Worker extracts attachments from emails sent to `docs@yourdomain.com` and forwards them to Paperless via Cloudflare Tunnel
5. All three intake channels produce correctly classified documents with appropriate source tags (`manual-upload`, `signal-import`, `email-import`)

## Architecture

```
Signal Group Photo ──► signal-router ──► Paperless API ──► AI Classification
                                                              │
Email w/ attachment ──► CF Email Worker ──► CF Tunnel ────────┘
                                                              │
Manual upload ────────► Paperless Web UI ─────────────────────┘
```

**Three ways to ingest documents:**
1. **Signal**: Send a photo/document to a Signal group — it gets routed to Paperless with your caption as context
2. **Email**: Forward an email to a dedicated address — attachments are extracted, or the whole email is saved as `.eml`
3. **Manual**: Upload via Paperless web UI as usual

**AI classification** runs on every document (Claude Haiku) and sets:
- Title, correspondent, document type
- Tags from a fixed taxonomy (category + source + year)
- AI summary for search
- Expiration date with retention guidelines

## Stack

| Component | Image/Tool | Role |
|-----------|-----------|------|
| paperless-ngx | paperless-ngx | Document management, OCR, storage |
| gotenberg | `gotenberg/gotenberg:8.21.0` | HTML/Office → PDF conversion for `.eml` rendering |
| tika | `apache/tika:3.2.3.0` | Document parsing and text extraction |
| signal-cli-rest-api | `bbernhard/signal-cli-rest-api:0.98` | Signal protocol handler (send/receive messages) |
| signal-router | `python:3.12-alpine` + custom script | Polls signal-cli, routes attachments to Paperless |
| Cloudflare Email Worker | Cloudflare Workers | Receives email, extracts attachments, POSTs to Paperless |

## Prerequisites

- **Paperless-ngx** running in Docker with API access
- **Docker Compose** for the Signal bot stack
- **Cloudflare** account with a domain using Email Routing (for email ingestion)
- **Cloudflare Tunnel** (`cloudflared`) running and connected to your network
- **Anthropic API key** (for AI classification)
- **A phone number** available for Signal registration (can be a VoIP number)

## Gotchas

1. **Custom fields format differs between POST and PATCH** — `post_document` uses `{field_id: value}` object mapping format; `PATCH` uses `[{field: id, value: val}]` array format. Mixing these up causes silent failures.

2. **Signal group ID mismatch** — The group ID returned by the create endpoint (`id` field, prefixed with `group.`) is a double-encoded base64 value. The actual ID you need for matching incoming messages is the `internal_id` field from the groups list endpoint. These are different strings — using the wrong one means your router will silently ignore all group messages.

3. **signal-cli requires SETUID/SETGID caps** — `cap_drop: ALL` + `cap_add: SETUID, SETGID` is required because signal-cli's entrypoint uses `setpriv` to switch users internally. Without these caps it will crash on startup. Do NOT use `no-new-privileges: true` as it conflicts with `setpriv`.

4. **signal-cli MODE must be `normal`** — In `json-rpc` mode, the `/v1/receive` endpoint requires WebSocket, but the router uses simple HTTP polling. Must use `MODE=normal`.

5. **The `/v1/receive` endpoint is destructive** — Messages are consumed on read. If the router crashes mid-processing, those messages are lost.

6. **Tika + Gotenberg required for `.eml` fallback** — Without them, Paperless has no parser for `message/rfc822` and will reject `.eml` files. The email worker's fallback (save entire email as `.eml` when no real attachments found) depends on this.

7. **Nested MIME parsing is limited** — The email worker's hand-rolled MIME parser handles the outermost boundary only. Deeply nested `multipart/mixed` containing `multipart/alternative` may miss attachments.

8. **AI script uses only stdlib** — The classification script uses only Python stdlib (`urllib`, `json`, `logging`) — no pip dependencies needed. Runs inside the Paperless container.

9. **Documents expiring within 30 days** get an automatic `expiring` tag added by the classification script.

10. **Tags are deduplicated and lowercased** before resolving against Paperless. The script uses a `get_or_create` pattern for correspondents, document types, tags, and custom fields (idempotent).

## Steps

### Step 1: Tika + Gotenberg (Required for Email Ingestion)

Add Gotenberg and Tika as sidecar containers in your Paperless stack. These enable Paperless to handle `.eml` files (email), Office documents, and other formats beyond PDF/image.

```yaml
  gotenberg:
    image: gotenberg/gotenberg:8.21.0
    container_name: gotenberg
    restart: unless-stopped
    networks:
      - internal          # Only Paperless needs to reach it
    command:
      - "gotenberg"
      - "--api-timeout=60s"
      - "--chromium-disable-javascript=true"
      - "--chromium-allow-list=file:///tmp/.*"
    mem_limit: "512m"

  tika:
    image: apache/tika:3.2.3.0
    container_name: tika
    restart: unless-stopped
    networks:
      - internal          # Only Paperless needs to reach it
    mem_limit: "512m"
```

Add these environment variables to your Paperless container:
```yaml
  - PAPERLESS_TIKA_ENABLED=true
  - PAPERLESS_TIKA_GOTENBERG_ENDPOINT=http://gotenberg:3000
  - PAPERLESS_TIKA_ENDPOINT=http://tika:9998
```

And add `depends_on` entries for both in Paperless. Neither container needs exposed ports — they communicate with Paperless via the internal Docker network only.

**What this enables:**
- `.eml` files → rendered as PDF with email headers + HTML body (via Chromium)
- Office docs (.docx, .xlsx, .pptx, .odt, etc.) → converted and OCR'd
- RTF files → parsed and indexed

### Step 2: AI Classification Script

This is a post-consume script that Paperless runs after OCR processing every document.

#### Custom Fields

Create three custom fields in Paperless (the script auto-creates them, but for reference):
- **AI Summary** (type: `longtext`) — searchable summary of the document
- **Ingestion Context** (type: `string`) — metadata from the import channel (Signal caption, email subject, etc.)
- **Expiration Date** (type: `date`) — suggested review/discard date

#### The Script

Create `ai-classify.py` and mount it into your Paperless container at `/usr/src/paperless/scripts/ai-classify.py`.

Set these environment variables on your Paperless container:
```yaml
environment:
  - PAPERLESS_POST_CONSUME_SCRIPT=/usr/src/paperless/scripts/ai-classify.py
  - PAPERLESS_API_TOKEN=<your-paperless-api-token>
  - ANTHROPIC_API_KEY=<your-anthropic-api-key>
```

**Script behavior:**
1. Fetches the document content from Paperless API
2. Checks for an "Ingestion Context" custom field (pre-set by Signal/email importers)
3. Sends the content + context to Claude Haiku with a classification prompt
4. Sets title, correspondent, document type, tags, created date, summary, and expiration date

**Tag taxonomy** (fixed categories — the AI picks exactly one):
```
Category (pick one): tax, insurance, banking, medical, legal, vehicle, housing,
                     utilities, receipt, shipping, warranty, travel, employment,
                     identity, subscription

Status (pick zero or one): action-required, expiring, pending

Auto-added by script: source tag (manual-upload, signal-import, email-import),
                      year tag (e.g. 2026)
```

**Retention guidelines** (for expiration date):
| Document Type | Retention |
|---------------|-----------|
| Shipping/tracking | 90 days |
| Receipts (general) | 1 year |
| Receipts (with warranty) | Warranty end date |
| Pay stubs, utility bills | 1 year |
| Tax, banking, legal, employment | 7 years |
| Insurance | Active + 1 year |
| Subscriptions | End of period + 30 days |
| Medical, identity | No expiration |

**Source detection** — the script reads the Ingestion Context field and auto-tags:
- Starts with `Signal:` → `signal-import`
- Starts with `Email:` → `email-import`
- Empty/missing → `manual-upload`

**Claude prompt structure:**
- System prompt defines the JSON output schema, document type list, tag taxonomy, and retention guidelines
- User message is the OCR'd document text (truncated to 15,000 chars)
- If ingestion context exists, it's appended to the prompt so Claude can use the sender's description

The script uses only Python stdlib (`urllib`, `json`, `logging`) — no pip dependencies needed.

### Step 3: Signal Bot

Two containers working together:

#### signal-cli REST API

[bbernhard/signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) — handles the Signal protocol.

```yaml
services:
  signal-cli:
    image: bbernhard/signal-cli-rest-api:0.98
    container_name: signal-cli
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:8080"   # localhost only
    environment:
      - MODE=normal              # NOT json-rpc (receive endpoint needs REST)
    volumes:
      - ./signal-data:/home/.local/share/signal-cli
    cap_drop:
      - ALL
    cap_add:
      - SETUID                   # Required — signal-cli uses setpriv internally
      - SETGID                   # Required — same reason
    mem_limit: "512m"
```

**Important notes:**
- `MODE=normal` is required. In `json-rpc` mode, the `/v1/receive` endpoint requires WebSocket, but the router uses simple HTTP polling.
- `cap_drop: ALL` + `cap_add: SETUID, SETGID` — signal-cli's entrypoint uses `setpriv` to switch users internally. Without these caps it will crash on startup. Do NOT use `no-new-privileges: true` as it conflicts with `setpriv`.
- Bind to `127.0.0.1` only — the API has no authentication.

#### Signal Registration (One-Time)

After the container is running:

```bash
# 1. Get a CAPTCHA token
#    Go to: https://signalcaptchas.org/registration/generate.html
#    Solve it, right-click "Open Signal", copy link
#    Extract the token (everything after signalcaptcha://)

# 2. Register your phone number
curl -X POST "http://127.0.0.1:8080/v1/register/+1XXXXXXXXXX" \
  -H "Content-Type: application/json" \
  -d '{"use_voice": false, "captcha": "signal-hcaptcha.YOUR_TOKEN_HERE"}'

# 3. Enter the SMS verification code
curl -X POST "http://127.0.0.1:8080/v1/register/+1XXXXXXXXXX/verify/NNNNNN"

# 4. Verify registration
curl "http://127.0.0.1:8080/v1/accounts"
# Should return: ["+1XXXXXXXXXX"]

# 5. Create a group for document ingestion
curl -X POST "http://127.0.0.1:8080/v1/groups/+1XXXXXXXXXX" \
  -H "Content-Type: application/json" \
  -d '{"name": "Paperless Docs", "members": ["+1XXXXXXXXXX"]}'
# Returns: {"id": "group.BASE64_ID"}

# 6. Get the internal group ID (this is what messages use)
curl "http://127.0.0.1:8080/v1/groups/+1XXXXXXXXXX"
# Use the "internal_id" field, NOT the "id" field
# The internal_id is what appears in groupInfo.groupId in received messages

# 7. Add other users to the group
curl -X POST "http://127.0.0.1:8080/v2/send" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello!", "number": "+1XXXXXXXXXX", "recipients": ["+1OTHER_NUMBER"]}'
# Then create a new group including them, or use the Signal app
```

#### signal-router

A Python script that polls signal-cli for messages and routes attachments to Paperless.

```yaml
  signal-router:
    image: python:3.12-alpine
    container_name: signal-router
    restart: unless-stopped
    command: ["python3", "/app/signal-router.py"]
    environment:
      - SIGNAL_API_URL=http://signal-cli:8080
      - SIGNAL_PHONE_NUMBER=+1XXXXXXXXXX
      - SIGNAL_DOCS_GROUP_ID=<internal_id from step 6>
      - PAPERLESS_URL=http://<your-paperless-host>:8000
      - PAPERLESS_API_TOKEN=<your-paperless-api-token>
      - POLL_INTERVAL=15
    volumes:
      - ./signal-router.py:/app/signal-router.py:ro
    depends_on:
      - signal-cli
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: "256m"
```

**Router behavior:**
1. Polls `GET /v1/receive/{phone}?send_read_receipts=true` every N seconds
2. Filters for messages with `dataMessage.groupInfo.groupId` matching the configured group
3. Downloads each attachment via `/v1/attachments/{id}`
4. POSTs to Paperless `post_document` API with multipart form data
5. Sets "Ingestion Context" custom field: `Signal: {caption} [from: {sender}]`

**Key details:**
- Uses `send_read_receipts=true` so messages show as "read" in Signal
- Uses only Python stdlib (no pip install needed — runs on `python:alpine`)
- Gracefully handles empty group ID (logs warning, doesn't route) for pre-registration deploys
- The `/v1/receive` endpoint is destructive — messages are consumed on read

**Custom fields format for `post_document`:**
```python
# CORRECT — object mapping
custom_fields_json = json.dumps({str(context_field_id): ingestion_context})

# WRONG — this format is for PATCH, not post_document
custom_fields_json = json.dumps([{"field": context_field_id, "value": ingestion_context}])
```

### Step 4: Email Ingestion (Cloudflare)

Three Cloudflare components: an Email Routing rule, a Worker, and a Tunnel ingress.

#### Architecture

```
docs@yourdomain.com
  → Cloudflare Email Routing
    → Email Worker (extracts attachments)
      → Cloudflare Tunnel (with Access service token)
        → Paperless API on your server
```

#### Cloudflare Email Routing Rule

Route `docs@yourdomain.com` to a worker. Your domain must be using Cloudflare Email Routing (MX records pointing to Cloudflare). Other addresses can still use catch-all forwarding.

#### Cloudflare Tunnel Ingress

Add a route for Paperless in your tunnel config so the worker can reach it:
```
hostname: paperless.yourdomain.com
service: http://your-paperless-host:8000
```

#### Cloudflare Access (Optional but Recommended)

If you want to protect the tunnel endpoint so only the worker can reach Paperless:
1. Create a **Service Token** (automated, no user login)
2. Create an **Access Application** for `paperless.yourdomain.com` with only the service token policy (no human identity policies)
3. The worker sends `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers

This means `paperless.yourdomain.com` is completely inaccessible to browsers — only the worker's service token can authenticate. Users access Paperless directly on their LAN or via VPN.

If you skip Access, the Paperless login page will be publicly visible (but the API still requires a token).

#### Email Worker

A Cloudflare Worker that receives emails and POSTs attachments to Paperless.

**Environment bindings needed:**
- `PAPERLESS_URL` — `https://paperless.yourdomain.com`
- `PAPERLESS_API_TOKEN` — your Paperless API token
- `CF_ACCESS_CLIENT_ID` — service token client ID (if using Access)
- `CF_ACCESS_CLIENT_SECRET` — service token client secret (if using Access)

**Smart attachment filtering logic:**

The worker doesn't just extract all attachments — it intelligently filters based on MIME structure:

| Content Type | Rule | Why |
|---|---|---|
| PDFs, Office docs, text/csv | Always save | These are always real documents |
| Images with `Content-ID` header | Always skip | Referenced in HTML body = logo/banner/icon |
| Images with `Content-Disposition: inline` | Always skip | Decorative element |
| Images with `Content-Disposition: attachment` | Always save | Sender explicitly attached it |
| `.p7s`, `.vcf`, `.ics` | Always skip | S/MIME signatures, contacts, calendar |
| `text/html` | Always skip | Email body markup |

**Fallback — save as `.eml`:** If no real attachments are found after filtering, the worker saves the entire raw email as a `.eml` file (`message/rfc822`). Paperless's Tika + Gotenberg pipeline handles `.eml` natively — it extracts email headers (Subject, From, Date, To), renders the HTML body via Chromium, and produces a clean PDF preview.

**Important:** This requires Tika + Gotenberg containers in your Paperless stack (see Step 1). Without them, Paperless has no parser for `message/rfc822` and will reject `.eml` files.

**Error handling:** On any processing error, the email is forwarded to a catch-all address so it isn't lost.

**Ingestion context format:** `Email: Subject: {subject} | From: {from} | Body: {first 500 chars of plain text}`

**MIME parsing notes:**
- The worker includes a hand-rolled MIME parser (Cloudflare Workers don't have access to Node.js email libraries)
- It handles `base64` and `quoted-printable` transfer encodings
- Nested multipart messages (e.g., `multipart/mixed` containing `multipart/alternative`) may not be fully parsed — the parser handles the outermost boundary only
- This works well for typical forwarded emails but may miss attachments in deeply nested structures

## Verification

Test each intake channel:

```bash
# Signal: Send a photo to the Signal group, then check:
docker logs signal-router --tail 20
# Look for "Successfully uploaded" messages

# Email: Send an email with a PDF to docs@yourdomain.com, then check:
# Cloudflare dashboard → Workers & Pages → your worker → Logs

# AI classification: Check Paperless container logs for post-consume output
docker logs paperless-webserver --tail 20
# Look for classification script output

# Verify documents in Paperless:
curl -s -H "Authorization: Token <api-token>" \
  "http://localhost:8000/api/documents/?ordering=-added" | python3 -m json.tool | head -50
```

**Full testing checklist:**
- Manual upload → AI Summary, tags (category + `manual-upload` + year), expiration date
- Signal photo with caption → `signal-import` tag, ingestion context shows caption and sender
- Signal photo without caption → same as above but context shows just sender
- Email with PDF attachment → `email-import` tag, PDF extracted and classified
- Email with only logo images → saved as `.eml` (logos filtered out, rendered as PDF)
- Plain forwarded email (no attachments) → saved as `.eml`, clean PDF with email headers and body
- Email with PDF + logo images → only PDF saved (logos filtered out)

## Operations

**Deployment order:**
1. Set up Paperless with API token and Anthropic API key
2. Deploy the AI classification script as a post-consume script
3. Test: Upload a document manually, verify AI classification runs
4. Deploy Signal bot containers (signal-cli + signal-router)
5. Register Signal phone number and create group
6. Test: Send a photo to the Signal group, verify it appears in Paperless with `signal-import` tag
7. Set up Cloudflare tunnel ingress, Access (optional), email routing, and worker
8. Test: Send an email with a PDF to `docs@yourdomain.com`, verify it appears with `email-import` tag

**Monitoring:**
- **Signal router**: `docker logs signal-router --tail 20` — check for poll errors, attachment processing
- **Email worker**: Cloudflare dashboard → Workers & Pages → your worker → Logs
- **AI classification**: `docker logs paperless-webserver --tail 20` — check for post-consume script output
- **Expiring documents**: Search Paperless for the `expiring` tag to find documents needing review

**Rebuilding:**
```bash
# Signal stack
docker compose up -d --build signal-cli signal-router

# Paperless stack (after changing classification script or Tika/Gotenberg)
docker compose up -d --build paperless-webserver gotenberg tika
```

## File Locations

| Logical Name | Path |
|-------------|------|
| Paperless compose additions | Tika + Gotenberg service definitions added to existing Paperless `compose.yaml` |
| AI classification script | `ai-classify.py` → mounted at `/usr/src/paperless/scripts/ai-classify.py` |
| Signal router script | `signal-router.py` → mounted at `/app/signal-router.py` |
| Signal bot compose | Signal services (`signal-cli`, `signal-router`) added to compose stack |
| Signal data volume | `./signal-data/` (Signal protocol state, keys, sessions) |
| Cloudflare Email Worker | Deployed via Cloudflare dashboard or Wrangler CLI |
| Cloudflare Tunnel config | Tunnel ingress rule for `paperless.yourdomain.com` |
