from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

from .storage import Storage


@dataclass
class ImageInfo:
    path: Path
    width: int
    height: int
    area: int
    cls: str  # "full", "half", "quarter", "small"


@dataclass
class PDFComposeResult:
    pdf_path: Path
    meta_path: Path


class PDFComposer:
    def __init__(self, storage: Storage, dpi: int = 300, margin_mm: float = 15.0):
        self.storage = storage
        self.dpi = dpi
        self.margin_mm = margin_mm

        # A4 at 300 DPI
        self.A4_W = int(8.27 * dpi)  # ~2480
        self.A4_H = int(11.69 * dpi)  # ~3508

    def _classify(self, w: int, h: int) -> str:
        a4w, a4h = self.A4_W, self.A4_H
        def meets(pct: float) -> bool:
            return (w >= pct * a4w) and (h >= pct * a4h)
        if meets(0.95):
            return "full"
        if (w >= 0.45 * a4w and h >= 0.45 * a4h):
            return "half"
        if (w >= 0.22 * a4w and h >= 0.22 * a4h):
            return "quarter"
        return "small"

    def _load_infos(self, files: List[Path]) -> List[ImageInfo]:
        infos: List[ImageInfo] = []
        for p in files:
            with Image.open(p) as im:
                w, h = im.size
            infos.append(ImageInfo(path=p, width=w, height=h, area=w*h, cls=self._classify(w, h)))
        # deterministic: sort by area desc then name
        infos.sort(key=lambda x: (-x.area, str(x.path)))
        return infos

    def _margin_px(self) -> int:
        from math import ceil
        margin_from_mm = int(round(self.margin_mm / 25.4 * self.dpi))
        margin_from_pct = int(round(0.03 * self.A4_W))
        return min(max(margin_from_mm, 0), max(margin_from_pct, 0)) or margin_from_mm

    def compose(self, job: Dict, image_files: List[Path]) -> PDFComposeResult:
        if not image_files:
            raise ValueError("No images to compose")
        infos = self._load_infos(image_files)
        margin = self._margin_px()

        pdf_path, meta_path = self.storage.pdf_output_paths(job["sender"], job["msg_id"])
        c = canvas.Canvas(str(pdf_path), pagesize=(self.A4_W, self.A4_H))

        packing_decisions: List[Dict] = []

        i = 0
        while i < len(infos):
            first = infos[i]
            if first.cls == "full":
                cells = [(first, (margin, margin, self.A4_W - 2*margin, self.A4_H - 2*margin))]
                i += 1
            else:
                # Try to pack 2 or 4 per page based on classes
                candidates = [first]
                j = i + 1
                # collect up to 3 more for quarter/small or 1 more for half
                if first.cls == "half":
                    if j < len(infos) and infos[j].cls in {"half", "small", "quarter"}:
                        candidates.append(infos[j])
                        j += 1
                else:
                    while j < len(infos) and len(candidates) < 4:
                        candidates.append(infos[j])
                        j += 1

                cells = self._compute_cells(candidates, margin)
                i = j

            # draw page with cells
            page_meta = {"items": []}
            for info, (x, y, w, h) in cells:
                # scale to fit cell, preserve aspect, no upscale
                scale = min(w / info.width, h / info.height, 1.0)
                rw, rh = int(info.width * scale), int(info.height * scale)
                # center within cell
                ox = x + (w - rw) // 2
                oy = y + (h - rh) // 2
                c.drawImage(str(info.path), ox, oy, width=rw, height=rh, preserveAspectRatio=True, anchor='c')
                page_meta["items"].append({
                    "file": str(info.path),
                    "orig": [info.width, info.height],
                    "placed": [ox, oy, rw, rh],
                    "cls": info.cls,
                })
            packing_decisions.append(page_meta)
            c.showPage()

        c.save()

        # meta
        meta = {
            "sender": job.get("sender"),
            "msg_id": job.get("msg_id"),
            "dpi": self.dpi,
            "page_size_px": [self.A4_W, self.A4_H],
            "margin_px": margin,
            "files": [str(p.path) for p in infos],
            "packing": packing_decisions,
        }
        meta_path.write_text(__import__("json").dumps(meta, indent=2))
        return PDFComposeResult(pdf_path=pdf_path, meta_path=meta_path)

    def _compute_cells(self, candidates: List[ImageInfo], margin: int):
        W, H = self.A4_W - 2 * margin, self.A4_H - 2 * margin
        x0, y0 = margin, margin

        def grid(cols: int, rows: int):
            cell_w = W // cols
            cell_h = H // rows
            cells = []
            k = 0
            for r in range(rows):
                for c in range(cols):
                    if k >= len(candidates):
                        break
                    info = candidates[k]
                    x = x0 + c * cell_w
                    y = y0 + (rows - 1 - r) * cell_h
                    cells.append((info, (x, y, cell_w, cell_h)))
                    k += 1
            return cells

        first = candidates[0]
        if first.cls == "half" and len(candidates) >= 2:
            # heuristic: vertical stack if portrait-like, else side-by-side
            orient_portrait = first.height >= first.width
            if orient_portrait:
                return grid(1, 2)[:2]
            else:
                return grid(2, 1)[:2]

        if any(c.cls == "quarter" for c in candidates) or len(candidates) >= 3:
            # prefer 2x2
            return grid(2, 2)[:min(4, len(candidates))]

        # fallback single
        return [(first, (x0, y0, W, H))]