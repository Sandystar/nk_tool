#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import hashlib
import os
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

def _supports_unicode() -> bool:
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in enc

def nk_say(msg: str = "") -> None:
    print(msg, flush=True)

def nk_info(msg: str) -> None:
    print("    " + msg, flush=True)

def nk_ok(msg: str) -> None:
    print("[OK] " + msg, flush=True)

def nk_warn(msg: str) -> None:
    print("[!] " + msg, file=sys.stderr, flush=True)

def nk_die(msg: str, code: int = 1) -> "None":
    nk_warn(msg)
    raise SystemExit(code)

def nk_banner(title: str) -> None:
    print("\n" + "=" * 78, flush=True)
    print(title, flush=True)
    print("=" * 78, flush=True)

def nk_human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f PB" % n

def nk_elapsed(t0: float) -> str:
    s = time.time() - t0
    if s < 60:
        return "%.1fs" % s
    return "%dm%02ds" % (s // 60, s % 60)

def nk_sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def nk_sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def nk_ensure_dir(path: str | os.PathLike) -> str:
    os.makedirs(path, exist_ok=True)
    return str(path)

def nk_rmtree(path: str | os.PathLike) -> None:
    
    import stat

    def onerror(func, p, _exc):  
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=onerror)

def nk_atomic_write(path: str | os.PathLike, data: bytes, mode: int | None = None) -> None:
    
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if mode is not None:
            os.chmod(path, mode)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

def _safe_relpath(name: str) -> str:
    
    name = name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ValueError("archive contains an absolute path: %r" % name)
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("archive contains a path traversal: %r" % name)
    return "/".join(parts)

def nk_extract_zip(archive: str | os.PathLike, dest: str | os.PathLike) -> list[str]:
    
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        for zi in zf.infolist():
            rel = _safe_relpath(zi.filename)
            if not rel:
                continue
            out = dest / rel
            if zi.is_dir() or zi.filename.endswith("/"):
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(zi) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            mode = (zi.external_attr >> 16) & 0o777
            if mode:
                os.chmod(out, mode)
            written.append(rel)
    return written

def nk_extract_tar(archive: str | os.PathLike, dest: str | os.PathLike) -> list[str]:
    
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with tarfile.open(archive, "r:*") as tf:
        for m in tf:
            rel = _safe_relpath(m.name)
            if not rel:
                continue
            out = dest / rel
            if m.isdir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            if m.issym():
                target = m.linkname.replace("\\", "/")
                if target.startswith("/") or any(p == ".." for p in target.split("/")):
                    nk_warn("skipping out-of-tree symlink: %s -> %s" % (m.name, m.linkname))
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                if out.exists() or out.is_symlink():
                    out.unlink()
                os.symlink(m.linkname, out)
                written.append(rel)
                continue
            if m.islnk():
                
                src = dest / _safe_relpath(m.linkname)
                out.parent.mkdir(parents=True, exist_ok=True)
                if src.exists():
                    shutil.copy2(src, out)
                    written.append(rel)
                continue
            if not m.isfile():
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            os.chmod(out, m.mode & 0o777)
            written.append(rel)
    return written

class nk_HttpError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

def _is_retryable(status: int | None) -> bool:
    return status is None or status in (403, 408, 425, 429, 500, 502, 503, 504)

def nk_open_url(url: str, headers: dict[str, str] | None = None, timeout: int = 60,
             retries: int = 4) -> urllib.response.addinfourl:
    
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=headers or {})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read(2000).decode("utf-8", "replace")
            last = nk_HttpError("HTTP %d: %s%s" % (e.code, url, (" - " + body[:200]) if body else ""),
                             e.code)
            if not _is_retryable(e.code) or attempt >= retries:
                raise last from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = nk_HttpError("request failed: %s (%s)" % (url, e))
            if attempt >= retries:
                raise last from e
        delay = min(20.0, 1.5 * (2 ** (attempt - 1)))
        nk_warn("retry %d/%d (%s): continuing in %.1fs" % (attempt, retries - 1, last, delay))
        time.sleep(delay)
    raise last if last else nk_HttpError("request failed: %s" % url)

def nk_http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 60,
              retries: int = 4) -> dict:
    import json
    with nk_open_url(url, headers=headers, timeout=timeout, retries=retries) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:  
        raise nk_HttpError("server did not return JSON: %s (%s)" % (url, raw[:160])) from e

def nk_download_file(url: str, dest: str | os.PathLike, headers: dict[str, str] | None = None,
                  expected_size: int | None = None, expected_sha256: str = "",
                  timeout: int = 120, retries: int = 4, quiet: bool = False) -> str:
    
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")

    if dest.exists() and expected_size is not None and dest.stat().st_size == expected_size:
        if not expected_sha256 or nk_sha256_file(dest) == expected_sha256:
            if not quiet:
                nk_info("already present, skipping: %s" % dest.name)
            return str(dest)

    resume = tmp.stat().st_size if tmp.exists() else 0
    if expected_size is not None and resume >= expected_size:
        resume = 0
        tmp.unlink()

    for attempt in range(1, retries + 1):
        hdrs = dict(headers or {})
        if resume:
            hdrs["Range"] = "bytes=%d-" % resume
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                mode = "ab" if resume and r.status == 206 else "wb"
                if mode == "wb":
                    resume = 0
                total = expected_size
                if total is None:
                    cl = r.headers.get("Content-Length")
                    total = (int(cl) + resume) if cl else None
                t0 = time.time()
                done = resume
                last_report = 0.0
                with open(tmp, mode) as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if not quiet and (time.time() - last_report > 0.5 or done == total):
                            speed = done / max(0.01, time.time() - t0)
                            pct = ("%.1f%%" % (100.0 * done / total)) if total else "?"
                            print("\r    %-42s %8s / %-8s %6s  %s/s" % (
                                dest.name[:42], nk_human_size(done),
                                nk_human_size(total) if total else "?", pct,
                                nk_human_size(speed)), end="", flush=True)
                            last_report = time.time()
                if not quiet:
                    print("\r" + " " * 110 + "\r", end="", flush=True)
        except urllib.error.HTTPError as e:
            if e.code == 416 and expected_size and resume == expected_size:
                pass
            elif _is_retryable(e.code) and attempt < retries:
                nk_warn("download interrupted (HTTP %d), retrying in %.1fs" % (e.code, min(20, 1.5 * 2 ** attempt)))
                time.sleep(min(20, 1.5 * 2 ** attempt))
                resume = tmp.stat().st_size if tmp.exists() else 0
                continue
            else:
                raise nk_HttpError("download failed (HTTP %d): %s" % (e.code, url), e.code) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                nk_warn("download interrupted (%s), retrying in %.1fs" % (e, min(20, 1.5 * 2 ** attempt)))
                time.sleep(min(20, 1.5 * 2 ** attempt))
                resume = tmp.stat().st_size if tmp.exists() else 0
                continue
            raise nk_HttpError("download failed: %s (%s)" % (url, e)) from e

        size = tmp.stat().st_size
        if expected_size is not None and size != expected_size:
            tmp.unlink(missing_ok=True)
            if attempt < retries:
                nk_warn("size mismatch (%d != %d), restarting" % (size, expected_size))
                resume = 0
                continue
            raise nk_HttpError("downloaded size mismatch: %s (%d != %d)" % (url, size, expected_size))
        if expected_sha256:
            got = nk_sha256_file(tmp)
            if got.lower() != expected_sha256.lower():
                tmp.unlink(missing_ok=True)
                if attempt < retries:
                    nk_warn("SHA-256 mismatch, restarting")
                    resume = 0
                    continue
                raise nk_HttpError("downloaded SHA-256 mismatch: %s" % url)
        os.replace(tmp, dest)
        return str(dest)

    raise nk_HttpError("download failed: %s" % url)

import re
from dataclasses import dataclass

@dataclass(frozen=True)
class Platform:
    name: str          
    os: str            
    goos: str          
    arch: str          
    fmt: str           
    ext: str           
    binary: str        
    asset_ext: str     

    @property
    def docker(self) -> str:
        return "linux/" + self.arch

PLATFORMS: tuple[Platform, ...] = (
    Platform("windows-amd64", "windows", "windows", "amd64", "PE", ".exe", "neokikoeru.exe", ".zip"),
    Platform("windows-arm64", "windows", "windows", "arm64", "PE", ".exe", "neokikoeru.exe", ".zip"),
    Platform("linux-amd64", "linux", "linux", "amd64", "ELF", "", "neokikoeru", ".tar.gz"),
    Platform("linux-arm64", "linux", "linux", "arm64", "ELF", "", "neokikoeru", ".tar.gz"),
    Platform("macos-amd64", "macos", "darwin", "amd64", "Mach-O", "", "neokikoeru", ".tar.gz"),
    Platform("macos-arm64", "macos", "darwin", "arm64", "Mach-O", "", "neokikoeru", ".tar.gz"),
)

BY_NAME = {p.name: p for p in PLATFORMS}
BY_ASSET = {"%s-%s" % (p.os, p.arch): p for p in PLATFORMS}
BY_TRIPLE = {(p.fmt, "x86-64" if p.arch == "amd64" else "arm64"): p for p in PLATFORMS}

ASSET_RE = re.compile(r"^neokikoeru-(?P<os>linux|macos|windows)-(?P<arch>amd64|arm64)"
                      r"(?P<ext>\.tar\.gz|\.zip)$")

def asset_name(p: Platform) -> str:
    return "neokikoeru-%s-%s%s" % (p.os, p.arch, p.asset_ext)

def parse_asset(name: str) -> Platform | None:
    m = ASSET_RE.match(name)
    if not m:
        return None
    return BY_ASSET.get("%s-%s" % (m.group("os"), m.group("arch")))

def parse_platforms(values: list[str] | None) -> list[Platform]:
    
    if not values:
        return list(PLATFORMS)
    out: list[Platform] = []
    for raw in values:
        for v in raw.split(","):
            v = v.strip()
            if not v:
                continue
            if v in ("all", "*"):
                return list(PLATFORMS)
            if v not in BY_NAME:
                raise ValueError("unknown platform %s (choose from: %s)" % (v, ", ".join(BY_NAME)))
            p = BY_NAME[v]
            if p not in out:
                out.append(p)
    return out

def platform_for_container(fmt: str, arch: str) -> Platform | None:
    return BY_TRIPLE.get((fmt, arch))

def normalize_version(tag: str) -> str:
    
    tag = tag.strip()
    if not tag:
        return tag
    if tag.lower() in ("latest", "newest", "stable"):
        return "latest"
    if tag[:1] in ("v", "V"):
        return "v" + tag[1:]
    return "v" + tag

import json
import os
import tempfile
from pathlib import Path

CONFIG_NAME = "patch_samples_config.json"

DEFAULT_CONFIG = {
    "license_port": 18443,
    "client_port": 5233,
    "license_key": "Hello-World",
    "license_server_dir": "",
    "output_suffix": "_patched",
    "copy_license_server": True,
    "emit_start_script": True,
    "start_script_name": "start",
    "license_server_log": "nplicense.log",
    "client_args": ["serve"],
    "work_dir": "",
}

INT_KEYS = ("license_port", "client_port")
BOOL_KEYS = ("copy_license_server", "emit_start_script")
STR_KEYS = ("license_key", "license_server_dir", "output_suffix",
            "start_script_name", "license_server_log", "work_dir")

def project_root() -> Path:
    
    here = Path(__file__).resolve().parent
    for start in (here, Path.cwd().resolve()):
        for candidate in [start, *start.parents]:
            if (candidate / "license_server").is_dir():
                return candidate
    return here.parent

def config_path() -> Path:
    here = Path(__file__).resolve().parent
    roots = [(here / CONFIG_NAME), (project_root() / "python_tool" / CONFIG_NAME),
             (Path.cwd() / CONFIG_NAME)]
    for probe in roots:
        if probe.is_file():
            return probe
    for candidate in [here, *here.parents, Path.cwd(), *Path.cwd().parents]:
        probe = candidate / CONFIG_NAME
        if probe.is_file():
            return probe
    return here / CONFIG_NAME

def load_config() -> dict:
    path = config_path()
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  
            raise SystemExit("[!] invalid %s: %s" % (path, exc))
    config = dict(DEFAULT_CONFIG)
    config.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    for key in INT_KEYS:
        try:
            config[key] = int(config[key])
        except Exception:  
            raise SystemExit("[!] %s must be an integer (%s)" % (key, path))
        if not 1 <= config[key] <= 65535:
            raise SystemExit("[!] %s out of range 1..65535 (%s)" % (key, path))
    for key in BOOL_KEYS:
        config[key] = bool(config[key])
    for key in STR_KEYS:
        config[key] = str(config[key])
    args = config.get("client_args") or []
    if not isinstance(args, list):
        raise SystemExit("[!] client_args must be a list (%s)" % path)
    config["client_args"] = [str(a) for a in args]
    config["_path"] = str(path)
    return config

def resolve_dir(value: str) -> Path:
    
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    path_file = config_path()
    base = path_file.parent if path_file.is_file() else project_root()
    if not value:
        return (project_root() / "license_server").resolve()
    return (base / path).resolve()

def license_endpoint(config: dict) -> str:
    return "http://127.0.0.1:%d" % config["license_port"]

def license_key(config: dict) -> str:
    return config["license_key"]

def license_server_dir(config: dict) -> Path:
    return resolve_dir(config["license_server_dir"])

def spki_file(config: dict) -> Path:
    return license_server_dir(config) / "spki.der"

def licserver_file(config: dict, platform) -> Path:
    return license_server_dir(config) / "bin" / platform.name / ("nplicense" + platform.ext)

def work_dir(config: dict) -> Path:
    value = (config.get("work_dir") or ".work").strip() or ".work"
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.mkdir(parents=True, exist_ok=True)
    return path

def require_spki(config: dict) -> bytes:
    path = spki_file(config)
    if not path.is_file():
        raise SystemExit("[!] missing %s (run license_server/build.py or fix "
                         "license_server_dir)" % path)
    return path.read_bytes()

def require_licserver(config: dict, platform) -> Path:
    path = licserver_file(config, platform)
    if not path.is_file():
        raise SystemExit("[!] missing %s for %s (run: python license_server/build.py)" % (
            path, platform.name))
    return path

import re
import struct
from dataclasses import dataclass

_PRINTABLE = re.compile(rb"^[\x20-\x7e\r\n\t]+$")

class ContainerError(Exception):
    pass

@dataclass
class Section:
    name: str
    va: int          
    fo: int          
    size: int        
    raw_size: int = 0

    def __repr__(self) -> str:
        return "Section(%s va=0x%X fo=0x%X size=0x%X)" % (self.name, self.va, self.fo, self.size)

class Container:
    

    PE, ELF, MACHO = "PE", "ELF", "Mach-O"

    def __init__(self, data: bytes, path: str = "") -> None:
        self.data = data
        self.path = path
        self.fmt = ""
        self.arch = ""
        self.bits = 0
        self.image_base = 0
        self.sections: list[Section] = []
        self.has_code_signature = False
        self.notes: list[str] = []
        self._detect()
        if not self.sections:
            raise ContainerError("no sections parsed, not an executable: %s" % path)
        t = self._pick_text()
        if t is None:
            raise ContainerError("code section not found (.text / __TEXT,__text): %s" % path)
        self.text = t

    
    def _detect(self) -> None:
        d = self.data
        if d[:2] == b"MZ":
            self._parse_pe()
        elif d[:4] == b"\x7fELF":
            self._parse_elf()
        elif d[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
            self._parse_macho()
        elif d[:4] == b"\xca\xfe\xba\xbe":
            raise ContainerError(
                "this is a macOS universal (fat) binary; split it into a single arch first: "
                "  lipo -thin arm64 in -output out")
        else:
            raise ContainerError("unknown executable format (magic=%s)" % d[:4].hex())

    def _parse_pe(self) -> None:
        d = self.data
        self.fmt = self.PE
        e = struct.unpack_from("<I", d, 0x3C)[0]
        if d[e:e + 4] != b"PE\0\0":
            raise ContainerError("bad PE signature")
        machine, nsec = struct.unpack_from("<HH", d, e + 4)
        opt_size = struct.unpack_from("<H", d, e + 20)[0]
        magic = struct.unpack_from("<H", d, e + 24)[0]
        self.bits = 64 if magic == 0x20B else 32
        self.arch = {0x8664: "x86-64", 0x14C: "x86", 0xAA64: "arm64", 0x1C0: "arm"}.get(
            machine, "0x%X" % machine)
        self.image_base = struct.unpack_from("<Q", d, e + 24 + 24)[0] if self.bits == 64 else \
            struct.unpack_from("<I", d, e + 24 + 28)[0]
        off = e + 24 + opt_size
        for i in range(nsec):
            p = off + 40 * i
            name = d[p:p + 8].rstrip(b"\0").decode("latin1")
            vsize, va, rsize, rptr = struct.unpack_from("<IIII", d, p + 8)
            self.sections.append(Section(name, self.image_base + va if va else 0,
                                         rptr, max(vsize, rsize), rsize))

    def _parse_elf(self) -> None:
        d = self.data
        self.fmt = self.ELF
        self.bits = 64 if d[4] == 2 else 32
        machine = struct.unpack_from("<H", d, 0x12)[0]
        self.arch = {0x3E: "x86-64", 0x03: "x86", 0xB7: "arm64", 0x28: "arm"}.get(
            machine, "0x%X" % machine)
        et = struct.unpack_from("<H", d, 0x10)[0]
        if et == 3:
            self.notes.append("ELF type ET_DYN (PIE)")
        if self.bits != 64:
            raise ContainerError("only 64-bit ELF is supported")
        shoff = struct.unpack_from("<Q", d, 0x28)[0]
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", d, 0x3A)
        raw = []
        for i in range(shnum):
            p = shoff + i * shentsize
            nm, typ = struct.unpack_from("<II", d, p)
            flags = struct.unpack_from("<Q", d, p + 8)[0]
            addr, offset, size = struct.unpack_from("<QQQ", d, p + 16)
            raw.append((nm, typ, flags, addr, offset, size))
        if not raw or shstrndx >= len(raw):
            raise ContainerError("ELF section table unavailable")
        stroff = raw[shstrndx][4]
        for nm, typ, flags, addr, offset, size in raw:
            if typ == 8 or size == 0:          
                continue
            q = stroff + nm
            name = d[q:d.index(b"\0", q)].decode("latin1")
            self.sections.append(Section(name, addr, offset, size, size))

    def _parse_macho(self) -> None:
        d = self.data
        self.fmt = self.MACHO
        self.bits = 64
        cputype, _sub, _ft, ncmds = struct.unpack_from("<iiii", d, 4)
        self.arch = {0x01000007: "x86-64", 0x0100000C: "arm64"}.get(cputype, "0x%X" % cputype)
        p = 32
        for _ in range(ncmds):
            cmd, cmdsize = struct.unpack_from("<II", d, p)
            if cmd == 0x19:                                
                seg = d[p + 8:p + 24].rstrip(b"\0").decode("latin1")
                vmaddr, vmsize, fileoff, filesize = struct.unpack_from("<QQQQ", d, p + 24)
                nsects = struct.unpack_from("<I", d, p + 64)[0]
                if seg == "__TEXT":
                    self.image_base = vmaddr
                sp = p + 72
                for _s in range(nsects):
                    sname = d[sp:sp + 16].rstrip(b"\0").decode("latin1")
                    addr, size, offset = struct.unpack_from("<QQI", d, sp + 32)
                    if size:
                        self.sections.append(Section("%s,%s" % (seg, sname), addr, offset, size, size))
                    sp += 80
            elif cmd == 0x1D:                              
                self.has_code_signature = True
            p += cmdsize
        if self.has_code_signature:
            self.notes.append("has LC_CODE_SIGNATURE: __TEXT changes require re-signing on macOS "
                              "(codesign --force --sign -), otherwise arm64 kills the process")

    def _pick_text(self) -> Section | None:
        want = ("__TEXT,__text",) if self.fmt == self.MACHO else (".text",)
        for s in self.sections:
            if s.name in want:
                return s
        
        cands = [s for s in self.sections if s.va and s.size]
        return max(cands, key=lambda s: s.size) if cands else None

    
    def fo_to_va(self, fo: int) -> int | None:
        for s in self.sections:
            if s.va and s.fo <= fo < s.fo + max(s.raw_size, s.size):
                return s.va + (fo - s.fo)
        return None

    def va_to_fo(self, va: int) -> int | None:
        for s in self.sections:
            if s.va and s.va <= va < s.va + s.size:
                return s.fo + (va - s.va)
        return None

    def section_of(self, fo: int) -> Section | None:
        for s in self.sections:
            if s.fo <= fo < s.fo + max(s.raw_size, s.size):
                return s
        return None

    def is_in_text(self, fo: int) -> bool:
        return self.text.fo <= fo < self.text.fo + self.text.size

    def find(self, pat: bytes, start: int = 0) -> int:
        return self.data.find(pat, start)

    def find_all(self, pat: bytes, limit: int = 8192) -> list[int]:
        out, p = [], self.data.find(pat)
        while p >= 0 and len(out) < limit:
            out.append(p)
            p = self.data.find(pat, p + 1)
        return out

    
    def find_pointers(self, va: int) -> list[int]:
        
        out: list[int] = []
        q = struct.pack("<Q", va)
        p = 0
        while True:
            p = self.data.find(q, p)
            if p < 0:
                break
            out.append(p)
            p += 1
        if self.fmt == self.MACHO and self.image_base:
            out.extend(self._macho_rebase_targets(va - self.image_base))
        return sorted(set(out))

    def _macho_rebase_targets(self, target_off: int) -> list[int]:
        
        if target_off < 0 or target_off >= (1 << 36):
            return []
        d, out = self.data, []
        for s in self.sections:
            if not s.va or s.name.endswith("__text") or s.fo == 0:
                continue
            end = min(s.fo + max(s.raw_size, s.size), len(d) - 8)
            off = s.fo + (-s.fo % 8)
            while off <= end:
                w = struct.unpack_from("<Q", d, off)[0]
                if (w & 0xFFFFFFFFF) == target_off and not (w >> 63):
                    out.append(off)
                off += 8
        return out

    
    def _data_sections(self):
        
        for s in self.sections:
            if not s.va or s.fo == 0 or s.size < 16:
                continue
            n = s.name.lower()
            if self.fmt == self.PE:
                if n == ".data" or n.startswith(".data"):
                    yield s
            elif self.fmt == self.ELF:
                if n in (".data", ".data.rel.ro", ".noptrdata", ".bss") or n.startswith(".data"):
                    yield s
            else:
                if n.startswith("__data"):
                    yield s

    def iter_string_headers(self, max_len: int = 512):
        
        d = self.data
        for s in self._data_sections():
            end = min(s.fo + max(s.raw_size, s.size), len(d) - 16)
            off = s.fo + (-s.fo % 8)
            while off <= end:
                w = struct.unpack_from("<Q", d, off)[0]
                if not w:
                    off += 8
                    continue
                if self.fmt == self.MACHO and self.image_base:
                    if w >> 63:                     
                        off += 8
                        continue
                    target = self.image_base + (w & 0xFFFFFFFFF)
                else:
                    target = w
                length = struct.unpack_from("<Q", d, off + 8)[0]
                if not (0 < length <= max_len):
                    off += 8
                    continue
                p = self.va_to_fo(target)
                if p is None or p + length > len(d):
                    off += 8
                    continue
                chunk = d[p:p + length]
                if _PRINTABLE.match(chunk):
                    yield off, p, int(length), chunk
                off += 8

    def describe(self) -> str:
        return "%s %s%s" % (self.fmt, self.arch,
                            "(signed)" if self.has_code_signature else "")

    def __repr__(self) -> str:
        return "<Container %s %s base=0x%X text=0x%X+0x%X>" % (
            self.fmt, self.arch, self.image_base, self.text.va, self.text.size)

def load(path: str) -> Container:
    with open(path, "rb") as f:
        return Container(f.read(), path)

import struct

def _sx(v: int, bits: int) -> int:
    m = 1 << (bits - 1)
    return (v ^ m) - m

class Arch:
    name = "?"

    
    def refs_to(self, c: Container, va: int) -> list[int]:
        raise NotImplementedError

    def branches_to(self, c: Container, targets: set[int]) -> list[int]:
        raise NotImplementedError

    def call_before(self, c: Container, fo: int, window: int = 96) -> dict | None:
        raise NotImplementedError

    def branch_target(self, c: Container, fo: int) -> int | None:
        raise NotImplementedError

class X64(Arch):
    name = "x86-64"

    def refs_to(self, c: Container, va: int) -> list[int]:
        
        d = c.data
        lo, hi = c.text.fo, c.text.fo + c.text.size
        tva = c.text.va
        out, i = [], lo
        while i < hi - 7:
            if d[i] in (0x48, 0x4C, 0x49, 0x4D) and d[i + 1] in (0x8D, 0x8B) and \
                    (d[i + 2] & 0xC7) == 0x05:
                tgt = tva + (i - lo) + 7 + struct.unpack_from("<i", d, i + 3)[0]
                if tgt == va:
                    out.append(i)
                i += 7
                continue
            i += 1
        return out

    def branches_to(self, c: Container, targets: set[int]) -> list[int]:
        d = c.data
        lo, hi = c.text.fo, c.text.fo + c.text.size
        tva = c.text.va
        out, i = [], lo
        while i < hi - 6:
            if d[i] == 0x0F and 0x80 <= d[i + 1] <= 0x8F:
                if tva + (i - lo) + 6 + struct.unpack_from("<i", d, i + 2)[0] in targets:
                    out.append(i)
                i += 6
                continue
            if d[i] == 0xE9:
                if tva + (i - lo) + 5 + struct.unpack_from("<i", d, i + 1)[0] in targets:
                    out.append(i)
                i += 5
                continue
            if d[i] in (0x74, 0x75, 0x7C, 0x7D, 0x7E, 0x7F, 0x72, 0x73, 0x76, 0x77,
                        0x78, 0x79, 0x7A, 0x7B, 0x70, 0x71, 0xEB):     
                if tva + (i - lo) + 2 + _sx(d[i + 1], 8) in targets:
                    out.append(i)
                i += 2
                continue
            i += 1
        return out

    def call_before(self, c: Container, fo: int, window: int = 96) -> dict | None:
        d = c.data
        lo = c.text.fo
        tva = c.text.va
        for i in range(fo - 1, max(lo, fo - window) - 1, -1):
            if d[i] == 0xE8:
                callee_va = tva + (i - lo) + 5 + struct.unpack_from("<i", d, i + 1)[0]
                return {"call_fo": i, "callee_va": callee_va, "callee_fo": c.va_to_fo(callee_va)}
        return None

    def branch_target(self, c: Container, fo: int) -> int | None:
        d = c.data
        tva = c.text.va + (fo - c.text.fo)
        b = d[fo]
        if b == 0x0F and 0x80 <= d[fo + 1] <= 0x8F:
            return tva + 6 + struct.unpack_from("<i", d, fo + 2)[0]
        if b == 0xE9:
            return tva + 5 + struct.unpack_from("<i", d, fo + 1)[0]
        if b in (0x74, 0x75, 0x7C, 0x7D, 0x7E, 0x7F, 0x72, 0x73, 0x76, 0x77,
                 0x78, 0x79, 0x7A, 0x7B, 0x70, 0x71, 0xEB):
            return tva + 2 + _sx(d[fo + 1], 8)
        return None

    
    def np3_identity(self, old: bytes) -> bytes:
        return b"\x90\x90\x90"

    def np6_return_nil(self) -> bytes:
        return b"\x31\xc0\xc3"                       

class Arm64(Arch):
    name = "arm64"

    
    @staticmethod
    def _is_adrp(w: int) -> bool:
        return (w & 0x9F000000) == 0x90000000

    @staticmethod
    def _is_add_imm(w: int) -> bool:
        return (w & 0x7F000000) == 0x11000000      

    @staticmethod
    def _adrp_target(pc: int, w: int) -> int:
        immhi = (w >> 5) & 0x7FFFF
        immlo = (w >> 29) & 0x3
        imm = _sx((immhi << 2) | immlo, 21) << 12
        return (pc & ~0xFFF) + imm

    @staticmethod
    def _add_imm_value(w: int) -> int:
        imm12 = (w >> 10) & 0xFFF
        sh = (w >> 22) & 1
        return imm12 << (12 if sh else 0)

    @staticmethod
    def _is_bl(w: int) -> bool:
        return (w & 0xFC000000) == 0x94000000

    @staticmethod
    def bl_target(pc: int, w: int) -> int:
        return pc + (_sx(w & 0x3FFFFFF, 26) << 2)

    @staticmethod
    def _is_b(w: int) -> bool:
        return (w & 0xFC000000) == 0x14000000

    @staticmethod
    def _is_bcond(w: int) -> bool:
        return (w & 0xFF000010) == 0x54000000

    @staticmethod
    def _is_cbz(w: int) -> bool:
        return (w & 0x7E000000) == 0x34000000

    @staticmethod
    def _is_tbz(w: int) -> bool:
        return (w & 0x7E000000) == 0x36000000

    def branches_to(self, c: Container, targets: set[int]) -> list[int]:
        d = c.data
        lo, hi = c.text.fo, c.text.fo + c.text.size
        out = []
        for i in range(lo, hi - 4, 4):
            w = struct.unpack_from("<I", d, i)[0]
            pc = c.text.va + (i - lo)
            tgt = None
            if self._is_bcond(w) or self._is_cbz(w):
                tgt = pc + (_sx((w >> 5) & 0x7FFFF, 21) << 2)
            elif self._is_tbz(w):
                tgt = pc + (_sx((w >> 5) & 0x3FFF, 14) << 2)
            elif self._is_b(w):
                tgt = pc + (_sx(w & 0x3FFFFFF, 26) << 2)
            if tgt is not None and tgt in targets:
                out.append(i)
        return out

    def refs_to(self, c: Container, va: int) -> list[int]:
        
        d = c.data
        lo, hi = c.text.fo, c.text.fo + c.text.size
        out = []
        for i in range(lo, hi - 8, 4):
            w = struct.unpack_from("<I", d, i)[0]
            if not self._is_adrp(w):
                continue
            page = self._adrp_target(c.text.va + (i - lo), w)
            w2 = struct.unpack_from("<I", d, i + 4)[0]
            if self._is_add_imm(w2) and page + self._add_imm_value(w2) == va:
                out.append(i)
        return out

    def call_before(self, c: Container, fo: int, window: int = 96) -> dict | None:
        d = c.data
        lo = c.text.fo
        for i in range(fo - 4, max(lo, fo - window) - 1, -4):
            if i < lo:
                break
            w = struct.unpack_from("<I", d, i)[0]
            if self._is_bl(w):
                callee = (c.text.va + (i - lo)) + (_sx(w & 0x3FFFFFF, 26) << 2)
                return {"call_fo": i, "callee_va": callee, "callee_fo": c.va_to_fo(callee)}
        return None

    def branch_target(self, c: Container, fo: int) -> int | None:
        d = c.data
        w = struct.unpack_from("<I", d, fo)[0]
        pc = c.text.va + (fo - c.text.fo)
        if self._is_bcond(w) or self._is_cbz(w):
            return pc + (_sx((w >> 5) & 0x7FFFF, 21) << 2)
        if self._is_tbz(w):
            return pc + (_sx((w >> 5) & 0x3FFF, 14) << 2)
        if self._is_b(w):
            return pc + (_sx(w & 0x3FFFFFF, 26) << 2)
        return None

    
    def np3_identity(self, old: bytes) -> bytes:
        
        w = struct.unpack("<I", old)[0]
        return struct.pack("<I", (w & ~(0x1F << 16)) | (31 << 16))

    def np3_is_patched(self, cur: bytes) -> bool:
        w = struct.unpack("<I", cur)[0]
        return (w & (0x1F << 16)) == (31 << 16)

    @staticmethod
    def is_eor_reg(w: int) -> bool:
        return (w & 0x7F200000) == 0x4A000000

    @staticmethod
    def is_ldrb_reg(w: int) -> bool:
        return (w & 0xFFE0FC00) == 0x38606800

    @staticmethod
    def is_cmp_imm(w: int, imm: int) -> bool:
        return (w & 0xFF00001F) == 0xF100001F and ((w >> 10) & 0xFFF) == imm

    def np6_return_nil(self) -> bytes:
        
        return struct.pack("<II", 0xAA1F03E0, 0xD65F03C0)

_ARCHES = {"x86-64": X64, "arm64": Arm64}

def for_container(c: Container) -> Arch:
    cls = _ARCHES.get(c.arch)
    if cls is None:
        raise NotImplementedError("unsupported architecture: %s (available: %s)"
                                 % (c.arch, ", ".join(_ARCHES)))
    return cls()

def for_name(name: str) -> Arch:
    cls = _ARCHES.get(name)
    if cls is None:
        raise NotImplementedError("unsupported architecture: %s" % name)
    return cls()

import base64
import re
import struct

SPKI_HEAD = b"MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA"
B64CHARS = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n")
ERR_STRING = b"invalid license"
PEM_TYPE_LEN = 0xB
LICENSE_PATHS = (b"/api/license/verify", b"/api/license/activate", b"/api/license/deactivate")
LOCAL_MARKER = b"/license/"
URL_LEN = {"verify": 44, "activate": 46, "deactivate": 48}

X64_AFTER_CALL = bytes.fromhex("4885c00f84")
X64_CMP_TYPE = bytes.fromhex("488378080b")
NP3_X64_OLD = bytes.fromhex("4531c8")
NP3_X64_NEW = bytes.fromhex("4531d1")
NP3_X64_PATCHED = b"\x90\x90\x90"
NP3_X64_TAIL_OLD = bytes.fromhex("42880c00")
NP3_X64_TAIL_NEW = bytes.fromhex("46880c00")

URL_RE = re.compile(rb"^https?://[A-Za-z0-9._:\-]+(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/?\-]*)?$")

class LocateError(Exception):
    pass

def locate_base_url(c: Container) -> dict | None:
    
    cands = []
    for header_fo, data_fo, length, chunk in c.iter_string_headers(max_len=256):
        if not URL_RE.match(chunk):
            continue
        cands.append({"header_fo": header_fo, "fo": data_fo, "len": length,
                      "text": chunk.decode("latin1")})
    if len(cands) == 1:
        return cands[0]
    if not cands:
        return None
    
    known = (b"123pan", b"chobit", b"github", b"dropbox", b"googleapis", b"baidu",
             b"aliyundrive", b"alipan", b"yandex", b"box.com", b"drive2dav.top")
    preferred = [x for x in cands
                 if x["text"].rstrip("/").endswith("/api")
                 and not any(k in x["text"].encode() for k in known[:-1])]
    if len(preferred) == 1:
        return preferred[0]
    
    non_third = [x for x in cands if not any(k in x["text"].encode() for k in known[:-1])]
    if len(non_third) == 1:
        return non_third[0]
    raise LocateError("multiple URL string headers in data sections, cannot pick the license base: %s"
                      % ", ".join(x["text"] for x in cands))

def locate_urls(data: bytes, url_base: str = "http://127.0.0.1:18443") -> dict:
    
    out: dict[str, dict] = {}
    for verb, want in URL_LEN.items():
        for marker, patched in ((b"/api/license/" + verb.encode(), False),
                                (LOCAL_MARKER + verb.encode(), True)):
            p = 0
            while True:
                i = data.find(marker, p)
                if i < 0:
                    break
                p = i + 1
                start = None
                for back in range(4, 100):
                    j = i - back
                    if j < 0:
                        break
                    if data[j:j + 3] == b"://":
                        start = j - 5 if data[j - 5:j] == b"https" else j - 4
                        break
                if start is None or i < start:
                    continue
                span = data[start:start + want]
                if len(span) != want:
                    continue
                txt = span.decode("latin1")
                ok = re.fullmatch(r"https?://[A-Za-z0-9._\-]+/api/license/%s" % verb, txt) \
                    if not patched else \
                    re.fullmatch(r"https?://[A-Za-z0-9._\-]+:\d+/license/%s/A*" % verb, txt)
                if ok and verb not in out:
                    out[verb] = {"fo": start, "len": want, "text": txt, "patched": patched}
        
        if verb not in out:
            p = 0
            while True:
                i = data.find(LOCAL_MARKER + verb.encode(), p)
                if i < 0:
                    break
                p = i + 1
                start = i - (want - len(LOCAL_MARKER) - len(verb))
                span = data[start:start + want]
                if len(span) == want and re.fullmatch(rb"[\x20-\x7e]+", span):
                    out[verb] = {"fo": start, "len": want, "text": span.decode("latin1"),
                                 "patched": True}
                    break
    return out

def locate_keys(data: bytes) -> dict:
    
    out: dict[str, dict] = {}
    pos = 0
    while True:
        i = data.find(SPKI_HEAD, pos)
        if i < 0:
            break
        pos = i + 1
        j = i
        while j < len(data) and data[j] in B64CHARS:
            j += 1
        span = data[i:j]
        try:
            der = base64.b64decode(span.replace(b"\n", b""), validate=True)
        except Exception:                                
            continue
        if len(der) != 294:
            continue
        before = data[max(0, i - 32):i]
        if data[j:j + 1] == b"`":
            kind = "js"
        elif span.endswith(b"\n") and b"KEY-----" in before:
            kind = "pem"
        else:
            continue
        out[kind] = {"fo": i, "len": len(span), "newlines": span.count(b"\n"), "der": der}
    return out

def _x64_np3_legacy(c: Container, d: bytes) -> dict | None:
    lo, hi = c.text.fo, c.text.fo + c.text.size
    anchors = []
    i = lo
    while i < hi - 6:
        if d[i] == 0xE8 and re.match(X64_AFTER_CALL, d[i + 5:i + 10]) and \
                X64_CMP_TYPE in d[i:i + 0x40]:
            anchors.append(i)
        i += 1
    if len(anchors) != 1:
        return None
    call_fo = anchors[0]
    callee_va = c.text.va + (call_fo - c.text.fo) + 5 + \
        struct.unpack_from("<i", d, call_fo + 1)[0]
    win_start = max(c.text.fo, call_fo - 0x30)
    win = d[win_start:call_fo]
    for pat, state in ((NP3_X64_OLD, "original"), (NP3_X64_PATCHED, "patched")):
        k = win.rfind(pat)
        if k >= 0:
            fo = win_start + k
            return {"fo": fo, "va": c.fo_to_va(fo), "len": 3, "state": state,
                    "pem_decode_va": callee_va, "guard_fo": call_fo + 5,
                    "anchor_fo": call_fo, "cur": d[fo:fo + 3], "style": "legacy"}
    return None

def _x64_np3_modern(c: Container, d: bytes) -> dict | None:
    
    pats = ((NP3_X64_NEW + NP3_X64_TAIL_NEW, "original"),
            (NP3_X64_OLD + NP3_X64_TAIL_OLD, "original"),
            (b"\x90\x90\x90" + NP3_X64_TAIL_NEW, "patched"),
            (b"\x90\x90\x90" + NP3_X64_TAIL_OLD, "patched"))
    hits = []
    for pat, state in pats:
        p = 0
        while True:
            i = d.find(pat, p)
            if i < 0:
                break
            p = i + 1
            
            if X64_CMP_TYPE not in d[i:i + 0x60]:
                continue
            hits.append((i, state, pat))
    
    uniq: dict[int, tuple[str, bytes]] = {}
    for i, state, pat in hits:
        uniq.setdefault(i, (state, pat))
    if len(uniq) != 1:
        return None
    fo, (state, _pat) = next(iter(uniq.items()))
    return {"fo": fo, "va": c.fo_to_va(fo), "len": 3, "state": state,
            "pem_decode_va": None, "guard_fo": None, "anchor_fo": None,
            "cur": d[fo:fo + 3], "style": "modern"}

def locate_np3(c: Container) -> dict | None:
    arch = for_container(c)
    d = c.data
    if c.arch == "x86-64":
        legacy = _x64_np3_legacy(c, d)
        if legacy:
            return legacy
        return _x64_np3_modern(c, d)

    
    lo, hi = c.text.fo, c.text.fo + c.text.size
    cands = []
    for i in range(lo, hi - 4, 4):
        w = struct.unpack_from("<I", d, i)[0]
        if not arch.is_cmp_imm(w, PEM_TYPE_LEN):
            continue
        bl = None
        for k in range(i - 4, max(lo, i - 40) - 1, -4):
            if k < lo:
                break
            if arch._is_bl(struct.unpack_from("<I", d, k)[0]):
                bl = k
                break
        if bl is None:
            continue
        eor = None
        for k in range(bl - 4, max(lo, bl - 24) - 1, -4):
            if k < lo:
                break
            if arch.is_eor_reg(struct.unpack_from("<I", d, k)[0]):
                eor = k
                break
        if eor is None:
            continue
        cands.append({"eor_fo": eor, "call_fo": bl, "cmp_fo": i})
    if len(cands) != 1:
        return {"error": "arm64 NP3 candidates: %d (expected 1)" % len(cands)}
    cd = cands[0]
    bl_w = struct.unpack_from("<I", d, cd["call_fo"])[0]
    callee_va = arch.bl_target(c.text.va + (cd["call_fo"] - lo), bl_w)
    cur = d[cd["eor_fo"]:cd["eor_fo"] + 4]
    state = "patched" if arch.np3_is_patched(cur) else "original"
    return {"fo": cd["eor_fo"], "va": c.fo_to_va(cd["eor_fo"]), "len": 4, "state": state,
            "pem_decode_va": callee_va, "guard_fo": cd["cmp_fo"], "anchor_fo": cd["call_fo"],
            "cur": cur, "style": "legacy"}

def locate_error_blocks(c: Container) -> list[int]:
    d = c.data
    sfo = d.find(ERR_STRING)
    if sfo < 0:
        return []
    sva = c.fo_to_va(sfo)
    if sva is None:
        return []
    st_fo = None
    for cand in c.find_pointers(sva):
        if cand + 16 <= len(d) and struct.unpack_from("<Q", d, cand + 8)[0] == len(ERR_STRING):
            st_fo = cand
            break
    if st_fo is None:
        return []
    st_va = c.fo_to_va(st_fo)
    if st_va is None:
        return []
    ifaces = []
    for p in c.find_pointers(st_va):
        va = c.fo_to_va(p)
        if va and va - 8 != st_va:
            ifaces.append(va - 8)
    arch = for_container(c)
    blocks: list[int] = []
    for iva in ifaces:
        blocks.extend(arch.refs_to(c, iva))
    blocks.sort()
    merged: list[int] = []
    for b in blocks:
        if not merged or b - merged[-1] > 16:
            merged.append(b)
    return merged

def _np6_legacy(c: Container, np3: dict | None) -> dict:
    arch = for_container(c)
    blocks = locate_error_blocks(c)
    res = {"candidates": [], "chosen": None, "err_blocks": blocks, "match": "exact"}
    if not blocks or not np3 or "fo" not in np3:
        return res

    def collect(targets: set[int]) -> list[dict]:
        out = []
        for g in arch.branches_to(c, targets):
            cb = arch.call_before(c, g)
            if not cb or cb["callee_fo"] is None:
                continue
            out.append({"guard_fo": g, "call_fo": cb["call_fo"], "callee_va": cb["callee_va"],
                        "callee_fo": cb["callee_fo"],
                        "prologue": c.data[cb["callee_fo"]:cb["callee_fo"] + 8]})
        return out

    exact = {c.fo_to_va(b) for b in blocks}
    exact.discard(None)
    cands = collect(exact)
    if not cands:
        loose = set()
        for va in exact:
            for delta in (-8, -4, 4, 8):
                loose.add(va + delta)
        cands = collect(loose)
        if cands:
            res["match"] = "loose(+/-8)"
    res["candidates"] = cands
    before = [x for x in cands if x["call_fo"] < np3["fo"]]
    if before:
        res["chosen"] = max(before, key=lambda x: x["call_fo"])
    return res

def _x64_func_start(c: Container, pos: int, span: int = 0x800) -> int:
    d = c.data
    lo = max(c.text.fo, pos - span)
    for i in range(pos, lo, -1):
        if d[i:i + 4] in (b"\x4d\x3b\x66\x10", b"\x49\x3b\x66\x10"):
            if d[i - 4:i] == b"\x4c\x8d\x64\x24":       
                return i - 4
            return i
    return pos

def _np6_modern_x64(c: Container, np3: dict) -> dict:
    d = c.data
    start = _x64_func_start(c, np3["fo"])
    for i in range(start, np3["fo"] - 5):
        if d[i] != 0xE8:
            continue
        after = d[i + 5:i + 13]
        if not (after.startswith(b"\x48\x85\xc0\x0f\x85") or after.startswith(b"\x48\x85\xc0\x74")):
            continue
        callee_va = c.fo_to_va(i) + 5 + struct.unpack_from("<i", d, i + 1)[0]
        callee_fo = c.va_to_fo(callee_va)
        if callee_fo is None or not c.is_in_text(callee_fo):
            continue
        return {"candidates": [{"guard_fo": i + 5, "call_fo": i, "callee_va": callee_va,
                                "callee_fo": callee_fo,
                                "prologue": d[callee_fo:callee_fo + 8]}],
                "chosen": {"guard_fo": i + 5, "call_fo": i, "callee_va": callee_va,
                           "callee_fo": callee_fo,
                           "prologue": d[callee_fo:callee_fo + 8]},
                "err_blocks": locate_error_blocks(c), "match": "modern-call"}
    return {"candidates": [], "chosen": None, "err_blocks": locate_error_blocks(c),
            "match": "modern-call"}

def _arm64_func_start(c: Container, pos: int, span: int = 0x600) -> int:
    d = c.data
    lo = max(c.text.fo, pos - span)
    pro = bytes.fromhex("900b40f9ff6330eb")             
    for i in range(pos, lo, -4):
        if d[i:i + 8] == pro:
            return i
    return pos

def _np6_modern_arm64(c: Container, np3: dict) -> dict:
    d = c.data
    arch = for_container(c)
    start = _arm64_func_start(c, np3["fo"])
    for i in range(start, np3["fo"] - 4, 4):
        w = struct.unpack_from("<I", d, i)[0]
        if not arch._is_bl(w):
            continue
        nxt = struct.unpack_from("<I", d, i + 4)[0] if i + 8 <= len(d) else 0
        
        if not (arch._is_cbz(nxt) or arch._is_bcond(nxt) or arch._is_tbz(nxt)):
            continue
        callee = arch.bl_target(c.text.va + (i - c.text.fo), w)
        callee_fo = c.va_to_fo(callee)
        if callee_fo is None or not c.is_in_text(callee_fo):
            continue
        cand = {"guard_fo": i + 4, "call_fo": i, "callee_va": callee,
                "callee_fo": callee_fo, "prologue": d[callee_fo:callee_fo + 8]}
        return {"candidates": [cand], "chosen": cand,
                "err_blocks": locate_error_blocks(c), "match": "modern-call"}
    return {"candidates": [], "chosen": None, "err_blocks": locate_error_blocks(c),
            "match": "modern-call"}

def locate_np6(c: Container, np3: dict | None, mode: str = "legacy") -> dict:
    if not np3 or "fo" not in np3:
        return {"candidates": [], "chosen": None, "err_blocks": [], "match": "none"}
    if mode == "modern":
        if c.arch == "x86-64":
            res = _np6_modern_x64(c, np3)
        else:
            res = _np6_modern_arm64(c, np3)
        if res["chosen"]:
            return res
        
        legacy = _np6_legacy(c, np3)
        legacy["match"] += "+legacy-fallback"
        return legacy
    return _np6_legacy(c, np3)

def locate_all(c: Container, url_base: str = "http://127.0.0.1:18443") -> dict:
    base = locate_base_url(c)
    mode = "modern" if base else "legacy"
    np3 = locate_np3(c)
    return {
        "container": c,
        "fmt": c.fmt,
        "arch": c.arch,
        "mode": mode,
        "base_url": base,
        "urls": {} if mode == "modern" else locate_urls(c.data, url_base),
        "keys": locate_keys(c.data),
        "np3": np3,
        "np6": locate_np6(c, np3 if np3 and "fo" in np3 else None, mode),
    }

import base64
import hashlib
import struct

DEFAULT_URL_BASE = "http://127.0.0.1:18443"

def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _patcher_b64_lines(der: bytes, trailing_newline: bool, width: int = 64) -> bytes:
    s = base64.b64encode(der).decode()
    lines = [s[i:i + width] for i in range(0, len(s), width)]
    out = "\n".join(lines)
    if trailing_newline:
        out += "\n"
    return out.encode()

def analyze(path: str, url_base: str = DEFAULT_URL_BASE) -> dict:
    c = load(path)
    rep = locate_all(c, url_base)
    rep["path"] = path
    rep["size"] = len(c.data)
    rep["sha256"] = sha256(c.data)
    rep["notes"] = list(c.notes)
    return rep

def build_patch_table(rep: dict, spki_der: bytes, url_base: str = DEFAULT_URL_BASE
                      ) -> list[tuple[int, bytes, bytes, str]]:
    
    c: Container = rep["container"]
    arch = for_container(c)
    d = c.data
    table: list[tuple[int, bytes, bytes, str]] = []

    
    if rep.get("mode") == "modern":
        b = rep.get("base_url")
        if not b:
            raise LocateError("license API base not located (modern protocol)")
        old_len = int(b["len"])
        new = url_base.encode()
        if len(new) > old_len:
            raise ValueError("local endpoint is %d bytes, original base is %d bytes: "
                             "equal-length replacement impossible" % (len(new), old_len))
        table.append((b["fo"], d[b["fo"]:b["fo"] + old_len],
                      new + b"\0" * (old_len - len(new)), "NP2 API base"))
        hdr = b["header_fo"] + 8
        table.append((hdr, struct.pack("<Q", old_len), struct.pack("<Q", len(new)),
                      "NP2 API base length"))
    else:
        for verb in ("verify", "activate", "deactivate"):
            u = rep["urls"].get(verb)
            if not u:
                continue
            want = URL_LEN[verb]
            new = url_base.encode() + b"/license/" + verb.encode() + b"/"
            if len(new) > want:
                raise ValueError("NP2 %s: local endpoint is %d bytes, original is %d bytes: "
                                 "equal-length replacement impossible" % (verb, len(new), want))
            new = new + b"A" * (want - len(new))
            table.append((u["fo"], d[u["fo"]:u["fo"] + want], new, "NP2 endpoint %s" % verb))

    
    js = rep["keys"].get("js")
    pem = rep["keys"].get("pem")
    if js:
        new = _patcher_b64_lines(spki_der, trailing_newline=False)
        if len(new) != js["len"]:
            raise ValueError("NP4 replacement length mismatch: %d != %d" % (len(new), js["len"]))
        table.append((js["fo"], d[js["fo"]:js["fo"] + js["len"]], new, "NP4 embedded public key (frontend JS)"))
    if pem:
        new = _patcher_b64_lines(spki_der, trailing_newline=True)
        if len(new) != pem["len"]:
            raise ValueError("NP5 replacement length mismatch: %d != %d" % (len(new), pem["len"]))
        table.append((pem["fo"], d[pem["fo"]:pem["fo"] + pem["len"]], new, "NP5 embedded public key (Go PEM)"))

    
    np3 = rep["np3"]
    if np3 and "fo" in np3:
        old = d[np3["fo"]:np3["fo"] + np3["len"]]
        table.append((np3["fo"], old, arch.np3_identity(old), "NP3 response secret XOR -> identity"))

    
    chosen = rep["np6"]["chosen"]
    if chosen:
        fo = chosen["callee_fo"]
        new = arch.np6_return_nil()
        old = d[fo:fo + len(new)]
        table.append((fo, old, new, "NP6 signature precheck -> return nil"))

    return table

def apply_patches(rep: dict, table: list[tuple[int, bytes, bytes, str]]) -> bytes:
    
    data = bytearray(rep["container"].data)
    for fo, old, new, name in table:
        if len(old) != len(new):
            raise ValueError("%s length mismatch (%d != %d)" % (name, len(old), len(new)))
        cur = bytes(data[fo:fo + len(old)])
        if cur != old and cur != new:
            raise ValueError("%s: bytes at the site match neither the original nor the patched form" % name)
        data[fo:fo + len(new)] = new
    return bytes(data)

def verify(path: str, url_base: str = DEFAULT_URL_BASE) -> dict:
    
    rep = analyze(path, url_base)
    np3 = rep["np3"] or {}
    np6 = rep["np6"]["chosen"]
    patched_np3 = np3.get("state") == "patched"
    patched_np6 = False
    if np6:
        arch = for_container(rep["container"])
        cur = rep["container"].data[np6["callee_fo"]:np6["callee_fo"] + len(arch.np6_return_nil())]
        patched_np6 = cur == arch.np6_return_nil()
    if rep.get("mode") == "modern":
        b = rep.get("base_url")
        urls_patched = bool(b and b["text"].startswith(url_base)
                            and b["len"] == len(url_base.encode()))
    else:
        urls_patched = all(u.get("patched") for u in rep["urls"].values()) if rep["urls"] else False
    return {"report": rep, "np3_patched": patched_np3, "np6_patched": patched_np6,
            "urls_patched": urls_patched,
            "fully_patched": bool(patched_np3 and patched_np6 and urls_patched)}

import hashlib
import json
import os
import time
from pathlib import Path

CLIENT_NAMES = {"neokikoeru", "neokikoeru.exe"}

SIGN_SCRIPT = """#!/bin/sh
set -e
cd "$(dirname "$0")"
for f in *; do
  case "$f" in
    *.command|*.sh|*.tar.gz|*.txt|*.json|*.log) continue ;;
  esac
  if [ -f "$f" ] && [ -x "$f" ]; then
    echo "==> codesign --force --sign - $f"
    codesign --force --sign - "$f"
  fi
done
echo "Done. You can now run ./{start}"
"""

START_CMD = """@echo off
setlocal
cd /d "%~dp0"
start "" /b "%~dp0{lic}" -addr 127.0.0.1:{port} -log "%~dp0{log}" -quiet
timeout /t 1 /nobreak >nul
"%~dp0{client}" {args}
taskkill /f /im "{lic}" >nul 2>&1
"""

START_SH = """#!/bin/sh
cd "$(dirname "$0")" || exit 1
./{lic} -addr 127.0.0.1:{port} -log ./{log} -quiet &
lic=$!
trap 'kill $lic 2>/dev/null' INT TERM
sleep 1
./{client} {args}
kill $lic 2>/dev/null
"""

README_TXT = """NeoKikoeru patched build ({platform})
{underline}

Files
  {client}
      patched client (licence endpoint {endpoint})
  {lic}
      local licence server for this platform
  {start}
      starts the server and the client, then cleans up

Run
  {run}

First start
  Finish the setup wizard, then activate once:
    web UI -> System / License -> licence key: {key}
  Until that is done /api/works/<id>/files returns an empty list.
{extra}"""

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def collect_samples(samples_file: str, samples_dir: str) -> list[tuple[Path, Path | None]]:
    if samples_file:
        path = Path(samples_file).expanduser()
        if not path.is_file():
            nk_die("sample file not found: %s" % path)
        return [(path.resolve(), None)]

    root = Path(samples_dir).expanduser()
    if not root.is_dir():
        nk_die("sample directory not found: %s" % root)
    root = root.resolve()
    found: list[tuple[Path, Path | None]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name.lower() in CLIENT_NAMES:
            found.append((path, path.relative_to(root)))
    if not found:
        nk_die("no client binary (neokikoeru / neokikoeru.exe) under %s" % root)
    return found

def artifact_path(out_root: Path, rel: Path | None, sample: Path, suffix: str) -> Path:
    parent = rel.parent if rel else Path(".")
    return out_root / parent / (sample.stem + suffix + sample.suffix)

def is_docker_sample(path: Path) -> bool:
    return any(part.lower() == "docker" for part in path.parts)

def platform_of(rep: dict):
    plat = platform_for_container(rep["fmt"], rep["arch"])
    if not plat:
        raise LocateError("unsupported target %s/%s" % (rep["fmt"], rep["arch"]))
    return plat

def check_complete(rep: dict) -> list[str]:
    problems = []
    if rep["mode"] == "modern":
        if not rep.get("base_url"):
            problems.append("NP2 license base not located")
    elif len(rep["urls"]) != 3:
        problems.append("NP2 endpoints %d/3" % len(rep["urls"]))
    if len(rep["keys"]) != 2:
        problems.append("embedded public keys %d/2" % len(rep["keys"]))
    np3 = rep["np3"] or {}
    if "fo" not in np3:
        problems.append("NP3 not located: %s" % np3.get("error", "?"))
    if not rep["np6"]["chosen"]:
        problems.append("NP6 not located")
    return problems

def site_map(rep: dict) -> dict:
    out = {}
    np3 = rep["np3"] or {}
    np6 = rep["np6"]["chosen"]
    if rep["mode"] == "modern" and rep.get("base_url"):
        out["NP2/base"] = "0x%X" % rep["base_url"]["fo"]
    for verb, url in (rep.get("urls") or {}).items():
        out["NP2/" + verb] = "0x%X" % url["fo"]
    for kind, key in rep["keys"].items():
        out["NP4" if kind == "js" else "NP5"] = "0x%X" % key["fo"]
    if "fo" in np3:
        out["NP3"] = "0x%X" % np3["fo"]
    if np6:
        out["NP6"] = "0x%X" % np6["callee_fo"]
    return out

def print_report(rep: dict, label: str) -> None:
    np3 = rep["np3"] or {}
    np6 = rep["np6"]["chosen"]
    print("\n== %-18s %s %s  %s  mode=%s" % (
        label, rep["fmt"], rep["arch"], nk_human_size(rep["size"]), rep["mode"]))
    print("   sha256 %s" % rep["sha256"])
    print("   NP2    %s" % (
        "base " + rep["base_url"]["text"] if rep.get("base_url") else
        " ".join("%s@0x%X" % (k, v["fo"]) for k, v in rep["urls"].items())))
    print("   NP4/NP5 x%d  NP3=%s  NP6=%s  error exits x%d" % (
        len(rep["keys"]),
        ("0x%X(%s)" % (np3["fo"], np3.get("state"))) if "fo" in np3 else "not located",
        ("0x%X" % np6["callee_fo"]) if np6 else "not located",
        len(rep["np6"]["err_blocks"])))

def print_table(table: list[tuple[int, bytes, bytes, str]]) -> None:
    print("\n   patch table (all equal length)")
    for fo, old, new, name in table:
        if len(old) > 12:
            print("     %-38s fo=0x%-9X %5d B  %s -> %s" % (
                name, fo, len(old), sha256_bytes(old)[:10], sha256_bytes(new)[:10]))
        else:
            print("     %-38s fo=0x%-9X %s -> %s" % (name, fo, old.hex(" "), new.hex(" ")))

def start_script(platform, config: dict, client_name: str, lic_name: str) -> tuple[str, str]:
    name = config["start_script_name"]
    values = {
        "lic": lic_name,
        "client": client_name,
        "port": config["license_port"],
        "log": config["license_server_log"],
        "args": " ".join(config["client_args"]),
    }
    if platform.goos == "windows":
        return name + ".cmd", START_CMD.format(**values)
    return name + ".sh", START_SH.format(**values)

def write_artifact_files(target: Path, platform, config: dict) -> dict:
    
    extra = {}
    lic_src = require_licserver(config, platform)
    lic_name = lic_src.name
    if config["copy_license_server"]:
        lic_dst = target.parent / lic_name
        nk_atomic_write(lic_dst, lic_src.read_bytes(), 0o755)
        extra["license_server"] = str(lic_dst)

    start_name = config["start_script_name"] + (
        ".cmd" if platform.goos == "windows" else ".sh")
    if config["emit_start_script"]:
        script_name, text = start_script(platform, config, target.name, lic_name)
        script = target.parent / script_name
        script.write_text(text, encoding="utf-8", newline="\n")
        if platform.goos != "windows":
            os.chmod(script, 0o755)
        extra["start_script"] = str(script)
        start_name = script_name

    if platform.goos == "darwin":
        sign = target.parent / "sign_macos.command"
        sign.write_text(SIGN_SCRIPT.format(start=start_name), encoding="utf-8", newline="\n")
        os.chmod(sign, 0o755)
        extra["sign_script"] = str(sign)
        nk_warn("macOS artifact must be re-signed locally before the first run: %s" % sign)

    run = (".\\%s" % start_name) if platform.goos == "windows" else ("./%s" % start_name)
    readme = target.parent / "README.txt"
    readme.write_text(README_TXT.format(
        platform=platform.name,
        underline="-" * (32 + len(platform.name)),
        client=target.name,
        lic=lic_name,
        start=start_name,
        endpoint=license_endpoint(config),
        key=license_key(config),
        run=run,
        extra=("\n  macOS: run ./sign_macos.command once in this folder before the first start.\n"
               if platform.goos == "darwin" else ""),
    ), encoding="utf-8", newline="\n")
    extra["readme"] = str(readme)
    return extra

def patch_one(sample: Path, rep: dict, out_root: Path, rel: Path | None,
              config: dict) -> dict:
    plat = platform_of(rep)
    problems = check_complete(rep)
    if problems:
        raise LocateError("; ".join(problems))

    table = build_patch_table(rep, require_spki(config),
                              license_endpoint(config))
    client = apply_patches(rep, table)
    if len(client) != rep["size"]:
        raise LocateError("patched size changed (%d -> %d)" % (rep["size"], len(client)))
    print_table(table)

    docker = is_docker_sample(sample)
    target = artifact_path(out_root, rel, sample, config["output_suffix"])
    target.parent.mkdir(parents=True, exist_ok=True)
    nk_atomic_write(target, client, 0o755)

    result = {
        "sample": str(sample),
        "sample_sha256": rep["sha256"],
        "platform": plat.name,
        "format": rep["fmt"],
        "arch": rep["arch"],
        "mode": rep["mode"],
        "container": docker,
        "sites": site_map(rep),
        "patches": [{"name": name, "fo": fo, "length": len(old),
                     "old_sha256": sha256_bytes(old), "new_sha256": sha256_bytes(new)}
                    for fo, old, new, name in table],
        "artifact": str(target),
        "artifact_sha256": sha256_bytes(client),
        "artifact_size": len(client),
    }

    if docker:
        
        
        result["extras"] = {}
    else:
        result["extras"] = write_artifact_files(target, plat, config)

    nk_ok("patched -> %s (%s)" % (target, nk_human_size(len(client))))
    return result

def run_patch(args) -> int:
    config = load_config()
    url_base = license_endpoint(config)

    items = collect_samples(getattr(args, "samples_file", "") or "",
                            getattr(args, "samples_dir", "") or "")
    out_root = Path(args.out).expanduser().resolve()

    nk_banner("NeoKikoeru patch")
    nk_info("config       %s" % config["_path"])
    nk_info("samples      %d" % len(items))
    nk_info("output       %s" % out_root)
    nk_info("license      %s (key %s)" % (url_base, license_key(config)))
    nk_info("client port  %d" % config["client_port"])

    plans = []
    failures = []
    for sample, rel in items:
        label = rel.as_posix() if rel else sample.name
        try:
            rep = analyze(str(sample), url_base)
            problems = check_complete(rep)
            plat = platform_of(rep)
            if problems:
                raise LocateError("; ".join(problems))
            plans.append((sample, rel, label, plat, rep))
        except Exception as exc:  
            failures.append((label, str(exc)))
            nk_warn("%s skipped: %s" % (label, exc))

    if args.report_only:
        for _s, _r, label, _p, rep in plans:
            print_report(rep, label)
        nk_banner("summary")
        print("   patchable %d / failed %d" % (len(plans), len(failures)))
        return 1 if failures else 0

    if not plans:
        nk_die("nothing to patch")

    results = []
    started = time.time()
    out_root.mkdir(parents=True, exist_ok=True)
    for sample, rel, label, _plat, rep in plans:
        try:
            print_report(rep, label)
            results.append(patch_one(sample, rep, out_root, rel, config))
        except Exception as exc:  
            failures.append((label, str(exc)))
            nk_warn("%s failed: %s" % (label, exc))

    report = {
        "tool": "patch_samples.py",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config": config["_path"],
        "license_endpoint": url_base,
        "client_port": config["client_port"],
        "license_key": license_key(config),
        "output_root": str(out_root),
        "samples": results,
        "failed": [{"sample": label, "reason": why} for label, why in failures],
    }
    report_path = out_root / "patch-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    nk_banner("summary")
    for res in results:
        print("   %-16s %-8s %s" % (res["platform"], res["mode"],
                                    Path(res["artifact"]).name))
    for label, why in failures:
        print("   %-16s failed: %s" % (label, why))
    print("\n   ok %d / failed %d, %s" % (len(results), len(failures), nk_elapsed(started)))
    print("   report %s" % report_path)
    if any(not res["container"] for res in results):
        print("\n   Activate once after the first start, otherwise /api/works/<id>/files is empty.")
        print("   Web UI -> System/License -> licence key: %s" % license_key(config))
    return 1 if failures else 0

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="NeoKikoeru patcher: analyze samples, apply equal-length patches and "
                    "write the patched artifacts")
    ap.add_argument("--out", required=True, help="output directory")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--samples-file", default="", help="path to a single sample binary")
    group.add_argument("--samples-dir", default="", help="path to a directory of samples")
    ap.add_argument("--report-only", action="store_true",
                    help="analyze only, write nothing")
    a = ap.parse_args(argv)
    return run_patch(a)


if __name__ == "__main__":
    raise SystemExit(main())
