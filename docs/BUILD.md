# Build & Release

How to cut a new `dl-tunnel` release: bump the version, tag the commit,
build the wheel, and attach it to a GitHub Release so users can install
with `uvx --from <wheel-url>`.

---

## Prerequisites

- Push access to `dataloop-ai-apps/dl-tunnel`.
- [`uv`](https://astral.sh/uv) installed (provides `uv build`).
- [GitHub CLI](https://cli.github.com/) authenticated: `gh auth status`.

---

## 1. Bump the version

Update the version in two places to the same value:

- [`pyproject.toml`](../pyproject.toml) — `[project] version = "X.Y.Z"`
- [`app/dataloop.json`](../app/dataloop.json) — `"version": "X.Y.Z"` (if the FaaS service changed)

Commit:

```bash
git add pyproject.toml app/dataloop.json
git commit -m "release: vX.Y.Z"
git push
```

---

## 2. Tag the release

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

The tag name **must** be `vX.Y.Z` to match the wheel URL convention used
in the README.

---

## 3. Build the wheel

From the repo root:

```bash
rm -rf dist
uv build --wheel
```

This produces `dist/dl_tunnel-X.Y.Z-py3-none-any.whl`.

Sanity-check the wheel:

```bash
uvx --from ./dist/dl_tunnel-X.Y.Z-py3-none-any.whl dl-tunnel --help
```

---

## 4. Create the GitHub Release

```bash
gh release create vX.Y.Z dist/dl_tunnel-X.Y.Z-py3-none-any.whl \
    --title "vX.Y.Z" \
    --notes "See CHANGELOG."
```

The wheel is now available at:

```
https://github.com/dataloop-ai-apps/dl-tunnel/releases/download/vX.Y.Z/dl_tunnel-X.Y.Z-py3-none-any.whl
```

---

## 5. Verify install

On any machine with `uv`:

```bash
uvx --from "https://github.com/dataloop-ai-apps/dl-tunnel/releases/download/vX.Y.Z/dl_tunnel-X.Y.Z-py3-none-any.whl" \
    dl-tunnel --help
```

If that prints the help text, the release is good. Update the README if
the example wheel URL still points at an older version.

---

## 6. (Optional) Deploy the relay

Only needed when anything under [`app/`](../app/) changed
([`runner.py`](../app/runner.py), [`relay.py`](../app/relay.py),
[`auth.py`](../app/auth.py), [`Dockerfile`](../app/Dockerfile), or
[`dataloop.json`](../app/dataloop.json)):

```bash
python app/deploy.py
```

The client always discovers the relay by DPK name, so a new client
release does not require a relay redeploy.
