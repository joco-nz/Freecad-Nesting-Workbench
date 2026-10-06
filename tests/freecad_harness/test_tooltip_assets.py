"""Does the Candidate Step tooltip actually show its diagram?

The tooltip's prose is easy to check and worth checking. The diagram is not: the
file can exist, the string can name it, and Qt can still render a broken-image
box. Qt resolves a rich-text image against a base URL that a tooltip does not
have, so the path form matters, and on Windows Qt parses `C:/x/y.svg` with
**scheme** `c` rather than as a path -- a bare Windows path is a URL with an
unknown scheme and loads nothing. Measured on Qt 6.8.3: a bare absolute POSIX
path does load, and backslashes do not even when the file exists.

So the check here is not "the file is there". It builds the tooltip with the
product's own `tooltip_with_image`, renders it through a real `QTextDocument`
under the offscreen platform, and asserts the document **grew by the image's
height**. That is what "it loads" means, and nothing short of it will do.

It also pins the width constant against the SVG's own `width` attribute, which
is the one duplication the design has. Qt sizes a rich tooltip from its text
rather than its image, so that number has to be written down somewhere; here it
is written down once and asserted, instead of being re-read from the file on
every panel build where a typo would surface as a mis-sized popup and nothing
else.

Run directly, or via tests/freecad_harness/run.sh. Writes
`.last_status_tooltip`; 0 pass, 1 fail.
"""
import os
import re
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_tooltip")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import FreeCAD
from PySide import QtCore, QtGui, QtWidgets

from freecad.nestingworkbench import TOOLTIPS_DIR
from freecad.nestingworkbench.ui_helpers import tooltip_with_image
from freecad.nestingworkbench.Tools.Nesting import ui_nesting

_failures = []
_checks = [0]

#: The diagram is 160px tall, so a loaded one grows the document by about that
#: and an unloaded one by nothing. 40 is a wide margin round the gap between
#: those two cases, not a threshold anyone tuned to the artwork.
_MIN_GROWTH_PX = 40.0

_PROSE = ("<p>A paragraph of prose standing in for the field's real help, long "
          "enough to wrap onto more than one line at the popup width, so the "
          "measured height is not simply the text height.</p>")


def emit(message=""):
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
        else:
            print(message)
    except Exception:
        pass


def check(condition, detail):
    _checks[0] += 1
    if not condition:
        _failures.append(detail)
        emit("  FAIL: %s" % detail)
    return bool(condition)


def check_the_asset_exists():
    emit("")
    emit("-- 1. the asset --")
    path = os.path.join(TOOLTIPS_DIR, ui_nesting._CANDIDATE_STEP_DIAGRAM)
    emit("  %s" % path)
    check(os.path.isfile(path), "the diagram is missing at %s" % path)
    return path


def check_the_widths_agree(path):
    emit("")
    emit("-- 2. the pinned width matches the SVG --")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    match = re.search(r'<svg\b[^>]*?\swidth="(\d+)"', text)
    if not check(match is not None,
                 "no width attribute on the <svg> element, so there is nothing "
                 "to pin the popup to"):
        return
    svg_width = int(match.group(1))
    declared = ui_nesting._CANDIDATE_STEP_DIAGRAM_WIDTH
    emit("  SVG says %d, the tooltip pins %d" % (svg_width, declared))
    check(svg_width == declared,
          "the SVG is %dpx wide but the tooltip pins the popup to %dpx; one of "
          "the two needs editing" % (svg_width, declared))


def check_qt_can_decode_it(path):
    emit("")
    emit("-- 3. Qt can decode it --")
    formats = [bytes(f).decode() for f in QtGui.QImageReader.supportedImageFormats()]
    check("svg" in formats,
          "Qt has no svg image plugin here, so an SVG diagram would silently "
          "not render at all")
    emit("  svg image plugin present: %s" % ("svg" in formats))
    reader = QtGui.QImageReader(path)
    image = reader.read()
    if not check(not image.isNull(),
                 "Qt could not decode %s: %s" % (path, reader.errorString())):
        return
    emit("  decoded %dx%d" % (image.width(), image.height()))


def check_the_tooltip_loads_it(path):
    """The check that matters: render the real tooltip and measure it."""
    emit("")
    emit("-- 4. the built tooltip actually loads the image --")
    with_image = tooltip_with_image(
        _PROSE, path, ui_nesting._CANDIDATE_STEP_DIAGRAM_WIDTH)

    src = re.search(r"<img src='([^']*)'", with_image)
    width = re.search(r"<table width='(\d+)'", with_image)
    emit("  src in the built string : %s" % (src.group(1) if src else "(none)"))
    emit("  table width attribute   : %s" % (width.group(1) if width else "(none)"))

    check(src is not None, "the built tooltip has no <img src>")
    check(width is not None, "the built tooltip has no width to pin the popup to")
    check(width is not None
          and int(width.group(1)) == ui_nesting._CANDIDATE_STEP_DIAGRAM_WIDTH,
          "the built tooltip pins a width other than the declared one")
    check(_PROSE in with_image,
          "the prose is not inside the wrapper, so the diagram would render "
          "with no explanation")
    # The src must resolve to the asset we think it does, not to something
    # that merely has the right filename in it.
    if src is not None:
        resolved = QtCore.QUrl(src.group(1)).toLocalFile()
        emit("  src resolves to          : %s" % resolved)
        check(os.path.isfile(resolved),
              "the src in the tooltip does not resolve to a file: %r" % resolved)
        check(os.path.samefile(resolved, path) if os.path.isfile(resolved) else False,
              "the src resolves to %s, not the asset at %s" % (resolved, path))

    def height(markup, text_width=320):
        doc = QtGui.QTextDocument()
        doc.setTextWidth(text_width)
        doc.setHtml(markup)
        doc.documentLayout().documentSize()
        return doc.size().height()

    bare = height(_PROSE)
    drawn = height(with_image)
    grew = drawn - bare
    emit("  prose alone %.2f px, with the diagram %.2f px, grew %.2f px"
         % (bare, drawn, grew))
    check(grew >= _MIN_GROWTH_PX,
          "the document grew only %.2f px, so Qt did not load the image -- the "
          "failure being a broken-image box, which is %.0f px short of this"
          % (grew, _MIN_GROWTH_PX))


def check_the_panel_wires_it_up():
    """The panel's own refresh path, read rather than reconstructed."""
    emit("")
    emit("-- 5. the panel wires it up --")
    try:
        import inspect
        source = inspect.getsource(ui_nesting.NestingPanel._refresh_unit_tooltips)
    except Exception as exc:
        check(False, "could not read _refresh_unit_tooltips: %s" % exc)
        return
    check("tooltip_with_image" in source,
          "_refresh_unit_tooltips no longer goes through tooltip_with_image, so "
          "the diagram is lost on the unit-refresh path even though it is set "
          "at construction")
    # The NAME, not its value: the source text contains the identifier, and
    # comparing the filename against it is a test that fails for the wrong
    # reason.
    check("_CANDIDATE_STEP_DIAGRAM" in source,
          "_refresh_unit_tooltips does not name the diagram constant, so it is "
          "not this file's checked constant that reaches the widget")
    check("_CANDIDATE_STEP_DIAGRAM_WIDTH" in source,
          "_refresh_unit_tooltips does not pass the declared width, so the "
          "popup is not pinned to what section 2 checked")
    check("setToolTip" in source, "it sets no tooltip at all")


def check_the_prose_explains_the_diagram():
    """The artwork carries no text, so the caption has to be in the prose."""
    emit("")
    emit("-- 6. the prose explains the picture --")
    try:
        import inspect
        source = inspect.getsource(ui_nesting.NestingPanel)
    except Exception as exc:
        check(False, "could not read NestingPanel: %s" % exc)
        return
    check("Below: the same part" in source,
          "the Candidate Step prose no longer introduces the diagram, so the "
          "image arrives with nothing pointing at it")
    for word in ("Left,", "Right,"):
        check(word in source,
              "the caption no longer says which panel is '%s', so a reader "
              "cannot tell the two apart" % word.rstrip(","))


def run():
    path = check_the_asset_exists()
    if os.path.isfile(path):
        check_the_widths_agree(path)
        check_qt_can_decode_it(path)
        check_the_tooltip_loads_it(path)
    check_the_panel_wires_it_up()
    check_the_prose_explains_the_diagram()


try:
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    run()
    emit("")
    emit("tooltip assets: %d checks, %d failure(s)"
         % (_checks[0], len(_failures)))
    status = 1 if _failures else 0
except Exception:
    traceback.print_exc()
    emit("tooltip assets: CRASHED")
    status = 1

with open(_STATUS_FILE, "w") as fh:
    fh.write(str(status))