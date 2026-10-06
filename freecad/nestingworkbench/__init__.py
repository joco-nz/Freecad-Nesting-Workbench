# SPDX-License-Identifier: LGPL-2.1-or-later
"""Nesting Workbench package.

Also resolves the on-disk locations of the addon's data directories. The
package lives at ``<addon>/freecad/nestingworkbench``, so the addon root is
three levels up from this file.
"""

import os

ADDON_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESOURCES_DIR = os.path.join(ADDON_DIR, "Resources")
ICONS_DIR = os.path.join(RESOURCES_DIR, "icons")
#: Diagrams embedded in tooltips. Referenced by absolute path inside a tooltip's
#: `<img src>`, because Qt resolves a rich-text image against a base URL that a
#: tooltip does not have -- see `ui_nesting` and the note on
#: `QUrl.fromLocalFile` there.
TOOLTIPS_DIR = os.path.join(RESOURCES_DIR, "tooltips")
FONTS_DIR = os.path.join(ADDON_DIR, "fonts")
DEFAULT_FONT = os.path.join(FONTS_DIR, "PoiretOne-Regular.ttf")
