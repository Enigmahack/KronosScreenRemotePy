"""Pull/push a `.KSC`/`.KMP` sample collection plus its whole dependency
closure over FTP — port of C#'s Core/Sample/SampleFtpClosure.cs (pull) and
SampleFtpPush.cs (push).

Mirrors the format's own folder convention throughout (kronosology doc §1.5/
§5): `<dest>/<ksc-basename>/<kmp-name>`, `<dest>/<ksc-basename>/
<kmp-basename>/<ksf-name>`. Reuses Tools.file_manager's `_FtpWorker` for the
actual transport (already the app's shared FTP client — see main_window.py's
`_verify_ftp_login`) rather than a bespoke client; adds the atomic stage-then-
promote pattern on top for both directions, since `_FtpWorker.upload`/
`download` are plain STOR/RETR with no staging of their own.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample
import Data.sample_path_guard as path_guard
from Tools.file_manager import _FtpWorker, _MAX_REMOTE_PATH_LENGTH

log = logging.getLogger(__name__)

_NEWMS_PLACEHOLDERS = ("NEWMS000", "NEWMS001")


def is_ignorable_placeholder_kmp(entry_name: str) -> bool:
    """NEWMS000.KMP/NEWMS001.KMP are the Kronos's own default placeholder
    multisample names, always present (and often empty/unpopulated) on a
    brand-new library — not finding one is the normal state, not a real
    pull/push failure worth surfacing to the user."""
    base = os.path.splitext(entry_name)[0]
    return base.upper() in _NEWMS_PLACEHOLDERS


def local_path_for(local_root: str, remote_full_path: str) -> str:
    """Every pulled file's local path mirrors its full remote path under
    local_root — lets KmpZone.ksf_path (which independently recomputes local
    paths from the picked .KSC's own path) find exactly what was pulled here."""
    combined = os.path.join(local_root, remote_full_path.lstrip("/").replace("/", os.sep))
    return path_guard.ensure_under(local_root, combined, remote_full_path)


def _ensure_remote_dir(ftp: _FtpWorker, remote_dir: str) -> None:
    """mkdir every path segment that doesn't already exist. ftplib's MKD
    raises on an existing directory (unlike FluentFTP's createRemoteDir),
    so each segment is checked first."""
    parts = [p for p in remote_dir.strip("/").split("/") if p]
    cur = ""
    for part in parts:
        cur = f"{cur}/{part}"
        if not ftp.file_exists(cur):
            try:
                ftp.mkdir(cur)
            except Exception:
                pass  # race with another client, or a permissions quirk — the upload itself will fail loudly if it matters


def _upload_one(ftp: _FtpWorker, local_path: str, remote_path: str,
                on_progress: Optional[Callable[[str], None]], failures: List[str]) -> None:
    """Stage to a unique sibling and promote only once the upload verifiably
    succeeds — a disconnect mid-transfer must not truncate a previously-valid
    remote file. The one chokepoint every sample push goes through."""
    if on_progress:
        on_progress(f"Uploading {os.path.basename(local_path)}...")
    part = f"{remote_path}.{uuid.uuid4().hex[:8]}.part"
    # Checked against `part`, not `remote_path` — it's the longer of the two
    # and what's actually sent to the server first.
    if len(part) > _MAX_REMOTE_PATH_LENGTH:
        failures.append(f"{os.path.basename(local_path)}: destination path too long "
                        f"({len(remote_path)} characters) — choose a shallower destination folder")
        return
    try:
        _ensure_remote_dir(ftp, os.path.dirname(part))
        ftp.upload(local_path, part)
        if ftp.file_exists(remote_path):
            ftp.delete_file(remote_path)
        ftp.rename(part, remote_path)  # already guards top-level-volume + path-length
    except Exception as ex:
        log.warning("Sample push: '%s' -> '%s' failed: %s", local_path, remote_path, ex)
        failures.append(f"{os.path.basename(local_path)}: {ex}")
        try:
            ftp.delete_file(part)
        except Exception:
            pass


def _download_one(ftp: _FtpWorker, remote_path: str, local_root: str,
                  remote_map: Dict[str, str],
                  on_progress: Optional[Callable[[str], None]]) -> bytes:
    """Via a temp file, promoted only on full success. A prior failed
    download must not leave/reuse a stale local file that a later push would
    then upload as if it were freshly pulled."""
    local_path = local_path_for(local_root, remote_path)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    if on_progress:
        on_progress(f"Downloading {os.path.basename(remote_path)}...")
    tmp_path = local_path + ".part"
    try:
        ftp.download(remote_path, tmp_path)
        os.replace(tmp_path, local_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    remote_map[local_path] = remote_path
    with open(local_path, "rb") as f:
        return f.read()


def _pull_zones(ftp: _FtpWorker, kmp_bytes: bytes, kmp_remote_path: str, local_root: str,
                remote_map: Dict[str, str], on_progress: Optional[Callable[[str], None]],
                failures: List[str]) -> None:
    m = KmpMultisample.open(kmp_bytes)
    if m is None:
        return
    kmp_base_remote_dir = kmp_remote_path[:-4]  # strip ".KMP" (4 chars, caller already checked)
    for zone in m.zones:
        if zone.is_skipped:
            continue
        ksf_remote_path = f"{kmp_base_remote_dir}/{zone.filename}"
        try:
            _download_one(ftp, ksf_remote_path, local_root, remote_map, on_progress)
        except Exception as ex:
            log.warning("Sample pull: skipping unreachable sample '%s': %s", ksf_remote_path, ex)
            failures.append(f"{zone.filename}: {ex}")


def pull(ftp: _FtpWorker, remote_entry_path: str, local_root: str,
        on_progress: Optional[Callable[[str], None]] = None) -> Tuple[str, Dict[str, str], List[str]]:
    """Pull a .KSC or .KMP plus its whole dependency closure (every listed
    .KMP, every non-skipped zone's .KSF) into local_root.

    Returns (local_path_of_the_entry, {local_path: remote_path, ...}, failures).
    """
    remote_map: Dict[str, str] = {}
    failures: List[str] = []
    name = os.path.basename(remote_entry_path)
    slash = remote_entry_path.rfind("/")
    remote_dir = "/" if slash <= 0 else remote_entry_path[:slash]

    data = _download_one(ftp, remote_entry_path, local_root, remote_map, on_progress)

    if name.upper().endswith(".KSC"):
        collection = KscCollection.open(data)
        ksc_base_remote_dir = f"{remote_dir.rstrip('/')}/{os.path.splitext(name)[0]}"
        for kmp_name in collection.entries:
            if not kmp_name.upper().endswith(".KMP"):
                continue
            kmp_remote_path = f"{ksc_base_remote_dir}/{kmp_name}"
            try:
                kmp_bytes = _download_one(ftp, kmp_remote_path, local_root, remote_map, on_progress)
            except Exception as ex:
                log.warning("Sample pull: skipping unreachable multisample '%s': %s", kmp_remote_path, ex)
                if not is_ignorable_placeholder_kmp(kmp_name):
                    failures.append(f"{kmp_name}: {ex}")
                continue
            _pull_zones(ftp, kmp_bytes, kmp_remote_path, local_root, remote_map, on_progress, failures)
    elif name.upper().endswith(".KMP"):
        _pull_zones(ftp, data, remote_entry_path, local_root, remote_map, on_progress, failures)

    return local_path_for(local_root, remote_entry_path), remote_map, failures


def push_closure(ftp: _FtpWorker, local_ksc_path: str, collection: KscCollection,
                 remote_dest_dir: str,
                 on_progress: Optional[Callable[[str], None]] = None,
                 only_ksf_paths: Optional[set] = None) -> List[str]:
    """Push a local collection to remote_dest_dir: always the .KSC (+
    _UserBank.KSC sibling if present) and every listed .KMP — both are small
    text/metadata files, safe to always resync — but a zone's .KSF is only
    re-uploaded when `only_ksf_paths` is None (push everything) or the
    zone's local path is IN that set.

    `only_ksf_paths` exists so a caller that only edited a couple of samples
    doesn't re-transfer every other multi-megabyte sample in the collection
    untouched, and — more importantly — never re-uploads a header-only .KSF
    this app never wrote in the first place (kronosology doc §3.3: there is
    no disk-only signal that proves a header-only file is a safe "just a
    link" rather than data loss in progress; the only safe rule is to never
    push one this app didn't itself just create as a deliberate stub via
    set_stub_target)."""
    failures: List[str] = []
    ksc_name = os.path.basename(local_ksc_path)
    remote_dest_trimmed = remote_dest_dir.rstrip("/")
    ksc_remote_path = f"{remote_dest_trimmed}/{ksc_name}"

    _upload_one(ftp, local_ksc_path, ksc_remote_path, on_progress, failures)

    # _UserBank.KSC — a save already wrote this sibling locally if it exists;
    # pushing just uploads whatever's there. Missing is not a failure (an
    # older collection saved before this existed, or a collection with no
    # save-with-userbank step taken).
    user_bank_local_path = os.path.join(
        os.path.dirname(local_ksc_path),
        os.path.splitext(ksc_name)[0] + "_UserBank.KSC")
    if os.path.isfile(user_bank_local_path):
        user_bank_remote_path = f"{remote_dest_trimmed}/{os.path.basename(user_bank_local_path)}"
        _upload_one(ftp, user_bank_local_path, user_bank_remote_path, on_progress, failures)

    content_dir = os.path.join(os.path.dirname(local_ksc_path), os.path.splitext(ksc_name)[0])
    ksc_base_remote_dir = f"{remote_dest_trimmed}/{os.path.splitext(ksc_name)[0]}"
    for entry in collection.entries:
        if not entry.upper().endswith(".KMP"):
            continue
        kmp_local_path = os.path.join(content_dir, entry)
        if not os.path.isfile(kmp_local_path):
            if not is_ignorable_placeholder_kmp(entry):
                failures.append(f"{entry}: not found locally")
            continue
        kmp_remote_path = f"{ksc_base_remote_dir}/{entry}"
        _upload_one(ftp, kmp_local_path, kmp_remote_path, on_progress, failures)

        try:
            with open(kmp_local_path, "rb") as f:
                m = KmpMultisample.open(f.read())
        except Exception as ex:
            failures.append(f"{entry}: couldn't read to find its zones ({ex})")
            continue
        if m is None:
            failures.append(f"{entry}: not a recognizable .KMP")
            continue

        kmp_base_remote_dir = kmp_remote_path[:-4]  # strip ".KMP"
        for zone in m.zones:
            if zone.is_skipped:
                continue
            ksf_local_path = zone.ksf_path(kmp_local_path)
            if only_ksf_paths is not None and ksf_local_path not in only_ksf_paths:
                continue
            if not os.path.isfile(ksf_local_path):
                failures.append(f"{zone.filename}: not found locally")
                continue
            ksf_remote_path = f"{kmp_base_remote_dir}/{zone.filename}"
            _upload_one(ftp, ksf_local_path, ksf_remote_path, on_progress, failures)

    return failures
