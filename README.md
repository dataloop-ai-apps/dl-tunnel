# dl-tunnel

Self-hosted WebSocket tunnel that lets **Cursor** (with full AI) reach any
Linux machine through your Dataloop FaaS app — no Microsoft/GitHub relay,
no VPN.

```
Your Laptop                      Dataloop FaaS              Linux Target
  Cursor / SSH client             dl-tunnel relay            sshd :22
  │                               (JWT-gated panel)          │
  │                               │                          │
  dl-tunnel start local ─WSS──────┤◄─────WSS register───── dl-tunnel start target
                                  │                          │
  ssh 127.0.0.1:N ──────TCP───┐   │                          │
                              └──►├──WSS connect────────────►├──TCP──► sshd
                                  │  (per session)           │
```

Three WebSocket channels per endpoint:
- **register** — target opens a long-lived control channel
- **connect** — developer opens a session channel (one per SSH connection)
- **data** — target opens a data channel matched by session ID

Authentication:
- **Dataloop JWT** — used to resolve the relay URL (gateway enforces org access)
- **Shared password** — used on the tunnel itself (hashed with SHA-256)

Multi-user: multiple developers can connect to the same target simultaneously.

---

## Prerequisites

| Where | What |
|---|---|
| Dataloop org | a deployed `dl-tunnel` FaaS service (see [Deploy the relay](#deploy-the-relay)) |
| Linux target | running `sshd` on `127.0.0.1:22`, [uv](https://astral.sh/uv) installed |
| Your laptop | [uv](https://astral.sh/uv) or `pip` installed |

The `<wheel>` placeholder below is the release wheel URL, e.g.
`https://github.com/dataloop-ai-apps/dl-tunnel/releases/download/v0.1.3/dl_tunnel-0.1.3-py3-none-any.whl`.

---

## On the target (Linux machine)

```bash
uvx --from <wheel> dl-tunnel start target \
  --name mybox \
  --password <shared-password>
```

Prompts for a Dataloop JWT (or pass `--token <jwt>` for headless use),
registers the machine, and forwards relay traffic to local `sshd`.
The process auto-reconnects if the relay connection drops.

To forward to a different local port:

```bash
dl-tunnel start target --name mybox --password <pw> --local 127.0.0.1:2222
```

---

## On your laptop

### Option A: `connect` (recommended)

Starts the tunnel and SSH in one command with auto-reconnect:

```bash
dl-tunnel connect \
  --name mybox \
  --password <shared-password> \
  --ssh-target <user> \
  --forward 443:remote-host:443
```

### Option B: `start local` (manual SSH)

```bash
dl-tunnel start local --name mybox --password <pw> --port 2222
```

Then SSH separately:

```bash
ssh -p 2222 user@127.0.0.1
```

---

## Getting a Dataloop token

If you have `dtlpy` installed and have run `dl.login()` once, the client
auto-uses the stored credentials — no prompt needed.

Otherwise, pass a JWT explicitly:

```bash
dl-tunnel start target --name mybox --password <pw> --token <jwt>
```

To get a JWT manually:

```bash
python -c "import dtlpy as dl; dl.setenv('prod'); dl.login(); print(dl.token())"
```

---

## Deploy the relay

Once per Dataloop org:

```bash
python app/deploy.py
```

The DPK is named `dl-tunnel`; the client looks the app up by that name and
reads the gate panel route automatically.

---

## Layout

```
dl-tunnel/
  pyproject.toml       client wheel definition (entry point: dl-tunnel)
  README.md
  client/              installed on user laptops and SSH targets
    dl_tunnel.py       CLI: start target | start local | connect
    requirements.txt
  app/                 FaaS relay (deployed once per Dataloop org)
    runner.py          FaaS entrypoint (asyncio WebSocket server)
    relay.py           three-channel relay + password auth
    auth.py            password hash/verify utilities
    deploy.py          one-shot deploy helper
    dataloop.json      FaaS service manifest
    Dockerfile         relay image
    requirements.txt
  tests/
  docs/
```

---

## Security notes

- The Dataloop gateway enforces org-level auth at the panel boundary.
  The JWT is only used client-side to resolve the relay URL.
- Tunnel authentication uses a shared password hashed with SHA-256
  and compared with timing-safe comparison.
- Endpoint key = machine name. Access requires knowing both the
  name and the password.
- Passwords and sessions are stored in relay RAM only (ephemeral).
- The client tears the tunnel down at the JWT `exp`; an idle session
  can't outlive the credential.
- SSH provides a second auth layer on top of WSS; use key-based auth
  and disable password auth on the target (`PasswordAuthentication no`).
