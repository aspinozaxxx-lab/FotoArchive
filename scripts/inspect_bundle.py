"""Inspect collected native imports, including every version of duplicate DLLs."""
from pathlib import Path
import pefile

root = Path(__file__).resolve().parents[1] / "dist/FotoArchive/_internal"
libraries = {}
for path in root.rglob("*"):
    if path.suffix.lower() in {".dll", ".pyd"}:
        libraries.setdefault(path.name.lower(), []).append(path)
exports = {}
visited = set()
pending = [root / "PySide6/QtCore.pyd"]
while pending:
    path = pending.pop()
    if path in visited:
        continue
    visited.add(path)
    pe = pefile.PE(str(path), fast_load=True, max_symbol_exports=65536)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
    for imported in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        name = imported.dll.decode().lower()
        for target in libraries.get(name, []):
            if target not in exports:
                target_pe = pefile.PE(str(target), fast_load=True, max_symbol_exports=65536)
                target_pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']])
                exports[target] = {item.name for item in target_pe.DIRECTORY_ENTRY_EXPORT.symbols}
                target_pe.close()
            missing = [s.name.decode() for s in imported.imports if s.name and s.name not in exports[target]]
            if missing:
                print(path.relative_to(root), '->', target.relative_to(root), 'MISSING', missing, flush=True)
            pending.append(target)
    pe.close()
print('Inspected', len(visited), 'bundled native files', flush=True)
