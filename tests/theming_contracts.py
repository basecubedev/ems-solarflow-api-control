# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the theming contracts of every surface are measured with.

Admin, the Appliance Manager and the EMS Dashboard are separate deployables
with separate stylesheets, so each needs its own contracts -- but the questions
are the same ones: does every palette redefine the whole set, does any rule
still carry a colour of its own, does every corner read a role. Only the
answers differ, and the exceptions each surface is entitled to.

This module holds the measuring, not the expectations. A surface's test module
says what it expects; nothing here asserts anything about a particular
stylesheet.
"""

import re

# Colour and shape are independent axes: a palette says what things are made of,
# an object style says what shape they are. Keeping them apart is what makes
# "void palette, square corners" possible at all, so shape tokens carry their own
# prefix and no theme is allowed to set one.
#
# The prefix is the demo's (`--o-`, for object), so the object styles worked out
# there transfer without renaming anything later.
SHAPE_PREFIX = "--o-"

# Rec. 709 luma; a ground this dark cannot be mistaken for a light theme.
DARK_MAX_LUMA = 90

# WCAG 2.1: 4.5:1 is the floor for normal text, 7:1 is the enhanced level. Body
# copy is held to the enhanced one because every palette already clears it by a
# wide margin; muted copy is held to the floor, which is what it is for.
TEXT_MIN_CONTRAST = 7.0
MUTED_MIN_CONTRAST = 4.5

# A veil of plain white or black lifts or sinks a surface by the same amount on
# any dark ground, so it survives a palette swap. Anything else is a hue chosen
# against one background that will be wrong on the next.
#
# Only half of that reasoning held up. Black still sinks a surface toward every
# palette's own ground, because every palette's ground is dark. White lifts it
# toward a colour no palette has: measured on the cockpit in copper, the films
# were grey where the whole page was warm. So white is no longer a way out --
# see `white_films` -- and a rule that wants to lift something reads `--veil`,
# which is the palette's own light.
NEUTRAL = re.compile(r"rgba?\(\s*(?:255,\s*255,\s*255|0,\s*0,\s*0)\b|#000\b|#fff\b", re.I)
PLAIN_WHITE = re.compile(r"rgba?\(\s*255\s*,\s*255\s*,\s*255\b|#fff(?:fff)?\b", re.I)
COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)")

# Density is the third axis, and unlike the other two it is a single number:
# the spacings were tuned against each other, so a density that redefined each
# one separately would be free to break that relationship. Multiplying them all
# keeps it. The scalar is unitless, which is what lets `calc(8px * var(--d))`
# stay a length.
DENSITY_SCALAR = "--d"

# All three axes hang off the root element, so all three kinds of block are
# "not a rule": `:root[data-theme=…]` declares a palette, `:root[data-style=…]`
# an object style and `:root[data-density=…]` a density, and none of them is a
# place where a page decides how something looks.
ROOT_BLOCK = r':root(\[data-(?:theme|style|density)="[a-z0-9-]+"\])?\s*\{.*?\}'

# What a spacing is measured in. Three kinds never scale, and each for its own
# reason: zero is already nothing, `auto` is a centring instruction rather than
# a distance, and a negative margin is a hairline or hanging-indent trick whose
# whole point is that it matches a border or an icon column exactly.
SPACING_PROPERTIES = (
    "padding", "padding-top", "padding-bottom", "padding-left", "padding-right",
    "gap", "row-gap", "column-gap",
    "margin", "margin-top", "margin-bottom", "margin-left", "margin-right",
)


def declarations(block):
    return {
        name: " ".join(value.split())
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)
    }


def base_tokens(css):
    block = re.search(r":root\s*\{(.*?)\}", css, re.S)
    assert block, "the stylesheet has no :root block"
    return declarations(block.group(1))


def themes(css):
    return {
        name: declarations(body)
        for name, body in re.findall(r':root\[data-theme="([a-z0-9-]+)"\]\s*\{(.*?)\}', css, re.S)
    }


def densities(css):
    return {
        name: declarations(block)
        for name, block in re.findall(
            r':root\[data-density="([a-z0-9-]+)"\]\s*\{(.*?)\}', css, re.S
        )
    }


def object_styles(css):
    return {
        name: declarations(block)
        for name, block in re.findall(r':root\[data-style="([a-z0-9-]+)"\]\s*\{(.*?)\}', css, re.S)
    }


def radius_tokens(css):
    return {
        name: value
        for name, value in shape_tokens(css).items()
        if name.endswith("-radius")
    }


def body(css):
    """Everything the palettes do not define -- the rules that read them."""

    return re.sub(ROOT_BLOCK, "", css, flags=re.S)


def rules(css):
    return re.findall(r"([^{}]+)\{([^{}]*)\}", body(css))


def derived_tokens(css):
    """Tokens whose value is built from another token.

    `--surface-sunken` is `--bg` at half strength, so it follows every palette
    without being written twelve times. A theme that redefined one would break
    exactly the derivation that makes it work.
    """

    return {
        name: value
        for name, value in base_tokens(css).items()
        if "var(--" in value and not name.startswith(SHAPE_PREFIX) and name != DENSITY_SCALAR
    }


def palette_tokens(css):
    """What a theme redefines: colour, and nothing else.

    The other two axes are excluded by name rather than by shape -- shape
    tokens by their prefix, the density scalar because it is one token and a
    prefix for it would be a prefix over a single name.
    """

    return {
        name: value
        for name, value in base_tokens(css).items()
        if not name.startswith(SHAPE_PREFIX) and "var(--" not in value and name != DENSITY_SCALAR
    }


def shape_tokens(css):
    return {name: value for name, value in base_tokens(css).items() if name.startswith(SHAPE_PREFIX)}


def hued_literals(css):
    found = []
    for selector, block in rules(css):
        for declaration in block.split(";"):
            if ":" not in declaration:
                continue
            _, _, value = declaration.partition(":")
            for literal in COLOUR.findall(value):
                if not NEUTRAL.search(literal):
                    found.append((" ".join(selector.split())[:44], literal.strip()))
    return found


def white_films(css):
    """Every place a rule still paints plain white on a themed surface.

    Returns `(selector, property, literal)`. A palette block is not a rule, so
    a palette naming `#ffffff` as its own `--text` -- which `contrast` does, on
    purpose -- is not counted here.
    """

    found = []
    for selector, block in rules(css):
        for declaration in block.split(";"):
            if ":" not in declaration:
                continue
            prop, _, value = declaration.partition(":")
            for literal in COLOUR.findall(value):
                if PLAIN_WHITE.search(literal):
                    found.append((" ".join(selector.split())[:44], prop.strip(), literal.strip()))
    return found


def spacings(css):
    """Every distance a rule puts around or between things.

    Returns `(selector, property, value)` for the declarations that could carry
    a density and do not already read a token -- the ones above are excluded
    because they are not distances, not because they were overlooked.
    """

    found = []
    for selector, block in rules(re.sub(r"/\*.*?\*/", "", css, flags=re.S)):
        for prop, value in re.findall(r"(?<![a-z-])([a-z-]+)\s*:\s*([^;}]+)", block):
            if prop not in SPACING_PROPERTIES:
                continue
            value = " ".join(value.split())
            if "auto" in value or re.search(r"-\d", value):
                continue
            if not re.search(r"\d*\.?\d+(?:px|r?em|ch|v[wh]|%|pt)", value):
                continue
            found.append((" ".join(selector.split())[:44], prop, value))
    return found


def corners(css):
    found = []
    for selector, block in rules(css):
        for value in re.findall(r"(?<![a-z-])border-radius:\s*([^;}]+)", block):
            found.append((" ".join(selector.split())[:44], " ".join(value.split())))
    return found


def luma(value):
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", value.strip())
    assert match, f"a theme background must be a plain hex colour, got {value!r}"
    red, green, blue = (int(match.group(1)[index : index + 2], 16) for index in (0, 2, 4))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def channels(value):
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", value.strip())
    assert match, f"expected a plain hex colour, got {value!r}"
    return tuple(int(match.group(1)[index : index + 2], 16) for index in (0, 2, 4))


def relative_luminance(rgb):
    def linear(channel):
        ratio = channel / 255
        return ratio / 12.92 if ratio <= 0.03928 else ((ratio + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground, background):
    lighter = max(relative_luminance(foreground), relative_luminance(background))
    darker = min(relative_luminance(foreground), relative_luminance(background))
    return (lighter + 0.05) / (darker + 0.05)
