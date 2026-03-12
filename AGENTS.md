# AGENTS.md — photo_importer

Guía para agentes de IA (Copilot, Claude, Cursor, etc.) que trabajen en este repositorio.

---

## Propósito del proyecto

`photo_importer` es una herramienta CLI en Python 3.11 que importa fotos desde
tarjetas SD a `~/Pictures`, organizándolas automáticamente por fecha EXIF y
fabricante de cámara. La única dependencia externa es `questionary` (selector
interactivo en terminal).

---

## Estructura del repositorio

```
photo_importer/
├── src/
│   └── photo_importer/
│       ├── __init__.py     # Script principal (todo el código de producción)
│       └── __main__.py     # Permite `python -m photo_importer`
├── tests.py                # Tests unitarios con unittest (stdlib)
├── pyproject.toml          # Metadatos del paquete y entry point CLI
├── requirements.txt        # Dependencias pinadas para el entorno de desarrollo
├── Formula/
│   └── photo-importer.rb   # Fórmula Homebrew (requiere actualizar sha256)
├── README.md
├── .gitignore
└── AGENTS.md               # Este archivo
```

---

## Convenciones de código

- **Python 3.11+** — usar type hints, `from __future__ import annotations`.
- **Dependencias mínimas** — solo `questionary` como externa; el resto es stdlib
  (`struct`, `pathlib`, `shutil`, `subprocess`, `os`, `datetime`, `io`,
  `collections`, `unittest`).
- **Funciones pequeñas y documentadas** — cada función pública tiene docstring
  con descripción, Args y Returns.
- **Nombres en inglés** para variables, funciones y clases. Comentarios y
  docstrings pueden ser en español o inglés, pero deben ser consistentes
  dentro de cada función.
- **`frozenset` para conjuntos de extensiones** — inmutables y con O(1) lookup.
- **`NamedTuple`** para estructuras de datos de solo lectura (`PhotoInfo`,
  `CopyResult`).

---

## Estructura del módulo principal (`src/photo_importer/__init__.py`)

El script está organizado en secciones bien delimitadas con comentarios de
separación. Al modificarlo, respetar este orden:

1. **Constantes globales** — extensiones, rutas, nombres especiales.
2. **Tipos de datos** — `NamedTuple` usados en todo el módulo.
3. **Detección de medios** — `list_external_volumes`, `display_volume_menu`.
4. **Parser EXIF** — funciones privadas `_parse_*`, `_read_ifd`, `_build_result`,
   y la función pública `read_exif`.
5. **Clasificación y escaneo** — `classify_file`, `scan_volume`, `_scan_dir`.
6. **Selector interactivo** — `_group_by_date`, `select_photos`.
7. **Rutas de destino** — `build_dest_path`, `_ensure_camera_subdirs`.
8. **Colisiones** — `resolve_collision`.
9. **Motor de copia** — `copy_photos`.
10. **Resumen** — `print_summary`.
11. **Punto de entrada** — `main`, bloque `if __name__ == "__main__"`.

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

## Selector interactivo — reglas

- `_group_by_date(photos)` devuelve `{año: {MM-DD: [PhotoInfo]}}` ordenado
  cronológicamente.
- `select_photos(photos)` usa `questionary.checkbox` con todos los días
  pre-marcados. Cada ítem tiene valor `"MM-DD"` y muestra el año en el label.
- Si el usuario cancela (Ctrl+C), `ask()` devuelve `None` → `select_photos`
  devuelve `[]`.
- El mismo `MM-DD` puede existir en distintos años; el filtrado combina
  `(año, MM-DD)` para evitar falsos positivos.

---

## Tests

```bash
# Con el entorno virtual activado:
source env/bin/activate
python -m unittest tests -v

# O directamente:
env/bin/python3.11 -m unittest tests -v
```

- Los tests de EXIF usan datos binarios sintéticos construidos con `struct`
  (no requieren fotos reales).
- Los tests de `select_photos` usan `unittest.mock.patch` sobre
  `questionary.checkbox` para simular respuestas del usuario.
- `_build_tiff_block()`, `_build_jpeg_with_exif()` y `_build_raf_with_exif()`
  son helpers en `tests.py` que generan bytes válidos para cada formato.
- Al añadir soporte para un nuevo formato RAW, añadir también:
  1. La extensión a `RAW_EXTENSIONS` en `__init__.py`.
  2. Un test `test_raw_<ext>` en `TestClassifyFile`.
  3. Si el formato tiene cabecera propia, un test en una clase dedicada.

---

## Instalación y packaging

### Desarrollo local

```bash
python3.11 -m venv env
source env/bin/activate
pip install -e .
photo-importer          # comando disponible globalmente en el venv
```

### Distribución con pip / PyPI

```bash
pip install build
python -m build         # genera dist/*.whl y dist/*.tar.gz
pip install dist/photo_importer-*.whl
```

### Distribución con pipx (recomendado para usuarios finales)

```bash
pipx install photo-importer   # instala en env aislado, expone el comando
pipx upgrade photo-importer
pipx uninstall photo-importer
```

### Distribución con Homebrew

La fórmula está en `Formula/photo-importer.rb`. Para publicarla:

1. Crear un release en GitHub con un tarball (`git tag v1.0.0 && git push --tags`).
2. Calcular el sha256 del tarball:
   ```bash
   curl -sL https://github.com/<user>/photo-importer/archive/refs/tags/v1.0.0.tar.gz \
     | shasum -a 256
   ```
3. Actualizar los campos `sha256` en la fórmula (paquete principal y recursos).
4. Crear un Homebrew tap:
   ```bash
   brew tap-new <user>/tap
   cp Formula/photo-importer.rb $(brew --repository <user>/tap)/Formula/
   brew install <user>/tap/photo-importer
   ```

---

## Flujo de la CLI

```
main()
  └── list_external_volumes()     → detecta /Volumes/* externos
  └── display_volume_menu()       → selector questionary, devuelve Path o None
  └── scan_volume()               → os.scandir recursivo → lista PhotoInfo ordenada por fecha
        └── classify_file()       → extensión → "SOOC" | "RAW" | None
        └── read_exif()           → (make, date) con fallback a mtime
  └── select_photos()             → checkbox por año/día, devuelve lista filtrada
        └── _group_by_date()      → {año: {MM-DD: [PhotoInfo]}}
  └── copy_photos()               → itera PhotoInfo seleccionados
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
- `chore:` tareas de mantenimiento (gitignore, CI, packaging, etc.)

---

## Añadir soporte para un nuevo formato RAW

1. Añadir la extensión (en minúsculas) a `RAW_EXTENSIONS` en `__init__.py`.
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
