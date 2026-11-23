"""The client and competitor lists, encrypted at rest with LISTS_KEY.

The two lists (CLIENT_LIST_PATH, COMPETITOR_LIST_PATH) name SUPPLECO's
customers and rivals. As plain workbooks, any copy of the folder showed
them: a backup, a shared zip, a synced folder. With LISTS_KEY set, each list
is kept only encrypted, read into memory only, and written back encrypted.

Fernet (from the `cryptography` package), not AES-GCM by hand: it is
authenticated (an HMAC-SHA256 over the AES ciphertext, so a wrong key or one
changed byte is refused, never read as something else), it makes a fresh
random IV for every write, so there is no nonce for this code to get wrong,
and it is one reviewed recipe rather than a file format made here. The cost:
the file is base64, a third larger, and it shows in clear when it was last
written. The lists are small, so reading one whole into memory costs nothing.

What it protects: a copy of the folder without the key. What it does not: a
thief with the whole disk and .env, which holds the key. That is BitLocker's
job (docs/DEPLOY.md).
"""

from __future__ import annotations

import base64
import glob
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# The first bytes of every encrypted list. A workbook starts with "PK".
MAGIC = b"SUPPLECO-ENCRYPTED-LIST-1\n"
SUFFIX = ".enc"
_KEY_LABEL = b"suppleco client and competitor lists at rest v1"

ENCRYPT_HINT = 'py -3.11 main.py lists encrypt "{name}"'


class ListRefused(Exception):
    """A list the agent will not read: the reason is in words, and names no
    key. Never an empty list in its place: an empty client list would let
    the agent email SUPPLECO's customers."""


def _fernet(key: str) -> Fernet:
    # LISTS_KEY is a long random string, like the other secrets, so HKDF is
    # enough to turn it into Fernet's 32 bytes; it is not a password hash.
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_LABEL).derive(
        key.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(raw))


def is_encrypted(data: bytes) -> bool:
    return data.startswith(MAGIC)


def encrypt(plain: bytes, key: str) -> bytes:
    if not key:
        raise ListRefused("LISTS_KEY is blank, so there is no key to encrypt with")
    return MAGIC + _fernet(key).encrypt(plain)


def decrypt(blob: bytes, key: str, name: str = "the list") -> bytes:
    if not is_encrypted(blob):
        raise ListRefused(f"{name} is not an encrypted list")
    try:
        return _fernet(key).decrypt(blob[len(MAGIC) :])
    except InvalidToken:
        raise ListRefused(
            f"{name} could not be decrypted: LISTS_KEY is not the key it was "
            "encrypted with, or the file was changed or damaged. Nothing was "
            "read from it."
        ) from None


def read_list(path, key: str) -> bytes:
    """The list's workbook bytes, in memory only.

    With a key, only an encrypted list is read; without one, only a plain
    one. Anything else is refused in words.
    """
    path = Path(path)
    data = path.read_bytes()
    if key:
        if not is_encrypted(data):
            raise ListRefused(
                f"{path.name} is a plain file, and LISTS_KEY is set: a plain "
                "list is refused. Encrypt it with "
                + ENCRYPT_HINT.format(name=path.name)
                + " and point the setting at the .enc file it writes."
            )
        return decrypt(data, key, path.name)
    if is_encrypted(data):
        raise ListRefused(
            f"{path.name} is encrypted, and LISTS_KEY is blank: set LISTS_KEY "
            "in .env to the key it was encrypted with."
        )
    return data


def seal(plain: bytes, key: str) -> bytes:
    """What to write back: encrypted with a key, as it was without one."""
    return encrypt(plain, key) if key else plain


def plain_copies(path) -> list[Path]:
    """Plain copies of an encrypted list still beside it: the workbook it
    was made from, and the dated backups marking a client made of that
    workbook (modules/clients.py)."""
    path = Path(path)
    if not path.name.endswith(SUFFIX):
        return []
    plain = path.with_name(path.name[: -len(SUFFIX)])
    found = [plain] if plain.is_file() else []
    found += path.parent.glob(f"{glob.escape(plain.stem)}.backup-*{plain.suffix}")
    copies = []
    for candidate in sorted(set(found)):
        try:
            with open(candidate, "rb") as f:
                head = f.read(len(MAGIC))
        except OSError:
            continue
        if not is_encrypted(head):
            copies.append(candidate)
    return copies


def overwrite_and_delete(path) -> None:
    """Overwrite the file with zeros, then delete it. On an SSD, a synced
    folder or a file system that keeps old copies, the old bytes may survive
    anyway; disk encryption covers that."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "r+b") as f:
        f.write(b"\0" * size)
        f.flush()
        os.fsync(f.fileno())
    path.unlink()


def configured_lists(settings) -> list[tuple[str, str, Path]]:
    """(label, setting, path) for each list the agent reads."""
    from config import PROJECT_ROOT

    lists = []
    for label, setting, enabled, raw in (
        (
            "Client list",
            "CLIENT_LIST_PATH",
            settings.client_filter_enabled,
            settings.client_list_path,
        ),
        (
            "Competitor list",
            "COMPETITOR_LIST_PATH",
            settings.competitor_filter_enabled,
            settings.competitor_list_path,
        ),
    ):
        raw = (raw or "").strip()
        if enabled and raw:
            lists.append((label, setting, PROJECT_ROOT / raw))
    return lists


def _shown(path: Path) -> str:
    from config import PROJECT_ROOT

    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path)


def _encrypt_command(args, settings) -> int:
    from modules.clients import ClientRegister, names_in

    key = settings.lists_key
    source = Path(args.file)
    if not key:
        print("  LISTS_KEY is blank: set it in .env first (docs/DEPLOY.md).")
        return 1
    if not source.is_file():
        print(f"  {source} not found.")
        return 1
    plain = source.read_bytes()
    if is_encrypted(plain):
        print(f"  {source} is already encrypted.")
        return 1
    try:
        names = names_in(plain)
    except Exception as e:  # noqa: BLE001 - openpyxl raises bare Exception subclasses
        print(f"  {source} cannot be read as a workbook ({e}). Nothing written.")
        return 1
    target = source.with_name(source.name + SUFFIX)
    try:
        with open(target, "xb") as f:
            f.write(encrypt(plain, key))
    except FileExistsError:
        print(
            f"  {target} already exists. Nothing written. Move it away first: "
            "it may hold clients marked since it was made."
        )
        return 1
    except OSError as e:
        target.unlink(missing_ok=True)
        print(f"  {target} could not be written ({e}). Nothing was kept.")
        return 1
    try:
        same = decrypt(target.read_bytes(), key, target.name) == plain
    except (ListRefused, OSError):
        same = False
    if not same:
        target.unlink(missing_ok=True)
        print(f"  {target} did not decrypt to the same file. It was deleted.")
        return 1
    count = ClientRegister(names).matchable
    print(f"  Encrypted {source} into {target} ({count} companies).")
    print("  Checked: it decrypts to the same file.")
    for _label, setting, path in configured_lists(settings):
        if path.resolve() == source.resolve():
            print(f"  Now set {setting}={_shown(target)} in .env.")
    if args.delete_plain:
        try:
            overwrite_and_delete(source)
        except OSError as e:
            print(f"  {source} could not be deleted ({e}). Delete it by hand.")
            return 1
        print(f"  {source} was overwritten and deleted.")
    else:
        print(
            f"  Then delete the plain file {source}: until it is gone, any "
            "copy of the folder still shows the list."
        )
    leftovers = [p for p in plain_copies(target) if p.resolve() != source.resolve()]
    if leftovers:
        print("  Plain copies still on disk, to delete too:")
        for p in leftovers:
            print(f"    {p}")
    print(
        "  Keep a copy of LISTS_KEY apart from the folder and its backups: "
        "without it the encrypted list cannot be read."
    )
    return 0


def _decrypt_command(args, settings) -> int:
    key = settings.lists_key
    source, out = Path(args.file), Path(args.out)
    if not key:
        print("  LISTS_KEY is blank, so there is no key to decrypt with.")
        return 1
    try:
        plain = decrypt(source.read_bytes(), key, source.name)
    except OSError as e:
        print(f"  {source} cannot be read ({e}).")
        return 1
    except ListRefused as e:
        print(f"  {e}")
        return 1
    created = False
    try:
        with open(out, "xb") as f:
            created = True
            f.write(plain)
    except FileExistsError:
        print(f"  {out} already exists. Nothing written.")
        return 1
    except OSError as e:
        # A half-written plain copy is still the list in plain form.
        if created:
            out.unlink(missing_ok=True)
        print(f"  {out} could not be written ({e}). Nothing was kept.")
        return 1
    print(f"  WARNING: {out} is the list in plain form.")
    print(
        "  Anyone who copies this folder, or the place you put it, can read "
        "it. Delete it as soon as you are done."
    )
    return 0


def _check_command(args, settings) -> int:
    from modules.clients import ClientRegister, load_from_xlsx

    key = settings.lists_key
    lists = configured_lists(settings)
    if not lists:
        print("  No list is configured.")
        return 0
    if not key:
        print(
            "  LISTS_KEY is blank: the lists are plain files, and any copy of "
            "the folder shows them."
        )
    failed = False
    for label, setting, path in lists:
        where = f"{label} ({setting}) {_shown(path)}"
        if not path.is_file():
            print(f"  {where}: not found - the agent refuses live sends.")
            failed = True
            continue
        try:
            register = ClientRegister(load_from_xlsx(path, key=key))
        except ListRefused as e:
            print(f"  {where}: REFUSED - {e}")
            failed = True
            continue
        except Exception as e:  # noqa: BLE001 - the agent's loader contains any error too
            print(f"  {where}: cannot be read ({e}).")
            failed = True
            continue
        form = "encrypted" if key else "plain"
        print(f"  {where}: {form}, {register.matchable} companies.")
        for copy in plain_copies(path):
            print(f"    A plain copy is still on disk: {copy} - delete it.")
    return 1 if failed else 0


def run_command(args, settings) -> int:
    """main.py lists encrypt | decrypt | check. Returns the exit status."""
    commands = {
        "encrypt": _encrypt_command,
        "decrypt": _decrypt_command,
        "check": _check_command,
    }
    return commands[args.action](args, settings)
