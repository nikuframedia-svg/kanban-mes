"""Orientação das folhas digitalizadas.

O impresso TPL102 é landscape mas chega-nos em portrait: o PDF do scanner é
página A4 vertical com a folha deitada lá dentro. Os cabeçalhos das colunas
correm na vertical pela margem direita.

Isto não é um problema estético. A imagem deitada vai para o OCR e o modelo
troca colunas — mediu-se uma folha em que as quantidades gravadas vieram das
marcas da coluna ao lado. Por isso o render corrigido serve tanto a página
como o OCR: é a imagem canónica da folha, e o ficheiro original nunca é tocado
(é ele o hash de auditoria, e há folhas frente/verso que partilham o mesmo
ficheiro).
"""

from __future__ import annotations

import os
from pathlib import Path

_PORTRAIT_AUTO_TURN = 90  # graus no sentido anti-horário


def auto_turn_for(width: int, height: int) -> int:
    """Correcção automática, em graus anti-horários.

    Retrato ⇒ a folha está deitada ⇒ roda 90° para a esquerda: a margem
    direita (onde estão os cabeçalhos) sobe para o topo. Verificado contra as
    20 digitalizações reais, todas 1191×1685.
    """
    return _PORTRAIT_AUTO_TURN if height > width else 0


def cache_path_for(image_path: Path, total_turn: int) -> Path:
    """Nome do render, com a rotação TOTAL no nome.

    O total (e não o pedido do humano) é o que determina o conteúdo, portanto
    é o que tem de estar no nome — senão dois ficheiros com o mesmo sufixo
    podiam ter conteúdos diferentes conforme a orientação da origem.
    """
    return image_path.with_name(f"{image_path.stem}.rot{total_turn}.png")


def render_oriented(image_path: Path, rotation_override: int = 0) -> Path:
    """Devolve o caminho da folha na orientação de leitura.

    Sem rotação a fazer, devolve o próprio original — não vale a pena
    re-codificar uma imagem para lhe dar 0°. O render fica em cache ao lado do
    original e é regenerado se o original for mais recente.

    PNG e não JPEG: esta imagem alimenta o OCR, e a rotação em múltiplos de 90°
    é exacta. Comprimir com perdas traços finos de caneta seria trocar precisão
    de leitura por uns quilobytes.
    """
    from PIL import Image, ImageOps

    if not image_path.is_file():
        return image_path

    with Image.open(image_path) as probe:
        probe = ImageOps.exif_transpose(probe) or probe
        auto = auto_turn_for(probe.width, probe.height)

    # O pedido do humano é em quartos de volta CW; internamente contamos CCW.
    total = (auto - int(rotation_override or 0)) % 360
    if total == 0:
        return image_path

    cache = cache_path_for(image_path, total)
    src_mtime = image_path.stat().st_mtime
    if cache.exists() and cache.stat().st_mtime >= src_mtime:
        return cache

    # Escrita atómica: a página web e o worker de OCR podem renderizar o mesmo
    # ficheiro ao mesmo tempo, e escrever direto no destino servia PNG truncado
    # a quem lesse a meio. O replace é atómico no mesmo filesystem.
    tmp = cache.with_name(cache.name + ".tmp")
    with Image.open(image_path) as im:
        im = ImageOps.exif_transpose(im) or im
        im = im.rotate(total, expand=True)
        im.save(tmp, "PNG", optimize=True)
    # Carimba o render com o mtime da origem: assim «cache mais velho que o
    # original» só é verdade quando o original mudar mesmo.
    os.utime(tmp, (src_mtime, src_mtime))
    os.replace(tmp, cache)
    return cache


def ink_fraction(image_path: Path) -> float:
    """Fração de píxeis com tinta, para detetar páginas em branco SEM gastar OCR.

    Um scanner alimentado com folhas de um só lado produz versos em branco no
    meio do PDF — e um LLM posto a transcrever uma página vazia inventa uma
    folha inteira plausível (aconteceu: «ALUPLAST», OF 1000000000). Detetar o
    vazio é um problema de contar píxeis, não de modelo.

    Método: greyscale, reduzir (barato e suficiente), papel = mediana da
    luminância, tinta = píxeis bem mais escuros que o papel. Medido nas
    digitalizações reais: páginas em branco ≈ 0.0000–0.0001; a folha mais rala
    que temos (gerada digitalmente) ≈ 0.002; scans manuscritos ≈ 0.14.
    """
    from PIL import Image, ImageOps
    import statistics

    with Image.open(image_path) as im:
        im = ImageOps.exif_transpose(im) or im
        im = im.convert("L")
        im.thumbnail((1000, 1000))
        # getdata está deprecado desde o Pillow 11; o sucessor nem sempre existe
        getter = getattr(im, "get_flattened_data", im.getdata)
        pixels = list(getter())
    if not pixels:
        return 0.0
    paper = statistics.median(pixels)
    dark = sum(1 for v in pixels if v < paper - 60)
    return dark / len(pixels)


def is_blank_page(image_path: Path, threshold: float) -> bool:
    """A página não tem conteúdo que valha um OCR? Em erro de leitura devolve
    False — na dúvida, deixa-se o OCR tentar (comportamento antigo)."""
    try:
        return ink_fraction(image_path) < threshold
    except OSError:
        return False


def clear_renders(image_path: Path) -> None:
    """Apaga os renders derivados de uma imagem (não a imagem) — incluindo
    `.tmp` de escritas interrompidas."""
    for path in image_path.parent.glob(f"{image_path.stem}.rot*"):
        try:
            path.unlink()
        except OSError:
            pass
