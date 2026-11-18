"""QR code helpers (no Pillow needed: the GUI draws the matrix on a canvas)."""
from __future__ import annotations

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def qr_matrix(text: str) -> list[list[bool]]:
    q = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=0, box_size=1)
    q.add_data(text)
    q.make(fit=True)
    return [[bool(c) for c in row] for row in q.get_matrix()]


def ascii_qr(text: str, quiet: int = 2) -> str:
    """Console QR for dark terminals: light modules are drawn as filled blocks,
    so the code appears as dark-on-light, which is what scanners expect."""
    m = qr_matrix(text)
    n = len(m)
    light, dark = "██", "  "
    blank = light * (n + 2 * quiet)
    rows = [blank] * quiet
    for row in m:
        rows.append(light * quiet + "".join(dark if c else light for c in row) + light * quiet)
    rows += [blank] * quiet
    return "\n".join(rows)
