import sys
import os
import re
import platform
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

from PySide6.QtCore import (
    Qt, QObject, QEvent,
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, QSettings,
    QByteArray, QMimeData, Signal, QPoint, QItemSelectionModel, QStandardPaths
)
from PySide6.QtGui import QAction, QKeySequence, QPixmap, QCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QFileDialog, QMessageBox, QStackedWidget, QLineEdit, QTableView, QFormLayout,
    QSpinBox, QCheckBox, QComboBox, QStatusBar, QListWidget, QListWidgetItem,
    QAbstractItemView, QFrame, QMenu
)

# ---------------- Parsing / writing ----------------

STREAM_RE = re.compile(r'^\s*stream_data\[(\d+)\]\s*:\s*"(.*)"\s*$')
STREAM_COUNT_RE = re.compile(r'^(\s*stream_data\s*:\s*)(\d+)(\s*)$')


@dataclass
class Station:
    url: str
    name: str
    genre: str
    language: str
    bitrate: int
    favorite: bool


def resource_path(relative_name: str) -> str:
    """Works in dev + PyInstaller (Windows/macOS)."""
    if hasattr(sys, "_MEIPASS"):
        return str(Path(sys._MEIPASS) / relative_name)  # type: ignore[attr-defined]
    return str(Path(__file__).resolve().parent / relative_name)


def _candidate_documents_dirs() -> list[Path]:
    """Cross-platform Documents discovery via Qt + common fallbacks."""
    cands: list[Path] = []
    try:
        qt_docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        if qt_docs:
            cands.append(Path(qt_docs))
    except Exception:
        pass

    home = Path.home()
    cands.append(home / "Documents")

    if platform.system().lower() == "windows":
        one = home / "OneDrive" / "Documents"
        if one.exists():
            cands.append(one)

    seen = set()
    out: list[Path] = []
    for p in cands:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def find_game_file(game_folder_name: str) -> Optional[Path]:
    for docs in _candidate_documents_dirs():
        p = docs / game_folder_name / "live_streams.sii"
        if p.exists():
            return p
    return None


def parse_live_streams(path: str):
    p = Path(path)
    if not p.exists():
        raise ValueError("File does not exist.")

    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(True)

    stations: list[Station] = []
    station_line_indexes: list[int] = []

    for i, line in enumerate(lines):
        m = STREAM_RE.match(line.rstrip("\n"))
        if not m:
            continue
        payload = m.group(2)
        parts = payload.split("|")
        if len(parts) != 6:
            continue

        url, name, genre, lang, bitrate, fav = parts
        try:
            bitrate_i = int(bitrate)
        except ValueError:
            bitrate_i = 0

        stations.append(Station(
            url=url.strip(),
            name=name.strip(),
            genre=genre.strip(),
            language=lang.strip(),
            bitrate=bitrate_i,
            favorite=(fav.strip() == "1")
        ))
        station_line_indexes.append(i)

    if not stations:
        raise ValueError("No stream_data[...] station entries found in this file.")

    return lines, station_line_indexes, stations


def station_to_line(i: int, s: Station) -> str:
    for field in (s.url, s.name, s.genre, s.language):
        if "|" in field:
            raise ValueError("Fields cannot contain the '|' character.")
    fav = "1" if s.favorite else "0"
    return f'stream_data[{i}]: "{s.url}|{s.name}|{s.genre}|{s.language}|{int(s.bitrate)}|{fav}"\n'


def _find_trailing_brace_tail_start(lines: list[str]) -> int:
    """Keep closing braces at the bottom by inserting new station lines before brace-tail."""
    i = len(lines)
    while i > 0:
        t = lines[i - 1].strip()
        if t == "" or t == "}":
            i -= 1
            continue
        break
    return i


def _update_stream_data_count_line(lines: list[str], count: int) -> bool:
    """Update the 'stream_data: N' count line to match number of stations."""
    for i, line in enumerate(lines):
        m = STREAM_COUNT_RE.match(line.rstrip("\n"))
        if not m:
            continue
        prefix, _old, suffix = m.groups()
        newline = "\n" if line.endswith("\n") else ""
        lines[i] = f"{prefix}{count}{suffix}{newline}"
        return True
    return False


def write_live_streams(path: str, original_lines, station_line_indexes, stations, strategy: str = "in_place"):
    """
    strategy:
      - "in_place": rewrite station block at same location
      - "new_slot": append clean station block before final braces block
    Always creates a backup in <folder>/live_streams_backup/
    """
    p = Path(path)

    backup_dir = p.parent / "live_streams_backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = backup_dir / f"{p.name}.bak_{ts}"
    backup.write_text("".join(original_lines), encoding="utf-8")

    new_station_lines = [station_to_line(i, s) for i, s in enumerate(stations)]

    lines = list(original_lines)
    idxs = sorted(station_line_indexes)

    # Remove old station lines
    for i in reversed(idxs):
        del lines[i]

    if strategy == "new_slot":
        tail_start = _find_trailing_brace_tail_start(lines)
        prefix = lines[:tail_start]
        tail = lines[tail_start:]

        if prefix and prefix[-1].strip() != "":
            prefix.append("\n")

        station_block = list(new_station_lines)

        # optional spacing before brace-tail
        if tail and tail[0].strip() == "}":
            if station_block and station_block[-1].strip() != "":
                station_block.append("\n")

        lines = prefix + station_block + tail
    else:
        first = idxs[0]
        first = max(0, min(first, len(lines)))
        for offset, line in enumerate(new_station_lines):
            lines.insert(first + offset, line)

    # Fix stream_data count line to match station count
    _update_stream_data_count_line(lines, len(stations))

    p.write_text("".join(lines), encoding="utf-8")
    return str(backup)


# ---------------- Junction / linking (FORCE ADMIN SYMLINK when needed) ----------------

def _is_windows() -> bool:
    return platform.system().lower() == "windows"


def _path_exists_any(p: Path) -> bool:
    return p.exists() or p.is_symlink()


def _remove_path_any(p: Path) -> None:
    """Remove file OR symlink OR directory/junction if some prior attempt left a weird object."""
    if not _path_exists_any(p):
        return

    if p.is_symlink():
        try:
            p.unlink()
            return
        except IsADirectoryError:
            os.rmdir(str(p))
            return

    if p.is_dir():
        shutil.rmtree(str(p), ignore_errors=False)
    else:
        p.unlink()


def _safe_backup_existing_dest(dest: Path) -> Optional[Path]:
    """
    If dest exists and is a normal file -> rename to .backup (timestamp if needed).
    If dest is a symlink or directory/junction -> remove it (no backup).
    """
    if not _path_exists_any(dest):
        return None

    if dest.is_dir() or dest.is_symlink():
        _remove_path_any(dest)
        return None

    backup = dest.with_name(dest.name + ".backup")
    if backup.exists() or backup.is_symlink():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = dest.with_name(dest.name + f".backup_{ts}")
    dest.rename(backup)
    return backup


def _run_cmd(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(["cmd", "/c", command], capture_output=True, text=True)


def _mklink_symlink_admin(dest: Path, src: Path) -> Tuple[bool, str]:
    """
    Force file symlink via UAC elevation using a temp .cmd and capturing output to a temp log.
    This avoids the 'needs admin' problem for symlink creation.
    """
    temp_dir = Path(os.environ.get("TEMP", str(Path.home())))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    cmd_file = temp_dir / f"radio_link_{ts}.cmd"
    log_file = temp_dir / f"radio_link_{ts}.log"

    mk_cmd = f'mklink "{dest}" "{src}"'  # FILE symlink (no /D, no /H)

    cmd_contents = (
        "@echo off\n"
        f'{mk_cmd} > "{log_file}" 2>&1\n'
        "exit /b %errorlevel%\n"
    )
    cmd_file.write_text(cmd_contents, encoding="utf-8")

    ps = f'Start-Process -FilePath "{cmd_file}" -Verb RunAs -WindowStyle Hidden -Wait'
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True,
        text=True
    )

    output = ""
    if log_file.exists():
        try:
            output = log_file.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            output = ""

    # cleanup cmd (keep log for debugging if you want)
    try:
        cmd_file.unlink(missing_ok=True)  # type: ignore[arg-type]
    except Exception:
        try:
            if cmd_file.exists():
                cmd_file.unlink()
        except Exception:
            pass

    # verify
    if dest.exists() or dest.is_symlink():
        return True, output or "mklink completed."

    extra = (r.stderr or r.stdout or "").strip()
    if extra and output:
        output = output + "\n\n" + extra
    elif extra and not output:
        output = extra

    return False, output or "mklink failed or was canceled."


def link_live_streams_force_admin_symlink(src_path: Path, dest_path: Path) -> Tuple[Optional[Path], str]:
    """
    Creates a link so editing either game affects the other.
    Order:
      1) If same drive: try hardlink (os.link) first (no admin)
      2) If hardlink not possible / fails: force elevated symlink via mklink (UAC)
    Destination handling:
      - If destination exists (normal file): rename to .backup then link
      - If destination missing: link, no backup
      - If destination is symlink/dir/junction: remove it, no backup
    """
    if not _is_windows():
        raise RuntimeError("This feature is Windows-only.")

    if not src_path.exists() or not src_path.is_file():
        raise RuntimeError(f"Source file not found or not a file:\n{src_path}")

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    backup = _safe_backup_existing_dest(dest_path)
    if _path_exists_any(dest_path):
        _remove_path_any(dest_path)

    # Try hardlink first if same drive (true shared file, no admin)
    try:
        if src_path.drive and dest_path.drive and src_path.drive.lower() == dest_path.drive.lower():
            os.link(str(src_path), str(dest_path))
            msg = "Created sync successfully (hardlink)."
            if backup:
                msg += f"\nRenamed existing destination to:\n{backup}"
            return backup, msg
    except Exception:
        pass

    # Force admin symlink for cross-drive (or if hardlink fails)
    ok, out = _mklink_symlink_admin(dest_path, src_path)
    if ok:
        msg = "Created sync successfully (symlink, elevated)."
        if backup:
            msg += f"\nRenamed existing destination to:\n{backup}"
        return backup, msg

    raise RuntimeError(
        "Failed to create symlink (elevated).\n\n"
        f'Attempted:\nmklink "{dest_path}" "{src_path}"\n\n'
        f"Output:\n{out}"
    )


# ---------------- Table model / drag multi-move ----------------

COL_FAV, COL_NAME, COL_GENRE, COL_LANG, COL_BITRATE, COL_URL = range(6)
HEADERS = ["★", "Name", "Genre", "Language", "Bitrate", "URL"]
MIME_ROWS = "application/x-radioeditor-rows"


class StationModel(QAbstractTableModel):
    reordered = Signal()

    def __init__(self, stations=None):
        super().__init__()
        self.stations: list[Station] = stations or []
        self.drag_enabled = False

    def rowCount(self, parent=QModelIndex()):
        return len(self.stations)

    def columnCount(self, parent=QModelIndex()):
        return len(HEADERS)

    def headerData(self, section, orientation, role):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return HEADERS[section]
        return section + 1

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        s = self.stations[index.row()]
        c = index.column()

        if role == Qt.UserRole:
            if c == COL_FAV:
                return 1 if s.favorite else 0
            if c == COL_NAME:
                return s.name.lower()
            if c == COL_GENRE:
                return s.genre.lower()
            if c == COL_LANG:
                return s.language.lower()
            if c == COL_BITRATE:
                return int(s.bitrate)
            if c == COL_URL:
                return s.url.lower()

        if role in (Qt.DisplayRole, Qt.EditRole):
            if c == COL_FAV:
                return "★" if s.favorite else "☆"
            if c == COL_NAME:
                return s.name
            if c == COL_GENRE:
                return s.genre
            if c == COL_LANG:
                return s.language
            if c == COL_BITRATE:
                return s.bitrate
            if c == COL_URL:
                return s.url

        return None

    def flags(self, index):
        base = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if not index.isValid():
            if self.drag_enabled:
                return base | Qt.ItemIsDropEnabled
            return base
        if self.drag_enabled:
            base |= Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled
        return base

    def removeRows(self, row, count, parent=QModelIndex()):
        if row < 0 or count <= 0 or row + count > len(self.stations):
            return False
        self.beginRemoveRows(parent, row, row + count - 1)
        del self.stations[row: row + count]
        self.endRemoveRows()
        return True

    def mimeTypes(self):
        return [MIME_ROWS]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes if i.isValid()})
        md = QMimeData()
        md.setData(MIME_ROWS, QByteArray(",".join(map(str, rows)).encode("utf-8")))
        return md

    def supportedDragActions(self):
        return Qt.MoveAction

    def supportedDropActions(self):
        return Qt.MoveAction

    def dropMimeData(self, data, action, row, column, parent):
        if not self.drag_enabled:
            return False
        if action != Qt.MoveAction:
            return False
        if not data.hasFormat(MIME_ROWS):
            return False

        raw = bytes(data.data(MIME_ROWS)).decode("utf-8").strip()
        if not raw:
            return False

        src_rows = []
        for x in raw.split(","):
            x = x.strip()
            if x.isdigit():
                src_rows.append(int(x))
        src_rows = sorted(set(src_rows))
        if not src_rows:
            return False
        if any(r < 0 or r >= len(self.stations) for r in src_rows):
            return False

        if row == -1:
            dst = parent.row() if parent.isValid() else len(self.stations)
        else:
            dst = row

        dst = max(0, min(dst, len(self.stations)))

        if src_rows[0] <= dst <= (src_rows[-1] + 1):
            return False

        removed_before_dst = sum(1 for r in src_rows if r < dst)
        dst_adj = max(0, dst - removed_before_dst)

        src_set = set(src_rows)
        dragged = [s for i, s in enumerate(self.stations) if i in src_set]
        remaining = [s for i, s in enumerate(self.stations) if i not in src_set]

        dst_adj = min(dst_adj, len(remaining))
        new_list = remaining[:dst_adj] + dragged + remaining[dst_adj:]

        self.beginResetModel()
        self.stations = new_list
        self.endResetModel()

        self.reordered.emit()
        return True


# ---------------- Selection clear filter ----------------

class GlobalClickClearSelectionFilter(QObject):
    def __init__(self, editor_page: "EditorPage"):
        super().__init__()
        self.editor_page = editor_page

    def _is_inside_table(self, w: Optional[QWidget]) -> bool:
        t = self.editor_page.table
        if w is None:
            return False
        if w == t or w == t.viewport():
            return True
        if w == t.horizontalHeader() or w == t.verticalHeader():
            return True
        try:
            if t.isAncestorOf(w):
                return True
        except Exception:
            pass
        return False

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress:
            try:
                gp = event.globalPosition().toPoint()
            except Exception:
                try:
                    gp = event.globalPos()
                except Exception:
                    return False
            w = QApplication.widgetAt(gp)
            if not self._is_inside_table(w):
                self.editor_page.table.clearSelection()
                self.editor_page.table.setCurrentIndex(QModelIndex())
        return False


# ---------------- Game tiles ----------------

class GameTile(QWidget):
    clicked = Signal()

    def __init__(self, label: str, width_px: int, height_px: int):
        super().__init__()
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self._w = width_px
        self._h = height_px

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.img = QLabel()
        self.img.setAlignment(Qt.AlignCenter)
        self.img.setFixedSize(self._w, self._h)

        self.caption = QLabel(label)
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setStyleSheet("font-size: 14px; font-weight: 600;")

        self.subcaption = QLabel("")
        self.subcaption.setAlignment(Qt.AlignCenter)
        self.subcaption.setStyleSheet("font-size: 12px; color: #bbbbbb;")

        lay.addWidget(self.img, 0, Qt.AlignCenter)
        lay.addWidget(self.caption, 0, Qt.AlignCenter)
        lay.addWidget(self.subcaption, 0, Qt.AlignCenter)

    def set_pixmap(self, pix: QPixmap):
        scaled = pix.scaled(self._w, self._h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.img.setPixmap(scaled)

    def set_customized_label(self, on: bool):
        self.subcaption.setText("(Customized)" if on else "")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# ---------------- Main page ----------------

class OpenPage(QWidget):
    def __init__(self, on_open_callback, settings: QSettings):
        super().__init__()
        self.on_open_callback = on_open_callback
        self.settings = settings

        self.ats_path: Optional[Path] = None
        self.ets2_path: Optional[Path] = None

        layout = QVBoxLayout(self)

        title = QLabel("Radio Station Editor")
        title.setStyleSheet("font-size: 34px; font-weight: 600;")
        layout.addWidget(title)

        layout.addWidget(QLabel("Click a game below to edit its radio stations (single click)."))

        btn_row = QHBoxLayout()
        self.btn_choose = QPushButton("Choose file…")
        self.btn_choose.clicked.connect(self.choose_file_and_associate)
        btn_row.addWidget(self.btn_choose)

        self.btn_refresh = QPushButton("Refresh game status")
        self.btn_refresh.clicked.connect(self.refresh_game_tiles)
        btn_row.addWidget(self.btn_refresh)
        layout.addLayout(btn_row)

        tiles_row = QHBoxLayout()
        tiles_row.setSpacing(24)

        self.tile_ats = GameTile("American Truck Simulator", width_px=172, height_px=400)
        self.tile_ets2 = GameTile("Euro Truck Simulator 2", width_px=216, height_px=400)

        self.tile_ats.clicked.connect(self.open_ats)
        self.tile_ets2.clicked.connect(self.open_ets2)

        tiles_row.addStretch(1)
        tiles_row.addWidget(self.tile_ats, 0, Qt.AlignCenter)
        tiles_row.addWidget(self.tile_ets2, 0, Qt.AlignCenter)
        tiles_row.addStretch(1)
        layout.addLayout(tiles_row)

        junction_row = QHBoxLayout()
        self.btn_junction_ats_into_ets2 = QPushButton("Sync ATS stations → ETS2")
        self.btn_junction_ats_into_ets2.clicked.connect(lambda: self.junction("ATS", "ETS2"))
        junction_row.addWidget(self.btn_junction_ats_into_ets2)

        self.btn_junction_ets2_into_ats = QPushButton("Sync ETS2 stations → ATS")
        self.btn_junction_ets2_into_ats.clicked.connect(lambda: self.junction("ETS2", "ATS"))
        junction_row.addWidget(self.btn_junction_ets2_into_ats)

        layout.addLayout(junction_row)

        if not _is_windows():
            self.btn_junction_ats_into_ets2.setEnabled(False)
            self.btn_junction_ets2_into_ats.setEnabled(False)
            self.btn_junction_ats_into_ets2.setToolTip("Windows-only feature.")
            self.btn_junction_ets2_into_ats.setToolTip("Windows-only feature.")

        copy_row = QHBoxLayout()
        self.btn_copy_ats_to_ets2 = QPushButton("Copy ATS stations → ETS2")
        self.btn_copy_ats_to_ets2.clicked.connect(lambda: self.copy_stations("ATS", "ETS2"))
        copy_row.addWidget(self.btn_copy_ats_to_ets2)

        self.btn_copy_ets2_to_ats = QPushButton("Copy ETS2 stations → ATS")
        self.btn_copy_ets2_to_ats.clicked.connect(lambda: self.copy_stations("ETS2", "ATS"))
        copy_row.addWidget(self.btn_copy_ets2_to_ats)

        layout.addLayout(copy_row)

        layout.addWidget(QLabel("Recent files:"))
        self.recent_list = QListWidget()
        self.recent_list.itemDoubleClicked.connect(self.open_from_list)
        layout.addWidget(self.recent_list)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #ffb3b3;")
        layout.addWidget(self.error_label)

        self.refresh_recent()
        self.refresh_game_tiles()

    def set_manual_game_file(self, game: str, path: str):
        self.settings.setValue("manual_ats_path" if game == "ATS" else "manual_ets2_path", path)

    def clear_manual_game_file(self, game: str):
        self.settings.setValue("manual_ats_path" if game == "ATS" else "manual_ets2_path", "")

    def clear_all_manual_game_files(self):
        self.settings.setValue("manual_ats_path", "")
        self.settings.setValue("manual_ets2_path", "")

    def get_manual_game_file(self, game: str) -> Optional[Path]:
        key = "manual_ats_path" if game == "ATS" else "manual_ets2_path"
        val = str(self.settings.value(key, "") or "").strip()
        if not val:
            return None
        p = Path(val)
        return p if p.exists() else None

    def refresh_recent(self):
        self.recent_list.clear()
        recents = self.settings.value("recent_files", [])
        if not isinstance(recents, list):
            recents = []
        for p in recents:
            self.recent_list.addItem(QListWidgetItem(p))

    def add_recent(self, path: str):
        recents = self.settings.value("recent_files", [])
        if not isinstance(recents, list):
            recents = []
        recents = [p for p in recents if str(p).lower() != str(path).lower()]
        recents.insert(0, path)
        recents = recents[:5]
        self.settings.setValue("recent_files", recents)
        self.refresh_recent()

    def choose_file_and_associate(self):
        last_dir = self.settings.value("last_dir", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(
            self, "Select live_streams.sii", str(last_dir),
            "SII files (*.sii);;All files (*.*)"
        )
        if not path:
            return
        self.settings.setValue("last_dir", str(Path(path).parent))

        mb = QMessageBox(self)
        mb.setWindowTitle("Choose game")
        mb.setText("Which game is this live_streams.sii for?")
        btn_ats = mb.addButton("ATS", QMessageBox.AcceptRole)
        btn_ets2 = mb.addButton("ETS2", QMessageBox.AcceptRole)
        btn_cancel = mb.addButton("Cancel", QMessageBox.RejectRole)
        mb.setDefaultButton(btn_ats)
        mb.exec()

        if mb.clickedButton() == btn_cancel:
            return
        game = "ATS" if mb.clickedButton() == btn_ats else "ETS2"

        try:
            parse_live_streams(path)
        except Exception as e:
            QMessageBox.warning(self, "Invalid file", f"That file can't be used:\n\n{e}")
            return

        self.set_manual_game_file(game, path)
        self.add_recent(path)
        self.refresh_game_tiles()

        try:
            self.on_open_callback(path)
        except Exception as e:
            self.error_label.setText(str(e))

    def open_from_list(self, item: QListWidgetItem):
        self.try_open(item.text())

    def try_open(self, path: str):
        self.error_label.setText("")
        try:
            self.on_open_callback(path)
            self.add_recent(path)
        except Exception as e:
            self.error_label.setText(str(e))

    def refresh_game_tiles(self):
        self.error_label.setText("")

        manual_ats = self.get_manual_game_file("ATS")
        manual_ets2 = self.get_manual_game_file("ETS2")

        auto_ats = find_game_file("American Truck Simulator")
        auto_ets2 = find_game_file("Euro Truck Simulator 2")

        self.ats_path = manual_ats or auto_ats
        self.ets2_path = manual_ets2 or auto_ets2

        ats_active = QPixmap(resource_path("ATS_active.png"))
        ats_inactive = QPixmap(resource_path("ATS_inactive.png"))
        ets2_active = QPixmap(resource_path("ETS2_active.png"))
        ets2_inactive = QPixmap(resource_path("ETS2_inactive.png"))

        if self.ats_path:
            self.tile_ats.set_pixmap(ats_active)
            self.tile_ats.set_customized_label(bool(manual_ats))
            self.tile_ats.setToolTip(str(self.ats_path))
        else:
            self.tile_ats.set_pixmap(ats_inactive)
            self.tile_ats.set_customized_label(False)
            self.tile_ats.setToolTip("ATS live_streams.sii not found / not chosen")

        if self.ets2_path:
            self.tile_ets2.set_pixmap(ets2_active)
            self.tile_ets2.set_customized_label(bool(manual_ets2))
            self.tile_ets2.setToolTip(str(self.ets2_path))
        else:
            self.tile_ets2.set_pixmap(ets2_inactive)
            self.tile_ets2.set_customized_label(False)
            self.tile_ets2.setToolTip("ETS2 live_streams.sii not found / not chosen")

    def open_ats(self):
        if not self.ats_path:
            QMessageBox.information(
                self, "ATS file missing",
                "ATS live_streams.sii was not found and no file was chosen.\n\n"
                "Use “Choose file…” to associate a file to ATS."
            )
            return
        self.try_open(str(self.ats_path))

    def open_ets2(self):
        if not self.ets2_path:
            QMessageBox.information(
                self, "ETS2 file missing",
                "ETS2 live_streams.sii was not found and no file was chosen.\n\n"
                "Use “Choose file…” to associate a file to ETS2."
            )
            return
        self.try_open(str(self.ets2_path))

    def _default_dest_path_for_game(self, game: str) -> Optional[Path]:
        folder = "American Truck Simulator" if game == "ATS" else "Euro Truck Simulator 2"
        for docs in _candidate_documents_dirs():
            game_dir = docs / folder
            if game_dir.exists():
                return game_dir / "live_streams.sii"
        return None

    def junction(self, src_game: str, dst_game: str):
        if not _is_windows():
            QMessageBox.information(self, "Not available", "This feature is Windows-only.")
            return

        self.refresh_game_tiles()
        src_path = self.ats_path if src_game == "ATS" else self.ets2_path
        dst_path = self.ats_path if dst_game == "ATS" else self.ets2_path

        if src_path is None:
            QMessageBox.warning(self, "Syncing failed", f"{src_game} live_streams.sii is not available.")
            return

        # If the destination isn't found/associated, target the default location anyway
        if dst_path is None:
            dst_path = self._default_dest_path_for_game(dst_game)
            if dst_path is None:
                QMessageBox.warning(
                    self, "Syncing failed",
                    f"{dst_game} folder not found in Documents. Choose/associate the {dst_game} file first."
                )
                return

        text = (
            f"This will sync {dst_game}'s live_streams.sii to {src_game}'s live_streams.sii.\n\n"
            f"Source:\n{src_path}\n\nDestination:\n{dst_path}\n\n"
            "If the destination is a normal file, it will be renamed to *.backup.\n"
            "If the destination is missing, the syncing will be created with no backup.\n\n"
            "This may trigger a UAC admin prompt (required for symlinks on many systems).\n\nContinue?"
        )
        if QMessageBox.question(self, "Confirm Syncing", text) != QMessageBox.Yes:
            return

        try:
            backup, msg = link_live_streams_force_admin_symlink(Path(src_path), Path(dst_path))
            self.refresh_game_tiles()

            mw = self.window()
            if isinstance(mw, QMainWindow):
                mw.statusBar().showMessage(msg, 9000)

            QMessageBox.information(self, "Syncing complete", msg)
        except Exception as e:
            QMessageBox.critical(self, "Syncing failed", str(e))

    def copy_stations(self, src_game: str, dst_game: str):
        self.refresh_game_tiles()

        src_path = self.ats_path if src_game == "ATS" else self.ets2_path
        dst_path = self.ats_path if dst_game == "ATS" else self.ets2_path

        if not src_path:
            QMessageBox.warning(self, "Copy failed", f"{src_game} live_streams.sii not available.")
            return
        if not dst_path:
            QMessageBox.warning(self, "Copy failed", f"{dst_game} live_streams.sii not available.")
            return

        if QMessageBox.question(
            self, "Confirm copy",
            f"This will overwrite {dst_game}'s station list with {src_game}'s station list.\n\nContinue?"
        ) != QMessageBox.Yes:
            return

        try:
            _, _, src_stations = parse_live_streams(str(src_path))
            dst_lines, dst_idxs, _ = parse_live_streams(str(dst_path))
            backup = write_live_streams(str(dst_path), dst_lines, dst_idxs, src_stations, strategy="in_place")

            mw = self.window()
            if isinstance(mw, QMainWindow):
                mw.statusBar().showMessage(f"Saved. Created Backup File. Backup: {backup}", 9000)

            QMessageBox.information(self, "Copy complete", f"Saved. Created Backup File.\nBackup: {backup}")
        except Exception as e:
            QMessageBox.critical(self, "Copy failed", str(e))


# ---------------- Editor page ----------------

class EditorPage(QWidget):
    def __init__(self, on_back_callback):
        super().__init__()
        self.on_back_callback = on_back_callback

        self.file_path: Optional[str] = None
        self.original_lines = None
        self.station_line_indexes = None
        self._suppress_reorder_autosave = False

        self.model = StationModel([])
        self.model.reordered.connect(self.on_model_reordered)

        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setSortRole(Qt.UserRole)

        main = QHBoxLayout(self)

        left = QVBoxLayout()
        topbar = QHBoxLayout()

        self.lbl_file = QLabel("File: (none)")
        topbar.addWidget(self.lbl_file, 1)

        self.btn_back = QPushButton("Back")
        self.btn_back.clicked.connect(self.on_back_callback)
        topbar.addWidget(self.btn_back)

        left.addLayout(topbar)

        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.textChanged.connect(self.on_view_mode_changed)
        controls.addWidget(self.search, 1)

        self.order_mode = QComboBox()
        self.order_mode.addItems([
            "Custom order (drag)",
            "Sort by Name",
            "Sort by Favorite",
            "Sort by Genre",
            "Sort by Language",
        ])
        self.order_mode.currentIndexChanged.connect(self.on_view_mode_changed)
        controls.addWidget(self.order_mode)
        left.addLayout(controls)

        self.table = QTableView()
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.doubleClicked.connect(self.on_row_double_clicked)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropMode(QAbstractItemView.InternalMove)
        self.table.setDragDropOverwriteMode(False)
        self.table.setDefaultDropAction(Qt.MoveAction)

        self.table.clicked.connect(self.on_table_clicked)

        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)

        left.addWidget(self.table, 1)

        self._clear_sel_filter = GlobalClickClearSelectionFilter(self)
        QApplication.instance().installEventFilter(self._clear_sel_filter)

        right = QVBoxLayout()
        form_title = QLabel("Add / Edit Station")
        form_title.setStyleSheet("font-size: 18px; font-weight: 600;")
        right.addWidget(form_title)

        info_box = QFrame()
        info_box.setFrameShape(QFrame.StyledPanel)
        info_box.setStyleSheet("QFrame{border:1px solid #555; border-radius:6px; padding:8px;}")
        info_lay = QVBoxLayout(info_box)
        info_lay.setContentsMargins(10, 10, 10, 10)
        info_lay.setSpacing(6)

        info_text = QLabel("Info: The games accepts MP3 streams only. ALWAYS test the URL in-game.")
        info_text.setWordWrap(True)
        info_lay.addWidget(info_text)

        link = QLabel('Find MP3 stations on the net, like <a href="https://fmstream.org">fmstream.org</a>.')
        link.setOpenExternalLinks(True)
        info_lay.addWidget(link)

        right.addWidget(info_box)

        form = QFormLayout()
        self.in_name = QLineEdit()

        self.in_url = QLineEdit()
        self.url_info_badge = QLabel("ℹ")
        self.url_info_badge.setToolTip("MP3 streams only. Test URL in-game.")
        self.url_info_badge.setAlignment(Qt.AlignCenter)
        self.url_info_badge.setFixedWidth(22)

        url_row = QWidget()
        url_row_lay = QHBoxLayout(url_row)
        url_row_lay.setContentsMargins(0, 0, 0, 0)
        url_row_lay.setSpacing(6)
        url_row_lay.addWidget(self.url_info_badge)
        url_row_lay.addWidget(self.in_url, 1)

        self.in_lang = QLineEdit()
        self.in_bitrate = QSpinBox()
        self.in_bitrate.setRange(0, 2000)
        self.in_genre = QLineEdit()
        self.in_fav = QCheckBox("Favorite")

        form.addRow("Name", self.in_name)
        form.addRow("URL", url_row)
        form.addRow("Language", self.in_lang)
        form.addRow("Bitrate", self.in_bitrate)
        form.addRow("Genre", self.in_genre)
        form.addRow("", self.in_fav)
        right.addLayout(form)

        btns = QHBoxLayout()
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear_fields_for_new_station)
        btns.addWidget(self.btn_clear)

        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(self.save_station)
        btns.addWidget(self.btn_save)

        right.addLayout(btns)

        right.addStretch(1)
        self.instructions = QLabel(
            "Tips:\n"
            "• Shift + Left Click: select a range\n"
            "• Ctrl + Left Click: select multiple\n"
            "• Holding Left-Click Down + Drag selected rows: move multiple stations together\n"
            "• Right-click a row: Favorite / Unfavorite / Delete\n"
            "• Double-click a station (non-star): load into the Add/Edit fields\n"
            "• Click the star column: toggle Favorite"
        )
        self.instructions.setWordWrap(True)
        self.instructions.setAlignment(Qt.AlignRight | Qt.AlignBottom)
        self.instructions.setStyleSheet("color: #bbbbbb; font-size: 12px;")
        right.addWidget(self.instructions, 0, Qt.AlignRight | Qt.AlignBottom)

        main.addLayout(left, 3)
        main.addLayout(right, 2)

        self.current_edit_source_row: Optional[int] = None
        self.on_view_mode_changed()

    def clear_fields_for_new_station(self):
        self.current_edit_source_row = None
        self.in_name.setText("")
        self.in_url.setText("")
        self.in_lang.setText("")
        self.in_bitrate.setValue(0)
        self.in_genre.setText("")
        self.in_fav.setChecked(False)

        mw = self.window()
        if isinstance(mw, QMainWindow):
            mw.statusBar().showMessage("Cleared fields. Save will add a new station.", 3500)

    def is_form_effectively_empty(self) -> bool:
        return (
            self.in_name.text().strip() == ""
            and self.in_url.text().strip() == ""
            and self.in_genre.text().strip() == ""
            and self.in_lang.text().strip() == ""
            and int(self.in_bitrate.value()) == 0
            and not self.in_fav.isChecked()
        )

    def load_file(self, path: str):
        self._suppress_reorder_autosave = True
        try:
            lines, idxs, stations = parse_live_streams(path)
            self.file_path = path
            self.original_lines = lines
            self.station_line_indexes = idxs

            self.model.beginResetModel()
            self.model.stations = stations
            self.model.endResetModel()

            self.lbl_file.setText(f"File: {path}")
            self.clear_fields_for_new_station()
            self.on_view_mode_changed()
        finally:
            self._suppress_reorder_autosave = False

    def is_custom_drag_mode_active(self) -> bool:
        return self.order_mode.currentIndex() == 0 and self.search.text().strip() == ""

    def on_view_mode_changed(self):
        search_text = self.search.text().strip()
        mode = self.order_mode.currentIndex()

        self.proxy.setFilterFixedString(search_text if search_text else "")
        custom_ok = (mode == 0 and search_text == "")

        if custom_ok:
            self.table.setModel(self.model)
            self.model.drag_enabled = True
            self.table.setSortingEnabled(False)
        else:
            self.table.setModel(self.proxy)
            self.model.drag_enabled = False
            self.table.setSortingEnabled(True)

            if mode == 1:
                self.proxy.sort(COL_NAME, Qt.AscendingOrder)
            elif mode == 2:
                self.proxy.sort(COL_FAV, Qt.DescendingOrder)
            elif mode == 3:
                self.proxy.sort(COL_GENRE, Qt.AscendingOrder)
            elif mode == 4:
                self.proxy.sort(COL_LANG, Qt.AscendingOrder)

        self.table.resizeColumnsToContents()

    def _get_source_row_from_index(self, index: QModelIndex) -> Optional[int]:
        if not index.isValid():
            return None
        current_model = self.table.model()
        if current_model is self.proxy:
            return self.proxy.mapToSource(index).row()
        return index.row()

    def _get_selected_source_rows(self) -> list[int]:
        sel = self.table.selectionModel()
        if not sel:
            return []
        rows = sel.selectedRows()
        src_rows = sorted({self._get_source_row_from_index(r) for r in rows if r.isValid()})
        return [r for r in src_rows if r is not None]

    def on_table_clicked(self, index: QModelIndex):
        if not index.isValid():
            return
        if index.column() != COL_FAV:
            return
        src_row = self._get_source_row_from_index(index)
        if src_row is None or not self.file_path:
            return

        # Toggle favorite
        self.model.stations[src_row].favorite = not self.model.stations[src_row].favorite
        tl = self.model.index(src_row, COL_FAV)
        br = self.model.index(src_row, COL_FAV)
        self.model.dataChanged.emit(tl, br, [Qt.DisplayRole, Qt.UserRole])

        # Save immediately (in-place)
        self.write_now(strategy_override="in_place", reload_ui=False)

    def delete_source_rows(self, src_rows: list[int]):
        if not self.file_path:
            return
        if not src_rows:
            QMessageBox.information(self, "Delete", "Select one or more stations (full rows) to delete.")
            return

        src_rows = sorted(set([r for r in src_rows if 0 <= r < len(self.model.stations)]))
        if not src_rows:
            QMessageBox.information(self, "Delete", "No valid station rows to delete.")
            return

        if QMessageBox.question(self, "Confirm delete", f"Delete {len(src_rows)} station(s)?") != QMessageBox.Yes:
            return

        for r in sorted(src_rows, reverse=True):
            self.model.removeRows(r, 1)

        if self.current_edit_source_row is not None and self.current_edit_source_row in src_rows:
            self.current_edit_source_row = None

        self.on_view_mode_changed()
        self.write_now(strategy_override="in_place", reload_ui=True)

    def show_context_menu(self, pos: QPoint):
        if not self.file_path:
            return

        index = self.table.indexAt(pos)
        if not index.isValid():
            return

        right_clicked_src = self._get_source_row_from_index(index)

        sel_model = self.table.selectionModel()
        if sel_model is not None and not sel_model.isRowSelected(index.row(), QModelIndex()):
            sel_model.select(index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            self.table.setCurrentIndex(index)

        src_rows = self._get_selected_source_rows()
        if (not src_rows) and (right_clicked_src is not None):
            src_rows = [right_clicked_src]

        menu = QMenu(self)
        act_add_fav = menu.addAction("Add to Favorite Station(s)")
        act_remove_fav = menu.addAction("Remove Favorite Station(s)")
        menu.addSeparator()
        act_delete = menu.addAction("Delete Station(s)")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        if chosen == act_delete:
            self.delete_source_rows(src_rows)
            return

        if chosen in (act_add_fav, act_remove_fav):
            new_val = (chosen == act_add_fav)
            changed_any = False
            for r in src_rows:
                if 0 <= r < len(self.model.stations):
                    if self.model.stations[r].favorite != new_val:
                        self.model.stations[r].favorite = new_val
                        changed_any = True
                        tl = self.model.index(r, COL_FAV)
                        br = self.model.index(r, COL_FAV)
                        self.model.dataChanged.emit(tl, br, [Qt.DisplayRole, Qt.UserRole])

            if changed_any:
                self.write_now(strategy_override="in_place", reload_ui=False)

    def on_model_reordered(self):
        if self._suppress_reorder_autosave:
            return
        if not self.file_path:
            return
        if not self.is_custom_drag_mode_active():
            return
        self.write_now(strategy_override="in_place", reload_ui=True)

    def on_row_double_clicked(self, index: QModelIndex):
        if not index.isValid():
            return
        if index.column() == COL_FAV:
            return
        src_row = self._get_source_row_from_index(index)
        if src_row is None:
            return
        self.load_station_into_form(src_row)

    def load_station_into_form(self, src_row: int):
        s = self.model.stations[src_row]
        self.current_edit_source_row = src_row
        self.in_name.setText(s.name)
        self.in_url.setText(s.url)
        self.in_lang.setText(s.language)
        self.in_bitrate.setValue(int(s.bitrate))
        self.in_genre.setText(s.genre)
        self.in_fav.setChecked(bool(s.favorite))

    def _validate_form(self):
        name = self.in_name.text().strip()
        url = self.in_url.text().strip()
        genre = self.in_genre.text().strip()
        lang = self.in_lang.text().strip()

        if not url or not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("URL missing or must contain http:// or https://.")

        for field, label in [(name, "Name"), (url, "URL"), (genre, "Genre"), (lang, "Language")]:
            if "|" in field:
                raise ValueError(f"{label} cannot contain the '|' character.")

        return Station(
            url=url,
            name=name,
            genre=genre,
            language=lang,
            bitrate=int(self.in_bitrate.value()),
            favorite=bool(self.in_fav.isChecked())
        )

    def save_station(self):
        if not self.file_path:
            return

        # Auto-detect insert mode by empty form OR explicit Clear
        if self.is_form_effectively_empty():
            self.current_edit_source_row = None

        try:
            s = self._validate_form()
        except Exception as e:
            QMessageBox.warning(self, "Invalid station", str(e))
            return

        is_insert = (self.current_edit_source_row is None)

        if is_insert:
            self.model.beginInsertRows(QModelIndex(), len(self.model.stations), len(self.model.stations))
            self.model.stations.append(s)
            self.model.endInsertRows()
            self.current_edit_source_row = len(self.model.stations) - 1
        else:
            r = self.current_edit_source_row
            if r is None or r < 0 or r >= len(self.model.stations):
                is_insert = True
                self.model.beginInsertRows(QModelIndex(), len(self.model.stations), len(self.model.stations))
                self.model.stations.append(s)
                self.model.endInsertRows()
                self.current_edit_source_row = len(self.model.stations) - 1
            else:
                self.model.stations[r] = s
                tl = self.model.index(r, 0)
                br = self.model.index(r, self.model.columnCount() - 1)
                self.model.dataChanged.emit(tl, br, [Qt.DisplayRole, Qt.UserRole])

        self.on_view_mode_changed()
        self.write_now(strategy_override="new_slot" if is_insert else "in_place", reload_ui=True)

    def delete_selected(self):
        self.delete_source_rows(self._get_selected_source_rows())

    def write_now(self, strategy_override: str = "in_place", reload_ui: bool = True):
        if not self.file_path:
            return
        if self.original_lines is None or self.station_line_indexes is None:
            QMessageBox.critical(self, "Write failed", "Internal state missing. Reload the file and try again.")
            return

        try:
            backup = write_live_streams(
                self.file_path,
                self.original_lines,
                self.station_line_indexes,
                self.model.stations,
                strategy=strategy_override
            )

            fresh_lines, fresh_idxs, _ = parse_live_streams(self.file_path)
            self.original_lines = fresh_lines
            self.station_line_indexes = fresh_idxs

            if reload_ui:
                self.load_file(self.file_path)

            msg = "Created New Slot, Saved, and Backed Up." if strategy_override == "new_slot" else "Saved. Created Backup File."
            mw = self.window()
            if isinstance(mw, QMainWindow):
                mw.statusBar().showMessage(f"{msg} Backup: {backup}", 9000)

        except Exception as e:
            QMessageBox.critical(self, "Write failed", str(e))


# ---------------- Main window ----------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Radio Station Editor (ATS/ETS2)")
        self.resize(1200, 720)

        self.settings = QSettings("RadioEditor", "RadioStationEditor")

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.open_page = OpenPage(self.open_file, self.settings)
        self.editor_page = EditorPage(self.go_open_page)

        self.stack.addWidget(self.open_page)
        self.stack.addWidget(self.editor_page)
        self.stack.setCurrentWidget(self.open_page)

        self.setStatusBar(QStatusBar())
        self._build_menus()

        delete_action = QAction(self)
        delete_action.setShortcut(QKeySequence(Qt.Key_Delete))
        delete_action.triggered.connect(self.editor_page.delete_selected)
        self.addAction(delete_action)

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("File")

        act_choose = QAction("Choose file…", self)
        act_choose.triggered.connect(self.open_page.choose_file_and_associate)
        file_menu.addAction(act_choose)

        file_menu.addSeparator()
        assoc_menu = self.menuBar().addMenu("Game Association")

        act_clear_ats = QAction("Clear ATS association", self)
        act_clear_ats.triggered.connect(lambda: self._clear_assoc("ATS"))
        assoc_menu.addAction(act_clear_ats)

        act_clear_ets2 = QAction("Clear ETS2 association", self)
        act_clear_ets2.triggered.connect(lambda: self._clear_assoc("ETS2"))
        assoc_menu.addAction(act_clear_ets2)

        assoc_menu.addSeparator()
        act_clear_both = QAction("Clear BOTH associations", self)
        act_clear_both.triggered.connect(self._clear_both_assoc)
        assoc_menu.addAction(act_clear_both)

        file_menu.addSeparator()
        act_exit = QAction("Exit", self)
        act_exit.setShortcut(QKeySequence.Quit)
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

    def _clear_assoc(self, game: str):
        self.open_page.clear_manual_game_file(game)
        self.open_page.refresh_game_tiles()
        self.statusBar().showMessage(f"Cleared {game} association.", 4000)

    def _clear_both_assoc(self):
        self.open_page.clear_all_manual_game_files()
        self.open_page.refresh_game_tiles()
        self.statusBar().showMessage("Cleared ATS + ETS2 associations.", 4000)

    def go_open_page(self):
        self.stack.setCurrentWidget(self.open_page)
        self.open_page.refresh_game_tiles()

    def open_file(self, path: str):
        self.editor_page.load_file(path)
        self.stack.setCurrentWidget(self.editor_page)
        self.statusBar().showMessage(f"Loaded {len(self.editor_page.model.stations)} stations.", 5000)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
