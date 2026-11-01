"""Safe, in-memory reader for package archives.

Nothing is extracted to disk and nothing is executed: members are read into bounded buffers and parsed as JSON. The central rule is that
declared sizes in a ZIP header are not trusted; every read is bounded by what is actually decompressed.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

MAX_ARCHIVE_BYTES = 1 * 1024 * 1024
MAX_ENTRIES = 40
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1 * 1024 * 1024
MAX_RATIO = 100                      # uncompressed / compressed, per member
MAX_COMMENT = 1024
MAX_JSON_DEPTH = 20
REQUIRED = ("manifest.json", "strategy/rules.json", "parameters.json", "metadata.json")
FIXED_ALLOWED = set(REQUIRED) | {"README.md"}
TESTS_RE = re.compile(r"^tests/[A-Za-z0-9_\-]{1,40}\.json$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-/]{1,100}$")
INSTALL_LIKE = re.compile(r"(^|/)(setup\.py|setup\.cfg|pyproject\.toml|requirements.*\.txt|package\.json|install.*|postinstall.*|makefile|dockerfile)$|\.(py|pyc|so|dll|exe|sh|bat|cmd|ps1|js|jar|wasm)$", re.I)


class PackageRejected(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


@dataclass
class OpenedPackage:
    sha256: str
    files: dict[str, bytes] = field(default_factory=dict)


def _reject_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _no_const(c):
    raise ValueError(f"non-finite number {c}")


def parse_json(data: bytes, what: str):
    try:
        text = data.decode("utf-8")
        obj = json.loads(text, object_pairs_hook=_reject_dupes, parse_constant=_no_const)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise PackageRejected("MALFORMED_JSON", f"{what}: {type(e).__name__}")
    depth = [0]

    def walk(o, d):
        if d > MAX_JSON_DEPTH:
            raise PackageRejected("JSON_TOO_DEEP", what)
        if isinstance(o, dict):
            for v in o.values():
                walk(v, d + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, d + 1)
    walk(obj, 0)
    return obj


def _check_name(name: str) -> None:
    if not SAFE_NAME.match(name) or "\x00" in name:
        raise PackageRejected("UNSAFE_PATH", repr(name)[:80])
    if name.startswith("/") or name.startswith("./") or "//" in name or "\\" in name or re.match(r"^[A-Za-z]:", name):
        raise PackageRejected("UNSAFE_PATH", name)
    parts = name.rstrip("/").split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise PackageRejected("UNSAFE_PATH", name)      # traversal, hidden files, empty segments


def open_package(data: bytes) -> OpenedPackage:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise PackageRejected("EMPTY_ARCHIVE")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise PackageRejected("ARCHIVE_TOO_LARGE", f"{len(data)} > {MAX_ARCHIVE_BYTES}")
    sha = hashlib.sha256(data).hexdigest()
    try:
        zf = zipfile.ZipFile(BytesIO(bytes(data)))
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as e:
        raise PackageRejected("NOT_A_ZIP", type(e).__name__)
    with zf:
        if len(zf.comment) > MAX_COMMENT:
            raise PackageRejected("COMMENT_TOO_LARGE")
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise PackageRejected("TOO_MANY_ENTRIES", str(len(infos)))
        seen: set[str] = set()
        out: dict[str, bytes] = {}
        total = 0
        for info in infos:
            name = info.filename
            _check_name(name)
            key = name.rstrip("/").lower()
            if key in seen:
                raise PackageRejected("DUPLICATE_PATH", name)
            seen.add(key)
            mode = (info.external_attr >> 16) & 0xFFFF
            ftype = stat.S_IFMT(mode)                    # many tools write permission bits only (type bits 0): that is a plain file
            if ftype == stat.S_IFLNK:
                raise PackageRejected("SYMLINK", name)
            if ftype not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise PackageRejected("SPECIAL_FILE", name)
            if info.flag_bits & 0x1:
                raise PackageRejected("ENCRYPTED_ENTRY", name)
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise PackageRejected("UNSUPPORTED_COMPRESSION", name)
            if info.is_dir():
                if name.rstrip("/") not in ("strategy", "tests"):
                    raise PackageRejected("UNSUPPORTED_ENTRY", name)
                continue
            if INSTALL_LIKE.search(name):
                raise PackageRejected("EXECUTABLE_OR_INSTALL_FILE", name)
            if name not in FIXED_ALLOWED and not TESTS_RE.match(name):
                raise PackageRejected("UNSUPPORTED_ENTRY", name)
            if info.file_size > MAX_FILE_BYTES:
                raise PackageRejected("FILE_TOO_LARGE", name)
            if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
                raise PackageRejected("COMPRESSION_RATIO", name)
            try:
                with zf.open(info) as fh:
                    buf = fh.read(MAX_FILE_BYTES + 1)          # bounded by what is really produced, not by the header
            except (zipfile.BadZipFile, OSError, ValueError, EOFError, RuntimeError, NotImplementedError) as e:
                raise PackageRejected("CORRUPT_ENTRY", f"{name}: {type(e).__name__}")
            if len(buf) > MAX_FILE_BYTES:
                raise PackageRejected("FILE_TOO_LARGE", name)
            if len(buf) != info.file_size:
                raise PackageRejected("SIZE_MISMATCH", name)
            total += len(buf)
            if total > MAX_TOTAL_BYTES:
                raise PackageRejected("TOTAL_TOO_LARGE")
            out[name] = buf
    missing = [r for r in REQUIRED if r not in out]
    if missing:
        raise PackageRejected("MISSING_FILE", ", ".join(missing))
    return OpenedPackage(sha256=sha, files=out)
