"""Desktop navigation and presentation controls; inference stays in the backend."""
from copy import deepcopy
from datetime import date
import calendar
import json
from collections import OrderedDict
from PySide6.QtCore import QByteArray, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPalette, QColor, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QSlider,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QAbstractItemView)
from .canvas import ElideLabel
from .gallery import PhotoModel


class Workspace:
    def workspace_init(self):
        self.people_records = []
        self.selected_people = []
        self.people_mode = 'any'
        self.current_person_id = None
        self.geo_bounds = ''
        self.expanded_stacks = set()
        self.history = []
        self.history_position = -1
        self._history_restoring = False
        self._view_state = None
        self._restoring_controls = True
        self._pending_anchor = None
        self._stack_change = None
        self.navigation_serial = 0
        self.map_serial = 0
        self._folder_signature = None
        self.auto_filter = QTimer(self)
        self.auto_filter.setSingleShot(True)
        self.auto_filter.setInterval(140)
        self.auto_filter.timeout.connect(self.search)
        self.save_context_timer = QTimer(self)
        self.save_context_timer.setSingleShot(True)
        self.save_context_timer.setInterval(600)
        self.save_context_timer.timeout.connect(self.persist_workspace)
        self.navigation_timer = QTimer(self)
        self.navigation_timer.setSingleShot(True)
        self.navigation_timer.setInterval(220)
        self.navigation_timer.timeout.connect(self.request_navigation)

    def install_workspace(self):
        # Keep the established filter widgets as the single source of their values.
        self.folder_combo.hide()
        self.media_combo.hide()
        for combo in (self.folder_combo, self.format_combo, self.camera_combo):
            combo.currentIndexChanged.connect(self.queue_filter)
        self.date_enabled.toggled.connect(self.queue_filter)
        self.unknown.toggled.connect(self.queue_filter)
        self.date_from.dateChanged.connect(self.queue_filter)
        self.date_to.dateChanged.connect(self.queue_filter)
        self.subfolders = QCheckBox('Включая подпапки')
        self.subfolders.setChecked(True)
        self.subfolders.toggled.connect(self.queue_filter)
        self.sidebar_layout.insertWidget(1,self.subfolders)
        self.navigation_tabs = QTabWidget()
        self.sidebar_layout.insertWidget(1,self.navigation_tabs,2)
        folder_page = QWidget()
        folder_layout = QVBoxLayout(folder_page)
        folder_layout.setContentsMargins(0,5,0,0)
        self.folder_find = QLineEdit()
        self.folder_find.setPlaceholderText('Найти папку…')
        self.folder_filter_timer = QTimer(self)
        self.folder_filter_timer.setSingleShot(True)
        self.folder_filter_timer.setInterval(150)
        self.folder_filter_timer.timeout.connect(lambda:self.filter_folder_tree(self.folder_find.text()))
        self.folder_find.textChanged.connect(lambda:self.folder_filter_timer.start())
        folder_layout.addWidget(self.folder_find)
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setUniformRowHeights(True)
        self.folder_tree.itemClicked.connect(self.choose_folder)
        folder_layout.addWidget(self.folder_tree)
        self.navigation_tabs.addTab(folder_page,'Папки')
        people_page = QWidget()
        people_layout = QVBoxLayout(people_page)
        people_layout.setContentsMargins(0,5,0,0)
        self.people_list = QListWidget()
        self.people_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.people_list.setIconSize(__import__('PySide6.QtCore',fromlist=['QSize']).QSize(44,44))
        self.people_list.itemDoubleClicked.connect(lambda item: self.choose_people())
        people_layout.addWidget(self.people_list,1)
        self.people_mode_combo = QComboBox()
        self.people_mode_combo.addItem('Любой из выбранных','any')
        self.people_mode_combo.addItem('Все вместе в одном кадре','all')
        people_layout.addWidget(self.people_mode_combo)
        button = QPushButton('Показать снимки людей')
        button.clicked.connect(self.choose_people)
        people_layout.addWidget(button)
        button = QPushButton('Управление людьми…')
        button.clicked.connect(self.manage_people)
        people_layout.addWidget(button)
        self.navigation_tabs.addTab(people_page,'Люди')
        places_page = QWidget()
        places_layout = QVBoxLayout(places_page)
        places_layout.setContentsMargins(0,5,0,0)
        self.place_query = QLineEdit()
        self.place_query.setPlaceholderText('Город, страна или место…')
        self.place_query.returnPressed.connect(self.search)
        places_layout.addWidget(self.place_query)
        self.gps_check = QCheckBox('Только с координатами')
        self.gps_check.toggled.connect(self.queue_filter)
        places_layout.addWidget(self.gps_check)
        self.places_list = QListWidget()
        self.places_list.itemClicked.connect(lambda item: (self.place_query.setText(item.data(Qt.UserRole)), self.search()))
        places_layout.addWidget(self.places_list,1)
        button = QPushButton('Показать карту')
        button.clicked.connect(lambda: self.map_toggle.setChecked(True))
        places_layout.addWidget(button)
        self.navigation_tabs.addTab(places_page,'Места')
        self.navigation_tabs.currentChanged.connect(lambda index: self.request_map() if index==2 else None)
        # A single settings menu replaces permanent maintenance controls.
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        for label, callback in [('Добавить папки…',self.add_folder),('Проверить обновления',self.check_updates),
                                ('Поворот фото…',self.orientation_dialog),('Модели…',self.models_dialog)]:
            action = menu.addAction(label,callback)
            if label == 'Проверить обновления':
                action.setToolTip(self.check_updates_button.toolTip())
        menu.addSeparator()
        themes = menu.addMenu('Оформление')
        for label,key in [('Как в Windows','system'),('Светлое','light'),('Тёмное','dark')]:
            themes.addAction(label,lambda key=key:self.apply_theme(key))
        stacks = menu.addMenu('Стопки')
        stacks.addAction('Интервал серии…',self.configure_stacks)
        stacks.addAction('Свернуть все',lambda:self.set_expanded_stacks(set()))
        self.library_menu.setMenu(menu)
        self.gallery.setContextMenuPolicy(Qt.CustomContextMenu)
        self.gallery.customContextMenuRequested.connect(self.gallery_menu)
        self.gallery.zoomRequested.connect(lambda delta:self.thumbnail_size.setValue(self.thumbnail_size.value()+delta))
        self.gallery.hoverVideo.connect(lambda asset:self.backend.send(action='library_storyboard',asset_id=asset['id'],version=asset['version']))
        self.delegate.stackToggled.connect(self.toggle_stack)
        self.delegate.momentOpened.connect(self.open_video_moment)
        self.gallery.verticalScrollBar().valueChanged.connect(self.workspace_scrolled)
        self.model.assetsReady.connect(lambda *_:self.update_month_header())
        self.face_save_button = QPushButton('Сохранить человека…')
        self.face_save_button.clicked.connect(self.save_person)
        self.face_panel.layout().itemAt(0).layout().insertWidget(1,self.face_save_button)
        self.face_panel.scroll.setFixedHeight(60)
        self.face_panel.hint.setMinimumWidth(200)
        self.face_panel.hint.setMaximumWidth(280)
        QShortcut(QKeySequence('Alt+Left'),self,activated=lambda:self.navigate_history(-1))
        QShortcut(QKeySequence('Alt+Right'),self,activated=lambda:self.navigate_history(1))
        panel_shortcut = QShortcut(QKeySequence('Tab'),self.gallery,activated=self.toggle_panels)
        panel_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        # Tab is not intercepted while editing text.
        self.sidebar_toggle.toggled.connect(lambda visible:self.show_side_panel(self.sidebar,visible))
        self.details_toggle.toggled.connect(lambda visible:self.show_side_panel(self.details_panel,visible))
        for panel in (self.sidebar,self.details_panel):
            self.splitter.setCollapsible(self.splitter.indexOf(panel),False)
        self.processing_toggle.toggled.connect(self.processing_panel.setVisible)
        self.map_toggle.toggled.connect(self.toggle_map)
        self.stacks_check.toggled.connect(lambda _:self.search(preserve_position=True) if not self._restoring_controls else None)
        self.sort_combo.currentIndexChanged.connect(self.queue_filter)
        self.thumbnail_size.valueChanged.connect(self.resize_thumbnails)
        self.layout_combo.currentIndexChanged.connect(self.resize_thumbnails)
        self.timeline_combo.activated.connect(self.choose_period)
        self.back_button.clicked.connect(lambda:self.navigate_history(-1))
        self.forward_button.clicked.connect(lambda:self.navigate_history(1))
        self.map_widget.boundsSelected.connect(self.select_map_bounds)
        self.map_delay = QTimer(self)
        self.map_delay.setSingleShot(True)
        self.map_delay.setInterval(180)
        self.map_delay.timeout.connect(self.request_map)
        self.map_widget.viewportChanged.connect(lambda:self.map_delay.start())
        state = self.preferences.values.get('browser_state',{})
        self.set_workspace_state(state)
        self.thumbnail_size.setValue(int(self.preferences.values.get('thumbnail_size',220)))
        self.layout_combo.setCurrentIndex(int(self.preferences.values.get('layout_mode',0)))
        self.sidebar_toggle.setChecked(self.preferences.values.get('sidebar',True))
        self.details_toggle.setChecked(self.preferences.values.get('details',False))
        self.sidebar.setVisible(self.sidebar_toggle.isChecked())
        self.details_panel.setVisible(self.details_toggle.isChecked())
        self.processing_panel.hide()
        self._pending_anchor = state.get('anchor')
        geometry = self.preferences.values.get('window_geometry')
        if geometry:
            self.restoreGeometry(QByteArray.fromHex(geometry.encode()))
        self.apply_theme(self.preferences.values.get('theme','system'),save=False)
        if self.preferences.values.get('splitter_sizes'):
            self.splitter.setSizes(self.preferences.values['splitter_sizes'])
        self.resize_thumbnails()
        self._restoring_controls = False
        self.refresh_chips()
        self._view_state = self.workspace_state()
        self.history = [deepcopy(self._view_state)]
        self.history_position = 0
        self.update_history_buttons()

    def queue_filter(self, *_):
        if not self._restoring_controls:
            self.auto_filter.start()

    def presentation_options(self):
        return dict(stacks=self.stacks_check.isChecked(),seconds=int(self.preferences.values.get('stack_seconds',2)),
                    versions=self.preferences.values.get('stack_versions',True),expanded=sorted(self.expanded_stacks),
                    sort=self.sort_combo.currentData() or 'newest')

    def capture_anchor(self):
        index = self.gallery.indexAt(QPoint(12,12))
        if not index.isValid():
            index = self.gallery.currentIndex()
        asset = index.data(PhotoModel.AssetRole) if index.isValid() else None
        if not asset:
            return None
        return dict(asset_id=asset['id'],row=index.row(),selected_id=(self.selected_asset or {}).get('id'),
                    offset_y=self.gallery.visualRect(index).y(),loaded_count=self.model.rowCount())

    def workspace_state(self):
        return dict(filters=self.filters(),query=self.search_box.text(),reference=self.reference,
                    reference_unit=self.reference_unit,complex=self.complex_check.isChecked(),
                    face_ids=list(self.face_examples),rejected=sorted(self.face_rejected),
                    people=[p['id'] for p in self.selected_people],people_mode=self.people_mode,
                    people_refs=[{k:p[k] for k in ('id','name','examples','rejected')} for p in self.selected_people],
                    presentation=self.presentation_options())

    def set_workspace_state(self, state):
        from PySide6.QtCore import QDate
        self._restoring_controls = True
        filters = state.get('filters',{})
        for field,combo in [('folder',self.folder_combo),('media_kind',self.media_combo),('camera',self.camera_combo),('extension',self.format_combo)]:
            value = filters.get(field,'')
            index = combo.findData(value)
            if index < 0:
                combo.addItem(value,value)
                index = combo.count()-1
            combo.setCurrentIndex(index)
        self.subfolders.setChecked(filters.get('include_subfolders',True))
        self.place_query.setText(filters.get('place',''))
        self.geo_bounds = filters.get('geo_bounds','')
        self.gps_check.setChecked(filters.get('has_gps',False))
        if filters.get('date_from') and filters.get('date_to'):
            lower,upper = date.fromisoformat(filters['date_from']),date.fromisoformat(filters['date_to'])
            self.date_slider.setBounds(min(self.date_slider.minimum,lower.toordinal()),max(self.date_slider.maximum,upper.toordinal()))
            self.date_slider.setRange(lower.toordinal(),upper.toordinal())
        self.date_enabled.setChecked(bool(filters.get('date_from')))
        self.unknown.setChecked(filters.get('unknown_date',False))
        self.search_box.setText(state.get('query',''))
        self.reference = tuple(state['reference']) if state.get('reference') else None
        self.reference_unit = state.get('reference_unit')
        self.complex_check.setChecked(state.get('complex',False))
        if 'face_ids' in state:
            self.face_examples = OrderedDict((key,self.face_cache.get(key,{'id':key})) for key in state['face_ids'])
            self.face_rejected = set(state.get('rejected',[]))
        self.pending_people = state.get('people',[])
        self.selected_people = state.get('people_refs',[])
        self.people_mode = state.get('people_mode','any')
        self.people_mode_combo.setCurrentIndex(max(0,self.people_mode_combo.findData(self.people_mode)))
        options = state.get('presentation',{})
        self.expanded_stacks = set(options.get('expanded',[]))
        self.stacks_check.setChecked(options.get('stacks',True))
        self.sort_combo.setCurrentIndex(max(0,self.sort_combo.findData(options.get('sort','newest'))))
        self._restoring_controls = False

    def before_workspace_search(self, preserve):
        self.auto_filter.stop()
        anchor = self._pending_anchor
        self._pending_anchor = None
        state = self.workspace_state()
        if not self._history_restoring and self._view_state is not None and state != self._view_state:
            if 0 <= self.history_position < len(self.history):
                self.history[self.history_position]['anchor'] = self.capture_anchor()
            self.history = self.history[:self.history_position+1] + [deepcopy(state)]
            self.history = self.history[-30:]
            self.history_position = len(self.history)-1
        self._view_state = deepcopy(state)
        self._history_restoring = False
        self.refresh_chips()
        self.update_history_buttons()
        self.navigation_timer.start()
        return anchor or (self.capture_anchor() if preserve else None)

    def navigate_history(self, delta):
        position = self.history_position+delta
        if not 0 <= position < len(self.history):
            return
        self.history[self.history_position]['anchor'] = self.capture_anchor()
        self.history_position = position
        state = self.history[position]
        self.set_workspace_state(state)
        self.selected_people = [p for p in self.people_records if p['id'] in self.pending_people]
        self._pending_anchor = state.get('anchor')
        self._history_restoring = True
        self.update_face_panel()
        self.search()

    def update_history_buttons(self):
        self.back_button.setEnabled(self.history_position>0)
        self.forward_button.setEnabled(self.history_position<len(self.history)-1)

    def persist_workspace(self):
        if not hasattr(self,'thumbnail_size') or self._restoring_controls:
            return
        state = self.workspace_state()
        state['anchor'] = self.capture_anchor()
        self.save_preferences(browser_state=state,thumbnail_size=self.thumbnail_size.value(),layout_mode=self.layout_combo.currentIndex(),
                              sidebar=self.sidebar_toggle.isChecked(),details=self.details_toggle.isChecked(),
                              splitter_sizes=self.splitter.sizes(),
                              window_geometry=bytes(self.saveGeometry().toHex()).decode())

    def workspace_scrolled(self,*_):
        self.update_month_header()
        self.save_context_timer.start()

    def update_month_header(self):
        index = self.gallery.indexAt(QPoint(12,12))
        asset = index.data(PhotoModel.AssetRole) if index.isValid() else None
        captured = (asset or {}).get('captured_at')
        months = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь']
        self.month_header.setText(f'{months[int(captured[5:7])-1]} {captured[:4]}' if captured else 'Дата неизвестна' if asset else '')

    def request_navigation(self):
        self.navigation_serial += 1
        self.backend.send(action='library_navigation',serial=self.navigation_serial,filters=self.filters())
        if self.map_toggle.isChecked():
            self.request_map()

    def request_map(self):
        self.map_serial += 1
        w,s,e,n = map(float,self.map_widget.visible_bounds().split(','))
        action = 'search_places' if self.mode!='browse' and not self.loading else 'library_places'
        self.backend.send(action=action,id=self.request_id,serial=self.map_serial,filters=self.filters(),
                          viewport=self.map_widget.visible_bounds(),step=max(.0001,(e-w)/60))

    def fill_navigation(self,event):
        folders = event['folders']
        signature = tuple((r['folder'],r['count']) for r in folders)
        if signature != self._folder_signature:
            expanded = {key for key,item in getattr(self,'folder_items',{}).items() if item.isExpanded()}
            self.folder_tree.clear()
            root = QTreeWidgetItem(self.folder_tree,['Весь архив'])
            root.setData(0,Qt.UserRole,'')
            root.setExpanded(True)
            items = {'':root}
            counts = {}
            for record in folders:
                parts = record['folder'].split('/')
                for i in range(1,len(parts)+1):
                    key = '/'.join(parts[:i])
                    counts[key] = counts.get(key,0)+record['count']
                    if key not in items:
                        item = QTreeWidgetItem(items['/'.join(parts[:i-1])],[parts[i-1]])
                        item.setData(0,Qt.UserRole,key)
                        item.setToolTip(0,key)
                        items[key] = item
            for key,item in items.items():
                if key:
                    item.setText(0,key.split('/')[-1]+f'  ·  {counts[key]:,}'.replace(',',' '))
                    item.setExpanded(key in expanded)
            self.folder_items = items
            self._folder_signature = signature
            self.filter_folder_tree(self.folder_find.text())
        current = self.folder_combo.currentData() or ''
        if current in self.folder_items:
            self.folder_tree.setCurrentItem(self.folder_items[current])
        self.timeline_combo.blockSignals(True)
        self.timeline_combo.clear()
        self.timeline_combo.addItem('Перейти к дате…','')
        years = {}
        for row in event['dates']:
            if row['month']:
                years[row['month'][:4]] = years.get(row['month'][:4],0)+row['count']
        for year,count in sorted(years.items(),reverse=True):
            self.timeline_combo.addItem(f'{year}  ·  {count:,}'.replace(',',' '),year)
            for row in reversed(event['dates']):
                if row['month'] and row['month'].startswith(year):
                    self.timeline_combo.addItem(f"    {row['month']}  ·  {row['count']}",row['month'])
        self.timeline_combo.addItem('Дата неизвестна','unknown')
        self.timeline_combo.blockSignals(False)
        self.date_slider.setHistogram(event['dates'])
        for key,button in self.media_buttons.items():
            count = sum(event['media'].values()) if not key else event['media'].get(key,0)
            label = {'':'Все','photo':'Фото','video':'Видео'}[key]
            button.setText(label if self.reference or self.search_box.text().strip() else f'{label} {count:,}'.replace(',',' '))
        self.refresh_people(event['people'])

    def choose_folder(self,item,*_):
        key = item.data(0,Qt.UserRole)
        index = self.folder_combo.findData(key)
        if index < 0:
            self.folder_combo.addItem(key,key)
            index = self.folder_combo.count()-1
        self.folder_combo.setCurrentIndex(index)

    def filter_folder_tree(self,text):
        text = text.casefold().strip()
        visible_paths = {''}
        if text:
            for key in getattr(self,'folder_items',{}):
                if text in key.casefold():
                    parts = key.split('/')
                    visible_paths.update('/'.join(parts[:i]) for i in range(1,len(parts)+1))
        for key,item in getattr(self,'folder_items',{}).items():
            visible = not text or key in visible_paths
            item.setHidden(bool(key) and not visible)
            if text and visible:
                item.setExpanded(True)

    def choose_period(self,index):
        value = self.timeline_combo.itemData(index)
        if not value:
            return
        if value == 'unknown':
            self.unknown.setChecked(True)
        else:
            year = int(value[:4])
            month = int(value[5:]) if len(value)>4 else None
            lower = date(year,month or 1,1)
            upper = date(year,month or 12,calendar.monthrange(year,month)[1] if month else 31)
            self.date_slider.setRange(lower.toordinal(),upper.toordinal())
            self.date_enabled.setChecked(True)
        self.search()

    def refresh_chips(self):
        if not hasattr(self,'chips_layout'):
            return
        while self.chips_layout.count():
            child = self.chips_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        entries = []
        filters = self.filters()
        if filters['folder']:
            entries.append((filters['folder'],lambda:self.folder_combo.setCurrentIndex(0),lambda:self.focus_filter(self.folder_find,0)))
        if filters['media_kind']:
            entries.append(({'photo':'Фото','video':'Видео'}[filters['media_kind']],lambda:self.media_combo.setCurrentIndex(0),self.edit_media_filter))
        if filters['date_from']:
            entries.append((f"{filters['date_from']} — {filters['date_to']}",lambda:self.date_enabled.setChecked(False),lambda:self.focus_filter(self.date_from)))
        if filters['unknown_date']:
            entries.append(('Без даты',lambda:self.unknown.setChecked(False),lambda:self.focus_filter(self.unknown)))
        for key,combo in [('extension',self.format_combo),('camera',self.camera_combo)]:
            if filters[key]:
                entries.append((filters[key],lambda combo=combo:combo.setCurrentIndex(0),lambda combo=combo:self.focus_filter(combo)))
        if filters.get('place'):
            entries.append((filters['place'],lambda:(self.place_query.clear(),self.search()),lambda:self.focus_filter(self.place_query,2)))
        if filters.get('geo_bounds'):
            entries.append(('Область карты',lambda:self.select_map_bounds(''),lambda:self.map_toggle.setChecked(True)))
        if filters.get('has_gps'):
            entries.append(('С координатами',lambda:self.gps_check.setChecked(False),lambda:self.focus_filter(self.gps_check,2)))
        if self.selected_people and self.reference and self.reference[0]=='face_search':
            for person in self.selected_people:
                entries.append((person['name'],lambda key=person['id']:self.remove_person_filter(key),lambda key=person['id']:self.edit_person_filter(key)))
        elif self.reference and self.reference[0] == 'face_search':
            entries.append(('Человек',self.clear_person,self.refine_person))
        if self.reference and self.reference[0] == 'similar':
            entries.append(('Похожие кадры',self.clear_query,lambda:self.focus_filter(self.search_box)))
        if self.search_box.text().strip() and not self.reference:
            entries.append((self.search_box.text().strip(),self.clear_query,lambda:self.search_box.setFocus()))
        self._filter_entries = entries
        for title,remove,edit in entries:
            chip = QWidget()
            row = QHBoxLayout(chip)
            row.setContentsMargins(0,0,0,0)
            row.setSpacing(1)
            button = QPushButton(title)
            button.setMaximumWidth(205)
            button.setToolTip(title+' — изменить фильтр')
            button.setAccessibleName('Изменить фильтр: '+title)
            button.clicked.connect(edit)
            row.addWidget(button)
            close = QPushButton('×')
            close.setFixedWidth(28)
            close.setStyleSheet('padding: 7px 0;')
            close.setToolTip('Снять фильтр: '+title)
            close.setAccessibleName('Снять фильтр: '+title)
            close.clicked.connect(remove)
            row.addWidget(close)
            self.chips_layout.addWidget(chip)
        if entries:
            clear = QPushButton('Сбросить всё')
            clear.clicked.connect(self.reset_all_filters)
            self.chips_layout.addWidget(clear)
        self.chips_layout.addStretch()
        self.chips_panel.setVisible(bool(entries))
        folder = filters['folder'] or 'Весь архив'
        self.breadcrumbs.setText(folder.replace('/','  ›  ')+('  · с подпапками' if filters.get('folder') and filters['include_subfolders'] else ''))
        for key,button in self.media_buttons.items():
            button.setChecked(key == filters['media_kind'])

    def focus_filter(self, widget, tab=None):
        if widget is not self.search_box:
            self.sidebar_toggle.setChecked(True)
        if tab is not None:
            self.navigation_tabs.setCurrentIndex(tab)
        widget.setFocus(Qt.ShortcutFocusReason)
        if isinstance(widget,QLineEdit):
            widget.selectAll()
        if isinstance(widget,QComboBox):
            widget.showPopup()

    def edit_media_filter(self):
        menu = QMenu(self)
        for title,key in [('Все',''),('Фото','photo'),('Видео','video')]:
            menu.addAction(title,lambda key=key:self.media_combo.setCurrentIndex(self.media_combo.findData(key)))
        menu.exec(self.chips_panel.mapToGlobal(QPoint(0,self.chips_panel.height())))

    def edit_person_filter(self, key):
        from .face_review import FaceReviewDialog
        person = next((p for p in self.selected_people if p['id']==key),None)
        if not person:
            return
        examples = OrderedDict((key,self.face_cache.get(key,{'id':key})) for key in person['examples'])
        dialog = FaceReviewDialog(self,examples=examples,rejected=person['rejected'],skipped=[])
        dialog.setWindowTitle('Уточнить фильтр: '+person['name'])
        if dialog.exec() == QDialog.Accepted:
            if not dialog.examples:
                self.remove_person_filter(key)
            else:
                updated = person | dict(examples=list(dialog.examples),rejected=sorted(dialog.rejected))
                self.selected_people = [updated if p['id']==key else p for p in self.selected_people]
                self.face_cache.update(dialog.examples)
                self.face_examples = OrderedDict((face,self.face_cache.get(face,{'id':face}))
                    for p in self.selected_people for face in p['examples'])
                self.face_rejected = set(updated['rejected']) if len(self.selected_people)==1 else set()
                self.backend.send(action='library_save_person',person_id=key,name=person['name'],
                    examples=updated['examples'],rejected=updated['rejected'],cover=person.get('cover'))
                self.activate_person_search()
        dialog.deleteLater()

    def reset_all_filters(self):
        self.selected_people = []
        self.current_person_id = None
        self.reference = None
        self.search_box.clear()
        self.face_examples.clear()
        self.face_rejected.clear()
        self.face_picking = False
        self.update_face_panel(save=True)
        self.reset_filters()

    def resize_thumbnails(self,*_):
        if not hasattr(self,'delegate'):
            return
        self.gallery.preserve_view_position()
        self.gallery.stop_scroll()
        self.delegate.edge = self.thumbnail_size.value()
        self.delegate.proportions = self.layout_combo.currentIndex()==1
        self.delegate.full_frame = self.layout_combo.currentIndex()==2
        self.gallery.setUniformItemSizes(not self.delegate.proportions)
        self.gallery.doItemsLayout()
        self.gallery.schedule_reflow()
        self.save_context_timer.start()

    def show_side_panel(self,panel,visible):
        index=self.splitter.indexOf(panel)
        sizes=self.splitter.sizes()
        if not visible and sizes[index]>0:
            panel.setProperty('expanded_width',sizes[index])
        panel.setVisible(visible)
        if visible:
            sizes[index]=max(panel.minimumWidth(),panel.property('expanded_width') or 320)
            sizes[1]=max(240,self.splitter.width()-sum(size for i,size in enumerate(sizes) if i!=1)-20)
            self.splitter.setSizes(sizes)
        self.save_context_timer.start()

    def toggle_panels(self):
        if isinstance(QApplication.focusWidget(),QLineEdit):
            return
        visible = not self.sidebar_toggle.isChecked() and not self.details_toggle.isChecked()
        self.sidebar_toggle.setChecked(visible)
        self.details_toggle.setChecked(visible)

    def configure_stacks(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Стопки снимков')
        form = QFormLayout(dialog)
        label = QLabel('Время между соседними кадрами одной камеры.\n0 секунд — объединять только версии. Оригиналы и поиск сохраняются.')
        form.addRow(label)
        from PySide6.QtWidgets import QSpinBox
        seconds = QSpinBox()
        seconds.setRange(0,60)
        seconds.setSuffix(' с')
        seconds.setValue(int(self.preferences.values.get('stack_seconds',2)))
        form.addRow('Интервал серии',seconds)
        versions = QCheckBox('Объединять RAW, JPEG и производные версии')
        versions.setChecked(self.preferences.values.get('stack_versions',True))
        form.addRow(versions)
        button = QPushButton('Применить')
        button.clicked.connect(dialog.accept)
        form.addRow(button)
        if dialog.exec() == QDialog.Accepted:
            self.save_preferences(stack_seconds=seconds.value(),stack_versions=versions.isChecked())
            self.expanded_stacks.clear()
            self.search(preserve_position=True)

    def toggle_stack(self,key, row=None):
        # Anchor the clicked cover itself, including when it is below the first
        # visible row. Only the geometry before this stack can be reused.
        candidates = [(offset+i, asset) for offset, items in self.model.blocks.items()
                      for i, asset in enumerate(items) if asset.get('stack_key') == key]
        if candidates:
            first, _ = min(candidates, key=lambda pair: pair[0])
            row, asset = next((pair for pair in candidates if pair[0] == row),
                              min(candidates, key=lambda pair: pair[0]))
            rect = self.gallery.visualRect(self.model.index(row))
            self._pending_anchor = dict(asset_id=asset['id'], row=row, selected_id=asset['id'],
                                        offset_y=rect.y(), loaded_count=self.model.rowCount())
            self._stack_change = dict(request_id=self.request_id+1, geometry_prefix=first)
            self.gallery.begin_stack_transition(key, rect)
        if key in self.expanded_stacks:
            self.expanded_stacks.remove(key)
        else:
            self.expanded_stacks.add(key)
        self.search(preserve_position=True)

    def set_expanded_stacks(self,values):
        self.expanded_stacks = values
        self.search(preserve_position=True)

    def gallery_menu(self,point):
        index = self.gallery.indexAt(point)
        asset = index.data(PhotoModel.AssetRole) if index.isValid() else None
        if not asset:
            return
        self.gallery.setCurrentIndex(index)
        menu = QMenu(self)
        menu.addAction('Открыть',lambda:self.view_photo(index))
        menu.addAction('Найти похожие',self.similar)
        if asset.get('matched_moments'):
            from .video import timestamp_text
            moments = menu.addMenu('Найденные моменты')
            for moment in asset['matched_moments'][:12]:
                moments.addAction(timestamp_text(moment['timestamp_ms']),lambda moment=moment:self.open_video_moment(asset,moment))
            moments.addAction('Все найденные моменты…',lambda:self.open_video_moment(asset,None))
        if asset.get('stack_count',0)>1:
            menu.addAction('Свернуть стопку' if asset.get('stack_expanded') else 'Развернуть стопку',lambda:self.toggle_stack(asset['stack_key']))
            menu.addAction('Сделать обложкой стопки',lambda:self.backend.send(action='library_stack_cover',stack_key=asset['stack_key'],asset_id=asset['id']))
            menu.addAction('Показывать этот кадр отдельно',lambda:self.backend.send(action='library_unstack',asset_id=asset['id']))
        menu.addAction('Указать место…',self.assign_place)
        menu.addSeparator()
        menu.addAction('Открыть оригинал',self.external_open)
        menu.addAction('Показать в Проводнике',self.reveal)
        menu.exec(self.gallery.viewport().mapToGlobal(point))

    def refresh_people(self,people):
        chosen = {item.data(Qt.UserRole) for item in self.people_list.selectedItems()}
        self.people_records = people
        self.people_list.clear()
        for person in people:
            item = QListWidgetItem(QIcon(person['thumbnail']),person['name']+f"  ·  {len(person['examples'])} прим.")
            item.setData(Qt.UserRole,person['id'])
            self.people_list.addItem(item)
            item.setSelected(person['id'] in chosen)
        if self.pending_people:
            self.selected_people = [p for p in people if p['id'] in self.pending_people]
            self.pending_people = []

    def choose_people(self):
        ids = {item.data(Qt.UserRole) for item in self.people_list.selectedItems()}
        self.selected_people = [p for p in self.people_records if p['id'] in ids]
        if not self.selected_people:
            return
        self.people_mode = self.people_mode_combo.currentData()
        self.current_person_id = self.selected_people[0]['id'] if len(self.selected_people)==1 else None
        self.face_examples = OrderedDict((key,self.face_cache.get(key,{'id':key})) for p in self.selected_people for key in p['examples'])
        self.face_rejected = set(self.selected_people[0]['rejected']) if len(self.selected_people)==1 else set()
        self.activate_person_search()

    def remove_person_filter(self,key):
        ids = [p['id'] for p in self.selected_people if p['id']!=key]
        for i in range(self.people_list.count()):
            item = self.people_list.item(i)
            item.setSelected(item.data(Qt.UserRole) in ids)
        if ids:
            self.choose_people()
        else:
            self.clear_person()

    def save_person(self):
        if not self.face_examples:
            return
        old = next((p for p in self.people_records if p['id']==self.current_person_id),None)
        name,ok = QInputDialog.getText(self,'Сохранить человека','Имя',text=old['name'] if old else '')
        if ok and name.strip():
            self.backend.send(action='library_save_person',person_id=self.current_person_id,name=name,
                examples=list(self.face_examples),rejected=sorted(self.face_rejected),cover=(old or {}).get('cover'))

    def manage_people(self):
        from .people_dialog import PeopleDialog
        dialog = PeopleDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def toggle_map(self,visible):
        self.map_panel.setVisible(visible)
        if visible:
            height=max(200,int(self.preferences.values.get('map_height',330)))
            self.canvas_splitter.setSizes([height,max(180,self.canvas_splitter.height()-height)])
            self.request_map()

    def remember_map_height(self,*_):
        if self.map_panel.isVisible():
            self.save_preferences(map_height=self.canvas_splitter.sizes()[0])

    def update_map_status(self,status):
        self.map_status.setText(status['message'])
        self.map_status.setVisible(status['state']!='ready')
        self.map_retry.setVisible(status['state']=='error')

    def select_map_bounds(self,bounds):
        self.geo_bounds = bounds
        self.search()

    def assign_place(self):
        if not self.selected_asset:
            return
        asset = dict(self.selected_asset)
        dialog = QDialog(self)
        dialog.setWindowTitle('Место в каталоге')
        form = QFormLayout(dialog)
        form.addRow(QLabel('Сохраняется в каталоге. GPS и содержимое оригинала не изменяются.'))
        name = QLineEdit(asset.get('user_place',''))
        latitude = QLineEdit(str(asset['user_latitude']) if asset.get('user_latitude') is not None else '')
        longitude = QLineEdit(str(asset['user_longitude']) if asset.get('user_longitude') is not None else '')
        form.addRow('Название',name)
        form.addRow('Широта (необязательно)',latitude)
        form.addRow('Долгота (необязательно)',longitude)
        error = QLabel()
        form.addRow(error)
        def save():
            try:
                lat = float(latitude.text().replace(',','.')) if latitude.text().strip() else None
                lon = float(longitude.text().replace(',','.')) if longitude.text().strip() else None
                if (lat is None)!=(lon is None) or lat is not None and not(-90<=lat<=90 and -180<=lon<=180):
                    raise ValueError()
            except ValueError:
                error.setText('Проверьте обе координаты')
                return
            self.backend.send(action='library_set_place',asset_id=asset['id'],version=asset['version'],name=name.text(),latitude=lat,longitude=lon)
            dialog.accept()
        button = QPushButton('Сохранить')
        button.clicked.connect(save)
        form.addRow(button)
        dialog.exec()

    def workspace_event(self,event):
        kind = event['type']
        if kind == 'library_navigation':
            if event.get('serial')==self.navigation_serial:
                self.fill_navigation(event)
        elif kind == 'library_places':
            if event.get('serial')==self.map_serial:
                self.map_widget.set_points(event['points'])
                self.places_list.clear()
                for row in event['places']:
                    item = QListWidgetItem(f"{row['place']}  ·  {row['count']}")
                    item.setData(Qt.UserRole,row['place'])
                    self.places_list.addItem(item)
        elif kind == 'library_storyboard':
            self.gallery.set_storyboard(event)
        elif kind in ('library_save_person','library_merge_people','library_delete_person','library_split_person'):
            self.refresh_people(event['people'])
            if kind == 'library_save_person':
                self.current_person_id = event['person_id'] if len(self.selected_people)<=1 else None
            self.message.setText('Каталог людей сохранён')
        elif kind in ('library_set_place','library_stack_cover','library_unstack'):
            self.search(preserve_position=True)
        elif kind == 'library_error':
            self.message.setText(event['message'])
        else:
            return False
        return True

    def apply_theme(self,theme,save=True):
        from .ui import STYLE
        app = QApplication.instance()
        dark = theme=='dark' or theme=='system' and app.styleHints().colorScheme()==Qt.ColorScheme.Dark
        palette = QPalette()
        for role,color in [(QPalette.Window,'#25292d' if dark else '#eef0f1'),(QPalette.WindowText,'#e5e9eb' if dark else '#26373d'),
                           (QPalette.Base,'#202428' if dark else '#ffffff'),(QPalette.Text,'#e5e9eb' if dark else '#26373d'),
                           (QPalette.Button,'#343a3e' if dark else '#f9fafb'),(QPalette.ButtonText,'#e5e9eb' if dark else '#26373d'),
                           (QPalette.Highlight,'#177c70'),(QPalette.HighlightedText,'#ffffff')]:
            palette.setColor(role,QColor(color))
        app.setPalette(palette)
        replacements = {'#f4f6f8':'#25292d','#24323c':'#e5e9eb','#133d38':'#b5ddd6','#233d3a':'#d6e8e4',
                        '#d5dfe3':'#465156','#e1e6ea':'#465156','#74828b':'#a4b0b5','#eef5f3':'#364f4c',
                        '#edf1f3':'#30383c','#bfccd0':'#617078','#f1f3f5':'#30383c','background: white':'background: #30363a',
                        'background: #fff;':'background: #30363a;'}
        style = STYLE
        if dark:
            for old,new in replacements.items():
                style = style.replace(old,new)
        style += '\nQTreeWidget,QListWidget { border:0; background:transparent; } QTabWidget::pane { border:0; } QPushButton:checked { background:#177c70; color:white; }'
        app.setStyleSheet(style)
        self.map_widget.set_theme(dark)
        if save:
            self.save_preferences(theme=theme)
