"""Explain exactly which folders and file formats the progress bars cover."""
import html

from PySide6.QtWidgets import QDialog, QHBoxLayout, QPushButton, QTextBrowser, QVBoxLayout

from .inventory import format_group, format_totals


def number(value):
    return f'{value:,}'.replace(',', ' ')


def folder_scope(includes):
    return 'весь источник с подпапками' if '.' in includes else f'выбрано папок с подпапками: {len(includes)}'


def source_summary(inventory):
    scope = folder_scope(inventory.get('includes', []))
    formats = inventory.get('format_counts')
    if inventory.get('phase') != 'ready' or formats is None:
        detail = 'подсчёт недоступен' if inventory.get('phase') == 'error' else 'подсчитываю состав папок…'
        return f'Источник: {scope} · {detail}'
    counts = format_totals(formats)
    return (f'Источник: {scope} · изображений: {number(counts["images"])} · '
            f'видео: {number(counts["video"])} · всего для каталога: {number(counts["supported"])}')


def source_html(inventory):
    escape = html.escape
    includes = inventory.get('includes', [])
    folders = '; '.join(escape(item) for item in includes if item != '.')
    if '.' in includes:
        folders = 'Весь источник со всеми подпапками'
    parts = [f'<h3>{escape(inventory.get("root", ""))}</h3>',
             f'<p><b>Подключённые папки:</b><br>{folders or "Папки не выбраны"}</p>',
             '<p>Счётчики относятся к подключённым папкам со всеми вложенными папками.</p>']
    formats = inventory.get('format_counts')
    if inventory.get('phase') != 'ready' or formats is None:
        parts.append('<p>' + escape(inventory.get('error') or 'Подсчитываю файлы. Закройте и откройте это окно через несколько секунд.') + '</p>')
        return ''.join(parts)
    counts = format_totals(formats)
    scanned = inventory.get('counts', {}).get('scanned', 0)
    parts.append(f'<p><b>Файлов изображений: {number(counts["images"])}</b><br>'
                 f'JPG/JPEG/BMP: {number(counts["jpeg"])}.<br>'
                 f'RAW: {number(counts["raw"])}; другие изображения: {number(counts["other_images"])}; '
                 f'видео: {number(counts["video"])}. Все эти файлы входят в прогресс «Каталог».</p>')
    if counts['supported'] and scanned >= counts['supported']:
        parts.append('<p>Каталогизация файлов завершена. '
                     'Этапы «Поиск», «Описания», «Лица» и «Места» имеют отдельный прогресс.</p>')
    parts.append('<p>XMP, AAE, служебные файлы macOS (._…) и другие сопутствующие файлы не считаются отдельными фотографиями. '
                 'Количество изображений — число файлов: RAW и его JPEG-копия считаются отдельно.</p>')
    parts.append('<p>Для видео индексируются кадры через 10 секунд. Счётчики прогресса относятся к файлам; '
                 'этап видео завершается после обработки его кадров. Нечитаемые файлы показаны в списке ошибок.</p>')
    labels = {'jpeg': 'Изображение', 'raw': 'RAW — изображение',
              'other_images': 'Изображение', 'video': 'Видео — поиск по кадрам',
              'sidecars': 'Сопутствующий файл', 'other': 'Другой файл'}
    parts.append('<table cellpadding="5"><tr><th align="left">Формат</th><th>Файлов</th><th align="left">Состояние</th></tr>')
    for extension, count in sorted(formats.items(), key=lambda item: (-item[1], item[0])):
        title = 'macOS ._…' if extension == '.appledouble' else extension.upper() or 'Без расширения'
        parts.append(f'<tr><td>{escape(title)}</td>'
                     f'<td align="right">{number(count)}</td><td>{labels[format_group(extension)]}</td></tr>')
    parts.append(f'</table><p>Всего файлов всех типов: {number(counts["files"])}.</p>')
    return ''.join(parts)


class SourceDetailsDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle('Состав каталога')
        self.resize(760, 660)
        layout = QVBoxLayout(self)
        self.details = QTextBrowser()
        self.details.setHtml(source_html(owner.source_inventory))
        layout.addWidget(self.details)
        buttons = QHBoxLayout()
        add = QPushButton('Добавить папки')
        add.clicked.connect(lambda: (self.accept(), owner.add_folder()))
        buttons.addWidget(add)
        buttons.addStretch()
        close = QPushButton('Закрыть')
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
