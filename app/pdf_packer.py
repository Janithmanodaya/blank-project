from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import random

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

    def _mm_to_px(self, mm: float) -> int:
        return int(round(mm / 25.4 * self.dpi))

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
            cells, used, allow_upscale, stretch = self._pack_page_advanced(infos[i:], margin)
            # draw page with cells
            page_meta = {"items": []}
            for info, (x, y, w, h) in cells:
                if stretch:
                    # fill the rectangle, ignore original aspect
                    ox, oy, rw, rh = int(x), int(y), int(w), int(h)
                    c.drawImage(str(info.path), ox, oy, width=rw, height=rh, preserveAspectRatio=False, anchor='c')
                else:
                    # scale to fit cell, preserve aspect
                    scale = min(w / info.width, h / info.height) if allow_upscale else min(w / info.width, h / info.height, 1.0)
                    rw, rh = int(info.width * scale), int(info.height * scale)
                    # center within cell
                    ox = int(x + (w - rw) // 2)
                    oy = int(y + (h - rh) // 2)
                    c.drawImage(str(info.path), ox, oy, width=rw, height=rh, preserveAspectRatio=True, anchor='c')
                page_meta["items"].append({
                    "file": str(info.path),
                    "orig": [info.width, info.height],
                    "placed": [ox, oy, rw, rh],
                    "cls": info.cls,
                })
            # draw 0.5 cm border around page
            border_inset = self._mm_to_px(5.0)
            c.setLineWidth(1)
            c.rect(border_inset, border_inset, self.A4_W - 2 * border_inset, self.A4_H - 2 * border_inset, stroke=1, fill=0)
            packing_decisions.append(page_meta)
            c.showPage()
            i += used

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

    def _pack_page(self, remaining: List[ImageInfo], margin: int) -> Tuple[List[Tuple[ImageInfo, Tuple[int,int,int,int]]], int, bool]:
        """
        Backward-compat packing kept for reference; not used once advanced is present.
        """
        cells, used, allow_upscale, _ = self._pack_page_advanced(remaining, margin)
        return cells, used, allow_upscale

    def _pack_page_advanced(self, remaining: List[ImageInfo], margin: int) -> Tuple[List[Tuple[ImageInfo, Tuple[int,int,int,int]]], int, bool, bool]:
        """
        Advanced packing based on target area fill.
        - Compute available area in mm^2 (A4 minus margins), target ~ 62370 mm^2 (user-guided constant).
        - Determine scaled sizes for a batch of images so total area approximates target, respecting gaps and border.
        - Place rectangles in a simple shelf-packing manner without overlap.
        Returns (cells, used_count, allow_upscale, stretch)
        - allow_upscale: True when we intentionally scale images up.
        - stretch: True if we fill cells ignoring aspect ratio to match target area.
        """
        # Convert geometry to mm to follow user's requirement
        W_px, H_px = self.A4_W - 2 * margin, self.A4_H - 2 * margin
        W_mm, H_mm = W_px / self.dpi * 25.4, H_px / self.dpi * 25.4
        target_area_mm2 = 62370.0  # as per instruction
        gap_mm = 5.0  # 0.5 cm gap
        gap_px = self._mm_to_px(gap_mm)

        # Decide how many images to try on this page: up to 8 provides variety
        max_try = min(8, len(remaining))
        batch = remaining[:max_try]

        # Base sizes: treat each input as \"A4-sized\" reference for fair split, then scale to target
        # Compute each image aspect, then assign widths proportionally so that sum of areas ~= target
        aspects = []
        for info in batch:
            ar = max(info.width, 1) / max(info.height, 1)
            aspects.append(ar)

        # Heuristic: split target area evenly by count, then adjust rectangles by aspect
        per_img_area = target_area_mm2 / max_try
        rects_mm = []  # (w_mm, h_mm)
        for ar in aspects:
            h_mm = (per_img_area / ar) ** 0.5
            w_mm = ar * h_mm
            rects_mm.append((w_mm, h_mm))

        # Convert to px
        rects_px = [(int(round(w_mm / 25.4 * self.dpi)), int(round(h_mm / 25.4 * self.dpi))) for (w_mm, h_mm) in rects_mm]

        # Shelf-pack left-to-right, top-to-bottom with gap, randomize order for \"random placement\"
        order = list(range(len(rects_px)))
        random.shuffle(order)

        positions: List[Tuple[int, int, int, int]] = []  # x, y, w, h
        x = 0
        y_top = H_px
        row_h = 0
        used_count = 0

        for idx in order:
            w, h = rects_px[idx]
            # If it doesn't fit in current row, move to new row
            if x > 0 and x + w > W_px:
                # new row
                y_top -= (row_h + gap_px)
                x = 0
                row_h = 0
            # If doesn't fit vertically, stop
            if y_top - h < 0:
                break
            # place
            positions.append((x, int(y_top - h), w, h))
            x += w + gap_px
            row_h = max(row_h, h)
            used_count += 1

        # Map positions back to actual page coordinates with margins
        cells = []
        for pos_idx, (px, py, pw, ph) in enumerate(positions):
            img_info = batch[order[pos_idx]]
            cells.append((img_info, (margin + px, margin + py, pw, ph)))

        allow_upscale = True  # we are assigning synthetic sizes
        stretch = True        # we fill rectangles ignoring original aspect when drawing

        # If nothing placed (e.g., extremely large rects), fall back to previous logic for robustness
        if not cells:
            cells_fallback, used_fb, allow_up_fb = self._pack_page(remaining, margin)
            return cells_fallback, used_fb, allow_up_fb, False

        return cells, used_count, allow_upscale, stretch