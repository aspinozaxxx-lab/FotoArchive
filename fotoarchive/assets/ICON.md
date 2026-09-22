# Иконка FotoArchive

Создана 17.09.2026 встроенным инструментом imagegen. PNG скопирован в проект без изменений, включая исходный альфа-канал. Фактический размер PNG — 1254 × 1254.

- [PNG](FotoArchive.png): исходное оформление.
- [ICO](FotoArchive.ico): контейнер Windows с размерами 16, 20, 24, 32, 40, 48, 64, 96, 128, 256 пикселей. Формат преобразован Pillow; сам рисунок не ретушировался.
- Повторное создание контейнера: `.venv\Scripts\python.exe scripts\build_icon.py` из корня проекта.

Иконка подключена к PyInstaller через `--icon`, включена в данные приложения и установлена через `QApplication.setWindowIcon`. Идентификатор панели задач — `FotoArchive.Desktop`.

## Точный промпт

Use case: logo-brand. Asset type: production Windows desktop application icon for FotoArchive, a local personal photo and video archive. Create a single polished square app icon, 1024x1024, with real transparent alpha outside the icon. Design: a deep teal rounded-square tile (#177c70, harmonizing with #133d38), containing two overlapping ivory photographic prints. The front print has a simple bold landscape silhouette in mint teal and a small warm amber sun; the rear print is offset slightly to suggest an organized photo collection. Large simple shapes, crisp geometric contours, flat vector-like finish, no fine details, high legibility at 16 and 32 pixels. Centered and optically balanced, tile fills about 94 percent of the canvas with an even transparent margin. Softly rounded corners on the photo cards. No lettering, no words, no watermark, no people, no gradients, no shadows outside the tile, no mockup, no presentation sheet, no extra icons. Deliver the icon artwork only, not a screenshot.
