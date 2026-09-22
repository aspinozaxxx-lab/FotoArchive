"""Canonical folder selection; existing coverage never triggers another scan."""
import os
from pathlib import Path


def folder_key(value):
    return value.replace("\\", "/").strip("/").casefold() or "."


def covered(relative, includes):
    key = folder_key(relative)
    return any(folder_key(item) == "." or key == folder_key(item) or key.startswith(folder_key(item)+"/") for item in includes)


def add_folders(cfg, paths):
    root = cfg.root.resolve()
    added, skipped, errors = [], [], []
    scan_excludes = list(cfg.includes)
    candidates = []
    for path in paths:
        try:
            folder = Path(path).resolve()
            relative = folder.relative_to(root).as_posix()
            if not folder.is_dir():
                raise ValueError("Папка недоступна")
            candidates.append(relative)
        except (OSError, ValueError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
    for relative in sorted(candidates, key=lambda value: (value.count("/"), folder_key(value))):
        if covered(relative, cfg.includes):
            skipped.append(relative)
        else:
            cfg.includes = [item for item in cfg.includes if not covered(item, [relative])]
            cfg.includes.append(relative)
            added.append(relative)
    if added:
        cfg.save()
    return {"added": added, "skipped": skipped, "errors": errors, "includes": cfg.includes, "scan_excludes": scan_excludes}


def choose_folders(owner):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractItemView, QDialog, QDialogButtonBox, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout
    dialog = QDialog(owner)
    dialog.setWindowTitle("Добавить папки")
    dialog.resize(760, 600)
    layout = QVBoxLayout(dialog)
    label = QLabel("Папки подключаются вместе со всеми подпапками: фотографии, RAW и видео. "
                   "Выберите несколько папок с Ctrl или Shift. "
                   "Повторно подключённые папки будут пропущены.")
    label.setWordWrap(True)
    layout.addWidget(label)
    tree = QTreeWidget()
    tree.setHeaderLabels([str(owner.cfg.root), "Подключение"])
    tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
    tree.setColumnWidth(0, 430)
    layout.addWidget(tree)

    def populate(parent, path):
        parent.takeChildren() if parent else tree.clear()
        try:
            with os.scandir(path) as entries:
                directories = sorted((Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)
                                      and not entry.is_symlink() and not getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400), key=lambda p:p.name.casefold())
            for folder in directories:
                relative = folder.relative_to(owner.cfg.root).as_posix()
                item = QTreeWidgetItem([folder.name, "Подключена со всеми подпапками" if covered(relative, owner.cfg.includes) else "Не подключена"])
                item.setData(0, Qt.UserRole, str(folder))
                item.setToolTip(0, str(folder))
                item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                (parent.addChild(item) if parent else tree.addTopLevelItem(item))
        except OSError as exc:
            label.setText(str(exc))
    tree.itemExpanded.connect(lambda item: populate(item, Path(item.data(0, Qt.UserRole))))
    populate(None, owner.cfg.root)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText("Добавить выбранные")
    buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    result = [item.data(0, Qt.UserRole) for item in tree.selectedItems()] if dialog.exec() == QDialog.Accepted else []
    dialog.deleteLater()
    return result
