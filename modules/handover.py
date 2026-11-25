"""The handover archive: the agent and the client's data in one encrypted file.

scripts/make_handover.py packs a stopped install into it on the owner's
computer; `main.py handover-restore <archive>` unpacks it on the client's
(docs/DEPLOY.md, "Handing the agent and its data to the client").

The archive is a zip, encrypted whole with AES-256-GCM under a key made from
a passphrase by scrypt (the `cryptography` package). GCM is authenticated: a
wrong passphrase and a changed or damaged byte both fail before anything is
written, and the two cannot be told apart. Inside, beside the files, travel
ERASURE_KEY and LISTS_KEY: the database opens only with the erasure key it was
made with, and without it the opt-outs and erasures in it would no longer be
recognised. They are written nowhere else, and never printed: on restore they
go into one new file readable by the current account only.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"the client-HANDOVER-1\n"
SALT_BYTES = 16
NONCE_BYTES = 12
# scrypt's cost as a power of two: 2**17 uses 128 MB and about a third of a
# second here. A restore accepts this range only, so a changed header cannot
# make it ask for gigabytes before the archive is found to be changed.
SCRYPT_LOG2_N = 17
SCRYPT_LOG2_N_RANGE = range(15, 21)
SCRYPT_R = 8
SCRYPT_P = 1
HEADER_BYTES = len(MAGIC) + SALT_BYTES + 3 + NONCE_BYTES
# AES-GCM in one call takes less than 2 GB; the agent's data is far smaller.
MAX_PAYLOAD_BYTES = 1024**3
PASSPHRASE_MIN_LENGTH = 16

PAYLOAD = "payload/"
MANIFEST = "meta/manifest.json"
KEYS = "meta/keys.json"
CARRIED_KEYS = ("ERASURE_KEY", "LISTS_KEY")
FORMAT = 1

KEYS_FILE = ".env.handover-keys"
README = "README_FOR_the client.md"
# The agent_state key modules.database records the erasure key's fingerprint
# under (ERASURE_KEY_FINGERPRINT there).
FINGERPRINT_STATE = "erasure_key_fingerprint"
# What the owner sends beside the archive: this file, which opens it on a
# computer that has no copy of the agent yet. It holds no data and no key.
OPENER_NAME = "open_handover.py"


class HandoverRefused(Exception):
    """The archive was not made or not opened. The reason is in words and
    names no key or passphrase."""


def _derive(passphrase: str, salt: bytes, log2n: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**log2n, r=SCRYPT_R, p=SCRYPT_P).derive(
        passphrase.encode("utf-8")
    )


def seal(payload: bytes, passphrase: str) -> bytes:
    """The archive's bytes: a header, then the payload encrypted under it."""
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise HandoverRefused(
            f"The files come to {len(payload) // 1024**2} MB, more than one "
            "archive holds (1024 MB)."
        )
    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    header = MAGIC + salt + bytes((SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P)) + nonce
    key = _derive(passphrase, salt, SCRYPT_LOG2_N)
    return header + AESGCM(key).encrypt(nonce, payload, header)


def unseal(blob: bytes, passphrase: str) -> bytes:
    """The payload, or HandoverRefused. The header is authenticated too."""
    if not blob.startswith(MAGIC) or len(blob) <= HEADER_BYTES:
        raise HandoverRefused("This file is not a handover archive.")
    header = blob[:HEADER_BYTES]
    at = len(MAGIC)
    salt = header[at : at + SALT_BYTES]
    log2n, r, p = header[at + SALT_BYTES : at + SALT_BYTES + 3]
    nonce = header[at + SALT_BYTES + 3 :]
    if log2n not in SCRYPT_LOG2_N_RANGE or (r, p) != (SCRYPT_R, SCRYPT_P):
        raise HandoverRefused(
            "This archive was changed or damaged (its header is not one this "
            "program writes). Nothing was written."
        )
    key = _derive(passphrase, salt, log2n)
    try:
        return AESGCM(key).decrypt(nonce, blob[HEADER_BYTES:], header)
    except InvalidTag:
        raise HandoverRefused(
            "The passphrase is wrong, or the archive was changed or damaged on "
            "the way. Nothing was written. Check the passphrase with the owner "
            "by phone; if it is right, ask for the archive again."
        ) from None


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pack(files: dict[str, bytes], keys: dict[str, str], made: str) -> bytes:
    """The zip inside the archive: every file under payload/, a manifest of
    their SHA-256 hashes, and the two keys. made is the agent's own time
    (modules.clock), which this file, run alone, does not import."""
    manifest = {
        "format": FORMAT,
        "made": made,
        "files": {name: digest(data) for name, data in sorted(files.items())},
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(files.items()):
            zf.writestr(PAYLOAD + name, data)
        zf.writestr(MANIFEST, json.dumps(manifest, indent=1))
        zf.writestr(KEYS, json.dumps({k: keys.get(k, "") for k in CARRIED_KEYS}))
    return buffer.getvalue()


def safe_name(name: str) -> bool:
    """A relative path inside the folder: no drive, no root, no '..'."""
    path = PurePosixPath(name)
    return (
        bool(name)
        and "\\" not in name
        and not path.is_absolute()
        and ".." not in path.parts
        and ":" not in path.parts[0]
    )


def unpack(payload: bytes) -> tuple[dict[str, bytes], dict[str, str], dict]:
    """The files, the keys and the manifest, each file checked against it."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
        names = set(zf.namelist())
        manifest = json.loads(zf.read(MANIFEST))
        keys = json.loads(zf.read(KEYS))
        listed = manifest["files"]
        files = {name: zf.read(PAYLOAD + name) for name in listed}
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError) as e:
        raise HandoverRefused(f"The archive's contents are not whole ({e}).") from None
    extra = names - {MANIFEST, KEYS} - {PAYLOAD + n for n in listed}
    bad = [n for n, data in files.items() if digest(data) != listed[n]]
    unsafe = [n for n in files if not safe_name(n)]
    if manifest.get("format") != FORMAT or extra or bad or unsafe:
        raise HandoverRefused(
            "The archive's contents do not match its own list of files. "
            "Nothing was written."
        )
    if not isinstance(keys, dict) or not str(keys.get("ERASURE_KEY") or "").strip():
        raise HandoverRefused(
            "The archive carries no ERASURE_KEY. Nothing was written."
        )
    return files, {k: str(keys.get(k) or "") for k in CARRIED_KEYS}, manifest


def erasure_key_fingerprint(key: str) -> str:
    """modules.database.erasure_key_fingerprint(key), repeated here so this
    file runs alone (a test holds the two equal)."""
    return hmac.new(
        key.strip().encode("utf-8"), b"erasure key fingerprint", hashlib.sha256
    ).hexdigest()[:16]


def stored_erasure_fingerprint(db_file: Path) -> str | None:
    """The fingerprint of the erasure key a database recorded, read-only."""
    conn = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM agent_state WHERE key = ?", (FINGERPRINT_STATE,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _what_is_in_the_way(folder: Path) -> str | None:
    if not folder.exists():
        return None
    if not folder.is_dir():
        return f"{folder} is a file, not a folder"
    if any(p.name.lower() == ".env" for p in folder.iterdir()):
        return f"{folder} already holds a .env file, which a restore must never replace"
    databases = [
        p
        for p in folder.rglob("*")
        if p.suffix.lower() in (".db", ".sqlite", ".sqlite3") and p.is_file()
    ]
    if databases:
        return (
            f"{folder} already holds a database ({databases[0].name}), which a "
            "restore must never replace"
        )
    if any(folder.iterdir()):
        return f"{folder} is not empty"
    return None


def default_folder(archive: Path) -> Path:
    name = archive.name.split(".")[0] or "omega-outreach-agent"
    return archive.resolve().parent / name


def _owner_only_file(path: Path) -> None:
    """Create path empty, readable and writable by this account only, or
    raise HandoverRefused. On Windows the inherited permissions are removed
    with icacls and this account alone is given access."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    if os.name == "posix":
        return
    domain = os.environ.get("USERDOMAIN", "")
    user = getpass.getuser()
    account = f"{domain}\\{user}" if domain else user
    try:
        done = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        path.unlink(missing_ok=True)
        raise HandoverRefused(
            f"Could not limit who may read the key file ({e})."
        ) from None
    if done.returncode != 0:
        path.unlink(missing_ok=True)
        raise HandoverRefused(
            "Could not limit who may read the key file (icacls answered "
            f"{done.returncode}). Nothing was unpacked."
        )


def _keys_text(keys: dict[str, str]) -> str:
    lines = [
        "# ERASURE_KEY and LISTS_KEY from the handover archive.",
        "# Copy these lines into the new .env, keep a copy of both apart from",
        "# this computer (never beside its backups), and never change",
        "# ERASURE_KEY: the database recognises the people erased and opted",
        "# out only with it. Then delete this file.",
    ]
    for name in CARRIED_KEYS:
        value = keys.get(name, "")
        if value:
            lines.append(f"{name}={value}")
        else:
            lines.append(f"# {name} was blank in the old install.")
    return "\n".join(lines) + "\n"


def _private_opener(path, flags):
    """Files it creates are owner-only on POSIX, as modules.private_files
    makes them (repeated here so this file runs alone). On Windows they take
    the permissions of the folder they are unpacked into, which IT sets."""
    return os.open(path, flags, 0o600)


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, "xb", opener=_private_opener) as f:
        f.write(data)


def run_doctor(folder: Path) -> int | None:
    """The new folder's own doctor, in that folder. None when it has no main.py."""
    main_py = folder / "main.py"
    if not main_py.is_file():
        return None
    return subprocess.run(
        [sys.executable, str(main_py), "doctor"], cwd=folder, check=False
    ).returncode


def restore(
    archive: Path,
    ask_passphrase,
    into: Path | None = None,
    *,
    out=print,
    doctor=run_doctor,
) -> Path:
    """Check the archive, unpack it into a new folder, put the two keys in a
    file only this account can read, then run doctor there. Returns the
    folder. Refuses (HandoverRefused) a folder holding a .env or a database,
    or anything at all, before the passphrase is asked and again before
    writing. ask_passphrase is called once, after those checks."""
    archive = Path(archive)
    folder = Path(into) if into else default_folder(archive)
    in_the_way = _what_is_in_the_way(folder)
    if in_the_way:
        raise HandoverRefused(f"{in_the_way}. Name a new folder with --into.")
    try:
        blob = archive.read_bytes()
    except OSError as e:
        raise HandoverRefused(f"Could not read the archive: {e}") from None
    if not blob.startswith(MAGIC):
        raise HandoverRefused("This file is not a handover archive.")
    files, keys, manifest = unpack(unseal(blob, ask_passphrase()))
    out(f"  Archive checked: {len(files)} files, made {manifest.get('made', '?')}.")

    in_the_way = _what_is_in_the_way(folder)
    if in_the_way:
        raise HandoverRefused(f"{in_the_way}. Name a new folder with --into.")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    keys_file = folder / KEYS_FILE
    try:
        _owner_only_file(keys_file)
    except HandoverRefused:
        with contextlib.suppress(OSError):
            folder.rmdir()
        raise
    for name, data in sorted(files.items()):
        _write_file(folder / name, data)
    with open(keys_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(_keys_text(keys))
    out(f"  Unpacked into {folder}")

    databases = [folder / n for n in files if n.lower().endswith(".db")]
    if databases and stored_erasure_fingerprint(databases[0]) not in (
        None,
        erasure_key_fingerprint(keys["ERASURE_KEY"]),
    ):
        raise HandoverRefused(
            f"The erasure key in the archive is not the one {databases[0].name} "
            f"was made with. Do not use {folder}; call the owner."
        )
    out("  The erasure key in the archive fits the database.")
    out(f"  ERASURE_KEY and LISTS_KEY are in {keys_file}")
    out("  (only this account can read that file; the keys are not shown here).")
    out("  Next, as README_FOR_the client.md in the new folder says:")
    out("    1. Make the new .env from .env.example and copy the lines from")
    out(f"       {KEYS_FILE} into it.")
    out("    2. Keep a copy of both keys apart from this computer. Never change")
    out("       ERASURE_KEY: with another key, the people who were erased or")
    out("       said no would no longer be recognised.")
    out(f"    3. Delete {KEYS_FILE}, then run doctor again until it passes.")
    out("  Running doctor in the new folder now. Until the packages are installed")
    out("  and the new .env is complete, it reports what is missing; that is")
    out("  expected.")
    if doctor(folder) is None:
        out("  The new folder has no main.py, so doctor could not run.")
    return folder


def _main(argv=None) -> int:
    """Run as a file alone: `py -3.15 open_handover.py <archive> [--into F]`."""
    import argparse

    parser = argparse.ArgumentParser(description="Open the owner's handover archive.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--into", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        restore(
            args.archive,
            lambda: getpass.getpass("  Passphrase (given by the owner by phone): "),
            args.into,
        )
    except HandoverRefused as e:
        print(f"\n  Refused: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
