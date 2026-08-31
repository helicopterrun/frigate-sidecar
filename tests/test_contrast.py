"""WCAG AA contrast: text tokens must clear 4.5:1 against their backgrounds."""
from __future__ import annotations

import re
from pathlib import Path

CSS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "frigate_sidecar" / "static" / "css" / "triage.css"
)

# WCAG 2.1 relative-luminance helpers
def _linearize(c: int) -> float:
    s = c / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)

def _contrast(fg: str, bg: str) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)

# Pairs: (text-token, background-token, min-ratio)
_PAIRS = [
    ("--muted-3", "--surface", 4.5),
    ("--muted-3", "--surface-2", 4.5),
    ("--text", "--surface", 4.5),
    ("--muted", "--surface", 4.5),
]

def _parse_themes(css: str) -> dict[str, dict[str, str]]:
    """Extract CSS custom properties per theme block."""
    themes: dict[str, dict[str, str]] = {}
    # Default :root block
    token_re = r'(--[\w-]+)\s*:\s*(#[0-9A-Fa-f]{6})'
    root_match = re.search(r':root\s*\{([^}]+)\}', css)
    if root_match:
        themes["default"] = dict(re.findall(token_re, root_match.group(1)))
    # Named themes
    for m in re.finditer(r'\[data-theme="([\w-]+)"\]\s*\{([^}]+)\}', css):
        themes[m.group(1)] = dict(re.findall(token_re, m.group(2)))
    return themes

def test_text_contrast_meets_aa() -> None:
    css = CSS_PATH.read_text()
    themes = _parse_themes(css)
    default_tokens = themes.get("default", {})
    errors: list[str] = []
    for name, tokens in themes.items():
        # Named themes inherit from default for any missing token
        merged = {**default_tokens, **tokens} if name != "default" else tokens
        for fg_name, bg_name, min_ratio in _PAIRS:
            fg = merged.get(fg_name)
            bg = merged.get(bg_name)
            if not fg or not bg:
                continue
            ratio = _contrast(fg, bg)
            if ratio < min_ratio:
                errors.append(
                    f"{name}: {fg_name} ({fg}) on {bg_name} ({bg}) = {ratio:.2f}:1 < {min_ratio}"
                )
    assert not errors, "WCAG AA contrast failures:\n" + "\n".join(errors)
