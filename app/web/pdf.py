"""Geração de PDFs (fpdf2) — folha kanban individual e relatório de estado.

Usa uma fonte TrueType Unicode instalada com a aplicação ou com o sistema.
Quando a máquina não tem nenhuma fonte compatível, o fallback Helvetica é
tolerante: preserva PT-PT/Windows-1252 e substitui apenas glifos impossíveis,
em vez de deixar o endpoint responder 500.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from fpdf import FPDF

from ..templates_spec import KanbanTemplate

_LOCAL_FONT_DIR = Path(__file__).resolve().parent / "fonts"

# Pares comuns nos três sistemas suportados. O nome da família registada no
# fpdf é nosso, por isso não precisamos de inferir os metadados do TTF.
_FONT_FILE_PAIRS = (
    ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf"),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf"),
    ("FreeSans.ttf", "FreeSansBold.ttf"),
    ("arial.ttf", "arialbd.ttf"),
    ("segoeui.ttf", "segoeuib.ttf"),
    ("calibri.ttf", "calibrib.ttf"),
    ("aptos.ttf", "aptos-bold.ttf"),
)


class _PortableFPDF(FPDF):
    """FPDF que nunca falha apenas por não existir um glifo no fallback.

    Fontes TrueType continuam a receber o Unicode original. A substituição
    só acontece no último recurso, quando se usa uma das fontes base do PDF.
    """

    def normalize_text(self, text: str) -> str:
        if self.current_font is not None and not self.is_ttf_font:
            encoding = self.core_fonts_encoding or "cp1252"
            return text.encode(encoding, errors="replace").decode("latin-1")
        return text


def _font_directories() -> Iterator[Path]:
    """Pastas de fontes por prioridade, sem depender do sistema operativo."""
    configured = os.getenv("MES_PDF_FONT_DIR")
    windir = os.getenv("WINDIR")
    local_app_data = os.getenv("LOCALAPPDATA")
    candidates = [
        Path(configured).expanduser() if configured else None,
        _LOCAL_FONT_DIR,
        Path(windir) / "Fonts" if windir else None,
        Path(local_app_data) / "Microsoft" / "Windows" / "Fonts"
        if local_app_data else None,
        Path("/usr/share/fonts/truetype/liberation"),
        Path("/usr/share/fonts/truetype/liberation2"),
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/truetype/freefont"),
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
        Path.home() / "Library" / "Fonts",
    ]
    seen: set[str] = set()
    for directory in candidates:
        if directory is None:
            continue
        key = os.path.normcase(os.path.abspath(str(directory)))
        if key not in seen:
            seen.add(key)
            yield directory


def _font_candidates() -> Iterator[tuple[Path, Path]]:
    """Pares regular/negrito existentes, incluindo overrides diretos."""
    configured_regular = os.getenv("MES_PDF_FONT_REGULAR")
    configured_bold = os.getenv("MES_PDF_FONT_BOLD")
    if configured_regular and configured_bold:
        regular = Path(configured_regular).expanduser()
        bold = Path(configured_bold).expanduser()
        if regular.is_file() and bold.is_file():
            yield regular, bold

    for directory in _font_directories():
        for regular_name, bold_name in _FONT_FILE_PAIRS:
            regular = directory / regular_name
            bold = directory / bold_name
            if regular.is_file() and bold.is_file():
                yield regular, bold


_INK = (21, 17, 11)
_MUTED = (107, 99, 87)
_ACCENT = (182, 73, 43)          # terracota (tema claro do design system)
_LINE = (216, 210, 198)
_HEAD_BG = (236, 238, 241)


def _make_pdf(orientation: str) -> tuple[FPDF, str]:
    """Devolve ``(pdf, family)`` com a primeira fonte Unicode utilizável."""
    pdf = _PortableFPDF(orientation=orientation, unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    for index, (regular, bold) in enumerate(_font_candidates()):
        family = f"KanbanUnicode{index}"
        try:
            pdf.add_font(family, "", str(regular))
            pdf.add_font(family, "B", str(bold))
        except Exception:
            # Uma fonte instalada pode estar corrompida ou inacessível. Tenta
            # a seguinte antes de recorrer à fonte base do PDF.
            continue
        return pdf, family

    # O em dash, aspas curvas e o euro existem em cp1252 (não em latin-1,
    # default do fpdf). _PortableFPDF substitui de forma segura o que exceder.
    pdf.core_fonts_encoding = "cp1252"
    return pdf, "helvetica"


def _brand_header(pdf: FPDF, family: str, title: str, subtitle: str) -> None:
    pdf.set_text_color(*_ACCENT)
    pdf.set_font(family, "B", 9)
    pdf.cell(0, 5, "KANBAN MES · METALOGALVA", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_INK)
    pdf.set_font(family, "B", 16)
    pdf.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 9)
    pdf.set_text_color(*_MUTED)
    pdf.cell(0, 5, subtitle, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_INK)
    pdf.ln(3)


def _table(pdf: FPDF, family: str, headers: list[str], rows: list[list[str]],
           widths: list[float]) -> None:
    pdf.set_font(family, "B", 7.5)
    pdf.set_fill_color(*_HEAD_BG)
    pdf.set_draw_color(*_LINE)
    for h, w in zip(headers, widths):
        pdf.cell(w, 6, h.upper(), border=1, fill=True)
    pdf.ln()
    pdf.set_font(family, "", 8)
    for row in rows:
        # quebra de página manual mantém o cabeçalho legível
        if pdf.get_y() > pdf.h - 24:
            pdf.add_page()
            pdf.set_font(family, "B", 7.5)
            for h, w in zip(headers, widths):
                pdf.cell(w, 6, h.upper(), border=1, fill=True)
            pdf.ln()
            pdf.set_font(family, "", 8)
        for value, w in zip(row, widths):
            text = "" if value is None else str(value)
            if len(text) > 40:
                text = text[:38] + "…"
            pdf.cell(w, 5.5, text, border=1)
        pdf.ln()


def sheet_pdf(sheet: dict, template: KanbanTemplate, edit_count: int) -> bytes:
    """Folha kanban em A4 retrato — espelho fiel do que está no ecrã."""
    data = sheet.get("sheet_data") or {}
    header = data.get("header") or {}
    rows = [row for _, row in sorted(enumerate(data.get("rows") or []), key=lambda item: item[1].get("_display_order", item[1].get("_paper_position", item[0]+1)))]
    footer = data.get("footer") or {}
    labels = template.field_labels or {}
    validated = sheet.get("status") == "validated"

    pdf, family = _make_pdf("portrait")
    pdf.add_page()
    # «operador» é o registo interno por omissão, não uma entidade — não se
    # imprime; um nome escrito de propósito (dados antigos) continua a sair.
    quem = sheet.get("validated_by")
    quem_s = f" · {quem}" if quem and quem != "operador" else ""
    estado = (
        f"VALIDADA{quem_s} · {sheet.get('validated_at')}"
        if validated else "RASCUNHO — ainda não validada"
    )
    public_no = sheet.get("sheet_no") or sheet.get("uid", "")[:8]
    _brand_header(pdf, family, template.label, f"Folha {public_no} · {estado}")

    # cabeçalho da folha
    pdf.set_font(family, "", 9)
    for f in template.header_fields:
        value = header.get(f)
        pdf.set_font(family, "B", 9)
        pdf.cell(38, 6, (labels.get(f) or f.replace("_", " ").capitalize()) + ":")
        pdf.set_font(family, "", 9)
        pdf.cell(0, 6, "" if value is None else str(value), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # linhas
    filled = [
        r for r in rows
        if r.get("_deleted") is not True
        and any(not str(k).startswith("_")
                and v is not None and str(v).strip() for k, v in r.items())
    ]
    headers = ["#"] + [labels.get(f, f) for f in template.row_fields]
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    widths = [8.0] + [(usable - 8.0) / len(template.row_fields)] * len(template.row_fields)
    body = [
        [str(i + 1)] + [r.get(f) for f in template.row_fields]
        for i, r in enumerate(filled)
    ]
    _table(pdf, family, headers, body or [["—"] + [""] * len(template.row_fields)], widths)
    pdf.ln(3)

    # rodapé da folha
    for f in template.footer_fields:
        value = footer.get(f)
        pdf.set_font(family, "B", 9)
        pdf.cell(48, 6, (labels.get(f) or f.replace("_", " ").capitalize()) + ":")
        pdf.set_font(family, "", 9)
        pdf.cell(0, 6, "" if value is None else str(value), new_x="LMARGIN", new_y="NEXT")

    # trilho de auditoria
    pdf.ln(4)
    pdf.set_font(family, "", 7)
    pdf.set_text_color(*_MUTED)
    sha = (sheet.get("image_sha256") or "")[:16]
    pdf.cell(0, 4,
             f"folha {public_no} · uid {sheet.get('uid', '')} · foto sha256 {sha or '—'} · "
             f"{edit_count} célula(s) corrigidas por humanos · gerado pelo Kanban MES",
             new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def estado_pdf(rows: list[dict], q: str, familia: str, kpis: dict) -> bytes:
    """Relatório de estado em A4 paisagem — a vista filtrada da página Estado."""
    pdf, family = _make_pdf("landscape")
    pdf.add_page()
    filtros = []
    if q:
        filtros.append(f"pesquisa «{q}»")
    if familia:
        filtros.append(f"família {familia}")
    sub = (
        f"{len(rows)} OFs · {kpis.get('folhas', 0)} folhas validadas · "
        f"{kpis.get('registos', 0)} linhas de produção"
        + (f" · filtros: {', '.join(filtros)}" if filtros else "")
    )
    _brand_header(pdf, family, "Estado da produção — plano vs validado", sub)

    headers = ["Cliente", "OV", "OF", "Família", "Planeada", "Progresso",
               "Validada (MES)", "Última folha", "Máquina"]
    widths = [52.0, 24.0, 24.0, 22.0, 20.0, 20.0, 26.0, 24.0, 55.0]
    body = []
    for r in rows[:800]:
        prog = r.get("progresso")
        body.append([
            r.get("cliente"), r.get("ov"), r.get("of"), r.get("familia"),
            f"{r['qtd_planeada']:.0f}" if r.get("qtd_planeada") is not None else "—",
            f"{prog * 100:.0f}%" if prog is not None else "—",
            f"{r['qtd_validada']:.0f}" if r.get("qtd_validada") else "—",
            str(r.get("ultima_folha") or "—"),
            r.get("maquinas"),
        ])
    _table(pdf, family, headers, body or [["—"] * len(headers)], widths)
    return bytes(pdf.output())
