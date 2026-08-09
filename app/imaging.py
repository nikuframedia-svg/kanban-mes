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

    with Image.open(image_path) as im:
        im = ImageOps.exif_transpose(im) or im
        im = im.rotate(total, expand=True)
        im.save(cache, "PNG", optimize=True)
    # Carimba o render com o mtime da origem: assim «cache mais velho que o
    # original» só é verdade quando o original mudar mesmo.
    os.utime(cache, (src_mtime, src_mtime))
    return cache


def clear_renders(image_path: Path) -> None:
    """Apaga os renders derivados de uma imagem (não a imagem)."""
    for path in image_path.parent.glob(f"{image_path.stem}.rot*.png"):
        try:
            path.unlink()
        except OSError:
            pass
