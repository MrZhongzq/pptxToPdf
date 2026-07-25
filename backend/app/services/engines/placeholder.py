from pathlib import Path

from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from app.services.engines.base import EMU_PER_POINT, ConversionEngine
from app.services.pptx_probe import PptxMeta


class PlaceholderEngine(ConversionEngine):
    """一期占位引擎：页数与页面尺寸真实，内容为占位文字。"""

    name = "placeholder"

    def convert(self, src: Path, meta: PptxMeta, dest: Path, *, timeout_s: float) -> None:
        width = meta.slide_width_emu / EMU_PER_POINT
        height = meta.slide_height_emu / EMU_PER_POINT

        dest.parent.mkdir(parents=True, exist_ok=True)
        pdf = canvas.Canvas(str(dest), pagesize=(width, height))
        for page in range(1, meta.slide_count + 1):
            pdf.setFont("Helvetica", 28)
            pdf.drawCentredString(
                width / 2, height / 2, f"{page} / {meta.slide_count}"
            )
            pdf.setFont("Helvetica", 12)
            pdf.drawCentredString(
                width / 2, height / 2 - 0.5 * inch, "engine not wired yet (phase 1)"
            )
            pdf.showPage()
        pdf.save()
