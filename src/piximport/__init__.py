#!/usr/bin/env python3.11
"""
piximport — CLI tool to import photos from SD cards to ~/Pictures.

Destination structure:
    ~/Pictures/<YEAR>/<MM-DD>/<MAKE>/<SOOC|RAW|EDITED>/

    The year and date come from each photo's EXIF metadata
    (DateTimeOriginal). If no EXIF is found, the file's modification
    time is used as a fallback.

Supported file types:
    SOOC  : .jpg .jpeg .heif .heic .hif
    RAW   : .arw .raf .nef .cr2 .cr3 .dng .orf .rw2

Usage:
    python3.11 -m piximport

Requirements:
    Python 3.11+
    questionary  (pip install questionary)
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import questionary
from questionary import Choice

# ---------------------------------------------------------------------------
# Global constants
# ---------------------------------------------------------------------------

PICTURES_ROOT = Path.home() / "Pictures"

# Classified extensions (lowercase)
SOOC_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".heif", ".heic", ".hif"})
RAW_EXTENSIONS: frozenset[str] = frozenset(
    {".arw", ".raf", ".nef", ".cr2", ".cr3", ".dng", ".orf", ".rw2"}
)
ALL_EXTENSIONS: frozenset[str] = SOOC_EXTENSIONS | RAW_EXTENSIONS

# Subfolder name used when the camera make cannot be determined
UNKNOWN_CAMERA = "NO_CAMERA"

# Subfolders always created inside each camera make directory
CAMERA_SUBDIRS = ("SOOC", "RAW", "EDITED")

# System volumes excluded from the selection menu
SYSTEM_VOLUMES: frozenset[str] = frozenset(
    {"Macintosh HD", "Data", "Preboot", "Recovery", "VM"}
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class PhotoInfo(NamedTuple):
    """Metadata extracted from a photo during the scan."""

    path: Path  # Absolute path to the original file on the SD card
    date: datetime  # Capture date (EXIF) or modification date (fallback)
    make: str  # Camera manufacturer, e.g. "SONY", "FUJIFILM"
    category: str  # "SOOC" or "RAW"


class CopyResult(NamedTuple):
    """Statistics from the copy process."""

    copied: int
    skipped: int
    errors: int


# ---------------------------------------------------------------------------
# External media detection (macOS)
# ---------------------------------------------------------------------------


def list_external_volumes() -> list[Path]:
    """
    Returns a list of paths to volumes mounted under /Volumes that do not
    belong to the Mac's internal system.

    Uses `diskutil info` to identify internal/system volumes and exclude
    them. Falls back to filtering by known system volume names.

    Returns:
        List of Path objects pointing to external volume mount points,
        sorted alphabetically.
    """
    volumes_root = Path("/Volumes")
    if not volumes_root.exists():
        return []

    external: list[Path] = []
    for entry in sorted(volumes_root.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name in SYSTEM_VOLUMES:
            continue
        if _is_internal_volume(entry):
            continue
        external.append(entry)

    return external


def _is_internal_volume(mount_point: Path) -> bool:
    """
    Queries `diskutil info` to determine whether a volume is internal.

    Args:
        mount_point: Path to the volume's mount point.

    Returns:
        True if the volume is internal or a system volume, False if external.
    """
    try:
        result = subprocess.run(
            ["diskutil", "info", str(mount_point)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout.lower()
        if (
            "internal:                  yes" in output
            or "internal:                 yes" in output
        ):
            return True
        if "protocol:               apple fabric" in output:
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def display_volume_menu(volumes: list[Path]) -> Path | None:
    """
    Shows an interactive menu listing available external volumes and
    returns the one chosen by the user.

    Args:
        volumes: List of paths to external volumes.

    Returns:
        Path to the selected volume, or None if the user cancels.
    """
    print(r"""
  ██████╗ ██╗██╗  ██╗██╗███╗   ███╗██████╗  ██████╗ ██████╗ ████████╗
  ██╔══██╗██║╚██╗██╔╝██║████╗ ████║██╔══██╗██╔═══██╗██╔══██╗╚══██╔══╝
  ██████╔╝██║ ╚███╔╝ ██║██╔████╔██║██████╔╝██║   ██║██████╔╝   ██║
  ██╔═══╝ ██║ ██╔██╗ ██║██║╚██╔╝██║██╔═══╝ ██║   ██║██╔══██╗   ██║
  ██║     ██║██╔╝ ██╗██║██║ ╚═╝ ██║██║     ╚██████╔╝██║  ██║   ██║
  ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝╚═╝     ╚═╝╚═╝      ╚═════╝ ╚═╝  ╚═╝   ╚═╝
""")

    if not volumes:
        print("  No SD cards or external volumes found.")
        print("  Connect an SD card and try again.\n")
        return None

    choices = [
        Choice(
            title=f"{vol.name:<22} {_get_volume_size(vol):>9}   {vol}",
            value=vol,
        )
        for vol in volumes
    ]
    _EXIT = object()
    choices.append(Choice(title="Exit", value=_EXIT))

    selected = questionary.select(
        "Select the volume to import:",
        choices=choices,
    ).ask()

    if selected is None or selected is _EXIT:
        return None

    print(f"\n  Selected volume: {selected.name} ({selected})\n")
    return selected


def _get_volume_size(path: Path) -> str:
    """
    Returns the total size of a volume as a human-readable string (e.g. "64.0 GB").

    Args:
        path: Path to the mount point.

    Returns:
        Formatted size string, or "" if it cannot be determined.
    """
    try:
        stat = os.statvfs(path)
        return _format_bytes(stat.f_blocks * stat.f_frsize)
    except OSError:
        return ""


def _format_bytes(n: int) -> str:
    """Converts bytes to a human-readable string with the appropriate unit (B → TB)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# EXIF parser (no external dependencies)
# ---------------------------------------------------------------------------

# Relevant EXIF tags
_TAG_MAKE = 0x010F  # IFD0: camera manufacturer
_TAG_EXIF_IFD = 0x8769  # IFD0: pointer to the EXIF sub-IFD
_TAG_DATE_ORIGINAL = 0x9003  # EXIF IFD: original capture date and time

# Standard EXIF date format: "YYYY:MM:DD HH:MM:SS"
_EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"


def read_exif(file_path: Path) -> tuple[str, datetime | None]:
    """
    Extracts the camera make and capture date from an image file using
    only the Python standard library.

    Supports:
    - JPEG / HEIF / HIF  → searches for APP1 segment with EXIF block
    - TIFF-based RAW     → ARW, NEF, CR2, CR3, DNG, ORF, RW2
    - RAF (Fujifilm)     → reads the embedded JPEG in the RAF header

    Args:
        file_path: Path to the image file.

    Returns:
        Tuple (make, date).
        - make is UNKNOWN_CAMERA if it cannot be determined.
        - date is None if no EXIF is found (use mtime as external fallback).
    """
    suffix = file_path.suffix.lower()
    try:
        with open(file_path, "rb") as fh:
            if suffix == ".raf":
                return _parse_raf(fh)
            elif suffix in (".jpg", ".jpeg", ".heif", ".heic", ".hif"):
                return _parse_jpeg_exif(fh)
            else:
                # ARW, NEF, CR2, CR3, DNG, ORF, RW2 — all are TIFF-based
                return _parse_tiff_exif(fh)
    except (OSError, struct.error, ValueError, UnicodeDecodeError):
        return UNKNOWN_CAMERA, None


# — JPEG ——————————————————————————————————————————————————————————————————


def _parse_jpeg_exif(fh) -> tuple[str, datetime | None]:
    """
    Locates the APP1 segment with magic "Exif\\x00\\x00" in a JPEG stream
    and delegates parsing to the TIFF block reader.

    Ignores other APP1 segments (e.g. XMP) and continues searching.
    """
    if fh.read(2) != b"\xff\xd8":
        return UNKNOWN_CAMERA, None

    while True:
        marker = fh.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            break

        raw_len = fh.read(2)
        if len(raw_len) < 2:
            break
        seg_len = (
            struct.unpack(">H", raw_len)[0] - 2
        )  # length excludes the 2 length bytes themselves

        if marker[1] == 0xE1:  # APP1
            data = fh.read(seg_len)
            if data[:6] == b"Exif\x00\x00":
                return _parse_tiff_block(data[6:])
            # Another APP1 (XMP, etc.) → keep searching
        else:
            fh.seek(seg_len, 1)

    return UNKNOWN_CAMERA, None


# — TIFF / RAW ————————————————————————————————————————————————————————————


def _parse_tiff_exif(fh) -> tuple[str, datetime | None]:
    """Reads the entire file as a TIFF block (for TIFF-based RAW formats)."""
    return _parse_tiff_block(fh.read())


def _parse_tiff_block(data: bytes) -> tuple[str, datetime | None]:
    """
    Parses a TIFF block and extracts Make and DateTimeOriginal.

    Navigates IFD0 to find the Make tag (0x010F) and the EXIF IFD pointer
    (0x8769), then reads DateTimeOriginal (0x9003) from the EXIF IFD.

    Args:
        data: Bytes of the TIFF block (must start with "II" or "MM").

    Returns:
        Tuple (make, date).
    """
    if len(data) < 8:
        return UNKNOWN_CAMERA, None

    byte_order = data[:2]
    if byte_order == b"II":
        endian = "<"
    elif byte_order == b"MM":
        endian = ">"
    else:
        return UNKNOWN_CAMERA, None

    if struct.unpack_from(f"{endian}H", data, 2)[0] != 42:
        return UNKNOWN_CAMERA, None

    ifd0_offset = struct.unpack_from(f"{endian}I", data, 4)[0]
    make_raw, exif_ifd_offset = _read_ifd(
        data, ifd0_offset, endian, {_TAG_MAKE, _TAG_EXIF_IFD}
    )

    date_raw: str | None = None
    if exif_ifd_offset:
        date_raw, _ = _read_ifd(data, exif_ifd_offset, endian, {_TAG_DATE_ORIGINAL})

    return _build_result(make_raw, date_raw)


def _read_ifd(
    data: bytes,
    offset: int,
    endian: str,
    tags_wanted: set[int],
) -> tuple[str | None, int | None]:
    """
    Reads a TIFF IFD and extracts the values of the requested tags.

    Supports ASCII (2) and LONG (4) types. Extracts only the first ASCII
    tag and the first LONG tag found among the requested tags.

    Args:
        data: Bytes of the full TIFF block.
        offset: Start position of the IFD within data.
        endian: "<" for little-endian or ">" for big-endian.
        tags_wanted: Set of tag IDs to look for.

    Returns:
        Tuple (ascii_value_or_None, long_value_or_None).
    """
    if offset + 2 > len(data):
        return None, None

    num_entries = struct.unpack_from(f"{endian}H", data, offset)[0]
    if num_entries > 1000:  # sanity check against corrupt data
        return None, None

    ascii_val: str | None = None
    long_val: int | None = None
    pos = offset + 2

    for _ in range(num_entries):
        if pos + 12 > len(data):
            break

        tag, dtype, count = struct.unpack_from(f"{endian}HHI", data, pos)
        value_or_offset = struct.unpack_from(f"{endian}I", data, pos + 8)[0]

        if tag in tags_wanted:
            if dtype == 2:  # ASCII
                if count <= 4:
                    raw = data[pos + 8 : pos + 8 + count]
                else:
                    raw = data[value_or_offset : value_or_offset + count]
                ascii_val = (
                    raw.rstrip(b"\x00").decode("ascii", errors="replace").strip()
                )

            elif dtype == 4:  # LONG — pointer to a sub-IFD
                long_val = value_or_offset

        pos += 12

    return ascii_val, long_val


def _build_result(
    make_raw: str | None, date_raw: str | None
) -> tuple[str, datetime | None]:
    """
    Normalises the make and parses the date extracted from the IFD.

    Normalisation takes the first word in uppercase and strips trailing
    commas/periods (some manufacturers include them in the Make tag).

    Args:
        make_raw: Raw value of the Make tag, may be None.
        date_raw: Raw value of the DateTimeOriginal tag, may be None.

    Returns:
        Tuple (normalised_make, date_or_None).
    """
    if make_raw:
        make = make_raw.split()[0].upper().rstrip(",.")
    else:
        make = UNKNOWN_CAMERA

    date: datetime | None = None
    if date_raw:
        try:
            date = datetime.strptime(date_raw.strip(), _EXIF_DATE_FORMAT)
        except ValueError:
            pass

    return make, date


# — RAF (Fujifilm) ————————————————————————————————————————————————————————


def _parse_raf(fh) -> tuple[str, datetime | None]:
    """
    Parses Fujifilm RAF files by extracting the embedded JPEG.

    RAF header structure (big-endian offsets):
        0x00  16 bytes  magic "FUJIFILMCCD-RAW "
        0x10   4 bytes  format version
        0x14   8 bytes  camera number
        0x1C  32 bytes  model name
        0x3C   4 bytes  directory version
        0x40  20 bytes  reserved
        0x54   4 bytes  offset to embedded JPEG
        0x58   4 bytes  length of embedded JPEG

    The EXIF data lives inside the embedded JPEG, so parsing is
    delegated to _parse_jpeg_exif().
    """
    # 0x58 + 4 = 92 bytes minimum to read both fields
    header = fh.read(92)
    if len(header) < 92:
        return UNKNOWN_CAMERA, None

    if header[:16] != b"FUJIFILMCCD-RAW ":
        return UNKNOWN_CAMERA, None

    jpeg_offset = struct.unpack_from(">I", header, 0x54)[0]
    jpeg_length = struct.unpack_from(">I", header, 0x58)[0]

    if jpeg_offset == 0 or jpeg_length == 0:
        return UNKNOWN_CAMERA, None

    fh.seek(jpeg_offset)
    jpeg_data = fh.read(jpeg_length)

    import io

    return _parse_jpeg_exif(io.BytesIO(jpeg_data))


# ---------------------------------------------------------------------------
# File classification and volume scanning
# ---------------------------------------------------------------------------


def classify_file(path: Path) -> str | None:
    """
    Determines the import category of a file based on its extension.

    Args:
        path: Path to the file (only the extension is examined).

    Returns:
        "SOOC" for JPEG/HEIF/HIF, "RAW" for RAW formats, None if ignored.
    """
    suffix = path.suffix.lower()
    if suffix in SOOC_EXTENSIONS:
        return "SOOC"
    if suffix in RAW_EXTENSIONS:
        return "RAW"
    return None


def scan_volume(volume: Path) -> list[PhotoInfo]:
    """
    Recursively scans a volume and collects metadata for all importable
    photos found.

    Uses os.scandir instead of pathlib.rglob for better performance on
    large volumes with thousands of files.

    Args:
        volume: Root path of the volume to scan.

    Returns:
        List of PhotoInfo sorted by capture date (oldest first).
    """
    photos: list[PhotoInfo] = []
    print(f"  Scanning {volume} ...")
    _scan_dir(volume, photos)
    photos.sort(key=lambda p: p.date)
    print(f"  {len(photos)} photo(s) found.\n")
    return photos


def _scan_dir(directory: Path, results: list[PhotoInfo]) -> None:
    """
    Recursively scans a directory, accumulating photos in results.

    Ignores files and directories whose names start with "." (system/hidden).

    Args:
        directory: Directory to scan.
        results: List where found PhotoInfo objects are accumulated.
    """
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    _scan_dir(Path(entry.path), results)
                elif entry.is_file(follow_symlinks=False):
                    path = Path(entry.path)
                    category = classify_file(path)
                    if category is None:
                        continue

                    make, date = read_exif(path)

                    # Date fallback: use the file's mtime from the filesystem
                    if date is None:
                        date = datetime.fromtimestamp(entry.stat().st_mtime)

                    results.append(
                        PhotoInfo(path=path, date=date, make=make, category=category)
                    )
    except PermissionError:
        pass


# ---------------------------------------------------------------------------
# Interactive photo selector
# ---------------------------------------------------------------------------

# Type alias for grouping: year → {MM-DD → [PhotoInfo]}
_Groups = dict[int, dict[str, list[PhotoInfo]]]


def _group_by_date(photos: list[PhotoInfo]) -> _Groups:
    """
    Groups a list of photos by year and date (MM-DD).

    Args:
        photos: List of PhotoInfo to group.

    Returns:
        Dictionary {year: {MM-DD: [PhotoInfo, ...]}} sorted chronologically.
    """
    groups: _Groups = defaultdict(lambda: defaultdict(list))
    for photo in photos:
        day_key = photo.date.strftime("%m-%d")
        groups[photo.date.year][day_key].append(photo)
    # Convert to sorted dicts for predictable iteration
    return {year: dict(sorted(days.items())) for year, days in sorted(groups.items())}


def select_photos(photos: list[PhotoInfo]) -> list[PhotoInfo]:
    """
    Shows an interactive checkbox selector that lets the user choose
    which days to import, grouped by year.

    The selector displays:
    - One entry per year (as a non-selectable visual separator)
    - One entry per day within each year, with a photo count
      and the makes present on that day

    The user can check/uncheck individual days with Space and confirm
    with Enter. To deselect an entire year, simply uncheck all its days.

    Args:
        photos: Full list of scanned photos.

    Returns:
        Filtered list of PhotoInfo for the selected days.
        Returns an empty list if the user cancels (Ctrl+C).
    """
    groups = _group_by_date(photos)

    choices: list[Choice | questionary.Separator] = []

    for year, days in groups.items():
        year_total = sum(len(v) for v in days.values())
        choices.append(
            questionary.Separator(f"\n  ── {year}  ({year_total} photos) ──")
        )

        for day_key, day_photos in days.items():
            makes = sorted({p.make for p in day_photos})
            makes_str = ", ".join(makes)
            n = len(day_photos)
            label = f"{year} / {day_key}  —  {n} photo{'s' if n != 1 else ''}   [{makes_str}]"
            choices.append(Choice(title=label, value=day_key, checked=True))

    print()
    selected_keys: list[str] | None = questionary.checkbox(
        "Select days to import  (Space = check/uncheck, Enter = confirm):",
        choices=choices,
    ).ask()

    # ask() returns None if the user presses Ctrl+C
    if selected_keys is None:
        return []

    # Each Choice value is just "MM-DD"; the same MM-DD can exist in
    # different years, so we filter by combined (year, MM-DD)
    selected_set: set[tuple[int, str]] = set()
    for year, days in groups.items():
        for day_key in days:
            if day_key in selected_keys:
                selected_set.add((year, day_key))

    return [
        p for p in photos if (p.date.year, p.date.strftime("%m-%d")) in selected_set
    ]


# ---------------------------------------------------------------------------
# Destination path building
# ---------------------------------------------------------------------------


def build_dest_path(photo: PhotoInfo, dest_root: Path) -> Path:
    """
    Builds the destination path for a photo and creates any required
    directories if they do not already exist.

    The structure follows this scheme:
        <dest_root>/<YEAR>/<MM-DD>/<MAKE>/<SOOC|RAW|EDITED>/

    The year and date are derived exclusively from photo.date (EXIF date or
    mtime), never from the current system date.

    Args:
        photo: Photo metadata.
        dest_root: Root destination folder (e.g. ~/Pictures).

    Returns:
        Full path to the destination file (the file itself is not created).
    """
    year_dir = str(photo.date.year)
    date_dir = photo.date.strftime("%m-%d")
    make_dir = photo.make.upper()

    dest_dir = dest_root / year_dir / date_dir / make_dir / photo.category
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Ensure SOOC, RAW and EDITED always exist as siblings
    _ensure_camera_subdirs(dest_root / year_dir / date_dir / make_dir)

    return dest_dir / photo.path.name


def _ensure_camera_subdirs(camera_dir: Path) -> None:
    """
    Creates the SOOC, RAW and EDITED subfolders inside the camera make
    directory if they do not already exist.

    Args:
        camera_dir: Camera make directory (e.g. ~/Pictures/2026/01-15/SONY).
    """
    for subdir in CAMERA_SUBDIRS:
        (camera_dir / subdir).mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Filename collision handling
# ---------------------------------------------------------------------------


def resolve_collision(dest_path: Path, source_path: Path) -> Path:
    """
    Resolves filename collisions at the destination.

    Strategy:
    1. If the destination does not exist → return dest_path as-is.
    2. If the destination exists and has the same size as the source →
       assume duplicate, return the same path (the caller will skip it).
    3. If the destination exists with a different size → append a numeric
       suffix (_1, _2, ...) until a free name is found.

    Args:
        dest_path:   Proposed destination path.
        source_path: Path to the source file.

    Returns:
        Final path where the file should be copied.
    """
    if not dest_path.exists():
        return dest_path

    if dest_path.stat().st_size == source_path.stat().st_size:
        return dest_path  # Duplicate signal for the caller

    stem, suffix, parent = dest_path.stem, dest_path.suffix, dest_path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Copy engine
# ---------------------------------------------------------------------------


def copy_photos(photos: list[PhotoInfo], dest_root: Path) -> CopyResult:
    """
    Copies all photos to the destination tree while showing real-time progress.

    For each photo:
    - Builds the destination path with build_dest_path()
    - Resolves filename collisions with resolve_collision()
    - If the file already exists with the same size, skips it
    - Otherwise copies it, preserving filesystem metadata via shutil.copy2()

    Args:
        photos:    List of photos to copy (already filtered by the selector).
        dest_root: Root destination folder (e.g. ~/Pictures).

    Returns:
        CopyResult with the count of copied, skipped and errored files.
    """
    total = len(photos)
    copied = skipped = errors = 0
    width = len(str(total))

    for idx, photo in enumerate(photos, start=1):
        prefix = f"  [{idx:>{width}}/{total}]"
        try:
            proposed = build_dest_path(photo, dest_root)
            final = resolve_collision(proposed, photo.path)

            if final == proposed and proposed.exists():
                print(f"{prefix} SKIP    {photo.path.name}  (already exists)")
                skipped += 1
                continue

            shutil.copy2(photo.path, final)
            print(
                f"{prefix} COPY    {photo.path.name}  →  {final.relative_to(dest_root)}"
            )
            copied += 1

        except (OSError, shutil.Error) as exc:
            print(f"{prefix} ERROR   {photo.path.name}: {exc}")
            errors += 1

    return CopyResult(copied=copied, skipped=skipped, errors=errors)


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------


def print_summary(result: CopyResult, dest_root: Path) -> None:
    """
    Prints a table with statistics from the import process.

    Args:
        result:    Statistics returned by copy_photos().
        dest_root: Root destination folder used (reported to the user).
    """
    total = result.copied + result.skipped + result.errors
    print("\n╔══════════════════════════════════════════╗")
    print("║             Import complete              ║")
    print("╠══════════════════════════════════════════╣")
    print(f"║  Total processed  : {total:<22}║")
    print(f"║  Copied           : {result.copied:<22}║")
    print(f"║  Skipped (dup.)   : {result.skipped:<22}║")
    print(f"║  Errors           : {result.errors:<22}║")
    print(f"║  Destination      : {str(dest_root):<22}║")
    print("╚══════════════════════════════════════════╝\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """
    Main CLI function. Orchestrates the full workflow:
    detection → volume selection → scan → date filter → copy.

    Returns:
        Exit code: 0 success, 1 no photos/cancelled, 2 copy errors.
    """
    # 1. Detect external volumes
    volumes = list_external_volumes()

    # 2. Volume selection menu
    selected_volume = display_volume_menu(volumes)
    if selected_volume is None:
        print("  No changes. Goodbye.\n")
        return 0

    # 3. Scan the volume (photos are sorted by EXIF date)
    photos = scan_volume(selected_volume)
    if not photos:
        print("  No photos with supported formats found on the volume.\n")
        return 1

    # 4. Interactive selector: the user chooses which days to import
    photos = select_photos(photos)
    if not photos:
        print("\n  No selection. Nothing will be copied.\n")
        return 1

    # 5. Final confirmation with the actual selection count
    print(f"\n  {len(photos)} photo(s) selected → destination: {PICTURES_ROOT}\n")
    try:
        confirm = input("  Continue with import? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        return 0

    if confirm not in ("", "y", "yes"):
        print("  Operation cancelled.\n")
        return 0

    print()

    # 6. Copy photos
    result = copy_photos(photos, PICTURES_ROOT)

    # 7. Show summary
    print_summary(result, PICTURES_ROOT)

    return 0 if result.errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
