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

    `--tone-card` is `--bg` with a little of the palette's own light mixed in,
    so it follows every palette without being written twelve times. A theme
    that redefined one would break exactly the derivation that makes it work.
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


TONES = ("--tone-well", "--tone-card", "--tone-inner", "--tone-hover")

# What a palette is made of, as opposed to what it accents with. A fill built
# from one of these is a surface tone; a fill built from --output or --danger
# is a thing saying something about itself.
NEUTRAL_TOKENS = ("veil", "muted", "muted2", "bg", "bg2", "panel", "panel-strong", "surface-raised", "text")


def layers(value):
    """A background's layers, split on the commas that separate them.

    `background` takes a comma-separated list, and every layer in it may be a
    function with commas of its own -- `color-mix(in srgb, var(--veil) 4%,
    transparent)` has two. Splitting on every comma turns one layer into three.
    """

    parts, depth, current = [], 0, ""
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def surface_fills(css):
    """The bottom layer of every background a rule paints.

    Layers are drawn over one another and the last one is the base -- the one
    that decides what tone the surface is. A stage card is
    `radial-gradient(...), var(--tone-card)`: the radial is what the card says
    about itself, and --tone-card is what it is made of. Only the base is a
    surface tone, so only the base is what this returns.

    Yields `(selector, base)`; an object-style wrapper is unwrapped first, since
    `var(--o-fill, X)` means "X unless a treatment answers for it" and X is the
    fill this rule owns.
    """

    found = []
    for selector, block in rules(css):
        for declaration in block.split(";"):
            prop, _, value = declaration.partition(":")
            if prop.strip() not in ("background", "background-color", "background-image"):
                continue
            value = " ".join(value.split())
            if not value:
                continue
            base = layers(value)[-1]
            unwrapped = re.fullmatch(r"var\(--o-fill,\s*(.*)\)", base, re.S)
            if unwrapped:
                base = layers(unwrapped.group(1))[-1]
            # `rules` splits on braces, so a comment sitting above a rule
            # arrives as part of its selector. Strip it, or the selector this
            # reports is prose and no exception list can name it.
            name = re.sub(r"/\*.*?\*/", " ", selector, flags=re.S)
            found.append((" ".join(name.split())[:60], base))
    return found


def mix_share(value):
    """How much of the first colour a two-colour `color-mix` carries, as 0..1.

    `--tone-inner` is `color-mix(in srgb, var(--veil) 11%, var(--bg))`, and a
    test that wants to know how bright a tile is has to read the 11 rather than
    carry a copy of it -- a copy is exactly what stops it noticing the number
    changing.
    """

    found = re.search(r"color-mix\(in srgb,\s*var\(--[a-z0-9-]+\)\s*([\d.]+)%", value)
    if not found:
        raise ValueError(f"not a two-colour srgb mix: {value!r}")
    return float(found.group(1)) / 100


def invents_a_tone(base):
    """Whether a base layer mixes its own surface colour instead of naming one.

    A tone is flat. A gradient in the base position is a texture or a sheen --
    the grid of hairlines behind the page, the highlight running off a button's
    corner -- and those are drawings, not levels: they say nothing about how far
    from the ground a surface sits, which is the only thing a tone says.
    """

    if any(f"var({name})" in base for name in TONES):
        return False
    if re.match(r"(linear|radial|conic|repeating-[a-z]+)-gradient\(", base):
        # A sheen and a hairline grid fade out; that is what makes them
        # drawings. A gradient with no transparent stop is a fill that happens
        # to be written as two colours -- `linear-gradient(180deg,
        # var(--panel-strong), var(--panel))` was the card level of this whole
        # product, and it is a tone under a longer name.
        if "transparent" in base:
            return False
    return any(re.search(rf"var\(--{token}\)", base) for token in NEUTRAL_TOKENS)


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
