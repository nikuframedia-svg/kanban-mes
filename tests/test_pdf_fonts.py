"""Regressões da seleção de fontes dos PDFs em Windows e Linux."""

from app.web import pdf as pdf_gen


def test_pdf_sem_fontes_unicode_continua_valido(monkeypatch):
    """O antigo fallback Helvetica explodia no primeiro em dash no Windows."""
    monkeypatch.setattr(pdf_gen, "_font_candidates", lambda: iter(()))

    content = pdf_gen.estado_pdf(
        rows=[{
            "cliente": "José — सिंह 🚀",
            "ov": "OV-1",
            "of": "OF-1",
            "familia": "Cantoneiras",
            "qtd_planeada": 10,
            "qtd_validada": 5,
            "progresso": 0.5,
            "ultima_folha": "31/08/2026",
            "maquinas": "Rapid “20T”",
        }],
        q="produção — revisão €",
        familia="cantoneiras",
        kpis={"folhas": 1, "registos": 1},
    )

    assert content.startswith(b"%PDF-")
    assert len(content) > 500


def test_descobre_fontes_padrao_do_windows(tmp_path, monkeypatch):
    """A descoberta não depende dos diretórios Linux de Liberation."""
    windows = tmp_path / "Windows"
    fonts = windows / "Fonts"
    fonts.mkdir(parents=True)
    regular = fonts / "arial.ttf"
    bold = fonts / "arialbd.ttf"
    regular.touch()
    bold.touch()

    monkeypatch.setenv("WINDIR", str(windows))
    monkeypatch.delenv("MES_PDF_FONT_DIR", raising=False)
    monkeypatch.delenv("MES_PDF_FONT_REGULAR", raising=False)
    monkeypatch.delenv("MES_PDF_FONT_BOLD", raising=False)
    monkeypatch.setattr(pdf_gen, "_LOCAL_FONT_DIR", tmp_path / "sem-assets")

    assert next(pdf_gen._font_candidates()) == (regular, bold)
