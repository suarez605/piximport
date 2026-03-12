# AGENTS.md — piximport

Guide for AI agents (Copilot, Claude, Cursor, etc.) working in this repository.

---

## Project purpose

`piximport` is a Python 3.11 CLI tool that imports photos from SD cards to
`~/Pictures`, automatically organising them by EXIF date and camera make.
The only external dependency is `questionary` (interactive terminal selector).

---

## Repository structure

```
piximport/
├── src/
│   └── piximport/
│       ├── __init__.py     # Main module (all production code)
│       └── __main__.py     # Enables `python -m piximport`
├── tests.py                # Unit tests using unittest (stdlib)
├── pyproject.toml          # Package metadata and CLI entry point
├── requirements.txt        # Pinned dependencies for the development environment
├── Formula/
│   └── piximport.rb        # Homebrew formula (requires updating sha256)
├── README.md
├── .gitignore
└── AGENTS.md               # This file
```

---

## Code conventions

- **Python 3.11+** — use type hints, `from __future__ import annotations`.
- **Minimal dependencies** — only `questionary` as external; the rest is stdlib
  (`struct`, `pathlib`, `shutil`, `subprocess`, `os`, `datetime`, `io`,
  `collections`, `unittest`).
- **Small, documented functions** — every public function has a docstring
  with a description, Args and Returns.
- **English names** for variables, functions and classes. Comments and
  docstrings must be in English and consistent within each function.
- **`frozenset` for extension sets** — immutable with O(1) lookup.
- **`NamedTuple`** for read-only data structures (`PhotoInfo`, `CopyResult`).

---

## Main module structure (`src/piximport/__init__.py`)

The module is organised into clearly delimited sections with separator comments.
When modifying it, respect this order:

1. **Global constants** — extensions, paths, special names.
2. **Data types** — `NamedTuple` used throughout the module.
3. **Media detection** — `list_external_volumes`, `display_volume_menu`.
4. **EXIF parser** — private `_parse_*` functions, `_read_ifd`, `_build_result`,
   and the public function `read_exif`.
5. **Classification and scanning** — `classify_file`, `scan_volume`, `_scan_dir`.
6. **Interactive selector** — `_group_by_date`, `select_photos`.
7. **Destination paths** — `build_dest_path`, `_ensure_camera_subdirs`.
8. **Collisions** — `resolve_collision`.
9. **Copy engine** — `copy_photos`.
10. **Summary** — `print_summary`.
11. **Entry point** — `main`, `if __name__ == "__main__"` block.

---

## EXIF parser — important rules

The EXIF parser is the most delicate component. When modifying it:

- **JPEG**: look for the APP1 segment (`0xFF 0xE1`) with magic `"Exif\x00\x00"`.
  If the segment is XMP or another type, continue to the next segment.
- **TIFF-based** (ARW, NEF, CR2, CR3, DNG, ORF, RW2): read the first 2 bytes
  to determine endianness (`II` = little-endian, `MM` = big-endian), verify
  magic 42, then traverse IFD0 and the underlying EXIF IFD.
- **RAF** (Fujifilm): read 92 header bytes, verify magic
  `"FUJIFILMCCD-RAW "` (16 bytes), read jpeg_offset from `0x54` and
  jpeg_length from `0x58` (both big-endian uint32), then parse the embedded
  JPEG with `_parse_jpeg_exif`.
- Tags of interest: `Make = 0x010F`, `ExifIFD pointer = 0x8769`,
  `DateTimeOriginal = 0x9003`.
- **Never raise exceptions to the caller**: any parser error returns
  `(UNKNOWN_CAMERA, None)`. Exceptions are caught in `read_exif`.

---

## Interactive selector — rules

- `_group_by_date(photos)` returns `{year: {MM-DD: [PhotoInfo]}}` sorted
  chronologically.
- `select_photos(photos)` uses `questionary.checkbox` with all days
  pre-checked. Each item has value `"MM-DD"` and shows the year in the label.
- If the user cancels (Ctrl+C), `ask()` returns `None` → `select_photos`
  returns `[]`.
- The same `MM-DD` can exist in different years; filtering combines
  `(year, MM-DD)` to avoid false positives.

---

## Tests

```bash
# With the virtual environment activated:
source env/bin/activate
python -m unittest tests -v

# Or directly:
env/bin/python3.11 -m unittest tests -v
```

- EXIF tests use synthetic binary data built with `struct`
  (no real photos required).
- `select_photos` tests use `unittest.mock.patch` on
  `questionary.checkbox` to simulate user responses.
- `_build_tiff_block()`, `_build_jpeg_with_exif()` and `_build_raf_with_exif()`
  are helpers in `tests.py` that generate valid bytes for each format.
- When adding support for a new RAW format, also add:
  1. The extension to `RAW_EXTENSIONS` in `__init__.py`.
  2. A `test_raw_<ext>` test in `TestClassifyFile`.
  3. If the format has its own header, a test in a dedicated class.

---

## Installation and packaging

### Local development

```bash
python3.11 -m venv env
source env/bin/activate
pip install -e .
piximport          # command available globally in the venv
```

### Distribution with pip / PyPI

```bash
pip install build
python -m build         # generates dist/*.whl and dist/*.tar.gz
pip install dist/piximport-*.whl
```

### Distribution with pipx (recommended for end users)

```bash
pipx install piximport        # installs in isolated env, exposes the command
pipx upgrade piximport
pipx uninstall piximport

# Or directly from GitHub:
pipx install git+https://github.com/suarez605/piximport.git@v1.0.0
```

---

## CLI flow

```
main()
  └── list_external_volumes()     → detects external /Volumes/*
  └── display_volume_menu()       → questionary selector, returns Path or None
  └── scan_volume()               → recursive os.scandir → sorted PhotoInfo list
        └── classify_file()       → extension → "SOOC" | "RAW" | None
        └── read_exif()           → (make, date) with mtime fallback
  └── select_photos()             → checkbox by year/day, returns filtered list
        └── _group_by_date()      → {year: {MM-DD: [PhotoInfo]}}
  └── copy_photos()               → iterates selected PhotoInfo
        └── build_dest_path()     → creates dirs, returns destination Path
        └── resolve_collision()   → skip / rename with _N
        └── shutil.copy2()        → copy with filesystem metadata
  └── print_summary()             → statistics table
```

---

## Commit conventions

Use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` new feature
- `fix:` bug fix
- `test:` add or fix tests
- `refactor:` refactoring with no behaviour change
- `docs:` documentation only
- `chore:` maintenance tasks (gitignore, CI, packaging, etc.)

---

## Adding support for a new RAW format

1. Add the extension (lowercase) to `RAW_EXTENSIONS` in `__init__.py`.
2. If the format is TIFF-based, no parser changes are needed (already works).
3. If the format has its own header:
   - Add a branch in `read_exif()` for the new extension.
   - Implement `_parse_<format>(fh)` following the pattern of `_parse_raf`.
   - Add tests in `tests.py`.

---

## Adding support for videos or sidecar files

Currently the tool ignores everything that is not SOOC or RAW. To add videos:

1. Add the extensions to a new `VIDEO_EXTENSIONS` set.
2. Update `classify_file()` to return `"VIDEO"`.
3. Update `CAMERA_SUBDIRS` if a dedicated subfolder is wanted.
4. Add corresponding tests.
