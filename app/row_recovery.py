"""Align a narrow OCR strip with physical positions, preserving stable row IDs."""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from PIL import Image, ImageOps
from .matching.geometry import canonical_code
from .matching.similarity import parse_number


def key(row):
    return canonical_code(row.get("modelo")), parse_number(row.get("qtd"))


def align_strip(candidates, positions, original, current):
    """Only interior omissions between consecutive, unique existing anchors."""
    anchors = []
    for offset, row in enumerate(candidates):
        model, qty = key(row)
        if not model or qty is None:
            raise ValueError("A zona relida contém uma referência ou quantidade ambígua.")
        hits = [i for i in range(len(original)) if key(row) in {key(original[i]), key(current[i])}]
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
                    if not row.get(field):
                        row[field] = source.get(field)
                row["_paper_position"] = positions[n]
                additions.append((left[1], row))
    return additions


def recover_missing(provider, image, template, sheet, detected):
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
        found = {}
        for end in range(len(filled), 0, -4):
            window = filled[max(0, end-6):end]
            strip = source.crop((0, window[0][1]["top"], source.width, window[-1][1]["bottom"]))
            page = Image.new("RGB", (source.width, header.height+strip.height), "white")
            page.paste(header, (0, 0)); page.paste(strip, (0, header.height))
            path = Path(directory) / f"strip-{end}.png"
            page.save(path)
            candidates = provider.extract(path, template).get("rows") or []
            if len(candidates) != len(window):
                raise ValueError("A leitura localizada não coincide com as linhas da grelha.")
            additions = align_strip(candidates, [p for p, _ in window], original, current)
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
    return observations, positions
