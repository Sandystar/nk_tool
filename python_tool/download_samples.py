#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = "vscodev/neokikoeru"
IMAGE = "ghcr.io/vscodev/neokikoeru:latest"
API = "https://api.github.com"
REGISTRY = "https://ghcr.io"
CHUNK = 1 << 20
TIMEOUT = 120
RETRIES = 5
PLATFORMS = ("windows-amd64", "windows-arm64", "linux-amd64", "linux-arm64", "macos-amd64", "macos-arm64")
ASSET_OS = {"windows": "windows", "linux": "linux", "macos": "macos"}
MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)

LOG_PATH = None


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def emit(line):
    text = "%s %s" % (now(), line)
    print(text, flush=True)
    if LOG_PATH is not None:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def warn(line):
    text = "%s WARN %s" % (now(), line)
    print(text, file=sys.stderr, flush=True)
    if LOG_PATH is not None:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def size_text(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(CHUNK), b""):
            h.update(c)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return str(path)


def open_url(url, headers=None, timeout=TIMEOUT, retries=RETRIES):
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=headers or {})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read(2000).decode("utf-8", "replace")
            last = "HTTP %d for %s %s" % (e.code, url, body[:200])
            if e.code not in (403, 408, 425, 429, 500, 502, 503, 504) or attempt >= retries:
                raise RuntimeError(last)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = "request failed for %s: %s" % (url, e)
            if attempt >= retries:
                raise RuntimeError(last)
        time.sleep(min(20.0, 1.5 * (2 ** (attempt - 1))))
    raise RuntimeError(last or ("request failed: %s" % url))


def http_json(url, headers=None, timeout=TIMEOUT):
    with open_url(url, headers=headers, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise RuntimeError("invalid JSON from %s: %s" % (url, e))


def download_file(url, dest, expected_size=None, expected_sha256="", headers=None, timeout=300, progress=True):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    if dest.exists() and expected_size and dest.stat().st_size == expected_size:
        if not expected_sha256 or sha256_file(dest) == expected_sha256:
            emit("skip existing %s" % dest.name)
            return dest

    resume = tmp.stat().st_size if tmp.exists() else 0
    for attempt in range(1, RETRIES + 1):
        hdrs = dict(headers or {})
        if resume:
            hdrs["Range"] = "bytes=%d-" % resume
        try:
            with open_url(url, headers=hdrs, timeout=timeout, retries=1) as r:
                status = getattr(r, "status", 200)
                mode = "ab" if resume and status == 206 else "wb"
                if mode == "wb":
                    resume = 0
                total = expected_size
                if total is None:
                    cl = r.headers.get("Content-Length")
                    total = int(cl) + resume if cl else None
                done = resume
                with open(tmp, mode) as f:
                    while True:
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress and total:
                            pct = 100.0 * done / total
                            print("\r    %-44s %5.1f%%  %s" % (dest.name[:44], pct, size_text(done)), end="", flush=True)
                if progress and total:
                    print("\r" + " " * 76 + "\r", end="", flush=True)
        except Exception as e:
            if attempt >= RETRIES:
                raise
            warn("retry %d/%d after: %s" % (attempt, RETRIES - 1, e))
            time.sleep(min(20.0, 1.5 * (2 ** (attempt - 1))))
            resume = tmp.stat().st_size if tmp.exists() else 0
            continue

        size = tmp.stat().st_size
        if expected_size and size != expected_size:
            tmp.unlink(missing_ok=True)
            if attempt < RETRIES:
                warn("size mismatch (%d != %d), retrying" % (size, expected_size))
                resume = 0
                continue
            raise RuntimeError("size mismatch for %s" % url)
        if expected_sha256 and sha256_file(tmp) != expected_sha256:
            tmp.unlink(missing_ok=True)
            if attempt < RETRIES:
                warn("sha256 mismatch, retrying")
                resume = 0
                continue
            raise RuntimeError("sha256 mismatch for %s" % url)
        os.replace(tmp, dest)
        return dest
    raise RuntimeError("download failed: %s" % url)


def safe_name(name):
    name = name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise RuntimeError("absolute path in archive: %r" % name)
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise RuntimeError("path traversal in archive: %r" % name)
    return "/".join(parts)


def extract_zip(archive, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for zi in zf.infolist():
            rel = safe_name(zi.filename)
            if not rel:
                continue
            out = dest / rel
            if zi.is_dir() or zi.filename.endswith("/"):
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(zi) as src, open(out, "wb") as dst:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)
            os.utime(out, None)


def extract_tar(archive, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tf:
        for m in tf:
            rel = safe_name(m.name)
            if not rel:
                continue
            out = dest / rel
            if m.isdir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            if m.issym():
                target = m.linkname.replace("\\", "/")
                if target.startswith("/") or any(p == ".." for p in target.split("/")):
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                if out.is_symlink() or out.exists():
                    out.unlink()
                os.symlink(m.linkname, out)
                continue
            if not m.isfile():
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(out, "wb") as dst:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)


def github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "neokikoeru-sampler",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def latest_release():
    return http_json("%s/repos/%s/releases/latest" % (API, REPO), github_headers())


def asset_platform(name):
    if not name.startswith("neokikoeru-") or not name.endswith((".tar.gz", ".zip")):
        return None
    middle = name[len("neokikoeru-"):]
    for suffix in (".tar.gz", ".zip"):
        if middle.endswith(suffix):
            middle = middle[: -len(suffix)]
            break
    return middle if middle in PLATFORMS else None


def fetch_release(version_dir, release):
    tag = str(release.get("tag_name") or "")
    if not tag:
        raise RuntimeError("release has no tag_name")
    emit("release tag=%s published=%s assets=%d" % (
        tag, release.get("published_at", ""), len(release.get("assets") or [])))
    root = Path(version_dir) / "release"
    records = []
    progress = len(release.get("assets") or []) <= 1

    def work(asset):
        name = str(asset.get("name"))
        platform = asset_platform(name)
        if platform is None:
            emit("release skip asset=%s reason=unknown-platform" % name)
            return None
        url = str(asset.get("browser_download_url"))
        expected_size = int(asset.get("size") or 0) or None
        digest = str(asset.get("digest") or "")
        expected_sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
        folder = root / platform
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        emit("release download platform=%s asset=%s url=%s size=%s" % (
            platform, name, url, expected_size or "?"))
        download_file(url, target, expected_size, expected_sha, github_headers(), progress=progress)
        actual = sha256_file(target)
        emit("release asset platform=%s asset=%s size=%d sha256=%s" % (
            platform, name, target.stat().st_size, actual))
        if name.endswith(".zip"):
            extract_zip(target, folder)
        else:
            extract_tar(target, folder)
        binary = None
        for candidate in ("neokikoeru.exe", "neokikoeru"):
            p = folder / candidate
            if p.is_file():
                binary = p
                break
        record = {
            "kind": "release",
            "platform": platform,
            "asset": name,
            "url": url,
            "size": target.stat().st_size,
            "sha256": actual,
        }
        if binary is not None:
            record["binary"] = str(binary.relative_to(version_dir)).replace("\\", "/")
            record["binary_size"] = binary.stat().st_size
            record["binary_sha256"] = sha256_file(binary)
            emit("release binary platform=%s path=%s size=%d sha256=%s" % (
                platform, record["binary"], record["binary_size"], record["binary_sha256"]))
        return record

    assets = [a for a in (release.get("assets") or []) if asset_platform(str(a.get("name"))) ]
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(assets) or 1))) as pool:
        futures = [pool.submit(work, a) for a in assets]
        for f in as_completed(futures):
            try:
                rec = f.result()
                if rec:
                    records.append(rec)
            except Exception as e:
                warn("release asset failed: %s" % e)
    records.sort(key=lambda r: r["platform"])
    return tag, records


class Registry:
    def __init__(self, ref):
        self.ref = ref
        registry, repo, reference = self.parse(ref)
        self.registry = registry
        self.repo = repo
        self.reference = reference
        self.base = "https://" + registry
        self.token = ""

    @staticmethod
    def parse(ref):
        digest = ""
        if "@" in ref:
            ref, digest = ref.split("@", 1)
        tag = "latest"
        idx = ref.rfind(":")
        if idx > ref.rfind("/"):
            ref, tag = ref[:idx], ref[idx + 1:]
        parts = ref.split("/")
        if len(parts) == 1:
            registry, repo = "docker.io", "library/" + parts[0]
        elif "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
            registry, repo = parts[0], "/".join(parts[1:])
        else:
            registry, repo = "docker.io", ref
        return registry, repo, digest or tag

    def auth(self):
        if self.token:
            return
        url = "%s/token?scope=repository:%s:pull&service=%s" % (self.base, self.repo, self.registry)
        data = http_json(url, {"User-Agent": "neokikoeru-sampler"}, timeout=60)
        self.token = data.get("token") or data.get("access_token") or ""

    def headers(self, accept=""):
        self.auth()
        h = {"User-Agent": "neokikoeru-sampler"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if accept:
            h["Accept"] = accept
        return h

    def manifest(self, reference):
        url = "%s/v2/%s/manifests/%s" % (self.base, self.repo, urllib.parse.quote(reference, safe=":"))
        with open_url(url, headers=self.headers(MANIFEST_ACCEPT)) as r:
            raw = r.read()
            digest = r.headers.get("Docker-Content-Digest", "")
        return json.loads(raw.decode("utf-8")), digest or ("sha256:" + sha256_bytes(raw))

    def blob_url(self, digest):
        return "%s/v2/%s/blobs/%s" % (self.base, self.repo, digest)

    def blob(self, digest):
        with open_url(self.blob_url(digest), headers=self.headers(), timeout=300) as r:
            data = r.read()
        if sha256_bytes(data) != digest.split(":", 1)[-1]:
            raise RuntimeError("blob checksum failed: %s" % digest)
        return data


TARGET_BINARY = "usr/local/bin/neokikoeru"


def layer_binary(layers):
    found = None
    for index, blob in enumerate(layers):
        try:
            tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:*")
        except tarfile.TarError:
            try:
                tf = tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob)), mode="r:")
            except Exception:
                continue
        with tf:
            for m in tf:
                rel = safe_name(m.name)
                if not rel:
                    continue
                if os.path.basename(rel).startswith(".wh."):
                    victim = rel.rsplit(".wh.", 1)[0]
                    if victim.rstrip("/") == TARGET_BINARY:
                        found = None
                    continue
                if rel != TARGET_BINARY:
                    continue
                if m.isfile():
                    f = tf.extractfile(m)
                    if f is not None:
                        found = (index, f.read())
    return found


def fetch_image(version_dir, ref):
    reg = Registry(ref)
    emit("image ref=%s registry=%s repository=%s reference=%s" % (
        ref, reg.registry, reg.repo, reg.reference))
    index, index_digest = reg.manifest(reg.reference)
    media = index.get("mediaType", "?")
    emit("image index_digest=%s media_type=%s" % (index_digest, media))

    entries = []
    if "manifests" in index:
        for m in index["manifests"]:
            p = m.get("platform") or {}
            if p.get("os") != "linux":
                continue
            if p.get("architecture") not in ("amd64", "arm64"):
                continue
            entries.append((p["architecture"], m["digest"]))
    else:
        entries.append(("amd64", index_digest))
    if not entries:
        raise RuntimeError("image has no linux/amd64 or linux/arm64 manifest")

    root = Path(version_dir) / "docker"
    records = []
    for arch, digest in entries:
        platform = "linux-" + arch
        mf, mf_digest = reg.manifest(digest)
        config_digest = mf["config"]["digest"]
        layers = mf.get("layers", [])
        emit("image platform=%s manifest_digest=%s config_digest=%s layers=%d" % (
            platform, mf_digest, config_digest, len(layers)))
        config = json.loads(reg.blob(config_digest).decode("utf-8"))
        blobs = []
        for layer in layers:
            ld = layer["digest"]
            emit("image layer platform=%s digest=%s size=%d" % (platform, ld, layer.get("size", 0)))
            blobs.append(reg.blob(ld))
        hit = layer_binary(blobs)
        if hit is None:
            warn("image platform=%s has no %s" % (platform, TARGET_BINARY))
            continue
        index_in_layers, data = hit
        folder = root / platform
        folder.mkdir(parents=True, exist_ok=True)
        binary = folder / "neokikoeru"
        binary.write_bytes(data)
        (folder / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "manifest.json").write_text(json.dumps(mf, ensure_ascii=False, indent=2), encoding="utf-8")
        record = {
            "kind": "docker",
            "platform": platform,
            "ref": ref,
            "index_digest": index_digest,
            "manifest_digest": mf_digest,
            "config_digest": config_digest,
            "layers": [l["digest"] for l in layers],
            "binary": str(binary.relative_to(version_dir)).replace("\\", "/"),
            "binary_size": binary.stat().st_size,
            "binary_sha256": sha256_file(binary),
            "layer_with_binary": index_in_layers,
        }
        emit("image binary platform=%s path=%s size=%d sha256=%s" % (
            platform, record["binary"], record["binary_size"], record["binary_sha256"]))
        records.append(record)
    if not records:
        raise RuntimeError("no client binary found inside image")
    return index_digest, records


def write_manifest(version_dir, tag, release_records, image_records, image_ref, index_digest):
    data = {
        "tag": tag,
        "generated_at": now(),
        "repository": REPO,
        "image_ref": image_ref,
        "image_index_digest": index_digest,
        "release": release_records,
        "docker": image_records,
    }
    path = Path(version_dir) / "manifest.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["kind\tplatform\tfile\turl\tsize\tsha256"]
    for r in release_records:
        lines.append("release\t%s\t%s\t%s\t%d\t%s" % (r["platform"], r["asset"], r["url"], r["size"], r["sha256"]))
    for r in image_records:
        lines.append("docker\t%s\t%s\t%s\t%d\t%s" % (
            r["platform"], r["binary"], "%s@%s" % (r["ref"], r["manifest_digest"]),
            r["binary_size"], r["binary_sha256"]))
    (Path(version_dir) / "manifest.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    global LOG_PATH
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    args = ap.parse_args(argv)

    dest = Path(os.path.abspath(args.dest))
    dest.mkdir(parents=True, exist_ok=True)

    release = latest_release()
    tag = str(release.get("tag_name") or "")
    if not tag:
        print("release metadata has no tag_name", file=sys.stderr)
        return 1
    version_dir = dest / tag
    version_dir.mkdir(parents=True, exist_ok=True)
    LOG_PATH = str(version_dir / "download.log")

    emit("dest=%s version=%s" % (dest, tag))
    failures = 0

    release_records = []
    try:
        _, release_records = fetch_release(version_dir, release)
        emit("release done platforms=%d" % len(release_records))
    except Exception as e:
        failures += 1
        warn("release failed: %s" % e)

    image_records = []
    index_digest = ""
    try:
        index_digest, image_records = fetch_image(version_dir, IMAGE)
        emit("image done platforms=%d index_digest=%s" % (len(image_records), index_digest))
    except Exception as e:
        failures += 1
        warn("image failed: %s" % e)

    write_manifest(version_dir, tag, release_records, image_records, IMAGE, index_digest)
    emit("manifest=%s" % (version_dir / "manifest.json"))
    emit("done version_dir=%s failures=%d" % (version_dir, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
