# license_server

Local licence responder used by the patched NeoKikoeru client.

The patched binary talks to `http://127.0.0.1:<license_port>` instead of the
vendor API. This Go program answers that endpoint with a gob-encoded
`ActivationWithToken` carrying the PKCS#8 private key that matches the SPKI
written into the client by `patch_samples.py`, so the client believes it was
activated with the configured licence key.

## Files

| file | role |
|---|---|
| `main.go` | the responder (stdlib only, no third-party modules) |
| `go.mod` | module definition |
| `key_pkcs8.pem` | private key, embedded with `go:embed`; must match `spki.der` |
| `spki.der` | SubjectPublicKeyInfo (294 B) that the patcher writes into NP4/NP5 |
| `key.json` | fingerprints of the key pair (for verification only) |
| `build.py` | cross-compiles every platform into `bin/<platform>/nplicense[.exe]` |
| `bin/` | prebuilt binaries consumed by `python_tool/patch_samples.py` |

## Build

```powershell
python .\build.py                # all six platforms
python .\build.py windows-amd64  # just one
python .\build.py --clean        # drop bin/
```

Requires Go 1.21+. `patch_samples.py` never invokes Go: it copies the matching
binary out of `bin/<platform>/` and fails with a clear message when it is
missing.

## Protocol

```text
POST /license/{activate,verify,deactivate}
request : {"params":"<b64>","enc_key":"<b64>","sign":"<hex>","ts":<unix>}
reply   : 200 gob(ActivationWithToken{Token: <PKCS#8 PEM>, ...})
```

Both the v3.28 field name (`Secret`) and the v3.32 name (`Token`) are populated,
so a single binary serves both protocol generations. `/health` returns `ok`.

## Flags

| flag | meaning |
|---|---|
| `-addr` | listen address, must equal the endpoint patched into the client |
| `-key` | external private key PEM (default: `private_key.pem` next to the exe, else embedded) |
| `-log` | also append every request line to this file |
| `-quiet` | suppress the per-request log lines |
| `-watch-stdin` | exit when stdin reaches EOF (start scripts use this for lifetime binding) |

## Rotating the key pair

1. Put the new private key at `key_pkcs8.pem` (PKCS#8 or PKCS#1 PEM).
2. Regenerate `spki.der` from it, then rebuild:
   `python -c "from cryptography.hazmat.primitives import serialization as s; ..."`
   (or any tool that emits a DER SubjectPublicKeyInfo).
3. `python build.py` — the patcher picks the new SPKI up on the next run.
