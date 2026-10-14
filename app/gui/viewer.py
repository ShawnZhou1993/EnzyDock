"""Embedded 3D viewer widget (QtWebEngine host for 3Dmol.js).

The widget owns the toolbar and the web surface; :mod:`app.visualization.viewer`
does all of the geometry (pocket residues, label anchors, hydrogen bonds) and
this class only serialises the resulting scene into the page::

    set_protein(path) / set_ligand(path)
        -> build_scene()               (app.visualization.viewer)
        -> scene.payload(display)
        -> window.enzydockViewer.setScene(json)

The page is a pure renderer, so the interesting logic stays unit testable in
Python and the JavaScript stays small.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..docking.docking_config import BoxConfig
from ..utils.logger import get_logger
from ..utils.paths import resource
from ..visualization.viewer import (
    CONTACT_MAX_DISTANCE,
    HBOND_MAX_DISTANCE,
    POCKET_CUTOFF,
    ViewerScene,
    build_scene,
    describe,
)

log = get_logger(__name__)

_VIEWER_PAGE = "index.html"

#: Toolbar toggles, in the order they are created.  ``residues`` is shown to
#: the user as "Labels" (it switches the residue-name labels on).
DISPLAY_TOGGLES: tuple[str, ...] = (
    "protein", "ligand", "pocket", "surface", "residues", "hbonds", "contacts",
)

#: ``(key, chip text, default)`` - laid out in two compact rows.
TOGGLE_LAYOUT: tuple[tuple[str, str, bool], ...] = (
    ("protein", "Protein", True),
    ("ligand", "Ligand", True),
    ("pocket", "Pocket", False),
    ("surface", "Surface", False),
    ("residues", "Labels", False),
    ("hbonds", "H-bonds", False),
    ("contacts", "Contacts", False),
)

#: What each chip does - shown as a tooltip, because "pocket" and "contacts"
#: mean different things to different people.
TOGGLE_TIPS: dict[str, str] = {
    "protein": "Draw the receptor as a cartoon ribbon.",
    "ligand": "Draw the docked pose as sticks.",
    "pocket": ("Show the residues lining the binding site: sticks plus a "
               "translucent surface."),
    "surface": ("Wrap the whole receptor in a translucent surface. Helpful to "
                "see the binding site in context, slower on large proteins."),
    "residues": "Write the residue name and number next to each pocket residue.",
    "hbonds": "Draw the possible hydrogen bonds (polar atoms within 3.5 A).",
    "contacts": ("Draw every possible interaction - H-bonds, salt bridges, "
                 "halogen bonds, pi-stacking and hydrophobic contacts - with "
                 "the distance of the directional ones."),
}


class MoleculeViewer(QWidget):
    """3D structure viewer with a small toolbar of display toggles."""

    #: ``(name, enabled)`` - a display toggle was switched by the user.
    display_toggled = Signal(str, bool)
    status_message = Signal(str)

    #: Page URL used for the "open in browser" fallback.
    _PAGE_INDEX_WEB = 0
    _PAGE_INDEX_FALLBACK = 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._web = None
        self._checks: dict[str, QCheckBox] = {}
        self._stack: QStackedWidget | None = None
        self._fallback_label: QLabel | None = None

        # --- scene state (Step 7) -------------------------------------------
        self._scene = ViewerScene()
        self._protein_path: Path | None = None
        self._ligand_path: Path | None = None
        self._box: BoxConfig | None = None
        self._pose_number: int | None = None
        self._pocket_cutoff = POCKET_CUTOFF
        self._hbond_distance = HBOND_MAX_DISTANCE
        self._contact_distance = CONTACT_MAX_DISTANCE

        self._page_ready = False
        self._pending_payload: dict[str, object] | None = None

        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # --- toolbar ---------------------------------------------------------
        # Two compact rows: seven chips plus Reset do not fit next to the 3D
        # view on a small laptop screen in a single row.
        toolbar = QGridLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setHorizontalSpacing(6)
        toolbar.setVerticalSpacing(4)

        self._reset_btn = QPushButton("Reset View")
        self._reset_btn.setToolTip("Zoom the camera back so everything fits.")
        self._reset_btn.clicked.connect(self.reset_view)
        toolbar.addWidget(self._reset_btn, 0, 0)

        column = 1
        row = 0
        for name, text, checked in TOGGLE_LAYOUT:
            if column > 3:
                column = 0
                row += 1
            box = QCheckBox(text)
            box.setChecked(checked)
            box.setToolTip(TOGGLE_TIPS.get(name, ""))
            box.stateChanged.connect(
                lambda state, key=name: self._on_toggle(key, bool(state))
            )
            toolbar.addWidget(box, row, column)
            self._checks[name] = box
            column += 1

        toolbar.setColumnStretch(column, 1)
        layout.addLayout(toolbar)

        # --- the actual 3D surface ---------------------------------------------
        self._stack = QStackedWidget()
        self._stack.setMinimumHeight(240)

        self._web = self._create_web_view()
        if self._web is None:
            self._stack.addWidget(self._build_message_page(
                "3D viewer unavailable:\n"
                "QtWebEngine could not be initialised in this build.\n\n"
                "Docking can still be computed and exported."
            ))
        else:
            self._stack.addWidget(self._web)

        self._stack.addWidget(self._build_fallback_page())
        self._stack.setCurrentIndex(self._PAGE_INDEX_WEB)
        layout.addWidget(self._stack, 1)

    # ------------------------------------------------------- fallback pages
    def _build_message_page(self, message: str) -> QWidget:
        """A muted, centred text page used when no web surface exists."""
        page = QWidget()
        page_layout = QVBoxLayout(page)
        label = QLabel(message)
        label.setObjectName("muted")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        page_layout.addWidget(label)
        return page

    def _build_fallback_page(self) -> QWidget:
        """Shown when the embedded engine cannot render (sandbox, no GPU ...)."""
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setSpacing(8)

        self._fallback_label = QLabel()
        self._fallback_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._fallback_label.setWordWrap(True)
        page_layout.addWidget(self._fallback_label, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)

        browser_btn = QPushButton("Open in system browser")
        browser_btn.clicked.connect(self.open_in_browser)
        buttons.addWidget(browser_btn)

        retry_btn = QPushButton("Retry")
        retry_btn.clicked.connect(self.reload_page)
        buttons.addWidget(retry_btn)
        buttons.addStretch(1)
        page_layout.addLayout(buttons)

        self._fallback_label.setText(
            self._FALLBACK_TEXT.format(detail="the renderer did not start")
        )
        return page

    _FALLBACK_TEXT = (
        "The embedded 3D viewer could not start.\n\n"
        "Reason: {detail}\n\n"
        "3D rendering needs QtWebEngine (Chromium). It is blocked in some "
        "virtual machines, remote sessions and sandboxed environments.\n\n"
        "You can still keep working: docking results and exports are unaffected."
    )

    def show_fallback(self, detail: str) -> None:
        """Switch the viewer to the explanatory fallback page."""
        if self._stack is None:
            return
        if self._fallback_label is not None:
            self._fallback_label.setText(self._FALLBACK_TEXT.format(detail=detail))
        self._stack.setCurrentIndex(self._PAGE_INDEX_FALLBACK)

    def _create_web_view(self) -> QWidget | None:
        """Create the QWebEngineView, or ``None`` when it is unavailable.

        ``ENZYDOCK_NO_WEBENGINE=1`` disables the web surface entirely; used by
        the test-suite and by the ``--no-webengine`` diagnostic switch.
        """
        if os.environ.get("ENZYDOCK_NO_WEBENGINE"):
            log.info("QtWebEngine disabled through ENZYDOCK_NO_WEBENGINE")
            return None
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except ImportError as exc:                     # pragma: no cover
            log.warning("QtWebEngineWidgets unavailable: %s", exc)
            return None

        try:
            view = QWebEngineView(self)
        except Exception as exc:                       # pragma: no cover
            log.warning("could not create QWebEngineView: %s", exc)
            return None

        view.setMinimumHeight(240)
        view.loadFinished.connect(self._on_page_loaded)
        try:
            view.page().renderProcessTerminated.connect(self._on_renderer_died)
        except Exception:                              # pragma: no cover
            pass

        page = resource("viewer", _VIEWER_PAGE)
        if page.is_file():
            log.debug("loading viewer page %s", page)
            view.setUrl(QUrl.fromLocalFile(str(page)))
        else:                                          # pragma: no cover
            log.warning("viewer page %s not found, using inline fallback", page)
            view.setHtml(self._placeholder_html())
        return view

    def _on_page_loaded(self, ok: bool) -> None:
        """Report whether the 3Dmol.js host page loaded.

        A scene set before the page finished loading is replayed here, so
        clicking a pose immediately after start-up still renders it.
        """
        self._page_ready = bool(ok)
        if ok:
            log.info("3D viewer page loaded")
            self.status_message.emit("3D viewer ready.")
            if self._stack is not None:
                self._stack.setCurrentIndex(self._PAGE_INDEX_WEB)
            if self._pending_payload is not None:
                payload, self._pending_payload = self._pending_payload, None
                self._send(payload)
        else:
            log.error("the 3D viewer page failed to load")
            self.show_fallback("the page could not be loaded")
            self.status_message.emit("3D viewer could not start - see the log.")

    def _on_renderer_died(self, status, exit_code: int) -> None:
        """Chromium's render process was killed (sandbox, VM, no GPU ...)."""
        log.error("3D viewer render process terminated (status=%s, code=%s)",
                  status, exit_code)
        self.show_fallback(f"the Chromium renderer stopped (code {exit_code})")
        self.status_message.emit("3D viewer stopped - see the log for details.")

    # ------------------------------------------------------ fallback actions
    def viewer_page(self) -> Path | None:
        """Path of the HTML page the viewer displays, if it exists."""
        page = resource("viewer", _VIEWER_PAGE)
        return page if page.is_file() else None

    def open_in_browser(self) -> None:
        """Open the viewer page in the user's default browser.

        Useful when the embedded Chromium cannot start (VMs, remote desktop,
        hardened environments): 3Dmol.js runs perfectly well in Edge/Chrome.
        """
        page = self.viewer_page()
        if page is None:                               # pragma: no cover
            self.status_message.emit("No viewer page to open.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(page)))
        log.info("opened viewer page in the system browser: %s", page)
        self.status_message.emit("Opened the 3D viewer in your browser.")

    def reload_page(self) -> None:
        """Reload the embedded page (used by the Retry button)."""
        if self._web is None:                          # pragma: no cover
            return
        log.info("reloading the 3D viewer page")
        self._web.reload()
        if self._stack is not None:
            self._stack.setCurrentIndex(self._PAGE_INDEX_WEB)

    @staticmethod
    def _placeholder_html() -> str:                    # pragma: no cover
        return (
            "<html><body style='background:#14181d;color:#93a1b1;"
            "font-family:Segoe UI;display:flex;align-items:center;"
            "justify-content:center;height:100%'>"
            "<div>3D viewer placeholder</div></body></html>"
        )

    # ---------------------------------------------------------- public API
    def is_web_available(self) -> bool:
        """``True`` when the QtWebEngine surface could be created."""
        return self._web is not None

    def reset_view(self) -> None:
        """Zoom back so that everything in the scene is visible."""
        self._run_javascript("if (window.enzydockViewer) window.enzydockViewer.resetView();")
        self.status_message.emit("View reset.")

    def set_protein(self, path: str | Path, style: str = "cartoon") -> None:
        """Load a receptor structure and redraw the scene.

        The file is only *displayed* here - if our own parser refuses it
        (a PDBQT receptor, for instance) the cartoon still appears and the
        scene carries a note explaining what could not be analysed.
        """
        self._protein_path = Path(path)
        log.info("viewer: receptor %s", self._protein_path.name)
        self._rebuild()
        if self._scene.has_protein:
            self.status_message.emit(f"Showing {self._protein_path.name}.")
        else:
            self.status_message.emit(
                f"Could not read the receptor structure {self._protein_path.name}."
            )

    def set_ligand(self, path: str | Path, style: str = "stick") -> None:
        """Load a ligand (or one docking pose) and redraw the scene."""
        self._ligand_path = Path(path)
        log.info("viewer: ligand %s", self._ligand_path.name)
        self._rebuild()

    def set_box(self, box: BoxConfig | None) -> None:
        """Set the docking box used to define the pocket before a pose exists."""
        self._box = box
        if self._protein_path is not None and self._ligand_path is None:
            self._rebuild()

    def show_pose(self, pose_number: int) -> None:
        """Remember which pose is on screen (the file was set by ``set_ligand``)."""
        self._pose_number = int(pose_number)
        self.status_message.emit(f"Pose {pose_number} selected.")

    def set_pocket_residues(self, residues: Sequence[str]) -> None:
        """Replace the pocket annotation (used by automatic site detection)."""
        self._scene.pocket_source = ", ".join(str(item) for item in residues)
        self._push()

    def clear_scene(self) -> None:
        """Drop every structure from the viewer."""
        self._protein_path = None
        self._ligand_path = None
        self._pose_number = None
        self._scene = ViewerScene()
        self._pending_payload = None
        self._run_javascript("if (window.enzydockViewer) window.enzydockViewer.clear();")

    # --------------------------------------------------------------- access
    @property
    def scene(self) -> ViewerScene:
        """The scene currently rendered (also handy in tests)."""
        return self._scene

    @property
    def interactions(self) -> list:
        """Every interaction of the scene currently rendered."""
        return list(self._scene.interactions)

    @property
    def pose_number(self) -> int | None:
        return self._pose_number

    def probe(self, callback) -> None:
        """Ask the page what it really drew - models, atoms, surfaces.

        Used by ``scripts/smoke_start.py --viewer-probe`` and by the test
        suite: a protein that silently fails to appear is exactly the bug
        this diagnostic exists to catch.
        """
        if self._web is None:
            callback(None)
            return
        script = ("JSON.stringify(window.enzydockViewer "
                  "? window.enzydockViewer.describe() : null);")
        try:
            self._web.page().runJavaScript(script, callback)
        except Exception as exc:                       # pragma: no cover
            log.debug("viewer probe failed: %s", exc)
            callback(None)

    def is_checked(self, name: str) -> bool:
        """State of a display toggle."""
        box = self._checks.get(name)
        return bool(box.isChecked()) if box else False

    def display_flags(self) -> dict[str, bool]:
        """Current state of every toolbar toggle."""
        return {name: self.is_checked(name) for name in DISPLAY_TOGGLES}

    # ------------------------------------------------------------ internals
    def _on_toggle(self, name: str, enabled: bool) -> None:
        log.debug("display toggle %s -> %s", name, enabled)
        self.display_toggled.emit(name, enabled)
        self._push()

    def _rebuild(self) -> None:
        """Recompute pocket and interactions, then redraw."""
        self._scene = build_scene(
            self._protein_path,
            self._ligand_path,
            self._box,
            pocket_cutoff=self._pocket_cutoff,
            hbond_distance=self._hbond_distance,
            contact_distance=self._contact_distance,
        )
        log.info(
            "viewer scene: %d pocket residues, %d contacts (%s)",
            len(self._scene.pocket_residues),
            len(self._scene.interactions),
            describe(self._scene.interactions),
        )
        self._push()

    def _push(self) -> None:
        """Serialise the scene and hand it to the page."""
        if self._scene.is_empty:
            return
        payload = self._scene.payload(self.display_flags())
        if not self._page_ready:
            log.debug("page not ready yet - queueing the scene")
            self._pending_payload = payload
            return
        self._send(payload)

    def _send(self, payload: dict[str, object]) -> None:
        try:
            encoded = json.dumps(payload)
        except (TypeError, ValueError) as exc:           # pragma: no cover
            log.error("could not serialise the viewer scene: %s", exc)
            return
        self._run_javascript(
            f"if (window.enzydockViewer) window.enzydockViewer.setScene({encoded});"
        )

    def _run_javascript(self, script: str) -> None:
        """Execute ``script`` inside the page (no-op without QtWebEngine)."""
        if self._web is None or not script:
            return
        try:
            self._web.page().runJavaScript(script)
        except Exception as exc:                       # pragma: no cover
            log.debug("javascript failed: %s", exc)


__all__ = ["MoleculeViewer"]
