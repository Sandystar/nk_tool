#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the local licence server for every supported platform.

The patcher copies an already-built binary out of ``bin/<platform>/``; this
script is the only place that needs the Go toolchain.

    python build.py                  # all platforms
    python build.py windows-amd64    # one platform
    python build.py --clean          # remove bin/ and exit
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE / "bin"

PLATFORMS = [
    ("windows-amd64", "windows", "amd64", ".exe"),
    ("windows-arm64", "windows", "arm64", ".exe"),
    ("linux-amd64", "linux", "amd64", ""),
    ("linux-arm64", "linux", "arm64", ""),
    ("macos-amd64", "darwin", "amd64", ""),
    ("macos-arm64", "darwin", "arm64", ""),
]


def say(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    print("[!] " + msg, file=sys.stderr, flush=True)
    raise SystemExit(1)


def find_go() -> str:
    for candidate in (os.environ.get("GO", ""), shutil.which("go") or "",
                      r"C:\Program Files\Go\bin\go.exe", "/usr/local/go/bin/go",
                      "/usr/bin/go"):
        if not candidate:
            continue
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    die("Go toolchain not found. Install Go 1.21+ or point GO at the go binary.")


def binary_path(name: str, ext: str) -> Path:
    return BIN / name / ("nplicense" + ext)


def build_one(go: str, name: str, goos: str, goarch: str, ext: str) -> None:
    out = binary_path(name, ext)
    out.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GOOS=goos, GOARCH=goarch, CGO_ENABLED="0", GOFLAGS="")
    cmd = [go, "build", "-trimpath", "-ldflags", "-s -w", "-o", str(out), "."]
    started = time.time()
    result = subprocess.run(cmd, cwd=str(HERE), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout.decode("utf-8", "replace"), file=sys.stderr)
        die("go build failed: %s -> %s/%s" % (name, goos, goarch))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    say("  %-14s %-16s %8.1f KB  %.1fs  sha256 %s" % (
        name, goos + "/" + goarch, out.stat().st_size / 1024.0,
        time.time() - started, digest[:16]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("platforms", nargs="*", help="platform names (default: all)")
    ap.add_argument("--clean", action="store_true", help="remove bin/ and exit")
    args = ap.parse_args(argv)

    if args.clean:
        if BIN.exists():
            shutil.rmtree(BIN)
            say("removed %s" % BIN)
        return 0

    key = HERE / "key_pkcs8.pem"
    if not key.is_file():
        die("missing %s (the server embeds this key with go:embed)" % key)

    wanted = args.platforms or [p[0] for p in PLATFORMS]
    known = {p[0] for p in PLATFORMS}
    unknown = [w for w in wanted if w not in known]
    if unknown:
        die("unknown platform(s): %s (choose from: %s)" % (
            ", ".join(unknown), ", ".join(sorted(known))))

    go = find_go()
    say("go %s" % subprocess.run([go, "version"], capture_output=True, text=True).stdout.strip())
    for name, goos, goarch, ext in PLATFORMS:
        if name in wanted:
            build_one(go, name, goos, goarch, ext)
    say("done; binaries under %s" % BIN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
