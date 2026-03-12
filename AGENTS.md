# AGENTS.md — photo_importer

Guía para agentes de IA (Copilot, Claude, Cursor, etc.) que trabajen en este repositorio.

---

## Propósito del proyecto

`photo_importer` es una herramienta CLI en Python 3.11 que importa fotos desde
tarjetas SD a `~/Pictures`, organizándolas automáticamente por fecha y fabricante
de cámara. No tiene dependencias externas: usa únicamente la biblioteca estándar.

---

## Estructura del repositorio

```
photo_importer/
├── photo_importer.py   # Script principal (único módulo de producción)
├── tests.py            # Tests unitarios con unittest (stdlib)
├── .gitignore
└── AGENTS.md           # Este archivo
```

---

## Convenciones de código

- **Python 3.11+** — usar type hints, `from __future__ import annotations`.
- **Sin dependencias externas** — solo stdlib (`struct`, `pathlib`, `shutil`,
  `subprocess`, `os`, `datetime`, `io`, `unittest`).
- **Funciones pequeñas y documentadas** — cada función pública tiene docstring
  con descripción, Args y Returns.
- **Nombres en inglés** para variables, funciones y clases. Comentarios y
  docstrings pueden ser en español o inglés, pero deben ser consistentes
  dentro de cada función.
- **`frozenset` para conjuntos de extensiones** — inmutables y con O(1) lookup.
- **`NamedTuple`** para estructuras de datos de solo lectura (`PhotoInfo`,
  `CopyResult`).

---

## Estructura del script principal

El script está organizado en secciones bien delimitadas con comentarios de
separación. Al modificarlo, respetar este orden:

1. **Constantes globales** — extensiones, rutas, nombres especiales.
2. **Tipos de datos** — `NamedTuple` usados en todo el script.
3. **Detección de medios** — `list_external_volumes`, `display_volume_menu`.
4. **Parser EXIF** — funciones privadas `_parse_*`, `_read_ifd`, `_build_result`,
   y la función pública `read_exif`.
5. **Clasificación y escaneo** — `classify_file`, `scan_volume`, `_scan_dir`.
6. **Rutas de destino** — `build_dest_path`, `_ensure_camera_subdirs`.
7. **Colisiones** — `resolve_collision`.
8. **Motor de copia** — `copy_photos`.
9. **Resumen** — `print_summary`.
10. **Punto de entrada** — `main`, bloque `if __name__ == "__main__"`.

---

## Parser EXIF — reglas importantes

El parser EXIF es el componente más delicado. Al modificarlo:

- **JPEG**: busca el segmento APP1 (`0xFF 0xE1`) con el magic `"Exif\x00\x00"`.
  Si el segmento es XMP u otro, debe continuar al siguiente segmento.
- **TIFF-based** (ARW, NEF, CR2, CR3, DNG, ORF, RW2): leer los primeros 2 bytes
  para determinar endianness (`II` = little-endian, `MM` = big-endian), verificar
  magic 42, luego recorrer IFD0 y el EXIF IFD subyacente.
- **RAF** (Fujifilm): leer 92 bytes de cabecera, verificar magic
  `"FUJIFILMCCD-RAW "` (16 bytes), leer jpeg_offset desde `0x54` y
  jpeg_length desde `0x58` (ambos big-endian uint32), luego parsear el JPEG
  embebido con `_parse_jpeg_exif`.
- Tags de interés: `Make = 0x010F`, `ExifIFD pointer = 0x8769`,
  `DateTimeOriginal = 0x9003`.
- **Nunca lanzar excepciones al llamador**: todo error en el parser devuelve
  `(UNKNOWN_CAMERA, None)`. Las excepciones se capturan en `read_exif`.

---

## Tests

```bash
# Ejecutar todos los tests
python3.11 tests.py

# Con salida verbose
python3.11 -m unittest tests -v
```

- Los tests usan datos binarios sintéticos construidos con `struct` (no
  requieren fotos reales).
- `_build_tiff_block()`, `_build_jpeg_with_exif()` y `_build_raf_with_exif()`
  son helpers en `tests.py` que generan bytes válidos para cada formato.
- Al añadir soporte para un nuevo formato RAW, añadir también:
  1. La extensión a `RAW_EXTENSIONS` en `photo_importer.py`.
  2. Un test `test_raw_<ext>` en `TestClassifyFile`.
  3. Si el formato tiene cabecera propia, un test en una clase dedicada.

---

## Flujo de la CLI

```
main()
  └── list_external_volumes()     → detecta /Volumes/* externos
  └── display_volume_menu()       → menú numerado, devuelve Path o None
  └── scan_volume()               → os.scandir recursivo → lista PhotoInfo
        └── classify_file()       → extensión → "SOOC" | "RAW" | None
        └── read_exif()           → (make, date) con fallbacks
  └── copy_photos()               → itera PhotoInfo
        └── build_dest_path()     → crea dirs, devuelve Path destino
        └── resolve_collision()   → skip / rename con _N
        └── shutil.copy2()        → copia con metadatos del FS
  └── print_summary()             → tabla de estadísticas
```

---

## Convenciones de commits

Usar [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` nueva funcionalidad
- `fix:` corrección de bug
- `test:` añadir o corregir tests
- `refactor:` refactoring sin cambio de comportamiento
- `docs:` solo documentación
- `chore:` tareas de mantenimiento (gitignore, CI, etc.)

---

## Añadir soporte para un nuevo formato RAW

1. Añadir la extensión (en minúsculas) a `RAW_EXTENSIONS` en `photo_importer.py`.
2. Si el formato es TIFF-based, no requiere cambios en el parser (ya funciona).
3. Si el formato tiene cabecera propia:
   - Añadir una rama en `read_exif()` con la nueva extensión.
   - Implementar `_parse_<formato>(fh)` siguiendo el patrón de `_parse_raf`.
   - Añadir tests en `tests.py`.

---

## Añadir soporte para vídeos o sidecar files

Actualmente el script ignora todo lo que no sea SOOC o RAW. Para añadir vídeos:

1. Añadir las extensiones a un nuevo conjunto `VIDEO_EXTENSIONS`.
2. Actualizar `classify_file()` para devolver `"VIDEO"`.
3. Actualizar `CAMERA_SUBDIRS` si se quiere una subcarpeta dedicada.
4. Añadir tests correspondientes.
