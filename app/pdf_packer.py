from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import random
import math
import logging
from datetime import datetime, timezone

from .storage import Storage

# Tolerance and layout constants
TOL_DIM = 0.18   # 18% per-dimension tolerance for "roughly A5"
TOL_AREA = 0.30  # 30% area tolerance for "roughly A5"
TOL_AR = 0.10    # 10% aspect ratio tolerance for "roughly A5"
# Consider "larger than A5" if long side >= 100% and short side >= 65% of A5 at comparison DPI
LARGER_MIN_SHORT = 0.65
LARGER_MIN_LONG = 1.00
PAIR_SEARCH_N = 50       # Search window for finding a second A5-like image
GUTTER_MM = 5.0          # Gap between side-by-side A5 cells

# Use a separate DPI for A5 comparison to better reflect typical phone images (often scaled)
# This makes "larger than A5" and "roughly A5" more permissive while still reasonable.
A5_COMPARE_DPI = 200


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

        # A4 at 300 DPI (inches: 8.27 x 11.69)
        self.A4_W = int(8.27 * dpi)  # ~2480
        self.A4_H = int(11.69 * dpi)  # ~3508

        # A5 size in pixels at given DPI (148 x 210 mm)
        self.A5_W = self._mm_to_px(148.0)
        self.A5_H = self._mm_to_px(210.0)

        # logger
        self._log = logging.getLogger(__name__).info
        try:
            self._log(f"pdf_packer init: dpi={self.dpi} A4_px=({self.A4_W}x{self.A4_H}) A5_px=({self.A5_W}x{self.A5_H})")
        except Exception:
            pass

    def _mm_to_px(self, mm: float) -> int:
        return int(round(mm / 25.4 * self.dpi))

    def _mm_to_px_custom(self, mm: float, dpi: int) -> int:
        """Convert mm to pixels at a custom DPI, used for A5 comparison thresholds."""
        return int(round(mm / 25.4 * dpi))

    def _mm_to_pts(self, mm: float) -> float:
        """Convert millimeters to points (1 pt = 1/72 inch). Useful if switching to points."""
        return mm * 72.0 / 25.4

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
        """
        Load image metadata, skipping non-image files gracefully (e.g., PDFs or corrupt files).
        """
        infos: List[ImageInfo] = []
        for p in files:
            try:
                # Quick extension filter to avoid obvious non-images
                if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}:
                    # Try to open anyway in case extension is misleading
                    with Image.open(p) as im:
                        w, h = im.size
                else:
                    with Image.open(p) as im:
                        w, h = im.size
            except Exception:
                # Skip files PIL cannot identify (e.g., PDFs or text)
                continue
            infos.append(ImageInfo(path=p, width=w, height=h, area=w*h, cls=self._classify(w, h)))
        # deterministic: sort by area desc then name
        infos.sort(key=lambda x: (-x.area, str(x.path)))
        return infos

    def _margin_px(self) -> int:
        from math import ceil
        margin_from_mm = int(round(self.margin_mm / 25.4 * self.dpi))
        margin_from_pct = int(round(0.03 * self.A4_W))
        return min(max(margin_from_mm, 0), max(margin_from_pct, 0)) or margin_from_mm

    # --- A5-specific logic ---
    def _a5_dims(self, use_compare_dpi: bool = False) -> Tuple[int, int]:
        """Return (short_side_px, long_side_px) for A5. If use_compare_dpi=True, use A5_COMPARE_DPI."""
        if use_compare_dpi:
            short = min(self._mm_to_px_custom(148.0, A5_COMPARE_DPI), self._mm_to_px_custom(210.0, A5_COMPARE_DPI))
            long = max(self._mm_to_px_custom(148.0, A5_COMPARE_DPI), self._mm_to_px_custom(210.0, A5_COMPARE_DPI))
        else:
            short = min(self.A5_W, self.A5_H)
            long = max(self.A5_W, self.A5_H)
        return short, long

    def _is_a5_roughly(self, w: int, h: int, tol: float = TOL_DIM) -> bool:
        """
        Return True if the image dimensions are roughly equal to A5.
        Accept either per-dimension match within tol, or (area within TOL_AREA and aspect ratio within TOL_AR).
        Orientation is normalized by sorting sides. Uses a more permissive comparison DPI for robustness.
        """
        img_short, img_long = sorted((w, h))
        # Use comparison DPI to reduce false negatives from scaling artifacts
        a5_short, a5_long = self._a5_dims(use_compare_dpi=True)

        # Per-dimension tolerance check
        dim_ok = (
            (1 - tol) * a5_short <= img_short <= (1 + tol) * a5_short and
            (1 - tol) * a5_long  <= img_long  <= (1 + tol) * a5_long
        )

        if dim_ok:
            return True

        # Fallback by area + aspect
        img_area = max(1, img_short * img_long)
        a5_area = a5_short * a5_long
        if a5_area <= 0:
            return False
        area_ok = abs(img_area - a5_area) / a5_area <= TOL_AREA

        img_ar = img_long / max(img_short, 1)
        a5_ar = a5_long / max(a5_short, 1)
        ar_ok = abs(img_ar - a5_ar) / a5_ar <= TOL_AR

        return area_ok and ar_ok

    def _is_larger_than_a5(self, w: int, h: int) -> bool:
        """
        Return True if the image should be considered larger than A5.
        Recommended strategy:
        - Primary: max_dim >= A5_long AND min_dim >= A5_short * LARGER_MIN_SHORT (e.g., 0.8)
        - Fallback: area >= A5 area * (1 + 10%)
        """
        img_short, img_long = sorted((w, h))
        a5_short, a5_long = self._a5_dims()

        primary = (img_long >= a5_long * LARGER_MIN_LONG) and (img_short >= a5_short * LARGER_MIN_SHORT)

        if primary:
            return True

        # Fallback by area
        img_area = img_short * img_long
        a5_area = a5_short * a5_long
        if a5_area > 0 and (img_area >= a5_area * 1.10):
            return True

        return False

    def _pack_page_a5(self, remaining: List[ImageInfo], margin: int) -> Tuple[List[Tuple[ImageInfo, Tuple[int,int,int,int]]], int, bool, List[int]]:
        """
        Implement the requested A5 logic:
        - If first image is larger than A5 -> put it as a single full-page image.
        - Else, search the first PAIR_SEARCH_N items for a pair of roughly-A5 images and place them side-by-side.
        Returns (cells, used_count, allow_upscale, used_indices_relative_to_remaining).
        """
        if not remaining:
            return [], 0, False, []

        page_w = self.A4_W - 2 * margin
        page_h = self.A4_H - 2 * margin
        x0, y0 = margin, margin

        first = remaining[0]
        a5_short, a5_long = self._a5_dims()
        try:
            self._log(f"[A5] page_w={page_w} page_h={page_h} a5_px=({a5_short}x{a5_long}) first=({first.width}x{first.height})")
        except Exception:
            pass

        if self._is_larger_than_a5(first.width, first.height):
            try:
                self._log(f"[A5] first image considered larger-than-A5 -> full page")
            except Exception:
                pass
            # Single full page cell
            return [(first, (x0, y0, page_w, page_h))], 1, False, [0]

        # Find two A5-like images within a window
        window_n = min(PAIR_SEARCH_N, len(remaining))
        a5_indices = []
        for idx in range(window_n):
            info = remaining[idx]
            if self._is_a5_roughly(info.width, info.height):
                a5_indices.append(idx)
                try:
                    self._log(f"[A5] candidate roughly A5 at idx={idx} size=({info.width}x{info.height})")
                except Exception:
                    pass

        if len(a5_indices) >= 2:
            i, j = a5_indices[0], a5_indices[1]
            left_info = remaining[i]
            right_info = remaining[j]
            gutter = max(self._mm_to_px(GUTTER_MM), 8)  # small gap between halves
            cell_w = (page_w - gutter) // 2
            left_cell = (x0, y0, cell_w, page_h)
            right_cell = (x0 + cell_w + gutter, y0, cell_w, page_h)
            try:
                self._log(f"[A5] pairing indices {i},{j} -> side-by-side cells width={cell_w} gutter={gutter}")
            except Exception:
                pass
            return [(left_info, left_cell), (right_info, right_cell)], 2, False, [i, j]

        try:
            self._log(f"[A5] no special A5 placement, fallback to advanced packer")
        except Exception:
            pass
        # no special packing
        return [], 0, False, []

    def compose(self, job: Dict, image_files: List[Path]) -> PDFComposeResult:
        if not image_files:
            raise ValueError("No images to compose")
        infos = self._load_infos(image_files)
        if not infos:
            # Prevent empty PDFs: no valid image detected
            raise ValueError("No valid images to compose (all inputs were non-image or unreadable)")
        margin = self._margin_px()

        pdf_path, meta_path = self.storage.pdf_output_paths(job["sender"], job["msg_id"])
        c = canvas.Canvas(str(pdf_path), pagesize=(self.A4_W, self.A4_H))

        packing_decisions: List[Dict] = []

        i = 0
        while i < len(infos):
            # Try A5 rules first. This may select non-consecutive indices; if so, we'll remove them explicitly.
            special = self._pack_page_a5(infos[i:], margin)
            if special[1] > 0:
                special_cells, used_special, allow_upscale_special, used_indices = special
                cells, allow_upscale = special_cells, allow_upscale_special

                # draw page with cells
                page_meta = {"items": []}
                for info, (x, y, w, h) in cells:
                    scale = min(w / info.width, h / info.height)
                    if not allow_upscale:
                        scale = min(scale, 1.0)
                    rw, rh = int(info.width * scale), int(info.height * scale)
                    ox = int(x + (w - rw) // 2)
                    oy = int(y + (h - rh) // 2)
                    c.drawImage(str(info.path), ox, oy, width=rw, height=rh, preserveAspectRatio=True, anchor='c')
                    page_meta["items"].append({
                        "file": str(info.path),
                        "orig": [info.width, info.height],
                        "placed": [ox, oy, rw, rh],
                        "cls": info.cls,
                    })
                border_inset = self._mm_to_px(5.0)
                c.setLineWidth(1)
                c.rect(border_inset, border_inset, self.A4_W - 2 * border_inset, self.A4_H - 2 * border_inset, stroke=1, fill=0)
                packing_decisions.append(page_meta)
                c.showPage()

                # Remove used items from infos[i:] by popping indices in descending order
                if used_indices:
                    for rel_idx in sorted(used_indices, reverse=True):
                        infos.pop(i + rel_idx)
                # Do not advance i; the next item shifts into position i
                continue
            else:
                # Fallback to advanced packer which always consumes from the head
                cells, used, allow_upscale, stretch = self._pack_page_advanced(infos[i:], margin)

                # draw page with cells
                page_meta = {"items": []}
                for info, (x, y, w, h) in cells:
                    scale = min(w / info.width, h / info.height)
                    if not allow_upscale:
                        scale = min(scale, 1.0)
                    rw, rh = int(info.width * scale), int(info.height * scale)
                    ox = int(x + (w - rw) // 2)
                    oy = int(y + (h - rh) // 2)
                    c.drawImage(str(info.path), ox, oy, width=rw, height=rh, preserveAspectRatio=True, anchor='c')
                    page_meta["items"].append({
                        "file": str(info.path),
                        "orig": [info.width, info.height],
                        "placed": [ox, oy, rw, rh],
                        "cls": info.cls,
                    })
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