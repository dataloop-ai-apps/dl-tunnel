
# Password-Based Authentication + Multi-User Tunnel

## Objective
Replace JWT-based tunnel authentication with a name/password model.
Allow multiple developers to SSH into the same destination machine
simultaneously through a single registered target tunnel.

JWT is still required by everyone to pass the Dataloop gateway
(`gate.dataloop.ai` sets a `JWT-APP` cookie). The password is used
**only** at the relay level to authorize tunnel access.

---

## Architecture

### Current (broken for multi-user)

```
Target machine                    Relay                        Developer
─────────────                    ─────                        ─────────
1 WS (register) ──────────────── 1:1 pipe ──────────────── 1 WS (connect)
      │                                                         │
 1 TCP to sshd                                           1 TCP listener
```

- Target opens ONE WebSocket + ONE TCP connection to sshd on startup.
- Relay directly pipes register-WS ↔ connect-WS.
- Only one developer can connect. A second developer would corrupt the
  SSH stream.

### New (multi-user)

```
Target machine                    Relay                        Developer A
─────────────                    ─────                        ───────────
control WS (register) ◄────────► Registry                     connect WS ──► pipe ──┐
      │                             │                                                │
      ├── data WS #1 ◄─────────────┼── pipe ◄──────────────── connect WS (A)        │
      ├── data WS #2 ◄─────────────┼── pipe ◄──────────────── connect WS (B)   Developer B
      └── data WS #3 ◄─────────────┼── pipe ◄──────────────── connect WS (C)   Developer C
      │       │       │
  TCP #1  TCP #2  TCP #3
  to sshd to sshd to sshd
```

**Three-channel design:**

1. **Control channel** — target registers with `{"action": "register", ...}`.
   This WS stays open for the lifetime of the tunnel. The relay sends
   `{"type": "open_session", "session_id": "..."}` over it whenever a
   developer connects.

2. **Data channels** — when the target receives `open_session`, it:
   - opens a new TCP connection to local sshd
   - opens a new WebSocket to the relay with
     `{"action": "data", "endpoint": "...", "session_id": "...", "password": "..."}`
   - relay pipes this data-WS ↔ developer's connect-WS

3. **Connect channel** — developer connects with
   `{"action": "connect", ...}`. Relay validates password, generates a
   `session_id`, notifies the target via control channel, then waits for
   the matching data-WS to arrive. Once matched, pipes them together.

---

## Handshake Protocol

### Register (target → relay)

```json
{
  "action": "register",
  "endpoint": "redlab",
  "password": "s3cret-shared-pwd"
}
```

Response: `{"status": "ok", "message": "registered as redlab"}`

The relay stores `endpoint → Registration(control_ws, password_hash)`.
The control WS stays open. The relay may send messages on it:

```json
{"type": "open_session", "session_id": "uuid4"}
```

### Data (target → relay, per session)

```json
{
  "action": "data",
  "endpoint": "redlab",
  "session_id": "uuid4",
  "password": "s3cret-shared-pwd"
}
```

Response: `{"status": "ok", "message": "data channel ready"}`

The relay matches this WS to the pending connect-WS with the same
`session_id` and pipes them together.

### Connect (developer → relay)

```json
{
  "action": "connect",
  "endpoint": "redlab",
  "password": "s3cret-shared-pwd"
}
```

Response: `{"status": "ok", "message": "connected"}`  
(sent after data channel arrives and pipe begins)

---

## Endpoint Key

**Old:** `endpoint_key = f"{identity.sub}:{machine_name}"` — user-isolated.

**New:** `endpoint_key = machine_name` — shared. Anyone with the
password can connect. No user namespace.

This means endpoint names must be unique across the entire relay. First
to register owns the name until they disconnect.

---

## Implementation Details

### 1. `app/auth.py`

```python
import hashlib, secrets

def hash_password(password: str) -> str:
    """SHA-256 hash. Acceptable for ephemeral RAM-only storage."""
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, password_hash: str) -> bool:
    return hashlib.sha256(password.encode()).hexdigest() == password_hash

def generate_password() -> str:
    return secrets.token_urlsafe(24)
```

Keep `validate_token()` — it's still used if you want to log who
connected (optional, not required for tunnel auth).

Remove `endpoint_key()` — the key is now just the machine name string.

### 2. `app/relay.py`

```python
@dataclass
class _PendingSession:
    """A developer is waiting for the target to open a data channel."""
    connect_ws: _WS
    data_ready: asyncio.Event = field(default_factory=asyncio.Event)
    data_ws: _WS | None = None

@dataclass
class _Registration:
    control_ws: _WS
    password_hash: str
    pending: dict[str, _PendingSession] = field(default_factory=dict)
```

**`_register(ws, endpoint, password)`**
- Hash password, store `_Registration(control_ws=ws, password_hash=hash)`.
- Send OK.
- Keep WS open (`await ws.wait_closed()`).
- On close, remove from registry and cancel all pending sessions.

**`_connect(ws, endpoint, password)`**
1. Look up registration by endpoint name.
2. Verify password against stored hash → reject if wrong.
3. Generate `session_id = uuid4()`.
4. Create `_PendingSession(connect_ws=ws)`, store in `reg.pending[session_id]`.
5. Send `{"type": "open_session", "session_id": session_id}` on control WS.
6. Wait for `data_ready` event (timeout 15s).
7. If timeout → send error, clean up.
8. If ready → send OK to developer, pipe `connect_ws ↔ data_ws`.
9. On pipe end, remove session from `reg.pending`.

**`_data(ws, endpoint, session_id, password)`**
1. Look up registration by endpoint name.
2. Verify password against stored hash.
3. Look up `_PendingSession` by `session_id`.
4. Set `pending.data_ws = ws`, fire `pending.data_ready`.
5. (The pipe is driven by `_connect` after the event fires.)
6. Hold this WS open via the pipe (don't return until pipe ends).

**`handle(ws)` dispatch**
- `action == "register"` → `_register`
- `action == "connect"` → `_connect`
- `action == "data"` → `_data`
- anything else → error

### 3. `client/dl_tunnel.py`

**Target side (`cmd_target`)**

```
prompt JWT → resolve relay → prompt/flag password
open control WS → handshake(register, name, password)
loop:
    recv message from control WS
    if message.type == "open_session":
        session_id = message.session_id
        spawn task:
            open TCP to local sshd
            open new WS to relay → handshake(data, name, session_id, password)
            bridge(data_ws, tcp_reader, tcp_writer)
```

The control WS is long-lived. Each `open_session` spawns a new
concurrent task with its own WS + TCP connection.

**Local side (`cmd_local`)** — mostly unchanged:

```
prompt JWT → resolve relay → prompt/flag password
start TCP listener on localhost:port
on each accepted TCP connection:
    open WS to relay → handshake(connect, name, password)
    bridge(ws, tcp_reader, tcp_writer)
```

Each developer SSH session is a separate TCP accept → separate WS →
separate pipe through the relay → separate sshd session on the target.

**CLI changes:**

```
dl-tunnel start target --name redlab [--password s3cret] [--local 127.0.0.1:22]
dl-tunnel start local  --name redlab [--password s3cret] [--port 0]
```

If `--password` is omitted, prompt with `getpass("Tunnel password: ")`.

JWT is still prompted separately (needed for gateway).

### 4. `_handshake()` wire change

Old:
```json
{"action": "register|connect", "endpoint": "name", "token": "jwt"}
```

New:
```json
{"action": "register|connect|data", "endpoint": "name", "password": "pwd"}
```

`session_id` is added only for `action: "data"`:
```json
{"action": "data", "endpoint": "name", "session_id": "uuid", "password": "pwd"}
```

JWT is no longer sent in the handshake. It's only used client-side for
gateway cookie resolution.

---

## Security Notes

- Passwords are hashed (SHA-256) in relay RAM. Cleared on pod restart.
- JWT is still required by all parties to pass the Dataloop gateway.
  Without a valid DL account, you can't reach the relay at all.
- The relay does NOT log or store which user connected (no JWT
  validation at relay level). Add optional JWT audit logging later if
  needed.
- Generic error messages ("authentication failed") to prevent endpoint
  enumeration.
- No brute-force protection in v1. Acceptable for internal use behind
  the DL gateway.

---

## Backward Compatibility

**Breaking change.** Old clients sending `{"token": "..."}` will get an
auth error. This is acceptable — the project is pre-1.0 and internal.

---

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Two targets register same name | Second gets "endpoint already registered" error |
| Target disconnects while sessions active | All active pipes break (SSH sessions drop). Pending sessions get timeout error |
| Target reconnects after disconnect | Re-registers with same name+password. New sessions work. Old sessions already dead |
| Wrong password on connect | Generic "authentication failed" error |
| Wrong password on data channel | Rejected. The pending session times out on the developer side |
| Developer connects, target never opens data channel | 15s timeout, error sent to developer |
| Control WS drops mid-open_session | Pending session times out |
| Multiple developers connect simultaneously | Each gets its own session_id, data channel, and sshd TCP connection. Fully independent |

---

## Implementation Order

1. **`app/auth.py`** — add `hash_password`, `verify_password`,
   `generate_password`. Remove `endpoint_key` (or keep as no-op).
2. **`app/relay.py`** — new `_Registration` / `_PendingSession`
   dataclasses, rewrite `_register`, new `_connect`, new `_data`,
   update `handle` dispatch.
3. **`client/dl_tunnel.py`** — rewrite `cmd_target` to use control
   channel + spawn data channels on `open_session`. Update
   `_handshake`. Add `--password` flag. Update `cmd_local` to send
   password instead of token.
4. **Tests** — update `tests/test_relay.py` and `tests/conftest.py`.
5. **Docs** — update `README.md` and `redlab-instrcutions.md`.

---

## Testing Plan

1. **Unit:** password hashing, verification, generation.
2. **Integration (in-process relay):**
   - Register target with password → OK
   - Connect developer with correct password → session opens → data
     channel arrives → pipe works
   - Connect with wrong password → rejected
   - Two developers connect simultaneously → two independent pipes
   - Target disconnects → pending sessions error out
   - Duplicate registration → rejected
3. **End-to-end (manual):**
   - Target machine runs `dl-tunnel start target --name redlab --password test123`
   - Developer A runs `dl-tunnel start local --name redlab --password test123 --port 2222`
   - Developer B runs `dl-tunnel start local --name redlab --password test123 --port 2223`
   - Both `ssh -p 2222` and `ssh -p 2223` work simultaneously
   - Target process killed → both sessions drop
   - Target restarted → new connections work
