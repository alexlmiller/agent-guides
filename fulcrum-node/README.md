# Private Bitcoin Core + Fulcrum Electrum Server

Build a private, self-hosted Bitcoin full node with a Fulcrum Electrum server.
This guide is written for an AI coding agent operating an Ubuntu host. The
server is reachable only over Tailscale; it does **not** expose Electrum, RPC,
or Fulcrum administration ports to the public internet.

## Acceptance Criteria

1. Bitcoin Core is a fully synchronized, non-pruned mainnet node with
   `txindex=1` and JSON-RPC restricted to loopback.
2. Fulcrum has completed its initial index and serves TLS Electrum on the
   server's Tailscale IP and port `50002`.
3. TCP `50001`, Fulcrum admin, Fulcrum stats, and Bitcoin RPC are not publicly
   reachable.
4. A client on the tailnet can call `server.version` and
   `blockchain.transaction.get` successfully.
5. Restarting either service is safe and state persists on dedicated storage.

## Architecture

```text
Electrum / Sparrow client (tailnet only)
              |
              | TLS Electrum :50002
              v
          Fulcrum
              |
              | loopback JSON-RPC + ZMQ
              v
         Bitcoin Core
              |
              v
        Bitcoin P2P network
```

## Non-Negotiable Requirements

- Use an SSD/NVMe-backed filesystem for both Bitcoin Core and Fulcrum data.
- Do **not** enable pruning. Fulcrum requires a full node with `txindex=1`.
- Keep Bitcoin RPC, Fulcrum `admin`, and Fulcrum `stats` bound to `127.0.0.1`.
- Bind the client-facing Fulcrum listener only to the Tailscale IP. Do not
  create public firewall or port-forwarding rules for it.
- Use a pinned Fulcrum release and verify its published SHA-256 before install.
- Treat the Bitcoin RPC password and TLS private key as secrets: never commit,
  print, or paste them into chat.

Fulcrum's upstream requirements also recommend a current Bitcoin Core release,
`txindex=1`, no pruning, and ZMQ block-hash notifications. Start with the
[upstream README](https://github.com/cculianu/Fulcrum/blob/v2.1.1/README.md)
and its [example configuration](https://github.com/cculianu/Fulcrum/blob/v2.1.1/doc/fulcrum-example-config.conf)
when a newer Fulcrum version is selected.

## Agent Operating Rules

Before making changes, inspect the host and tell the owner:

1. Available SSD capacity, RAM, CPU, and operating system.
2. Whether Bitcoin Core is already installed and fully synchronized.
3. The desired tailnet hostname, Tailscale IPv4 address, and whether a valid
   DNS name/certificate is available for Electrum TLS.
4. Whether this is mainnet, testnet, or signet. Do not mix their data paths.

Stop and ask before deleting any Bitcoin or Fulcrum data directory, changing a
public firewall rule, enabling port forwarding, or replacing an existing wallet
or RPC credential.

## Step 1: Plan Storage and Accounts

Use separate persistent paths, for example:

```text
/srv/bitcoin      Bitcoin Core chainstate, blocks, and indexes
/srv/fulcrum      Fulcrum index database
/etc/bitcoin      Bitcoin configuration and secrets
/etc/fulcrum      Fulcrum configuration and TLS material
```

Create dedicated, non-login service users. The exact UID/GID may be assigned
by the system; do not reuse a person’s account.

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin bitcoind
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin fulcrum
sudo install -d -o bitcoind -g bitcoind -m 0750 /srv/bitcoin /etc/bitcoin
sudo install -d -o fulcrum -g fulcrum -m 0750 /srv/fulcrum /etc/fulcrum
```

Do not estimate storage from an old blog post. Check the current chain size and
leave substantial growth headroom before starting initial block download.

## Step 2: Install and Configure Bitcoin Core

Install a current stable Bitcoin Core release from an official source. Prefer
the official signed release artifacts; verify signatures and/or checksums before
installing. Do not pipe an installer from the internet into a shell.

Create `/etc/bitcoin/bitcoin.conf` with a generated, unique RPC password. Use
the current mainnet defaults unless the owner explicitly selected another
network.

```ini
server=1
daemon=0
txindex=1
prune=0

# RPC: Fulcrum is the only expected consumer.
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=8332
rpcuser=fulcrum
rpcpassword=<generate-and-store-a-long-random-secret>

# Lets Fulcrum learn about new blocks without polling.
zmqpubhashblock=tcp://127.0.0.1:28332

# Optional: run a useful P2P node without exposing management interfaces.
listen=1
port=8333
```

Set the configuration to `0640`, owned by `root:bitcoind`. Fulcrum does not
need to read this file: place the same dedicated RPC credential only in its
own `root:fulcrum` configuration file. Never make `bitcoin.conf`
world-readable.

Create a `bitcoind.service` appropriate for the installed release. It must run
as the `bitcoind` user, use `/srv/bitcoin` as its data directory, restart on
failure, and start before Fulcrum. Enable it, then wait for full sync:

```bash
bitcoin-cli -datadir=/srv/bitcoin getblockchaininfo
bitcoin-cli -datadir=/srv/bitcoin getindexinfo
```

Do not proceed until `initialblockdownload` is `false` and the `txindex` is
fully synchronized. Initial synchronization can take a long time.

## Step 3: Install Fulcrum

Download a pinned Fulcrum release from its official GitHub release page. Record
the version and SHA-256 in the host’s configuration-management repository or an
operator runbook. Verify the digest before installing the `Fulcrum` binary and
`FulcrumAdmin` tool under `/usr/local/bin`.

```bash
# Illustrative flow — substitute a reviewed version, URL, and published digest.
sha256sum Fulcrum-<version>-x86_64-linux.tar.gz
sudo install -o root -g root -m 0755 Fulcrum /usr/local/bin/Fulcrum
sudo install -o root -g root -m 0755 FulcrumAdmin /usr/local/bin/FulcrumAdmin
sudo /usr/local/bin/Fulcrum -h
```

Never replace a working binary until the new download’s checksum has been
verified. Keep the previous binary available until post-upgrade verification is
complete.

## Step 4: TLS and Tailnet Listener

Use a valid TLS certificate for the hostname clients will use. A DNS-01
certificate works well when the service is tailnet-only. A self-signed
certificate is acceptable only for a temporary test when every client is
explicitly configured to trust it.

The private key must be readable by Fulcrum but not by other users:

```bash
sudo chown root:fulcrum /etc/fulcrum/cert.pem /etc/fulcrum/key.pem
sudo chmod 0644 /etc/fulcrum/cert.pem
sudo chmod 0640 /etc/fulcrum/key.pem
```

Discover the server's tailnet address, then use that address—not `0.0.0.0`—in
the client listener:

```bash
tailscale ip -4
```

## Step 5: Configure Fulcrum

Create `/etc/fulcrum/fulcrum.conf` as `root:fulcrum` mode `0640`:

```ini
# Bitcoin Core RPC (loopback only)
bitcoind = 127.0.0.1:8332
rpcuser = fulcrum
rpcpassword = <same-secret-as-bitcoin.conf>

# Storage
datadir = /srv/fulcrum
db_mem = 2048.0

# Client traffic: tailnet only. Use the actual address from `tailscale ip -4`.
ssl = <tailscale-ip>:50002
cert = /etc/fulcrum/cert.pem
key = /etc/fulcrum/key.pem

# Keep operations endpoints local.
admin = 127.0.0.1:8000
stats = 127.0.0.1:8080

# Private server defaults
peering = false
bitcoind_timeout = 30

# Per-client send/receive backlog. Upstream default is 8,000,000 bytes.
# Use 32 MiB when trusted clients legitimately retrieve large histories or
# transaction batches; do not raise it casually on a public service.
max_buffer = 33554432
```

`max_buffer` is per client and is a DoS control. Fulcrum permits values from
64 KB through 100 MB; make the value proportional to RAM and the number of
concurrent trusted clients. The default is usually appropriate for a public
server. Fulcrum's local `FulcrumAdmin maxbuffer` command can inspect or adjust
this setting at runtime, but configuration management should remain the source
of truth.

## Step 6: Run Fulcrum as a Hardened Systemd Service

Create `/etc/systemd/system/fulcrum.service`:

```ini
[Unit]
Description=Fulcrum Electrum Server
After=network-online.target bitcoind.service tailscaled.service
Wants=network-online.target
Requires=bitcoind.service

[Service]
Type=simple
User=fulcrum
Group=fulcrum
ExecStart=/usr/local/bin/Fulcrum /etc/fulcrum/fulcrum.conf
Restart=on-failure
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=300
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/fulcrum

[Install]
WantedBy=multi-user.target
```

Then start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fulcrum.service
sudo systemctl status fulcrum.service --no-pager
```

Fulcrum does not accept client connections until its index is ready. Monitor
the service rather than repeatedly restarting it during initial indexing.

## Step 7: Firewall and Exposure Checks

Allow the TLS Electrum port only on the Tailscale interface. Keep RPC, admin,
and stats loopback-only.

```bash
# Example UFW rule; adapt to the host firewall before applying.
sudo ufw allow in on tailscale0 to any port 50002 proto tcp

# Do not add public rules for 50001, 50002, 8000, 8080, or 8332.
sudo ss -ltnp | rg ':(50001|50002|8000|8080|8332)'
```

If Bitcoin P2P is intentionally public, expose only port `8333` and document
that decision separately. It is not required for Fulcrum clients.

## Verification

Run these checks after initial indexing and after every upgrade:

```bash
# Bitcoin chain and transaction index
bitcoin-cli -datadir=/srv/bitcoin getblockchaininfo
bitcoin-cli -datadir=/srv/bitcoin getindexinfo

# Fulcrum local service and config
sudo systemctl is-active fulcrum.service
sudo grep -E '^(ssl|admin|stats|max_buffer)[[:space:]]*=' /etc/fulcrum/fulcrum.conf
sudo /usr/local/bin/FulcrumAdmin -p 8000 getinfo
```

From a tailnet client, replace the host and certificate policy with the chosen
values. This sends no wallet data:

```bash
printf '%s\n' '{"id":1,"method":"server.version","params":["verify","1.4"]}' \
  | openssl s_client -quiet -connect <tailnet-host>:50002 -servername <tls-hostname>
```

Expect a JSON-RPC `server.version` response. Then test a known ordinary
transaction with `blockchain.transaction.get`. Do not use the genesis coinbase
transaction as a test: Bitcoin Core intentionally does not return it as an
ordinary transaction.

## Operations and Troubleshooting

| Symptom | Check | Likely resolution |
|---|---|---|
| Fulcrum never listens on `50002` | `systemctl status fulcrum`, index logs | Wait for initial Fulcrum indexing and confirm Bitcoin Core is fully synced. |
| `tx not found` / daemon errors | `bitcoin-cli getindexinfo` | Enable `txindex=1`; wait for the index to finish. Do not prune. |
| Client disconnects during a large response | Fulcrum logs and `max_buffer` | Confirm the client is reading normally; for trusted tailnet clients, increase `max_buffer` within RAM limits and restart Fulcrum. |
| TLS warning | Certificate SAN/hostname and client setting | Use a certificate valid for the hostname the client uses; do not broadly disable TLS verification. |
| RPC auth failure | File permissions and configured credentials | Regenerate/rotate the dedicated RPC secret and update both services atomically. |

For a config-only Fulcrum change, validate the file, restart only Fulcrum, and
then run the tailnet `server.version` check. Do not restart Bitcoin Core unless
its configuration changed.

## Backup and Upgrade Policy

- Back up `/srv/fulcrum` before an upgrade or filesystem migration.
- Back up Bitcoin Core configuration and any wallets separately. Do not copy a
  live wallet casually; follow Bitcoin Core’s wallet-backup guidance.
- Upgrade one component at a time: Fulcrum first when Bitcoin Core remains
  supported, then verify; upgrade Bitcoin Core separately after reading its
  release notes.
- Pin versions and checksums in configuration management. Never rely on a
  floating `latest` container tag or release URL.

## Handoff Checklist

Before declaring the node complete, report:

- Bitcoin Core version, block height, sync state, and txindex state
- Fulcrum version and index readiness
- Data paths and free disk space
- Tailnet hostname and TLS listener port (not RPC credentials)
- Firewall rules proving the Electrum listener is tailnet-only
- Output of the tailnet `server.version` check
- Backup location and the tested restore procedure
