# dl-tunnel

Self-hosted WebSocket tunnel that lets **Cursor** (with full AI) reach any
Linux machine through your Dataloop FaaS app — no Microsoft/GitHub relay,
no VPN, Dataloop JWT auth only.

```
Your Laptop (Cursor)             Dataloop FaaS              Linux Target
  Remote-SSH ext                  dl-tunnel relay            sshd :22
  │                               (JWT-gated panel)          │
  └─ ssh dl-mybox ── 127.0.0.1:N ─┤ ◄────── WSS register ────┘
                                  │
                                  └────── WSS connect ───────┐
                                                             │
       dl-tunnel start local  ──────────────────────────  dl-tunnel start target
```

- **No persisted credentials.** Token is prompted, used, and forgotten.
- **No install step on either side.** Each role is one `uvx` command.
- **Tunnel dies with the JWT.** When the token expires (~24 h) the tunnel
  closes and the user must restart.

---

## Prerequisites

| Where | What |
|---|---|
| Dataloop org | a deployed `dl-tunnel` FaaS service (see [Deploy the relay](#deploy-the-relay)) |
| Linux target | running `sshd` on `127.0.0.1:22`, [uv](https://astral.sh/uv) installed |
| Your laptop | Cursor installed, [uv](https://astral.sh/uv) installed |

The `<wheel>` placeholder below is the dl-tunnel release wheel URL, e.g.
`https://github.com/dataloop-ai-apps/dl-tunnel/releases/download/v0.1.0/dl_tunnel-0.1.0-py3-none-any.whl`,
or just `dl-tunnel` once it's on PyPI.

---

## On the target (Linux machine)

```bash
uvx --from <wheel> dl-tunnel start target --name mybox
```

Prompts for a Dataloop JWT, registers the machine, and forwards relay
traffic to local `sshd`. Keep the process running for the session.

To forward to a different local port:

```bash
uvx --from <wheel> dl-tunnel start target --name mybox --local 127.0.0.1:2222
```

---

## On your laptop

```bash
uvx --from <wheel> dl-tunnel start local --name mybox
```

Prompts for a Dataloop JWT, then prints something like:

```
listening on 127.0.0.1:54321
```

Add a stable, token-free entry to `~/.ssh/config`:

```sshconfig
Host dl-mybox
    HostName 127.0.0.1
    Port 54321
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
```

To pin a specific port:

```bash
uvx --from <wheel> dl-tunnel start local --name mybox --port 2222
```

---

## Connect

```bash
ssh dl-mybox
```

Or in Cursor: `Ctrl+Shift+P → Remote-SSH: Connect to Host → dl-mybox`.

The first connect downloads `vscode-server` to the target (~30 s); after
that it's instant until the JWT expires.

---

## Getting a Dataloop token

```bash
python -c "import dtlpy as dl; dl.setenv('prod'); dl.login(); print(dl.client_api.token)"
```

Paste the printed JWT into the prompt. Both sides need a token; they can
be different users — endpoint keys are namespaced by JWT `sub`.

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
    dl_tunnel.py       CLI: start target | start local
    requirements.txt
  app/                 FaaS relay (deployed once per Dataloop org)
    runner.py          FaaS entrypoint (asyncio WebSocket server)
    relay.py           endpoint registry + bidirectional pipe
    auth.py            local JWT decode + claim validation
    deploy.py          one-shot deploy helper
    dataloop.json      FaaS service manifest
    Dockerfile         relay image
    requirements.txt
  tests/
  docs/
```

---

## Security notes

- JWT validation on the relay checks `exp` and the presence of `email`/`sub`.
  Signature verification is intentionally off — the gateway enforces auth
  at the panel boundary.
- Endpoint key = `sub:machine_name`, so cross-user access is impossible.
- Tokens are never written to disk by the client. Bash history can still
  see `--name`, but never the JWT.
- The client tears the tunnel down at the JWT `exp`; an idle session
  can't outlive the credential.
- SSH provides a second auth layer on top of WSS; use key-based auth
  and disable password auth on the target (`PasswordAuthentication no`).
