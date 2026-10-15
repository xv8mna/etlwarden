"""Draw the etlwarden icon: etlwarden/icon.png for the window, packaging/etlwarden.ico for the .exe.

Run it again only to change the icon: .venv\\Scripts\\python packaging\\make_icon.py
"""

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BLUE, WHITE = QColor("#0969da"), QColor("#ffffff")


def draw(size: int) -> QImage:
    """A clock face on a blue tile."""
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    scale = size / 256
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(BLUE)
    painter.drawRoundedRect(QRectF(8 * scale, 8 * scale, 240 * scale, 240 * scale), 48 * scale, 48 * scale)
    pen = QPen(WHITE, max(1.5, 18 * scale))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    centre = QPointF(128 * scale, 128 * scale)
    painter.drawEllipse(centre, 78 * scale, 78 * scale)
    painter.drawLine(centre, QPointF(128 * scale, 78 * scale))
    painter.drawLine(centre, QPointF(164 * scale, 146 * scale))
    painter.end()
    return image


def png(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def write_ico(path: Path, sizes: list[int]) -> None:
    """An .ico file is a small directory followed by one PNG per size."""
    images = [png(draw(size)) for size in sizes]
    offset = 6 + 16 * len(images)
    directory = b""
    for size, data in zip(sizes, images):
        directory += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + directory + b"".join(images))


if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    draw(256).save(str(ROOT / "etlwarden" / "icon.png"))
    write_ico(ROOT / "packaging" / "etlwarden.ico", [16, 24, 32, 48, 64, 128, 256])
    print("wrote etlwarden/icon.png and packaging/etlwarden.ico")
