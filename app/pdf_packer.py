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
                # Always preserve original aspect ratio.
                # Compute scale to fit inside the cell, optionally avoiding upscaling.
                scale = min(w / info.width, h / info.height)
                if not allow_upscale:
                    scale = min(scale, 1.0)
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
        Advanced packing based on target area fill with guillotine bin packing and shape selection.
        - Target total area per page: ~62,370 mm^2.
        - Aim to place up to 8 images; choose shapes (square, portrait, landscape) to fit free spaces.
        - Adds 0.5 cm gaps between items by shrinking split rectangles.
        Returns (cells, used_count, allow_upscale, stretch).
        """
        # Geometry and constants
        page_w, page_h = self.A4_W - 2 * margin, self.A4_H - 2 * margin
        target_area_mm2 = 62370.0
        gap_mm = 5.0  # 0.5 cm gap
        gap_px = self._mm_to_px(gap_mm)
        target_n = min(8, len(remaining))
        batch = remaining[:target_n]

        # Candidate aspect options per image (orig, square, common ratios)
        def aspect_options(info: ImageInfo) -> List[float]:
            ar = max(info.width, 1) / max(info.height, 1)
            opts = [ar, 1.0, 4/3, 3/4, 16/9, 9/16]
            # Dedup while preserving order
            seen = set()
            uniq = []
            for a in opts:
                key = round(a, 4)
                if key not in seen:
                    seen.add(key)
                    uniq.append(a)
            return uniq

        # Helper: compute rect size (w,h) in px that best fits inside free rect for a desired area and aspect
        def best_size_in(fr_w: int, fr_h: int, desired_area_mm2: float, aspect: float) -> Tuple[int, int, int]:
            # Convert desired area to px^2
            desired_area_px2 = desired_area_mm2 * (self.dpi / 25.4) ** 2
            # Start from the maximum that fits in free rect keeping aspect
            # Two candidates: limited by width or by height
            w_by_w = fr_w
            h_by_w = int(w_by_w / aspect)
            if h_by_w > fr_h:
                h_by_w = fr_h
                w_by_w = int(h_by_w * aspect)
            area_w = w_by_w * h_by_w

            h_by_h = fr_h
            w_by_h = int(h_by_h * aspect)
            if w_by_h > fr_w:
                w_by_h = fr_w
                h_by_h = int(w_by_h / aspect)
            area_h = w_by_h * h_by_h

            # Choose the larger feasible candidate
            if area_w >= area_h:
                w0, h0, area0 = w_by_w, h_by_w, area_w
            else:
                w0, h0, area0 = w_by_h, h_by_h, area_h

            # Adjust towards desired area by uniform scale if possible (not exceeding free rect)
            if area0 > 0 and desired_area_px2 > 0:
                scale = (desired_area_px2 / area0) ** 0.5
                w1 = min(fr_w, int(w0 * scale))
                h1 = min(fr_h, int(h0 * scale))
                # Re-normalize to aspect
                if w1 / max(h1, 1) > aspect:
                    w1 = int(h1 * aspect)
                else:
                    h1 = int(w1 / aspect)
                # Ensure positive
                w1 = max(w1, 1)
                h1 = max(h1, 1)
                return w1, h1, w1 * h1
            return w0, h0, area0

        # Free rectangles list: start with whole page
        class Rect:
            __slots__ = ("x", "y", "w", "h")
            def __init__(self, x: int, y: int, w: int, h: int):
                self.x, self.y, self.w, self.h = x, y, w, h

        free: List[Rect] = [Rect(0, 0, page_w, page_h)]

        # Utilities for splitting and maintaining free rects
        def split_free_rect(fr: Rect, px: int, py: int, pw: int, ph: int):
            """Guillotine split: split along placed rect to create right and bottom spaces, with gaps."""
            # Right rect
            rx = px + pw + gap_px
            rw = fr.x + fr.w - rx
            if rw > 0:
                free.append(Rect(rx, fr.y, rw, fr.h))
            # Bottom rect
            by = py + ph + gap_px
            bh = fr.y + fr.h - by
            if bh > 0:
                free.append(Rect(fr.x, by, fr.w, bh))

        def prune_free():
            """Remove contained rectangles to reduce fragmentation."""
            i = 0
            while i < len(free):
                a = free[i]
                removed = False
                j = 0
                while j < len(free):
                    if i != j:
                        b = free[j]
                        if (a.x >= b.x and a.y >= b.y and a.x + a.w <= b.x + b.w and a.y + a.h <= b.y + b.h):
                            # a contained in b
                            free.pop(i)
                            removed = True
                            break
                    j += 1
                if not removed:
                    i += 1

        def find_best_placement(desired_area_mm2: float):
            best = None  # (score, img_idx, fr_idx, x, y, w, h)
            for fr_idx, fr in enumerate(free):
                # usable space inside free rect considering leading gaps at top/left
                ux, uy, uw, uh = fr.x, fr.y, fr.w, fr.h
                for img_idx, info in enumerate(batch):
                    if info is None:
                        continue
                    for ar in aspect_options(info):
                        w, h, area = best_size_in(uw, uh, desired_area_mm2, ar)
                        if w <= 0 or h <= 0:
                            continue
                        # Score: prioritize higher area use and better aspect fit to space
                        fill_ratio = area / (uw * uh)
                        # Additional penalty for leftover slivers
                        score = fill_ratio
                        candidate = (score, img_idx, fr_idx, ux, uy, w, h)
                        if (best is None) or (score > best[0]):
                            best = candidate
            return best

        # Try decreasing per-image area to fit all 8 if needed
        success_cells: List[Tuple[ImageInfo, Tuple[int,int,int,int]]] = []
        used_flags = [False] * len(batch)

        for scale_area in [1.0, 0.9, 0.8, 0.75, 0.7, 0.65, 0.6]:
            # reset free rects for each attempt
            free = [Rect(0, 0, page_w, page_h)]
            success_cells.clear()
            for i in range(len(batch)):
                used_flags[i] = False
            per_area = (target_area_mm2 / target_n) * scale_area

            placed = 0
            while placed < target_n:
                best = find_best_placement(per_area)
                if best is None:
                    break
                score, img_idx, fr_idx, x, y, w, h = best
                # Place
                info = batch[img_idx]
                success_cells.append((info, (x + margin, y + margin, w, h)))
                placed += 1
                used_flags[img_idx] = True
                batch[img_idx] = None  # mark used

                # Split free rect
                fr = free.pop(fr_idx)
                split_free_rect(fr, x, y, w, h)
                prune_free()

            if placed == target_n:
                allow_upscale = True
                stretch = True  # fill rectangles ignoring aspect for full coverage
                return success_cells, placed, allow_upscale, stretch

            # restore batch for next attempt
            # Rebuild batch from remaining with original order
            batch = [remaining[i] for i in range(target_n)]

        # If couldn't place all target_n, return what we managed to place
        allow_upscale = True
        stretch = True
        return success_cells, len(success_cells), allow_upscale, stretch