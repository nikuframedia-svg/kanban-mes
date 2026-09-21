"""Independent physical-row accounting. Never infer production from grid strokes."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

ALGORITHM_VERSION = 4
EXCLUSION_REASONS = {"out_of_scope", "duplicate", "ocr_artifact"}


def _filled(row: dict) -> bool:
    return any(value is not None and str(value).strip()
               for key, value in row.items() if not key.startswith("_"))


def nonempty_rows(extraction: dict) -> list[dict]:
    return [row for row in extraction.get("rows", []) if isinstance(row, dict)
            and row.get("_deleted") is not True and _filled(row)]


def row_accounting(data: dict) -> dict:
    rows = data.get("rows") or []
    active = len(nonempty_rows(data))
    physical, classified, removed = 0, 0, 0
    unknown = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("_deleted") is not True:
            continue
        removed += 1
        decision = row.get("_exclusion") or {}
        reason = decision.get("reason")
        valid = reason in EXCLUSION_REASONS
        if reason == "duplicate":
            target = decision.get("duplicate_of")
            valid = (type(target) is int and 0 <= target < len(rows) and target != i
                     and isinstance(rows[target], dict) and _filled(rows[target])
                     and rows[target].get("_deleted") is not True)
        if valid:
            classified += 1
            physical += int(reason == "out_of_scope" and _filled(row))
        elif _filled(row):
            unknown.append(i)
    # Removal is a production decision, not evidence that a physical row is
    # an OCR artifact or a duplicate. Keep its contribution explicitly unknown.
    return {"included_rows": active, "physical_exclusions": physical,
            "justified_exclusions": classified, "removed_rows": removed,
            "pending_exclusions": [], "unclassified_exclusions": unknown,
            "accounted_rows": active + physical,
            "accounted_max": active + physical + len(unknown)}


def count_compatible(count, accounting):
    return type(count) is int and accounting["accounted_rows"] <= count <= accounting["accounted_max"]


def sheet_identity(sheet: dict) -> dict:
    return {"image_sha256": sheet.get("image_sha256"),
            "image_rotation": int(sheet.get("image_rotation") or 0),
            "extraction_generation": int(sheet.get("extraction_generation") or 0)}


def structure_fingerprint(data: dict) -> str:
    # Values/plan substitutions do not change physical row coverage.
    structure = [{"filled": _filled(row), "deleted": row.get("_deleted") is True,
                  "exclusion": row.get("_exclusion")}
                 for row in data.get("rows", []) if isinstance(row, dict)]
    return hashlib.sha256(json.dumps(structure, sort_keys=True).encode()).hexdigest()


def coverage_resolved(data: dict, sheet: dict | None = None) -> bool:
    accounting = row_accounting(data)
    coverage = data.get("_ocr_coverage")
    if not coverage:
        return not (sheet and sheet.get("image_path"))  # Manual sheets have no paper to reconcile.
    if sheet is not None and coverage.get("context") != sheet_identity(sheet):
        return False
    confirmation = coverage.get("confirmation")
    if confirmation and (confirmation.get("structure") == structure_fingerprint(data)
            and confirmation.get("context") == coverage.get("context")
            and type(confirmation.get("count")) is int
            and count_compatible(confirmation["count"], accounting)):
        return True
    # A stale human confirmation is never reused. Independent, current image
    # evidence can nevertheless resolve the sheet without another manual step.
    return (coverage.get("algorithm_version") == ALGORITHM_VERSION
            and type(coverage.get("expected_rows")) is int
            and count_compatible(coverage["expected_rows"], accounting))


def confirm_count(data: dict, count: int, sheet: dict, actor: str, at: str) -> None:
    accounting = row_accounting(data)
    if type(count) is not int or not 0 <= count <= 200 or not count_compatible(count, accounting):
        raise ValueError("A contagem do papel não coincide com as linhas registadas, incluindo as retiradas.")
    coverage = data.setdefault("_ocr_coverage", {})
    # A changed image/generation needs recalculation; don't bind an old estimate
    # to new evidence through a count form.
    if coverage.get("context") is not None and coverage["context"] != sheet_identity(sheet):
        raise ValueError("A imagem ou a leitura mudou. Recalcula a conferência primeiro.")
    coverage["context"] = sheet_identity(sheet)
    coverage.pop("confirmed_rows", None)
    coverage["confirmation"] = {"count": count, "actor": actor, "at": at,
                                "structure": structure_fingerprint(data),
                                "context": sheet_identity(sheet)}


def coverage_view(data: dict, sheet: dict) -> dict:
    coverage = copy.deepcopy(data.get("_ocr_coverage") or {})
    coverage.update(row_accounting(data))
    coverage["extracted_rows"] = coverage["included_rows"]
    coverage["resolved"] = coverage_resolved(data, sheet)
    confirmation = coverage.get("confirmation") or {}
    human = (confirmation.get("structure") == structure_fingerprint(data)
             and confirmation.get("context") == coverage.get("context")
             and count_compatible(confirmation.get("count"), coverage))
    coverage["physical_verified"] = coverage["resolved"] and (human or not coverage["unclassified_exclusions"])
    coverage["confirmed_by_human"] = human and coverage["resolved"]
    coverage["status"] = ("complete" if coverage["physical_verified"] else "reviewed") if coverage["resolved"] else (
        "unverified" if coverage.get("expected_rows") is None else "incomplete")
    coverage["stale"] = (coverage.get("algorithm_version") != ALGORITHM_VERSION
                         or coverage.get("context") != sheet_identity(sheet))
    return coverage


def _detect(image_path: Path) -> tuple[dict | None, dict]:
    import cv2
    import numpy as np

    with Image.open(image_path) as source:
        source = ImageOps.exif_transpose(source).convert("L")
        original_size = source.size
        source.thumbnail((1200, 1200))
        gray = np.asarray(source)
    height, width = gray.shape
    diagnostics = {"width": width, "height": height}
    if width < 300 or height < 200:
        return None, diagnostics | {"reason": "image_too_small"}
    ink = (gray < 150).astype("uint8") * 255
    lines = cv2.HoughLinesP(ink, 1, np.pi / 1800, threshold=70,
                            minLineLength=int(width * .45), maxLineGap=12)
    horizontals = [] if lines is None else [line[0] for line in lines
        if abs(int(line[0][2]) - int(line[0][0])) > width * .5
        and abs(int(line[0][3]) - int(line[0][1])) < height * .12]
    if len(horizontals) < 6:
        return None, diagnostics | {"reason": "horizontal_grid_not_found"}
    angles = [math.degrees(math.atan2(int(y2)-int(y1), int(x2)-int(x1)))
              for x1, y1, x2, y2 in horizontals]
    angle = statistics.median(angles)
    if abs(angle) > 6 or statistics.median(abs(a-angle) for a in angles) > .8:
        return None, diagnostics | {"reason": "irregular_grid"}
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1)
    gray = cv2.warpAffine(gray, matrix, (width, height), borderValue=255)
    ink = (gray < 150).astype("uint8") * 255
    segments = cv2.HoughLinesP(ink, 1, np.pi / 1800, threshold=60,
        minLineLength=max(60, int(height * .23)), maxLineGap=12)
    mask = np.zeros_like(ink)
    horizontal_segments, vertical_xs, vertical_tops, vertical_bottoms = [], [], [], []
    if segments is not None:
        for x1, y1, x2, y2 in segments[:, 0]:
            x1, y1, x2, y2 = map(int, (x1, y1, x2, y2))
            dx, dy = abs(x2-x1), abs(y2-y1)
            horizontal = dx > width * .16 and dy < height * .018
            vertical = dy > height * .23 and dx < width * .025
            if horizontal or vertical:
                cv2.line(mask, (x1, y1), (x2, y2), 255, max(5, round(width * .0075)))
            if horizontal:
                # Evaluate each fitted line at the page centre.
                horizontal_segments.append((round(y1 + (width/2-x1) * (y2-y1) / (x2-x1)), min(x1,x2), max(x1,x2)))
            if vertical:
                vertical_xs.append(round((x1+x2)/2))
                if dy > height * .55 and width * .08 < (x1+x2)/2 < width * .92:
                    vertical_tops.append(min(y1,y2))
                    vertical_bottoms.append(max(y1,y2))
    # Merge collinear segments before measuring support: handwriting often
    # interrupts a table rule. Short header boxes must not extend the row run.
    bands = []
    for segment in sorted(horizontal_segments):
        if not bands or segment[0] - bands[-1][-1][0] > 6:
            bands.append([segment])
        else:
            bands[-1].append(segment)
    lines_y = []
    for band in bands:
        intervals = sorted((left, right) for _, left, right in band)
        covered, left, right = 0, *intervals[0]
        for a, b in intervals[1:]:
            if a > right:
                covered += right-left
                left, right = a, b
            else:
                right = max(right, b)
        covered += right-left
        if covered >= width * .80:
            lines_y.append(round(statistics.median(y for y, _, _ in band)))
    best = []
    for start in range(len(lines_y) - 4):
        run = lines_y[start:start+2]
        gap = run[1] - run[0]
        if run[0] < height * .10 or not height * .018 < gap < height * .12:
            continue
        for y in lines_y[start+2:]:
            if abs(y-run[-1]-gap) > gap * .15:
                break
            run.append(y)
        if len(run) > len(best):
            best = run
    distinct_xs = []
    for x in sorted(vertical_xs):
        if not distinct_xs or x - distinct_xs[-1] > width * .025:
            distinct_xs.append(x)
    diagnostics.update(deskew_angle=round(angle, 4), vertical_lines=len(distinct_xs),
                       horizontal_lines=len(best), grid_y=best, vertical_tops=vertical_tops)
    if (len(best) < 6 or best[0] < height * .1 or best[-1] < height * .6
            or len(distinct_xs) < 4):
        return None, diagnostics | {"reason": "incomplete_grid"}
    # Long interior dividers identify the top of the table independently.
    # If horizontal rules disappeared in the written area, a shorter regular
    # run of blank rows must not be reported as a complete table.
    if len(vertical_tops) < 3:
        return None, diagnostics | {"reason": "table_boundary_unverified"}
    table_top = statistics.median(vertical_tops)
    gap = statistics.median(b-a for a,b in zip(best,best[1:]))
    # TPL102: the printed column heading is shorter than a production row.
    # The regular run begins at the FIRST data row and ends below the footer.
    # Verify both independent divider ends; never skip the first written row
    # or count footer labels as production (the TPL999 layout is different).
    bottom = statistics.median(vertical_bottoms)
    diagnostics.update(table_top=table_top, divider_bottom=bottom, template="TPL102")
    if (len(best) != 17 or not .50 * gap < best[0] - table_top < 1.15 * gap
            or abs(bottom - best[-2]) > .35 * gap):
        return None, diagnostics | {"reason": "table_boundary_mismatch"}
    clean = cv2.bitwise_and(ink, cv2.bitwise_not(mask))
    rows = []
    for top, bottom in zip(best[:-2], best[1:-1]):
        margin = max(6, round((bottom-top)*.20))
        region = clean[top+margin:bottom-margin, int(width*.03):int(width*.97)]
        ratio = float(np.mean(region > 0)) if region.size else 0
        # Residual long strokes across most of a blank row indicate a failed
        # mask. Multiple narrow residuals must not be called handwriting.
        columns = np.sum(region > 0, axis=0) if region.size else []
        long_columns = sum(value > region.shape[0] * .9 for value in columns)
        if ratio < .025 and long_columns > width * .012:
            return None, diagnostics | {"reason": "grid_removal_unreliable"}
        # Sparse rows (only a short reference/quantity or a profile and X)
        # must not be diluted by the width of all the empty cells. Measure
        # handwriting components after removing the grid; discard isolated
        # scanner specks instead of lowering a page-wide density threshold.
        count, _, components, _ = cv2.connectedComponentsWithStats(region, 8)
        strokes = [stat for stat in components[1:count]
                   if stat[cv2.CC_STAT_WIDTH] >= 2
                   and stat[cv2.CC_STAT_HEIGHT] >= max(3, region.shape[0] * .18)
                   and stat[cv2.CC_STAT_AREA] >= 6
                   and not (stat[cv2.CC_STAT_HEIGHT] > 3 * stat[cv2.CC_STAT_WIDTH]
                            and any(abs(int(width*.03) + stat[cv2.CC_STAT_LEFT]
                                        + stat[cv2.CC_STAT_WIDTH]/2 - x) < width*.012
                                    for x in distinct_xs))]
        written_area = sum(int(stat[cv2.CC_STAT_AREA]) for stat in strokes)
        filled = written_area >= max(18, region.shape[0] * .8)
        rows.append({"top": top, "bottom": bottom, "filled": filled,
                     "ink_ratio": round(ratio, 6), "written_area": written_area})
    scale = original_size[1] / height
    detected = {"header_bottom": round(best[0]*scale), "table_bottom": round(best[-2]*scale),
                "deskew_angle": float(angle),
                "rows": [{**row, "top": round(row["top"]*scale),
                          "bottom": round(row["bottom"]*scale)} for row in rows]}
    return detected, diagnostics | {"reason": "regular_grid"}


def table_rows(image_path: Path) -> dict | None:
    return _detect(image_path)[0]


def check_coverage(image_path: Path, extraction: dict) -> dict:
    detected, diagnostics = _detect(image_path)
    actual = row_accounting(extraction)["accounted_rows"]
    expected = sum(row["filled"] for row in detected["rows"]) if detected else None
    return {"status": "unverified" if expected is None else
            "complete" if expected == actual else "incomplete",
            "expected_rows": expected, "extracted_rows": len(nonempty_rows(extraction)),
            "method": "fitted_grid" if detected else "manual_count_required",
            "algorithm_version": ALGORITHM_VERSION,
            "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
            "diagnostics": diagnostics}


def recover_coverage(provider, image_path: Path, template, extraction: dict) -> dict:
    coverage = check_coverage(image_path, extraction)
    extraction = {**extraction, "_ocr_coverage": coverage}
    if coverage["status"] != "incomplete":
        return extraction
    detected = table_rows(image_path)
    if not detected:
        return extraction
    groups = [detected["rows"][i:i+4] for i in range(0, len(detected["rows"]), 4)]
    recovered = []
    from .provider import OcrError
    try:
        with Image.open(image_path) as source, tempfile.TemporaryDirectory(prefix="kanban-rows-") as directory:
            source = ImageOps.exif_transpose(source).convert("RGB")
            source = source.rotate(detected["deskew_angle"], resample=Image.Resampling.BICUBIC,
                                   expand=False, fillcolor="white")
            header = source.crop((0, 0, source.width, detected["header_bottom"]))
            for number, group in enumerate(groups):
                expected = sum(row["filled"] for row in group)
                if not expected:
                    continue
                strip = source.crop((0, group[0]["top"], source.width, group[-1]["bottom"]))
                page = Image.new("RGB", (source.width, header.height+strip.height), "white")
                page.paste(header, (0, 0)); page.paste(strip, (0, header.height))
                path = Path(directory) / f"rows-{number}.png"
                page.save(path)
                rows = nonempty_rows(provider.extract(path, template))
                if len(rows) != expected:
                    raise OcrError(f"Grupo {number+1}: esperadas {expected} linhas; lidas {len(rows)}.")
                recovered.extend(rows)
    except OcrError as exc:
        coverage["recovery_error"] = str(exc)[:500]
        return extraction
    if len(recovered) == coverage["expected_rows"]:
        extraction["rows"] = recovered
        coverage.update(status="complete", extracted_rows=len(recovered), recovered_by_groups=True)
    return extraction
