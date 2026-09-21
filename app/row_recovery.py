"""Align a narrow OCR strip with physical positions, preserving stable row IDs."""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from PIL import Image, ImageOps
from .matching.geometry import canonical_code
from .matching.similarity import parse_number
from .matching.angle_geometry import parse_profile
from .templates_spec import field_value, is_marked


def key(row):
    if is_marked(field_value(row, "perf_comp")):
        profile = parse_profile(row.get("perfil"))
        # A complete profile is a physical row even without a piece reference
        # or numeric quantity. Its X must never attach to a neighbouring piece.
        if (profile.family == "L" and len(profile.dimensions) == 3
                and all(d > 0 for d in profile.dimensions)
                and not canonical_code(row.get("modelo"))
                and parse_number(row.get("qtd")) is None):
            return "full", profile.key
        return None
    model, qty = canonical_code(row.get("modelo")), parse_number(row.get("qtd"))
    return ("piece", model, qty) if model and qty is not None else None


def align_strip(candidates, positions, original, current, anchor_observations=None):
    """Only interior omissions between consecutive, unique existing anchors."""
    anchors = []
    for offset, row in enumerate(candidates):
        identity = key(row)
        if identity is None:
            raise ValueError("A zona relida contém uma referência, quantidade ou perfil completo ambíguo.")
        hits = [i for i in range(len(original)) if identity in {key(original[i]), key(current[i])}]
        if len(hits) > 1:
            raise ValueError("Referências repetidas impedem o alinhamento automático.")
        if hits:
            anchors.append((offset, hits[0]))
    if any(b[1] <= a[1] for a, b in zip(anchors, anchors[1:])):
        raise ValueError("A ordem da nova leitura não coincide com a folha.")
    additions = []
    for left, right in zip(anchors, anchors[1:]):
        if right[0] - left[0] > 1:
            if right[1] != left[1] + 1:
                raise ValueError("Não foi possível isolar a linha em falta.")
            for n in range(left[0] + 1, right[0]):
                row = copy.deepcopy(candidates[n])
                # Only inherit from the immediate preceding physical anchor;
                # a following OF never identifies an earlier unmatched row.
                source = current[left[1]]
                for field in ("of", "ov", "cliente", "perfil"):
                    if not row.get(field) or str(row[field]).strip() in {'"', '”', '〃', "''"}:
                        row[field] = source.get(field)
                if (anchor_observations is not None and key(row)[0] == "full"
                        and n == left[0] + 1
                        and not candidates[left[0]].get("perfil")
                        and parse_profile(original[left[1]].get("perfil")).key == key(row)[1]):
                    # The reread proves that the original OCR put this full
                    # profile on the preceding piece. Preserve current values;
                    # correct only the evidence used by future cross-checks.
                    anchor_observations[str(left[1])] = {"perfil": None}
                row["_paper_position"] = positions[n]
                additions.append((left[1], row))
    return additions


def recover_missing(provider, image, template, sheet, detected, anchor_observations=None):
    original = (sheet.get("raw_extraction") or {}).get("rows") or []
    current = (sheet.get("sheet_data") or {}).get("rows") or []
    if not detected or len(original) != len(current):
        raise ValueError("A estrutura mudou; é necessária revisão da zona em falta.")
    filled = [(i+1, r) for i, r in enumerate(detected["rows"]) if r["filled"]]
    missing = len(filled) - len(current)
    if missing <= 0:
        raise ValueError("Não existe uma omissão comprovada pela grelha.")
    # Six-row windows, two anchor rows of overlap. Stop as soon as every
    # proved omission is bracketed; never replace already transcribed rows.
    with Image.open(image) as source, tempfile.TemporaryDirectory(prefix="kanban-strip-") as directory:
        source = ImageOps.exif_transpose(source).convert("RGB").rotate(
            detected["deskew_angle"], resample=Image.Resampling.BICUBIC, fillcolor="white")
        header = source.crop((0, 0, source.width, detected["header_bottom"]))
        found, anchor_reads = {}, {}
        for end in range(len(filled), 0, -4):
            window = filled[max(0, end-6):end]
            strip = source.crop((0, window[0][1]["top"], source.width, window[-1][1]["bottom"]))
            page = Image.new("RGB", (source.width, header.height+strip.height), "white")
            page.paste(header, (0, 0)); page.paste(strip, (0, header.height))
            path = Path(directory) / f"strip-{end}.png"
            page.save(path)
            from .ocr.provider import extract_checked
            def validate(reading):
                candidates = reading.get("rows") or []
                if len(candidates) != len(window):
                    raise ValueError("A leitura localizada não coincide com as linhas da grelha.")
                align_strip(candidates, [p for p, _ in window], original, current)
            candidates = extract_checked(provider, path, template, validate).get("rows") or []
            additions = align_strip(candidates, [p for p, _ in window], original, current, anchor_reads)
            for previous, row in additions:
                position = row["_paper_position"]
                if position in found and found[position] != (previous, row):
                    raise ValueError("As leituras localizadas discordam.")
                found[position] = (previous, row)
            if len(found) == missing:
                break
        if len(found) != missing:
            raise ValueError("A omissão não ficou delimitada por duas linhas conhecidas.")
    # Check all anchor/physical offsets implied by the insertion, not just count.
    positions = {}
    additions_before = {}
    for position, (previous, row) in sorted(found.items()):
        additions_before[previous+1] = additions_before.get(previous+1, 0) + 1
    ordered_positions = [p for p, _ in filled if p not in found]
    for i, position in enumerate(ordered_positions):
        positions[i] = position
    for position, (previous, row) in found.items():
        if not positions[previous] < position < positions[previous+1]:
            raise ValueError("A posição física não confirma o alinhamento da leitura.")
    observations = {str(len(current)+j): row for j, (_, row) in enumerate(
        value for _, value in sorted(found.items()))}
    if anchor_observations is not None:
        anchor_observations.update(anchor_reads)
    return observations, positions
