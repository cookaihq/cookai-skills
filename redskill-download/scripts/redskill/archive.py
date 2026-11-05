import hashlib
import re
import stat
import unicodedata
import zipfile
from pathlib import Path

from .errors import DownloadError

MAX_FILES = 20000
MAX_UNPACKED = 512 * 1024 * 1024
MAX_MEMBER = 100 * 1024 * 1024


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized(name):
    return unicodedata.normalize("NFC", name.rstrip("/")).casefold()


def inspect_zip(path):
    if Path(path).stat().st_size > 100 * 1024 * 1024:
        raise DownloadError("invalid_archive", "ZIP exceeds 100 MiB")
    archive = zipfile.ZipFile(path)
    try:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_FILES:
            raise DownloadError("invalid_archive", "Archive is empty or has too many members")
        seen, files, total = set(), set(), 0
        for entry in infos:
            if entry.orig_filename != entry.filename:
                raise DownloadError("invalid_archive", "ZIP library normalized an unsafe member name")
            name = entry.filename.rstrip("/")
            parts = name.split("/")
            if (not name or "\\" in name or len(name.encode("utf-8")) > 1024 or len(parts) > 32
                    or any(part in ("", ".", "..") or part.endswith((".", " "))
                           or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                           or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?", part)
                           for part in parts)):
                raise DownloadError("invalid_archive", "Archive contains an unsafe or non-portable path", member=entry.filename)
            canonical = normalized(name)
            if canonical in seen:
                raise DownloadError("invalid_archive", "Archive paths collide across supported platforms", member=name)
            seen.add(canonical)
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR) != entry.is_dir() and kind != 0:
                raise DownloadError("invalid_archive", "Archive contains a link or special file", member=name)
            if entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise DownloadError("invalid_archive", "Encrypted or unsupported ZIP member", member=name)
            if entry.file_size > MAX_MEMBER:
                raise DownloadError("invalid_archive", "Archive member exceeds 100 MiB", member=name)
            total += entry.file_size
            if total > MAX_UNPACKED:
                raise DownloadError("invalid_archive", "Archive expands beyond 512 MiB")
            if not entry.is_dir():
                files.add(canonical)
        for entry in infos:
            parts = normalized(entry.filename).split("/")
            if any("/".join(parts[:index]) in files for index in range(1, len(parts))):
                raise DownloadError("invalid_archive", "File/directory path collision")
        return archive
    except Exception:
        archive.close()
        raise


def extract(path, destination):
    destination = Path(destination)
    destination.mkdir()
    members = []
    with inspect_zip(path) as archive:
        for entry in archive.infolist():
            target = destination.joinpath(*entry.filename.rstrip("/").split("/"))
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with archive.open(entry) as source, target.open("xb") as output:
                while chunk := source.read(65536):
                    size += len(chunk)
                    if size > entry.file_size or size > MAX_MEMBER:
                        raise DownloadError("invalid_archive", "Expanded size differs from the ZIP header")
                    digest.update(chunk)
                    output.write(chunk)
            if size != entry.file_size:
                raise DownloadError("invalid_archive", "Incomplete ZIP member")
            if entry.external_attr >> 16 & 0o111:
                target.chmod(target.stat().st_mode | 0o111)
            members.append({"path": entry.filename, "bytes": size, "sha256": digest.hexdigest()})
    return members


def unchanged(folder, expected_hash):
    """Verify against the original ZIP, not against a mutable inventory JSON."""
    folder = Path(folder)
    try:
        bundle, tree = folder / "bundle.zip", folder / "skill"
        if (folder.is_symlink() or folder.is_junction() or bundle.is_symlink()
                or tree.is_symlink() or tree.is_junction() or not tree.is_dir()
                or sha256(bundle) != expected_hash):
            return False
        expected, expected_dirs = {}, set()
        with inspect_zip(bundle) as archive:
            for info in archive.infolist():
                relative = info.filename.rstrip("/")
                expected_dirs.update(parent.as_posix() for parent in Path(relative).parents if str(parent) != ".")
                if info.is_dir():
                    expected_dirs.add(relative)
                    continue
                with archive.open(info) as source:
                    expected[info.filename] = hashlib.file_digest(source, "sha256").hexdigest()
        actual, actual_dirs = {}, set()
        for path in tree.rglob("*"):
            if path.is_symlink() or path.is_junction():
                return False
            if path.is_file():
                actual[path.relative_to(tree).as_posix()] = sha256(path)
            elif path.is_dir():
                actual_dirs.add(path.relative_to(tree).as_posix())
        return actual == expected and actual_dirs == expected_dirs
    except (OSError, ValueError, zipfile.BadZipFile, DownloadError):
        return False
