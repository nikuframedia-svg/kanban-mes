"""TPL102 geometry: both sides are legs and the final dimension is thickness."""
from dataclasses import replace
from functools import lru_cache
from .geometry import Profile, parse_profile as parse_literal


@lru_cache(maxsize=16384)
def parse_profile(value) -> Profile:
    profile = parse_literal(value)
    if profile.family in ("", "L") and len(profile.dimensions) in (2, 3):
        dimensions = profile.dimensions
        if len(dimensions) == 2:
            dimensions = (dimensions[0], dimensions[0], dimensions[1])
        return replace(profile, family="L", dimensions=dimensions)
    return profile


def profile_key(value) -> str:
    return parse_profile(value).key


def profile_interpretations(value):
    # TPL102 has no cut-length column. Splitting off thickness would erase
    # precisely the distinction between L60X60X4 and L60X60X5.
    return ((parse_profile(value), None),)
