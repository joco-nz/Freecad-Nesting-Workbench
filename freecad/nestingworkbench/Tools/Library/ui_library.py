# SPDX-License-Identifier: LGPL-2.1-or-later
"""Unified Library window: the Sheets and Materials tabs (Beds, later)."""
import os
from typing import Any, Dict, Optional, Tuple

from PySide import QtGui, QtWidgets

from ... import ICONS_DIR
from ..SheetLibrary.ui_materials import MaterialsTab
from ..SheetLibrary.ui_sheet_library import SheetsTab

translate = QtWidgets.QApplication.translate


class LibraryDialog(QtWidgets.QDialog):
    """Main library management dialog with tabs for sheets, etc."""

    def __init__(
        self,
        parent: Optional[QtWidgets.QWidget] = None,
        tab: str = "sheets",
        sheets_root: Optional[str] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(translate("LibraryDialog", "Library"))

        layout = QtWidgets.QVBoxLayout(self)

        self.tab_widget = QtWidgets.QTabWidget(self)
        self.sheets_tab = SheetsTab(self, root=sheets_root)
        self.tab_widget.insertTab(
            0,
            self.sheets_tab,
            QtGui.QIcon(os.path.join(ICONS_DIR, "Nesting_SheetLibrary_Icon.svg")),
            translate("LibraryDialog", "Sheets"),
        )

        # No icon: the repo has no material icon (SHLIB-077 asks the owner).
        self.materials_tab = MaterialsTab(self, sheets_root=sheets_root)
        self.tab_widget.addTab(self.materials_tab, translate("LibraryDialog", "Materials"))

        self._tabs = {"sheets": self.sheets_tab, "materials": self.materials_tab}

        layout.addWidget(self.tab_widget)

        self.button_box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        # Connect tab change so newly visible tab reloads
        self.tab_widget.currentChanged.connect(self._on_current_changed)

        # Select initial tab
        self.show_tab(tab)

    def _on_current_changed(self, index: int) -> None:
        widget = self.tab_widget.widget(index)
        if widget and hasattr(widget, "reload"):
            widget.reload()

    def show_tab(self, key: str) -> None:
        """Select a tab by its string key ('sheets', etc.)."""
        if key not in self._tabs:
            raise KeyError(f"Unknown library tab: {key}")
        target_widget = self._tabs[key]
        for idx in range(self.tab_widget.count()):
            if self.tab_widget.widget(idx) is target_widget:
                self.tab_widget.setCurrentIndex(idx)
                break
