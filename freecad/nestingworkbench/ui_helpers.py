# SPDX-License-Identifier: LGPL-2.1-or-later
# freecad/nestingworkbench/ui_helpers.py

"""
Shared Qt widget helpers for the Nesting Workbench.

Currently one class, `CollapsibleSection`, replacing the
`QGroupBox` + `_set_group_collapsed` pattern that `ui_nesting.py` used
inline. The structure is upstream's; the reasoning in the comments is ours,
because upstream's version solves the same problem without recording why.
"""

from PySide import QtCore, QtWidgets


class CollapsibleSection(QtWidgets.QWidget):
    """A titled section whose contents can be tucked away entirely.

    Why this is a composite rather than a checkable ``QGroupBox``
    ----------------------------------------------------------------
    The obvious way to do this is a ``QGroupBox`` with ``setCheckable(True)``,
    and it does not work. A checkable group box that is unchecked *disables*
    its children; it does not hide them. Measured on a two-widget group: 64px
    checked and 64px unchecked, with the child reporting ``isHidden()=False``
    in both states. So the toggle on its own greys the controls out and
    reclaims no space, which is the worst of both -- it looks broken and saves
    nothing. Hiding has to be done explicitly, and that is what ``content_area``
    is for: an explicit region to show and hide, rather than one inferred from
    a group's children.

    Why ``setVisible`` and not ``setEnabled``
    ----------------------------------------------------------------
    Deliberately ``setVisible``. A hidden widget still returns its real state,
    so ``_collect_ui_params`` keeps reading ``verbose_logging``,
    ``add_labels``, the font path and the label fields exactly as it does when
    the section is open. Disabling them would have been the obvious one-liner
    and would have silently changed what a run uses -- the controls would go
    grey *and* stop contributing. ``probe_unit_panel.py`` pins both halves of
    this: the children stay hidden, and the run params still see through it.

    Nesting
    ----------------------------------------------------------------
    A section placed inside another section's ``content_area`` is a direct
    child of it, so collapsing the outer section hides the inner one whole --
    header included. That is not the same as the old behaviour, which touched
    only direct children of the group box and left a nested group's own state
    alone. No section is nested today; if one ever is, this is the trap.
    """

    def __init__(self, title="", expanded=True, parent=None):
        super().__init__(parent)

        self.main_layout = QtWidgets.QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 8)
        self.main_layout.setSpacing(0)

        self.toggle_button = QtWidgets.QToolButton()
        self.toggle_button.setText(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(
            QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self.toggle_button.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)

        self.toggle_button.setStyleSheet("""
            QToolButton {
                background-color: rgba(128, 128, 128, 40);
                border: 1px solid palette(mid);
                border-radius: 4px;
                font-weight: bold;
                padding: 6px;
                text-align: left;
            }
            QToolButton:hover {
                background-color: palette(highlight);
                color: palette(highlighted-text);
                border-color: palette(highlight);
            }
            QToolButton:checked {
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
                background-color: rgba(128, 128, 128, 60);
            }
        """)

        # Parented before it is hidden. setVisible(True) on a parentless widget
        # makes it a top-level window, which flashes on screen until a layout
        # reparents it -- and this happens during panel construction, so the
        # flash is visible every time the panel opens.
        self.content_area = QtWidgets.QWidget(self)
        self.content_area.setObjectName("content_area")
        self.content_area.setStyleSheet("""
            QWidget#content_area {
                background-color: transparent;
                border: 1px solid palette(mid);
                border-top: none;
                border-bottom-left-radius: 4px;
                border-bottom-right-radius: 4px;
            }
        """)
        # Set from the constructor argument rather than collapsed after the
        # fact. The old QGroupBox arrangement had to collapse as a separate
        # step once the children existed, because there was nothing to hide
        # until setLayout had run -- which made the ordering a live hazard with
        # a comment explaining it. Here there is nothing to sequence.
        self.content_area.setVisible(expanded)

        # Deliberately NOT built here. Installed eagerly, a later addLayout()
        # would find the widget already owning a layout, and QWidget::setLayout
        # refuses to replace one -- the grid is then silently dropped and
        # deleteLater() on the still-installed form destroys the child widgets,
        # which surfaces much later as "Internal C++ object already deleted"
        # on some unrelated field. Built on first addRow instead, so exactly
        # one layout is ever installed.
        self.content_layout = None

        self.main_layout.addWidget(self.toggle_button)
        self.main_layout.addWidget(self.content_area)

        self.toggle_button.clicked.connect(self.toggle)

    def toggle(self, checked=None):
        """Show or hide the contents, and point the arrow to match."""
        if checked is None:
            checked = self.toggle_button.isChecked()
        if checked:
            self.toggle_button.setArrowType(QtCore.Qt.DownArrow)
            self.content_area.setVisible(True)
        else:
            self.toggle_button.setArrowType(QtCore.Qt.RightArrow)
            self.content_area.setVisible(False)

    def setExpanded(self, expanded):
        """Set the collapsed state without going through a click."""
        self.toggle_button.setChecked(expanded)
        self.toggle(expanded)

    def isExpanded(self):
        """Whether the contents are currently shown.

        Tests ``isHidden()``, not ``isVisible()``. isVisible() is False for a
        widget whose *ancestor* is hidden, so before the panel is shown it would
        report every section as collapsed regardless of its state -- and this is
        called before the panel is shown, to assert that a fresh panel starts
        collapsed. isHidden() is the explicit hide flag, which is what the
        toggle actually controls.
        """
        return not self.content_area.isHidden()

    def _form_layout(self):
        """The content area's QFormLayout, created on first use.

        Lazy because QWidget::setLayout refuses to replace an installed layout.
        Creating it in __init__ meant a section built with addLayout() silently
        kept the empty form and lost its own contents.
        """
        if self.content_layout is None:
            self.content_layout = QtWidgets.QFormLayout(self.content_area)
            self.content_layout.setContentsMargins(12, 10, 12, 10)
            self.content_layout.setSpacing(8)
        return self.content_layout

    def addRow(self, label, widget=None):
        """Add a labelled row to the content area.

        ``widget`` of None adds a label with nothing beside it, for a row that
        exists only to carry a heading or a comment.
        """
        layout = self._form_layout()
        if widget is not None:
            layout.addRow(label, widget)
        else:
            layout.addRow(label)

    def addLayout(self, layout):
        """Install a pre-built layout as the whole content area.

        Needed because not every section is a form. The Nesting Settings
        section is a QGridLayout whose column widths and row stretches are
        argued at length in ui_nesting, and the Optimizations section is a
        two-column grid built by _two_column_grid. Forcing either through a
        QFormLayout would undo that work, so this hands the finished layout
        over unchanged.

        Refuses if rows have already been added: the widget can only hold one
        layout, and Qt's refusal is silent, so catching it here turns a
        confusing later failure into an immediate one.
        """
        if self.content_layout is not None:
            raise RuntimeError(
                "addRow has already been called on this section; a QWidget "
                "holds one layout, so choose addRow or addLayout, not both")
        self.content_area.setLayout(layout)
