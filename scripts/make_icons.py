"""Generate the EnzyDock application icons.

macOS refuses to give an ``.app`` a proper Dock icon without an ``.icns``
file, and Windows wants an ``.ico``.  Rather than shipping a binary blob of
unknown origin we draw the icon here, deterministically, from a few numbers.

The picture: a receptor drawn as an open crescent (the binding pocket) with
a hexagonal ligand docked inside it - which is exactly what EnzyDock does.

Usage::

    python scripts/make_icons.py

Writes ``app/resources/icons/enzydock.{png,icns,ico}``.
"""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPaintDevice,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = PROJECT_ROOT / "app" / "resources" / "icons"

SOURCE_SIZE = 1024

# --- palette ---------------------------------------------------------------
BACKGROUND_TOP = QColor("#0e7490")
BACKGROUND_BOTTOM = QColor("#134e4a")
POCKET_COLOUR = QColor("#7dd3fc")
LIGAND_FILL = QColor("#f59e0b")
LIGAND_EDGE = QColor("#b45309")
BOND_COLOUR = QColor("#fef3c7")


def _rounded_square(painter: QPainter, size: int) -> None:
    canvas = float(size)
    gradient = QLinearGradient(0.0, 0.0, 0.0, canvas)
    gradient.setColorAt(0.0, BACKGROUND_TOP)
    gradient.setColorAt(1.0, BACKGROUND_BOTTOM)
    painter.setBrush(QBrush(gradient))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0.0, 0.0, canvas, canvas,
                            canvas * 0.22, canvas * 0.22)


def _pocket(painter: QPainter, size: int) -> None:
    """The receptor: a thick crescent opening to the right."""
    canvas = float(size)
    pen = QPen(POCKET_COLOUR, canvas * 0.085)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))

    # An arc that stops short of closing, leaving a mouth for the ligand.
    import math

    centre = QPointF(canvas * 0.46, canvas * 0.5)
    radius = canvas * 0.28
    points: list[QPointF] = []
    # Sweep from 50 degrees round to 310 degrees (i.e. a "C" shape).
    for step in range(91):
        angle = math.radians(50.0 + step * (260.0 / 90.0))
        points.append(
            QPointF(
                centre.x() + radius * math.cos(angle),
                centre.y() + radius * math.sin(angle),
            )
        )
    painter.drawPolyline(QPolygonF(points))


def _ligand(painter: QPainter, size: int) -> None:
    """The ligand: a hexagon sitting inside the pocket."""
    canvas = float(size)
    import math

    centre = QPointF(canvas * 0.56, canvas * 0.5)
    radius = canvas * 0.155
    hexagon = QPolygonF()
    for corner in range(6):
        angle = math.radians(60.0 * corner)
        hexagon.append(
            QPointF(
                centre.x() + radius * math.cos(angle),
                centre.y() + radius * math.sin(angle),
            )
        )
    painter.setPen(QPen(LIGAND_EDGE, canvas * 0.018))
    painter.setBrush(QBrush(LIGAND_FILL))
    painter.drawPolygon(hexagon)

    # Two short "contacts" from the ligand back to the pocket wall.
    pen = QPen(BOND_COLOUR, canvas * 0.022)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setStyle(Qt.PenStyle.DashLine)
    painter.setPen(pen)
    painter.drawLine(centre.x() - radius * 0.95, centre.y() - radius * 0.5,
                     centre.x() - radius * 1.75, centre.y() - radius * 0.75)
    painter.drawLine(centre.x() - radius * 0.95, centre.y() + radius * 0.5,
                     centre.x() - radius * 1.75, centre.y() + radius * 0.75)


def render(size: int) -> QImage:
    """Draw the icon at ``size`` x ``size`` and return it."""
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    try:
        scale = size / float(SOURCE_SIZE)
        painter.scale(scale, scale)
        _rounded_square(painter, SOURCE_SIZE)
        _pocket(painter, SOURCE_SIZE)
        _ligand(painter, SOURCE_SIZE)
    finally:
        painter.end()
    return image


def _png_bytes(image: QImage) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    array: QByteArray = buffer.data()
    return bytes(array.data())


# --- .icns -----------------------------------------------------------------

# (type, pixel size) entries; macOS 10.7+ accepts PNG payloads for all of these.
ICNS_ENTRIES = (
    (b"ic07", 128),
    (b"ic08", 256),
    (b"ic09", 512),
    (b"ic10", 1024),
    (b"ic11", 64),
    (b"ic12", 256),
)


def write_icns(path: Path, make) -> None:
    """Write an Apple icon set."""
    chunks = bytearray()
    for kind, size in ICNS_ENTRIES:
        payload = _png_bytes(make(size))
        entry = kind + struct.pack(">I", len(payload) + 8) + payload
        chunks += entry
    header = b"icns" + struct.pack(">I", len(chunks) + 8)
    path.write_bytes(header + bytes(chunks))


# --- .ico ------------------------------------------------------------------

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def write_ico(path: Path, make) -> None:
    """Write a Windows icon holding PNG-compressed images (Vista+)."""
    payloads = [(size, _png_bytes(make(size))) for size in ICO_SIZES]
    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = 6 + 16 * len(payloads)

    directory = bytearray()
    body = bytearray()
    for size, payload in payloads:
        # 0 means 256 in the single-byte width/height fields.
        directory += struct.pack(
            "<BBBBHHII",
            size % 256, size % 256, 0, 0, 1, 32, len(payload), offset,
        )
        body += payload
        offset += len(payload)
    path.write_bytes(header + bytes(directory) + bytes(body))


def main() -> int:
    # QPainter needs a QApplication before it can paint into a QImage.
    app = QApplication([])

    def make(size: int) -> QImage:
        return render(size)

    ICON_DIR.mkdir(parents=True, exist_ok=True)

    master = make(SOURCE_SIZE)
    png_path = ICON_DIR / "enzydock.png"
    master.save(str(png_path), "PNG")

    write_icns(ICON_DIR / "enzydock.icns", make)
    write_ico(ICON_DIR / "enzydock.ico", make)

    app.deleteLater()
    for name in ("enzydock.png", "enzydock.icns", "enzydock.ico"):
        written = ICON_DIR / name
        print(f"wrote {written} ({written.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
