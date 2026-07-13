# Agent Guides

Setup guides for self-hosted infrastructure, designed to be executed by AI coding agents.

Each guide is a complete, standalone walkthrough that an agent (Claude, Codex, Gemini, etc.) can follow end-to-end. Feed the guide to your agent and let it handle the implementation — you approve the key decisions and provide credentials.

## Guides

| Guide | What it builds |
|-------|---------------|
| [contact-sync](./contact-sync/) | Google + iCloud contact sync with fuzzy dedup via Radicale + vdirsyncer |
| [drive-sync](./drive-sync/) | Google Drive backup to local storage or Seafile via rclone |
| [gmail-sync](./gmail-sync/) | Gmail IMAP backup to local Maildir via offlineimap3 |
| [paperless-ingestion](./paperless-ingestion/) | Paperless-ngx with Signal, email, and AI-powered document classification |
| [fulcrum-node](./fulcrum-node/) | Private Bitcoin Core + Fulcrum Electrum server over Tailscale |

## How to use

1. Pick a guide, open the folder's `README.md`
2. Paste it into your agent's context (or point the agent at the file)
3. The agent walks through setup — you provide credentials and approve infrastructure decisions
4. Each guide produces a Docker Compose stack you own and control

## Prerequisites

- Docker and Docker Compose
- A GCP project with OAuth credentials (for Google-connected guides)
- Basic familiarity with DNS, reverse proxies, and self-hosting concepts

## Structure

Each guide folder contains:
- `README.md` — the full setup walkthrough
- Supporting files (scripts, configs) where applicable

## Contributing

New guides welcome. Follow the existing pattern:
- Self-contained — no external dependencies beyond Docker
- Written for agents — clear steps, explicit configs, no ambiguity
- Include a "What This Builds" section with an architecture diagram
- Test the guide by running it through an agent on a fresh environment
