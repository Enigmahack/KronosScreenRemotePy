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

import ftplib
import logging
import os
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample
from Data.ksf_sample import KsfSample
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


def unsafe_name_reason(name: str) -> Optional[str]:
    """Why a NAME taken from a .KSC entry line or a .KMP zone's filename field can't be used as a single path
    component, or None when it can. Both formats only ever hold a bare file name (kronosology doc §1.5/§5: the
    folder is implied by the convention), so anything that could name another folder - "..", a slash or
    backslash, a drive prefix, a rooted path - is a crafted or corrupt manifest. Such a name must not steer a
    pull or push outside the collection folder, locally (os.path.join drops its first argument for a rooted
    name) or remotely (the FTP server follows "..")."""
    if not name or not name.strip():
        return "empty file name"
    if name in (".", ".."):
        return f"'{name}' is not a file name"
    if any(c in name for c in "/\\:"):
        return f"'{name}' names a folder or drive, not a plain file name"
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return f"{name!r} contains control characters"
    return None


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
    remote file. The one chokepoint every sample push goes through.

    The previous remote file is never deleted until the new one is in place:
    the staged file is renamed straight over it, and only a server that refuses
    to overwrite (a 5xx reply) gets the swap — old file renamed to a `.old`
    sibling, new one renamed in, `.old` deleted. If the swap fails the old file
    is renamed back; if even that fails the failure message names the `.old`
    file that still holds the previous version."""
    if on_progress:
        on_progress(f"Uploading {os.path.basename(local_path)}...")
    token = uuid.uuid4().hex[:8]
    part = f"{remote_path}.{token}.part"
    aside = f"{remote_path}.{token}.old"
    # Checked against the longer name, not `remote_path` — it's what's
    # actually sent to the server first.
    if max(len(part), len(aside)) > _MAX_REMOTE_PATH_LENGTH:
        failures.append(f"{os.path.basename(local_path)}: destination path too long "
                        f"({len(remote_path)} characters) — choose a shallower destination folder")
        return
    try:
        _ensure_remote_dir(ftp, os.path.dirname(part))
        ftp.upload(local_path, part)
        _promote(ftp, part, remote_path, aside)
    except Exception as ex:
        log.warning("Sample push: '%s' -> '%s' failed: %s", local_path, remote_path, ex)
        failures.append(f"{os.path.basename(local_path)}: {ex}")
        try:
            ftp.delete_file(part)
        except Exception:
            pass


def _promote(ftp: _FtpWorker, part: str, remote_path: str, aside: str) -> None:
    # Both instruments accept RNTO over an existing file and reject MLSD (Nautilus 2026-10-22, Kronos 2026-11-11).
    # The swap-aside branch below is therefore only a fallback; it still needs an end-to-end run, including the
    # .old cleanup, should a server ever refuse.
    try:
        ftp.rename(part, remote_path)  # already guards top-level-volume + path-length
        return
    except ftplib.error_perm:
        # The server refused (typically "file exists"). A connection drop or any other
        # error is not a refusal and propagates with the old file untouched.
        if not ftp.file_exists(remote_path):
            raise
    ftp.rename(remote_path, aside)
    try:
        ftp.rename(part, remote_path)
    except Exception as ex:
        # The reply may have been lost after the new file landed; renaming the old one back over it would
        # silently throw the new bytes away, so only put the old file back when the final path is verifiably empty.
        try:
            landed = ftp.file_exists(remote_path)
        except Exception:
            landed = None
        if landed is False:
            try:
                ftp.rename(aside, remote_path)
            except Exception:
                raise OSError(f"{ex}; the previous version is still on the instrument as {aside}") from ex
            raise
        if landed:
            raise OSError(f"{ex}; {remote_path} now holds a file (most likely the new one) and the previous "
                          f"version is kept as {aside}") from ex
        raise OSError(f"{ex}; the previous version is kept as {aside} - check whether {remote_path} exists") from ex
    try:
        ftp.delete_file(aside)
    except Exception:
        try:
            ftp.delete_file(aside)
        except Exception as ex:
            raise OSError(f"the new file is in place but the replaced one couldn't be deleted ({ex}); "
                          f"delete {aside} on the instrument") from ex


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
        failures.append(f"{os.path.basename(kmp_remote_path)}: not a complete .KMP, so its samples weren't pulled")
        return
    kmp_base_remote_dir = kmp_remote_path[:-4]  # strip ".KMP" (4 chars, caller already checked)
    for zone in m.zones:
        if zone.is_skipped:
            continue
        bad = unsafe_name_reason(zone.filename)
        if bad:
            failures.append(f"{os.path.basename(kmp_remote_path)}: zone sample not pulled - {bad}")
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
    .KMP, every non-skipped zone's .KSF, and every bare .KSF) into local_root.

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
        for entry in collection.entries:
            is_kmp = entry.upper().endswith(".KMP")
            if not is_kmp and not entry.upper().endswith(".KSF"):
                continue
            bad = unsafe_name_reason(entry)
            if bad:
                failures.append(f"{name}: collection entry not pulled - {bad}")
                continue
            entry_remote_path = f"{ksc_base_remote_dir}/{entry}"
            try:
                entry_bytes = _download_one(ftp, entry_remote_path, local_root, remote_map, on_progress)
            except Exception as ex:
                log.warning("Sample pull: unreachable collection entry '%s': %s", entry_remote_path, ex)
                if not (is_kmp and is_ignorable_placeholder_kmp(entry)):
                    failures.append(f"{entry}: {ex}")
                continue
            if is_kmp:
                _pull_zones(ftp, entry_bytes, entry_remote_path, local_root, remote_map, on_progress, failures)
    elif name.upper().endswith(".KMP"):
        _pull_zones(ftp, data, remote_entry_path, local_root, remote_map, on_progress, failures)

    return local_path_for(local_root, remote_entry_path), remote_map, failures


def push_closure(ftp: _FtpWorker, local_ksc_path: str, collection: KscCollection,
                 remote_dest_dir: str,
                 on_progress: Optional[Callable[[str], None]] = None,
                 only_ksf_paths: Optional[set] = None) -> List[str]:
    """Push a local collection to remote_dest_dir: the .KSC (+ _UserBank.KSC
    sibling if present) and every listed .KMP — both are small text/metadata
    files, safe to always resync — but a zone's .KSF is only re-uploaded when
    `only_ksf_paths` is None (push everything) or the zone's local path is IN
    that set. Bare .KSF entries follow the same filter.

    Upload order is child before parent: every .KSF first, then each .KMP,
    then the .KSC and _UserBank.KSC last. A parent is only uploaded when every
    child of it that this push was going to send actually landed; otherwise it
    is skipped and reported, so the instrument never gets a new .KMP/.KSC that
    points at a sample/multisample which failed or was missing locally. A child
    left out by `only_ksf_paths` is assumed to be on the instrument already and
    doesn't block; neither does a missing NEWMS placeholder .KMP.

    `only_ksf_paths` exists so a caller that only edited a couple of samples
    doesn't re-transfer every other multi-megabyte sample in the collection
    untouched, and — more importantly — never re-uploads a header-only .KSF
    this app never wrote in the first place (kronosology doc §3.3: there is
    no disk-only signal that proves a header-only file is a safe "just a
    link" rather than data loss in progress; the only safe rule is to never
    push one this app didn't itself just create as a deliberate stub via
    set_stub_target).

    Every name taken from the .KSC/.KMP is checked first (`unsafe_name_reason`):
    one that could reach outside the collection folder, locally or on the
    instrument, refuses the whole push before any byte is sent."""
    failures: List[str] = []
    ksc_name = os.path.basename(local_ksc_path)
    remote_dest_trimmed = remote_dest_dir.rstrip("/")
    ksc_remote_path = f"{remote_dest_trimmed}/{ksc_name}"

    # Every file is checked before the first byte is sent: a truncated or unreadable local file must
    # never replace the instrument's good copy, and a crafted name must never be followed.
    invalid: List[str] = []
    leaves: Dict[str, str] = {}                     # remote .KSF -> local; each sent once
    kmps: List[Tuple[str, str, List[str]]] = []     # (local, remote, remote .KSF children it needs)
    ksc_children: List[str] = []                    # remote .KMP / bare .KSF paths the .KSC needs
    blocked: Dict[str, str] = {}                    # remote child -> why it can't land (e.g. missing locally)

    # _UserBank.KSC — a save already wrote this sibling locally if it exists;
    # pushing just uploads whatever's there. Missing is not a failure (an
    # older collection saved before this existed, or a collection with no
    # save-with-userbank step taken).
    user_bank_local_path = os.path.join(
        os.path.dirname(local_ksc_path),
        os.path.splitext(ksc_name)[0] + "_UserBank.KSC")
    tops: List[Tuple[str, str]] = [(local_ksc_path, ksc_remote_path)]
    if os.path.isfile(user_bank_local_path):
        tops.append((user_bank_local_path, f"{remote_dest_trimmed}/{os.path.basename(user_bank_local_path)}"))

    content_dir = os.path.join(os.path.dirname(local_ksc_path), os.path.splitext(ksc_name)[0])
    ksc_base_remote_dir = f"{remote_dest_trimmed}/{os.path.splitext(ksc_name)[0]}"
    for entry in collection.entries:
        is_ksf = entry.upper().endswith(".KSF")
        if not is_ksf and not entry.upper().endswith(".KMP"):
            continue
        bad = unsafe_name_reason(entry)
        if bad:
            invalid.append(f"{ksc_name}: collection entry refused - {bad}")
            continue
        try:
            entry_local_path = path_guard.ensure_under(content_dir, os.path.join(content_dir, entry), entry)
        except OSError as ex:
            invalid.append(f"{entry}: {ex}")
            continue
        entry_remote_path = f"{ksc_base_remote_dir}/{entry}"
        if is_ksf:
            if only_ksf_paths is not None and entry_local_path not in only_ksf_paths:
                continue
            ksc_children.append(entry_remote_path)
            if entry_remote_path in leaves or entry_remote_path in blocked:
                continue
            if not os.path.isfile(entry_local_path):
                failures.append(f"{entry}: not found locally")
                blocked[entry_remote_path] = "not found locally"
                continue
            leaves[entry_remote_path] = entry_local_path
            continue

        kmp_local_path = entry_local_path
        if not os.path.isfile(kmp_local_path):
            if not is_ignorable_placeholder_kmp(entry):
                failures.append(f"{entry}: not found locally")
                ksc_children.append(entry_remote_path)
                blocked[entry_remote_path] = "not found locally"
            continue
        ksc_children.append(entry_remote_path)

        try:
            with open(kmp_local_path, "rb") as f:
                m = KmpMultisample.open(f.read())
        except Exception as ex:
            invalid.append(f"{entry}: couldn't read to find its zones ({ex})")
            continue
        if m is None:
            invalid.append(f"{entry}: not a recognizable or complete .KMP")
            continue

        kmp_base_remote_dir = entry_remote_path[:-4]  # strip ".KMP"
        children: List[str] = []
        for zone in m.zones:
            if zone.is_skipped:
                continue
            bad = unsafe_name_reason(zone.filename)
            if bad:
                invalid.append(f"{entry}: zone sample refused - {bad}")
                continue
            try:
                ksf_local_path = zone.ksf_path(kmp_local_path)
            except OSError as ex:
                invalid.append(f"{entry}: {ex}")
                continue
            if only_ksf_paths is not None and ksf_local_path not in only_ksf_paths:
                continue
            ksf_remote_path = f"{kmp_base_remote_dir}/{zone.filename}"
            children.append(ksf_remote_path)
            if ksf_remote_path in leaves or ksf_remote_path in blocked:
                continue   # a repeated zone: sent once, its result counts for every reference
            if not os.path.isfile(ksf_local_path):
                failures.append(f"{zone.filename}: not found locally")
                blocked[ksf_remote_path] = "not found locally"
                continue
            leaves[ksf_remote_path] = ksf_local_path
        kmps.append((kmp_local_path, entry_remote_path, children))

    for local_path in leaves.values():
        name = os.path.basename(local_path)
        try:
            with open(local_path, "rb") as f:
                ok = KsfSample.open(f.read()) is not None
        except OSError as ex:
            invalid.append(f"{name}: couldn't be read ({ex})")
            continue
        if not ok:
            invalid.append(f"{name}: not a recognizable or complete .KSF (truncated?)")
    if invalid:
        log.warning("Sample push refused before uploading anything: %s", invalid)
        return failures + invalid + ["Nothing was uploaded; the instrument's copies were left untouched."]

    landed: Dict[str, bool] = {path: False for path in blocked}

    def send(local_path: str, remote_path: str) -> None:
        before = len(failures)
        _upload_one(ftp, local_path, remote_path, on_progress, failures)
        landed[remote_path] = len(failures) == before

    def not_landed(children: List[str]) -> str:
        return ", ".join(sorted({c.rsplit("/", 1)[-1] for c in children if not landed.get(c, False)}))

    # 1. Leaves: every .KSF (zone samples and bare collection entries alike).
    for remote_path, local_path in leaves.items():
        send(local_path, remote_path)

    # 2. Multisamples, each only once all of its own samples are in place.
    for kmp_local_path, kmp_remote_path, children in kmps:
        lost = not_landed(children)
        if lost:
            landed[kmp_remote_path] = False
            failures.append(f"{os.path.basename(kmp_local_path)}: skipped, not uploaded - its sample(s) {lost} "
                            f"didn't reach the instrument, so its previous copy there was left as it was")
            continue
        send(kmp_local_path, kmp_remote_path)

    # 3. The collection itself (+ _UserBank.KSC), last, and only when everything it lists landed.
    lost = not_landed(ksc_children)
    if lost:
        for local_path, _ in tops:
            failures.append(f"{os.path.basename(local_path)}: skipped, not uploaded - {lost} didn't reach the "
                            f"instrument, so its previous copy there was left as it was")
        return failures
    for local_path, remote_path in tops:
        send(local_path, remote_path)
    return failures


