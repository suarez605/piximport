#!/usr/bin/env python3.11
"""
tests.py — Tests unitarios para photo_importer.py

Cubre:
    - Clasificación de archivos por extensión
    - Parser EXIF: JPEG, TIFF-based RAW, RAF
    - Construcción de rutas de destino
    - Resolución de colisiones de nombre
    - Normalización del fabricante
    - Formato de bytes

Ejecutar:
    python3.11 tests.py
    python3.11 -m unittest tests -v
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

# Importar el módulo bajo prueba
import photo_importer as pi


# ---------------------------------------------------------------------------
# Helpers para construir bytes EXIF sintéticos
# ---------------------------------------------------------------------------

def _pack_ifd_entry(endian: str, tag: int, dtype: int, count: int, value: int) -> bytes:
    """Empaqueta una entrada IFD de 12 bytes."""
    return struct.pack(f"{endian}HHII", tag, dtype, count, value)


def _build_tiff_block(
    make: str,
    date_str: str,
    endian: str = "<",
) -> bytes:
    """
    Construye un bloque TIFF mínimo con los tags Make y DateTimeOriginal.

    Estructura:
        - 8 bytes de cabecera TIFF (byte order + magic + offset IFD0)
        - IFD0: num_entries(2) + entradas(12*n) + next_ifd(4)
        - IFD EXIF: num_entries(2) + entradas(12*n) + next_ifd(4)
        - Datos de cadenas (Make, Date)
    """
    # Las cadenas se almacenan al final del bloque
    make_bytes = make.encode("ascii") + b"\x00"
    date_bytes = date_str.encode("ascii") + b"\x00"

    # Cabecera TIFF: 8 bytes
    # IFD0: 2 + 2*12 + 4 = 30 bytes → starts at offset 8
    # EXIF IFD: 2 + 1*12 + 4 = 18 bytes
    # Strings start after both IFDs

    ifd0_offset = 8
    # IFD0 tiene 2 entries: Make (0x010F) y ExifIFD pointer (0x8769)
    ifd0_size = 2 + 2 * 12 + 4  # 30 bytes
    exif_ifd_offset = ifd0_offset + ifd0_size  # = 38

    # EXIF IFD tiene 1 entry: DateTimeOriginal (0x9003)
    exif_ifd_size = 2 + 1 * 12 + 4  # 18 bytes
    strings_offset = exif_ifd_offset + exif_ifd_size  # = 56

    make_offset = strings_offset
    date_offset = make_offset + len(make_bytes)

    # Cabecera TIFF
    bo_bytes = b"II" if endian == "<" else b"MM"
    header = bo_bytes + struct.pack(f"{endian}HI", 42, ifd0_offset)

    # IFD0
    ifd0_num = struct.pack(f"{endian}H", 2)
    entry_make = _pack_ifd_entry(endian, pi._TAG_MAKE, 2, len(make_bytes), make_offset)
    entry_exif = _pack_ifd_entry(endian, pi._TAG_EXIF_IFD, 4, 1, exif_ifd_offset)
    ifd0_next = struct.pack(f"{endian}I", 0)
    ifd0 = ifd0_num + entry_make + entry_exif + ifd0_next

    # EXIF IFD
    exif_num = struct.pack(f"{endian}H", 1)
    entry_date = _pack_ifd_entry(endian, pi._TAG_DATE_ORIGINAL, 2, len(date_bytes), date_offset)
    exif_next = struct.pack(f"{endian}I", 0)
    exif_ifd = exif_num + entry_date + exif_next

    return header + ifd0 + exif_ifd + make_bytes + date_bytes


def _build_jpeg_with_exif(make: str, date_str: str) -> bytes:
    """
    Construye un JPEG mínimo con un segmento APP1 que contiene EXIF.
    """
    tiff_block = _build_tiff_block(make, date_str)
    exif_payload = b"Exif\x00\x00" + tiff_block

    # APP1 segment: marker(2) + length(2, incluye los 2 bytes de longitud) + data
    segment_length = len(exif_payload) + 2  # +2 por los bytes de longitud
    app1 = b"\xff\xe1" + struct.pack(">H", segment_length) + exif_payload

    # SOI + APP1 + EOI (mínimo válido para el parser)
    return b"\xff\xd8" + app1 + b"\xff\xd9"


def _build_raf_with_exif(make: str, date_str: str) -> bytes:
    """
    Construye un RAF mínimo con un JPEG embebido que contiene EXIF.
    El parser RAF lee el JPEG desde el offset indicado en la cabecera.
    """
    # Cabecera RAF: 92 bytes (el parser lee hasta offset 0x58+4 = 92)
    # El JPEG embebido empieza inmediatamente después de la cabecera
    jpeg_data = _build_jpeg_with_exif(make, date_str)
    jpeg_offset = 92  # JPEG starts right after the 92-byte header
    jpeg_length = len(jpeg_data)

    header = bytearray(92)
    header[0:16] = b"FUJIFILMCCD-RAW "
    # El parser lee jpeg_offset desde offset 0x54 (84) y jpeg_length desde 0x58 (88)
    struct.pack_into(">I", header, 0x54, jpeg_offset)
    struct.pack_into(">I", header, 0x58, jpeg_length)

    return bytes(header) + jpeg_data


# ---------------------------------------------------------------------------
# Tests: clasificación de archivos
# ---------------------------------------------------------------------------

class TestClassifyFile(unittest.TestCase):
    """Tests para la función classify_file."""

    def test_sooc_jpg(self):
        self.assertEqual(pi.classify_file(Path("IMG_001.JPG")), "SOOC")

    def test_sooc_jpeg(self):
        self.assertEqual(pi.classify_file(Path("photo.jpeg")), "SOOC")

    def test_sooc_heif(self):
        self.assertEqual(pi.classify_file(Path("shot.heif")), "SOOC")

    def test_sooc_heic(self):
        self.assertEqual(pi.classify_file(Path("shot.heic")), "SOOC")

    def test_sooc_hif(self):
        self.assertEqual(pi.classify_file(Path("shot.hif")), "SOOC")

    def test_raw_arw(self):
        self.assertEqual(pi.classify_file(Path("DSC0001.ARW")), "RAW")

    def test_raw_raf(self):
        self.assertEqual(pi.classify_file(Path("DSCF0001.RAF")), "RAW")

    def test_raw_nef(self):
        self.assertEqual(pi.classify_file(Path("DSC_0001.NEF")), "RAW")

    def test_raw_cr2(self):
        self.assertEqual(pi.classify_file(Path("IMG_0001.CR2")), "RAW")

    def test_raw_cr3(self):
        self.assertEqual(pi.classify_file(Path("IMG_0001.CR3")), "RAW")

    def test_raw_dng(self):
        self.assertEqual(pi.classify_file(Path("file.DNG")), "RAW")

    def test_raw_orf(self):
        self.assertEqual(pi.classify_file(Path("PA000001.ORF")), "RAW")

    def test_raw_rw2(self):
        self.assertEqual(pi.classify_file(Path("P1000001.RW2")), "RAW")

    def test_unknown_xmp(self):
        self.assertIsNone(pi.classify_file(Path("file.xmp")))

    def test_unknown_thm(self):
        self.assertIsNone(pi.classify_file(Path("file.THM")))

    def test_unknown_mp4(self):
        self.assertIsNone(pi.classify_file(Path("video.mp4")))

    def test_no_extension(self):
        self.assertIsNone(pi.classify_file(Path("noextension")))


# ---------------------------------------------------------------------------
# Tests: parser EXIF — JPEG
# ---------------------------------------------------------------------------

class TestExifParserJPEG(unittest.TestCase):
    """Tests para el parser EXIF sobre archivos JPEG sintéticos."""

    def _parse(self, jpeg_bytes: bytes) -> tuple[str, datetime | None]:
        return pi._parse_jpeg_exif(io.BytesIO(jpeg_bytes))

    def test_make_extracted(self):
        data = _build_jpeg_with_exif("SONY", "2026:01:15 10:30:00")
        make, _ = self._parse(data)
        self.assertEqual(make, "SONY")

    def test_date_extracted(self):
        data = _build_jpeg_with_exif("SONY", "2026:01:15 10:30:00")
        _, date = self._parse(data)
        self.assertIsNotNone(date)
        self.assertEqual(date.year, 2026)
        self.assertEqual(date.month, 1)
        self.assertEqual(date.day, 15)

    def test_fujifilm_make_normalised(self):
        data = _build_jpeg_with_exif("FUJIFILM", "2025:06:20 08:00:00")
        make, _ = self._parse(data)
        self.assertEqual(make, "FUJIFILM")

    def test_make_with_trailing_comma(self):
        """Algunos fabricantes incluyen comas en el tag Make."""
        data = _build_jpeg_with_exif("NIKON,", "2024:03:01 12:00:00")
        make, _ = self._parse(data)
        self.assertEqual(make, "NIKON")

    def test_invalid_jpeg_header(self):
        make, date = self._parse(b"\x00\x00\x00\x00")
        self.assertEqual(make, pi.UNKNOWN_CAMERA)
        self.assertIsNone(date)

    def test_jpeg_no_exif(self):
        """JPEG sin segmento APP1 EXIF."""
        # SOI + APP0 JFIF + EOI
        app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
        data = b"\xff\xd8" + app0 + b"\xff\xd9"
        make, date = self._parse(data)
        self.assertEqual(make, pi.UNKNOWN_CAMERA)
        self.assertIsNone(date)


# ---------------------------------------------------------------------------
# Tests: parser EXIF — TIFF-based RAW
# ---------------------------------------------------------------------------

class TestExifParserTIFF(unittest.TestCase):
    """Tests para el parser EXIF sobre bloques TIFF (ARW, NEF, etc.)."""

    def _parse_block(self, make: str, date_str: str, endian: str = "<"):
        block = _build_tiff_block(make, date_str, endian)
        return pi._parse_tiff_block(block)

    def test_little_endian_make(self):
        make, _ = self._parse_block("SONY", "2026:01:15 10:30:00", "<")
        self.assertEqual(make, "SONY")

    def test_big_endian_make(self):
        make, _ = self._parse_block("NIKON", "2026:01:15 10:30:00", ">")
        self.assertEqual(make, "NIKON")

    def test_date_year(self):
        _, date = self._parse_block("CANON", "2025:11:20 16:45:00", "<")
        self.assertIsNotNone(date)
        self.assertEqual(date.year, 2025)
        self.assertEqual(date.month, 11)
        self.assertEqual(date.day, 20)

    def test_invalid_magic(self):
        """Bloque con magic number incorrecto."""
        bad_block = b"II" + struct.pack("<H", 99) + b"\x00" * 20
        make, date = pi._parse_tiff_block(bad_block)
        self.assertEqual(make, pi.UNKNOWN_CAMERA)
        self.assertIsNone(date)

    def test_empty_block(self):
        make, date = pi._parse_tiff_block(b"")
        self.assertEqual(make, pi.UNKNOWN_CAMERA)
        self.assertIsNone(date)

    def test_truncated_block(self):
        make, date = pi._parse_tiff_block(b"II\x2a\x00")
        self.assertEqual(make, pi.UNKNOWN_CAMERA)
        self.assertIsNone(date)


# ---------------------------------------------------------------------------
# Tests: parser EXIF — RAF (Fujifilm)
# ---------------------------------------------------------------------------

class TestExifParserRAF(unittest.TestCase):
    """Tests para el parser RAF de Fujifilm."""

    def _parse(self, raf_bytes: bytes):
        return pi._parse_raf(io.BytesIO(raf_bytes))

    def test_fujifilm_make(self):
        data = _build_raf_with_exif("FUJIFILM", "2026:03:10 09:00:00")
        make, _ = self._parse(data)
        self.assertEqual(make, "FUJIFILM")

    def test_fujifilm_date(self):
        data = _build_raf_with_exif("FUJIFILM", "2026:03:10 09:00:00")
        _, date = self._parse(data)
        self.assertIsNotNone(date)
        self.assertEqual(date.month, 3)
        self.assertEqual(date.day, 10)

    def test_invalid_magic(self):
        data = b"\x00" * 200
        make, date = self._parse(data)
        self.assertEqual(make, pi.UNKNOWN_CAMERA)
        self.assertIsNone(date)

    def test_truncated_header(self):
        data = b"FUJIFILMCCD-RAW " + b"\x00" * 10  # header too short
        make, date = self._parse(data)
        self.assertEqual(make, pi.UNKNOWN_CAMERA)
        self.assertIsNone(date)


# ---------------------------------------------------------------------------
# Tests: construcción de rutas de destino
# ---------------------------------------------------------------------------

class TestBuildDestPath(unittest.TestCase):
    """Tests para la función build_dest_path."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_photo(self, make: str, date: datetime, category: str, name: str) -> pi.PhotoInfo:
        return pi.PhotoInfo(
            path=Path(f"/fake/sd/{name}"),
            date=date,
            make=make,
            category=category,
        )

    def test_correct_year_dir(self):
        photo = self._make_photo("SONY", datetime(2026, 1, 15), "SOOC", "IMG_001.JPG")
        dest = pi.build_dest_path(photo, self.tmp)
        self.assertIn("2026", dest.parts)

    def test_correct_month_day_dir(self):
        photo = self._make_photo("SONY", datetime(2026, 1, 15), "SOOC", "IMG_001.JPG")
        dest = pi.build_dest_path(photo, self.tmp)
        self.assertIn("01-15", dest.parts)

    def test_correct_make_dir(self):
        photo = self._make_photo("fujifilm", datetime(2026, 5, 20), "RAW", "DSCF001.RAF")
        dest = pi.build_dest_path(photo, self.tmp)
        self.assertIn("FUJIFILM", dest.parts)

    def test_correct_category_dir(self):
        photo = self._make_photo("SONY", datetime(2026, 1, 15), "RAW", "DSC0001.ARW")
        dest = pi.build_dest_path(photo, self.tmp)
        self.assertIn("RAW", dest.parts)

    def test_filename_preserved(self):
        photo = self._make_photo("SONY", datetime(2026, 1, 15), "SOOC", "IMG_0042.JPG")
        dest = pi.build_dest_path(photo, self.tmp)
        self.assertEqual(dest.name, "IMG_0042.JPG")

    def test_camera_subdirs_created(self):
        """Verifica que SOOC, RAW y EDITED se crean dentro del dir del fabricante."""
        photo = self._make_photo("CANON", datetime(2026, 2, 10), "SOOC", "IMG_001.JPG")
        pi.build_dest_path(photo, self.tmp)
        camera_dir = self.tmp / "2026" / "02-10" / "CANON"
        for subdir in ("SOOC", "RAW", "EDITED"):
            self.assertTrue((camera_dir / subdir).is_dir(), f"Falta {subdir}")

    def test_unknown_camera_dir(self):
        photo = self._make_photo(pi.UNKNOWN_CAMERA, datetime(2026, 7, 4), "SOOC", "file.jpg")
        dest = pi.build_dest_path(photo, self.tmp)
        self.assertIn(pi.UNKNOWN_CAMERA, dest.parts)


# ---------------------------------------------------------------------------
# Tests: resolución de colisiones
# ---------------------------------------------------------------------------

class TestResolveCollision(unittest.TestCase):
    """Tests para la función resolve_collision."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, content: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(content)
        return p

    def test_no_collision(self):
        dest = self.tmp / "IMG_001.JPG"
        source = self._write("source.jpg", b"data")
        result = pi.resolve_collision(dest, source)
        self.assertEqual(result, dest)

    def test_same_size_treated_as_duplicate(self):
        """Mismo tamaño → se devuelve la misma ruta (señal de duplicado)."""
        content = b"same content"
        dest = self._write("IMG_001.JPG", content)
        source = self._write("source.jpg", content)
        result = pi.resolve_collision(dest, source)
        self.assertEqual(result, dest)

    def test_different_size_gets_suffix(self):
        """Distinto tamaño → nuevo nombre con sufijo _1."""
        dest = self._write("IMG_001.JPG", b"old content longer")
        source = self._write("source.jpg", b"new")
        result = pi.resolve_collision(dest, source)
        self.assertEqual(result.name, "IMG_001_1.JPG")

    def test_multiple_collisions_increment(self):
        """Si _1 también existe, debe probar _2, _3, etc."""
        self._write("IMG_001.JPG", b"old")
        self._write("IMG_001_1.JPG", b"also old")
        dest = self.tmp / "IMG_001.JPG"
        source = self._write("source.jpg", b"new different content")
        result = pi.resolve_collision(dest, source)
        self.assertEqual(result.name, "IMG_001_2.JPG")


# ---------------------------------------------------------------------------
# Tests: normalización del fabricante
# ---------------------------------------------------------------------------

class TestBuildResult(unittest.TestCase):
    """Tests para la función _build_result (normalización de make y fecha)."""

    def test_make_uppercased(self):
        make, _ = pi._build_result("Nikon Corporation", None)
        self.assertEqual(make, "NIKON")

    def test_make_trailing_comma(self):
        make, _ = pi._build_result("Canon,", None)
        self.assertEqual(make, "CANON")

    def test_make_none_returns_unknown(self):
        make, _ = pi._build_result(None, None)
        self.assertEqual(make, pi.UNKNOWN_CAMERA)

    def test_date_parsed(self):
        _, date = pi._build_result("SONY", "2026:01:15 10:30:00")
        self.assertIsNotNone(date)
        self.assertEqual(date, datetime(2026, 1, 15, 10, 30, 0))

    def test_invalid_date_returns_none(self):
        _, date = pi._build_result("SONY", "not-a-date")
        self.assertIsNone(date)

    def test_empty_date_returns_none(self):
        _, date = pi._build_result("SONY", "")
        self.assertIsNone(date)


# ---------------------------------------------------------------------------
# Tests: formato de bytes
# ---------------------------------------------------------------------------

class TestFormatBytes(unittest.TestCase):
    """Tests para la función _format_bytes."""

    def test_bytes(self):
        self.assertEqual(pi._format_bytes(500), "500.0 B")

    def test_kilobytes(self):
        self.assertEqual(pi._format_bytes(2048), "2.0 KB")

    def test_megabytes(self):
        self.assertEqual(pi._format_bytes(1024 * 1024), "1.0 MB")

    def test_gigabytes(self):
        self.assertEqual(pi._format_bytes(64 * 1024 ** 3), "64.0 GB")


# ---------------------------------------------------------------------------
# Tests: escaneo de volumen (con sistema de archivos temporal)
# ---------------------------------------------------------------------------

class TestScanVolume(unittest.TestCase):
    """Tests de integración para scan_volume con un volumen simulado."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _create_file(self, rel_path: str, content: bytes = b"\x00") -> Path:
        p = self.tmp / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def test_finds_jpeg(self):
        jpeg = _build_jpeg_with_exif("SONY", "2026:01:15 10:30:00")
        self._create_file("DCIM/100MSDCF/DSC00001.JPG", jpeg)
        photos = pi.scan_volume(self.tmp)
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0].category, "SOOC")

    def test_finds_raw(self):
        tiff = _build_tiff_block("SONY", "2026:01:15 10:30:00")
        self._create_file("DCIM/100MSDCF/DSC00001.ARW", tiff)
        photos = pi.scan_volume(self.tmp)
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0].category, "RAW")

    def test_ignores_unsupported(self):
        self._create_file("MISC/note.txt", b"hello")
        self._create_file("VIDEO/clip.mp4", b"\x00" * 100)
        photos = pi.scan_volume(self.tmp)
        self.assertEqual(len(photos), 0)

    def test_ignores_hidden_files(self):
        self._create_file(".Spotlight-V100/Store-V2/photo.jpg", b"\xff\xd8\xff\xd9")
        photos = pi.scan_volume(self.tmp)
        self.assertEqual(len(photos), 0)

    def test_mixed_files(self):
        jpeg = _build_jpeg_with_exif("CANON", "2026:02:10 08:00:00")
        tiff = _build_tiff_block("CANON", "2026:02:10 08:00:00")
        self._create_file("DCIM/IMG_001.JPG", jpeg)
        self._create_file("DCIM/IMG_001.CR2", tiff)
        self._create_file("DCIM/IMG_001.xmp", b"<?xml")
        photos = pi.scan_volume(self.tmp)
        self.assertEqual(len(photos), 2)

    def test_fallback_date_from_mtime(self):
        """Si no hay EXIF, la fecha debe venir del mtime del archivo."""
        self._create_file("DCIM/noexif.jpg", b"\xff\xd8\xff\xd9")  # JPEG vacío
        photos = pi.scan_volume(self.tmp)
        self.assertEqual(len(photos), 1)
        self.assertIsNotNone(photos[0].date)

    def test_make_extracted_from_jpeg(self):
        jpeg = _build_jpeg_with_exif("OLYMPUS", "2026:04:22 12:00:00")
        self._create_file("DCIM/PA220001.JPG", jpeg)
        photos = pi.scan_volume(self.tmp)
        self.assertEqual(photos[0].make, "OLYMPUS")


# ---------------------------------------------------------------------------
# Tests: copia de fotos (integración)
# ---------------------------------------------------------------------------

class TestCopyPhotos(unittest.TestCase):
    """Tests de integración para copy_photos."""

    def setUp(self):
        self.src_tmp = Path(tempfile.mkdtemp())
        self.dst_tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.src_tmp, ignore_errors=True)
        shutil.rmtree(self.dst_tmp, ignore_errors=True)

    def _photo(self, name: str, make: str, date: datetime, category: str) -> pi.PhotoInfo:
        p = self.src_tmp / name
        p.write_bytes(b"fake photo content " + name.encode())
        return pi.PhotoInfo(path=p, date=date, make=make, category=category)

    def test_copy_creates_file(self):
        photo = self._photo("DSC00001.ARW", "SONY", datetime(2026, 1, 15), "RAW")
        result = pi.copy_photos([photo], self.dst_tmp)
        self.assertEqual(result.copied, 1)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.errors, 0)

    def test_duplicate_is_skipped(self):
        photo = self._photo("DSC00001.ARW", "SONY", datetime(2026, 1, 15), "RAW")
        # Primera copia
        pi.copy_photos([photo], self.dst_tmp)
        # Segunda copia del mismo archivo
        result = pi.copy_photos([photo], self.dst_tmp)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.copied, 0)

    def test_error_counted_on_missing_source(self):
        photo = pi.PhotoInfo(
            path=Path("/nonexistent/ghost.jpg"),
            date=datetime(2026, 1, 1),
            make="SONY",
            category="SOOC",
        )
        result = pi.copy_photos([photo], self.dst_tmp)
        self.assertEqual(result.errors, 1)


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# Tests: agrupamiento por fecha
# ---------------------------------------------------------------------------

class TestGroupByDate(unittest.TestCase):
    """Tests para la función _group_by_date."""

    def _photo(self, year: int, month: int, day: int, make: str = "SONY") -> pi.PhotoInfo:
        return pi.PhotoInfo(
            path=Path(f"/sd/{year}{month:02d}{day:02d}.jpg"),
            date=datetime(year, month, day, 10, 0, 0),
            make=make,
            category="SOOC",
        )

    def test_groups_by_year(self):
        photos = [self._photo(2025, 1, 10), self._photo(2026, 3, 8)]
        groups = pi._group_by_date(photos)
        self.assertIn(2025, groups)
        self.assertIn(2026, groups)

    def test_groups_by_day_within_year(self):
        photos = [
            self._photo(2025, 1, 10),
            self._photo(2025, 1, 11),
            self._photo(2025, 1, 10),
        ]
        groups = pi._group_by_date(photos)
        self.assertIn("01-10", groups[2025])
        self.assertIn("01-11", groups[2025])
        self.assertEqual(len(groups[2025]["01-10"]), 2)
        self.assertEqual(len(groups[2025]["01-11"]), 1)

    def test_sorted_years(self):
        photos = [self._photo(2026, 1, 1), self._photo(2024, 1, 1), self._photo(2025, 1, 1)]
        groups = pi._group_by_date(photos)
        self.assertEqual(list(groups.keys()), [2024, 2025, 2026])

    def test_sorted_days_within_year(self):
        photos = [self._photo(2025, 3, 15), self._photo(2025, 1, 5), self._photo(2025, 2, 20)]
        groups = pi._group_by_date(photos)
        days = list(groups[2025].keys())
        self.assertEqual(days, sorted(days))

    def test_empty_input(self):
        self.assertEqual(pi._group_by_date([]), {})


# ---------------------------------------------------------------------------
# Tests: selector interactivo de fotos
# ---------------------------------------------------------------------------

class TestSelectPhotos(unittest.TestCase):
    """
    Tests para select_photos(), simulando respuestas del usuario mediante
    unittest.mock.patch sobre questionary.checkbox.

    questionary.checkbox().ask() devuelve:
    - lista de valores de las Choice marcadas → usuario confirmó
    - None                                   → usuario canceló (Ctrl+C)
    """

    def _make_photos(self) -> list[pi.PhotoInfo]:
        """Crea un conjunto de fotos de prueba con 3 días distintos en 2 años."""
        base = [
            # 2025
            pi.PhotoInfo(Path("/sd/a.jpg"), datetime(2025, 1, 10), "SONY",     "SOOC"),
            pi.PhotoInfo(Path("/sd/b.arw"), datetime(2025, 1, 10), "SONY",     "RAW"),
            pi.PhotoInfo(Path("/sd/c.jpg"), datetime(2025, 6, 20), "FUJIFILM", "SOOC"),
            # 2026
            pi.PhotoInfo(Path("/sd/d.jpg"), datetime(2026, 3,  8), "CANON",    "SOOC"),
            pi.PhotoInfo(Path("/sd/e.cr3"), datetime(2026, 3,  8), "CANON",    "RAW"),
        ]
        return base

    def test_all_selected_returns_all(self):
        """Cuando el usuario selecciona todos los días, vuelven todas las fotos."""
        photos = self._make_photos()
        # Los valores de Choice son "MM-DD"
        with patch("questionary.checkbox") as mock_cb:
            mock_cb.return_value.ask.return_value = ["01-10", "06-20", "03-08"]
            result = pi.select_photos(photos)
        self.assertEqual(len(result), len(photos))

    def test_deselect_one_day(self):
        """Desmarcar un día excluye solo las fotos de ese día."""
        photos = self._make_photos()
        with patch("questionary.checkbox") as mock_cb:
            # Excluir 2025/01-10 (2 fotos) → deben quedar 3
            mock_cb.return_value.ask.return_value = ["06-20", "03-08"]
            result = pi.select_photos(photos)
        self.assertEqual(len(result), 3)
        dates = {p.date.strftime("%m-%d") for p in result}
        self.assertNotIn("01-10", dates)

    def test_deselect_entire_year_2025(self):
        """Desmarcar todos los días de 2025 devuelve solo fotos de 2026."""
        photos = self._make_photos()
        with patch("questionary.checkbox") as mock_cb:
            mock_cb.return_value.ask.return_value = ["03-08"]
            result = pi.select_photos(photos)
        self.assertTrue(all(p.date.year == 2026 for p in result))
        self.assertEqual(len(result), 2)

    def test_cancel_returns_empty(self):
        """Ctrl+C (ask() devuelve None) → lista vacía."""
        photos = self._make_photos()
        with patch("questionary.checkbox") as mock_cb:
            mock_cb.return_value.ask.return_value = None
            result = pi.select_photos(photos)
        self.assertEqual(result, [])

    def test_empty_selection_returns_empty(self):
        """Selección vacía (ningún día marcado) → lista vacía."""
        photos = self._make_photos()
        with patch("questionary.checkbox") as mock_cb:
            mock_cb.return_value.ask.return_value = []
            result = pi.select_photos(photos)
        self.assertEqual(result, [])

    def test_same_mmdd_in_different_years_disambiguated(self):
        """
        Si existe el mismo MM-DD en dos años distintos y el usuario selecciona
        ese valor, deben incluirse fotos de AMBOS años (ya que el valor de
        Choice es solo "MM-DD" y puede aparecer en ambos).
        """
        photos = [
            pi.PhotoInfo(Path("/sd/x.jpg"), datetime(2025, 3, 8), "SONY",  "SOOC"),
            pi.PhotoInfo(Path("/sd/y.jpg"), datetime(2026, 3, 8), "CANON", "SOOC"),
        ]
        with patch("questionary.checkbox") as mock_cb:
            mock_cb.return_value.ask.return_value = ["03-08"]
            result = pi.select_photos(photos)
        # Ambas fotos tienen 03-08 aunque en distintos años → deben incluirse las dos
        self.assertEqual(len(result), 2)
