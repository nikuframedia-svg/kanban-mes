"""Typed, decimal-preserving evidence for the Cross V3 matcher.

Parsing never edits a sheet. In particular, a possible length at the end of a
profile is an alternative interpretation to compare with a plan reference.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache


_NUMBER = re.compile(r"^[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)$")
_THOUSANDS_DOT = re.compile(r"^[+-]?[1-9]\d{0,2}(?:\.\d{3})+$")
_THOUSANDS_COMMA = re.compile(r"^[+-]?[1-9]\d{0,2}(?:,\d{3})+$")


def parse_decimal(value: object) -> Decimal | None:
    """Parse a complete numeric value; never turn ``1+1`` into ``11``.

    Native Decimal values do not pass through a locale heuristic. Strings
    follow the Portuguese sheet convention: ``1.200`` is a grouped integer,
    while comma is the decimal separator. Mixed separators are accepted only
    when the grouping is valid.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return Decimal(str(value))
    text = str(value).strip().replace("\N{NO-BREAK SPACE}", " ")
    if not text:
        return None
    if " " in text:
        # Accept actual groups of thousands, not arbitrary OCR whitespace.
        if not re.fullmatch(r"[+-]?[1-9]\d{0,2}(?: \d{3})+(?:[.,]\d+)?", text):
            return None
        text = text.replace(" ", "")
    if "." in text and "," in text:
        decimal_sep = "," if text.rindex(",") > text.rindex(".") else "."
        group_sep = "." if decimal_sep == "," else ","
        whole, fraction = text.rsplit(decimal_sep, 1)
        grouping = _THOUSANDS_DOT if group_sep == "." else _THOUSANDS_COMMA
        if not fraction.isdigit() or not grouping.fullmatch(whole):
            return None
        text = whole.replace(group_sep, "") + "." + fraction
    elif _THOUSANDS_DOT.fullmatch(text):
        text = text.replace(".", "")
    elif _NUMBER.fullmatch(text):
        text = text.replace(",", ".")
    else:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def decimal_text(value: Decimal | object) -> str:
    number = value if isinstance(value, Decimal) else parse_decimal(value)
    if number is None:
        return ""
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "+0") else text


def _ascii(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).upper()
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def canonical_code(value: object, prefix: str = "") -> str:
    """Ignore code decoration, but keep O and zero as different evidence."""
    text = re.sub(r"[^A-Z0-9]", "", _ascii(value))
    pfx = re.sub(r"[^A-Z0-9]", "", _ascii(prefix))
    if pfx and text.startswith(pfx):
        text = text[len(pfx):]
    return text


_FAMILIES = (
    ("TUBORECTANGULAR", "RECT"), ("TUBORETANGULAR", "RECT"),
    ("TUBOQUADRADO", "SQUARE"), ("TUBOREDONDO", "ROUND"),
    ("CANTONEIRA", "L"), ("CANTON.", "L"), ("CANT.", "L"), ("CALHA", "CALHA"),
    ("DIAMETRO", "ROUND"), ("DIAM", "ROUND"),
    ("PERFILUPN", "UPN"), ("PERFILUPE", "UPE"),
    ("PERFILHEA", "HEA"), ("PERFILHEB", "HEB"),
    ("PERFILIPE", "IPE"), ("PERFILIPN", "IPN"),
    ("UPN", "UPN"), ("UPE", "UPE"), ("HEA", "HEA"),
    ("HEB", "HEB"), ("HEM", "HEM"), ("IPE", "IPE"),
    ("IPN", "IPN"), ("PFC", "PFC"), ("UB", "UB"), ("UC", "UC"),
    ("CHAPA", "CHAPA"), ("BARRA", "BARRA"),
    ("TUBO", ""), ("L", "L"), ("U", "U"), ("C", "C"),
    ("T", "T"), ("W", "W"), ("M", "M"),
)


@dataclass(frozen=True)
class Profile:
    family: str
    dimensions: tuple[Decimal, ...]
    literal: str
    ocr_cost_bits: float = 0.0
    material: str = ""

    @property
    def key(self) -> str:
        if not self.dimensions:
            return self.literal
        prefix = "" if self.family in ("", "ROUND") else self.family
        return prefix + "X".join(decimal_text(d) for d in self.dimensions)


@lru_cache(maxsize=16384)
def _parse_profile(text: str) -> Profile:
    literal = re.sub(r"\s+", "", _ascii(text)).replace("×", "X").replace("*", "X")
    literal = literal.replace(",", ".").replace("⌀", "Ø").replace("Φ", "Ø")
    literal = re.sub(r"MM$", "", literal)
    rest, family, ocr_cost, material = literal, "", 0.0, ""
    # The material qualifier is separate from both section dimensions and
    # possible cut length. Retain it in the literal and every interpretation.
    suffix = re.search(r"(?<=\d)(INOX(?:IDAVEL)?)$", rest)
    if suffix:
        material = "INOX"
        rest = rest[:suffix.start()]
    # On the physical TUBO column, OCR reads the diameter symbol as Q.
    # Keep the interpretation cost: it is not an exact textual agreement.
    if re.fullmatch(r"Q\d+(?:\.\d+)?(?:X\d+(?:\.\d+)?)+", rest):
        rest, family, ocr_cost = rest[1:], "ROUND", 2.0
    for prefix, kind in _FAMILIES:
        if rest.startswith(prefix):
            tail = rest[len(prefix):]
            if tail and (tail[0].isdigit() or tail[0] in "Ø.:"):
                rest, family = tail.lstrip(":"), kind
                break
    if rest.startswith("Ø"):
        family = family or "ROUND"
        rest = rest.lstrip("Ø")
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)(?:X(?:\d+(?:\.\d+)?|\.\d+))*", rest):
        return Profile(family, (), literal, ocr_cost, material)
    try:
        dimensions = tuple(Decimal(part) for part in rest.split("X"))
    except InvalidOperation:
        return Profile(family, (), literal, ocr_cost, material)
    if any(d <= 0 or not d.is_finite() for d in dimensions):
        return Profile(family, (), literal, ocr_cost, material)
    return Profile(family, dimensions, literal, ocr_cost, material)


def parse_profile(value: object) -> Profile:
    """Profile dimensions always use decimal dots, including ``2.650``."""
    return _parse_profile(str(value or ""))


def profile_key(value: object) -> str:
    return parse_profile(value).key


def family_for_material(value: object) -> str:
    text = canonical_code(value)
    for prefix, kind in _FAMILIES:
        if text == canonical_code(prefix) and kind:
            return kind
    return ""


def material_key(value: object) -> str:
    """Recognized chemical material only; geometric families are unknown."""
    return {
        "INOX": "INOX", "INOXIDAVEL": "INOX", "ACOINOX": "INOX",
        "ACOINOXIDAVEL": "INOX", "ACOCARBONO": "CARBON_STEEL",
    }.get(canonical_code(value), "")


def profile_interpretations(value: object) -> tuple[tuple[Profile, Decimal | None], ...]:
    """Literal profile first, then a possible appended piece length.

    There is intentionally no 250 mm threshold: ``Ø12x230`` is a real short
    cut. A split must win against the plan, not against a formatting heuristic.
    """
    profile = parse_profile(value)
    readings: list[tuple[Profile, Decimal | None]] = [(profile, None)]
    if len(profile.dimensions) > 1:
        head = Profile(profile.family, profile.dimensions[:-1], profile.literal,
                       profile.ocr_cost_bits, profile.material)
        readings.append((head, profile.dimensions[-1]))
    return tuple(readings)
