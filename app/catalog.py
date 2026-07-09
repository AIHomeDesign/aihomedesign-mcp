"""Static catalog of every AI HomeDesign V3 tool, its input slots and widgets.

This is the source of truth the MCP exposes through `list_capabilities` and
`describe_tool`, and what the high-level convenience tools use to build the
`asset_map` / `widgets` payloads for `POST /v3/process`.

Slug convention (self-describing, from the official docs):
    widget slug : widget-<purpose>-<tool-slug>
    item slug   : item-<value>-<widget-slug>
"""

# Canonical, VERIFIED option values from aihomedesign.com (confirmed valid against
# the live API for the style/space widgets). The MCP constrains user input to these.
STYLES = ["prime", "modern", "farmhouse", "scandinavian", "hampton",
          "industrial", "traditional", "contemporary"]
ROOMS = ["bedroom", "living-room", "kitchen", "bathroom", "dining-room",
         "home-office", "outdoor", "nursery"]
AREAS = ["indoor", "outdoor"]

# Friendly (label, one-line description) used to present choices to the user.
STYLE_INFO = {
    "prime":        ("Prime", "AI HomeDesign's signature balanced, magazine-ready look"),
    "modern":       ("Modern", "Clean lines, neutral palette, minimal clutter, sleek finishes"),
    "farmhouse":    ("Farmhouse", "Warm and rustic — natural wood, soft neutrals, country charm"),
    "scandinavian": ("Scandinavian", "Light woods, white walls, airy minimalism, bright and functional"),
    "hampton":      ("Hampton", "Coastal-elegant — crisp whites, navy accents, relaxed luxury"),
    "industrial":   ("Industrial", "Exposed brick and metal, raw textures, urban-loft feel"),
    "traditional":  ("Traditional", "Classic timeless furnishings, rich woods, formal and symmetrical"),
    "contemporary": ("Contemporary", "Of-the-moment trends, bold but balanced, mixed textures"),
}
ROOM_LABELS = {
    "bedroom": "Bedroom", "living-room": "Living Room", "kitchen": "Kitchen",
    "bathroom": "Bathroom", "dining-room": "Dining Room", "home-office": "Home Office",
    "outdoor": "Outdoor", "nursery": "Nursery",
}
AREA_LABELS = {"indoor": "Indoor", "outdoor": "Outdoor"}

# Tools confirmed unavailable upstream ("tool repository: tool not found").
# The MCP returns a clean message instead of a raw 500 if these are requested.
UNAVAILABLE_TOOLS = {"tool-furniture-set-restyle", "tool-sky-change"}

# category -> human label, used for grouping on the landing page / list output
CATEGORIES = {
    "staging": "Virtual Staging",
    "design": "Interior Design",
    "editing": "Photo Editing",
    "renovation": "Home Renovation",
    "exterior": "Exterior",
}

# Each tool:
#   name        human label
#   category    one of CATEGORIES
#   desc        what it does
#   slots       list of input-slot keys (asset_map keys). 1 image each unless noted.
#   widgets     list of {slug, purpose, select: 'single'|'multi', required, items[]}
#               `items` are *example* values (the API accepts more); item slug is
#               built as item-<value>-<widget-slug>.
TOOLS = {
    "tool-virtual-staging": {
        "name": "AI Virtual Staging",
        "category": "staging",
        "desc": "Furnish an empty room with AI-generated furniture in a chosen style.",
        "slots": ["tool-virtual-staging-input-image"],
        "widgets": [
            {"slug": "widget-space-tool-virtual-staging", "purpose": "Room type",
             "select": "single", "required": True, "items": ROOMS},
            {"slug": "widget-style-tool-virtual-staging", "purpose": "Design style",
             "select": "single", "required": True, "items": STYLES},
        ],
    },
    "tool-virtual-restaging": {
        "name": "AI Virtual Restaging",
        "category": "staging",
        "desc": "Replace the existing furniture in a staged room with a new style.",
        "slots": ["tool-virtual-restaging-input-image"],
        "widgets": [
            {"slug": "widget-space-tool-virtual-restaging", "purpose": "Room type",
             "select": "single", "required": True, "items": ROOMS},
            {"slug": "widget-style-tool-virtual-restaging", "purpose": "Design style",
             "select": "single", "required": True, "items": STYLES},
        ],
    },
    "tool-interior-design": {
        "name": "AI Interior Designer",
        "category": "design",
        "desc": "Fully redesign a room: walls, floors, furniture and decor in a chosen style and palette.",
        "slots": ["tool-interior-design-input-image"],
        "widgets": [
            {"slug": "widget-space-tool-interior-design", "purpose": "Room type",
             "select": "single", "required": True, "items": ROOMS},
            {"slug": "widget-style-tool-interior-design", "purpose": "Design style",
             "select": "single", "required": True, "items": STYLES},
            {"slug": "widget-color-tool-interior-design", "purpose": "Colour palette",
             "select": "single", "required": False,
             "items": ["earthy-neutrals", "warm-tones", "cool-tones", "monochrome", "pastel"]},
        ],
    },
    "tool-furniture-set-restyle": {
        "name": "AI Furniture Restyle",
        "category": "design",
        "desc": "Swap a room's furniture set for a different style while keeping the structure.",
        "slots": ["tool-furniture-set-restyle-input-image"],
        "widgets": [
            {"slug": "widget-space-tool-furniture-set-restyle", "purpose": "Room type",
             "select": "single", "required": True, "items": ROOMS},
            {"slug": "widget-style-tool-furniture-set-restyle", "purpose": "Design style",
             "select": "single", "required": True, "items": STYLES},
        ],
    },
    "tool-image-enhancement": {
        "name": "AI Image Enhancement",
        "category": "editing",
        "desc": "Improve lighting, sharpness and colour balance, with optional effects (fire in fireplace, screen on TV).",
        "slots": ["tool-image-enhancement-input-image"],
        "widgets": [
            {"slug": "widget-area-tool-image-enhancement", "purpose": "Area type",
             "select": "single", "required": True, "items": AREAS},
            {"slug": "widget-enhancement-options-tool-image-enhancement", "purpose": "Optional effects",
             "select": "multi", "required": False,
             "items": ["add-fire-to-fireplace", "add-screen-to-tv"]},
        ],
    },
    "tool-item-removal": {
        "name": "AI Item Removal",
        "category": "editing",
        "desc": "Automatically detect and remove furniture and clutter, leaving a clean empty room.",
        "slots": ["tool-item-removal-input-image"],
        "widgets": [],
    },
    "tool-item-removal-mask": {
        "name": "AI Item Removal (Mask)",
        "category": "editing",
        "desc": "Remove exactly the objects you paint on a mask (blue #7878CD = remove, black = keep).",
        "slots": ["tool-item-removal-mask-input-image", "tool-item-removal-mask-input-mask"],
        "widgets": [],
    },
    "tool-darkness": {
        "name": "AI Darkness",
        "category": "editing",
        "desc": "Adjust the darkness/brightness of a before-and-after image pair so they match.",
        "slots": ["tool-darkness-input-image-before", "tool-darkness-input-image-after"],
        "widgets": [
            {"slug": "widget-darkness-factor-tool-darkness", "purpose": "How much darker/lighter",
             "select": "single", "required": True,
             "items": ["factor-darker", "factor-lighter"]},
        ],
    },
    "tool-wall-change": {
        "name": "AI Wall Change",
        "category": "renovation",
        "desc": "Change wall colour, texture or wallpaper.",
        "slots": ["tool-wall-change-input-image"],
        "widgets": [
            {"slug": "widget-space-tool-wall-change", "purpose": "Room type",
             "select": "single", "required": True,
             "items": ["bedroom", "living-room", "kitchen", "bathroom"]},
            {"slug": "widget-material-tool-wall-change", "purpose": "Wall material/finish",
             "select": "single", "required": True,
             "items": ["brick-wall", "white-paint", "wallpaper", "concrete"]},
        ],
    },
    "tool-floor-change": {
        "name": "AI Floor Change",
        "category": "renovation",
        "desc": "Swap the floor material — hardwood, marble, tile, carpet and more.",
        "slots": ["tool-floor-change-input-image"],
        "widgets": [
            {"slug": "widget-space-tool-floor-change", "purpose": "Room type",
             "select": "single", "required": True,
             "items": ["bedroom", "living-room", "kitchen", "bathroom"]},
            {"slug": "widget-material-tool-floor-change", "purpose": "Floor material",
             "select": "single", "required": True,
             "items": ["white-marble", "hardwood", "tile", "carpet"]},
        ],
    },
    "tool-ceiling-change": {
        "name": "AI Ceiling Change",
        "category": "renovation",
        "desc": "Change the ceiling material or colour.",
        "slots": ["tool-ceiling-change-input-image"],
        "widgets": [
            {"slug": "widget-space-tool-ceiling-change", "purpose": "Room type",
             "select": "single", "required": True,
             "items": ["bedroom", "living-room", "kitchen"]},
            {"slug": "widget-material-tool-ceiling-change", "purpose": "Ceiling material",
             "select": "single", "required": True,
             "items": ["wooden-ceiling", "white-paint", "coffered"]},
        ],
    },
    "tool-backsplash-change": {
        "name": "AI Backsplash Change",
        "category": "renovation",
        "desc": "Change the kitchen or bathroom backsplash material.",
        "slots": ["tool-backsplash-change-input-image"],
        "widgets": [
            {"slug": "widget-space-tool-backsplash-change", "purpose": "Room type",
             "select": "single", "required": True,
             "items": ["kitchen", "bathroom"]},
            {"slug": "widget-material-tool-backsplash-change", "purpose": "Backsplash material",
             "select": "single", "required": True,
             "items": ["grey-marble", "subway-tile", "mosaic", "white-marble"]},
        ],
    },
    "tool-under-construction": {
        "name": "AI Under Construction",
        "category": "renovation",
        "desc": "Visualise a finished space from an unfinished / under-construction room photo.",
        "slots": ["tool-under-construction-input-image"],
        "widgets": [
            {"slug": "widget-space-tool-under-construction", "purpose": "Target room type",
             "select": "single", "required": True, "items": ROOMS},
            {"slug": "widget-style-tool-under-construction", "purpose": "Design style",
             "select": "single", "required": True, "items": STYLES},
        ],
    },
    "tool-day-to-dusk": {
        "name": "AI Day to Dusk",
        "category": "exterior",
        "desc": "Convert a daytime exterior into a dramatic dusk / twilight scene.",
        "slots": ["tool-day-to-dusk-input-image"],
        "widgets": [
            {"slug": "widget-sky-style-tool-day-to-dusk", "purpose": "Sky / lighting mood",
             "select": "single", "required": True,
             "items": ["dusk-with-cloud", "clear-dusk", "golden-hour"]},
            {"slug": "widget-day-to-dusk-options-tool-day-to-dusk", "purpose": "Optional edits",
             "select": "multi", "required": False,
             "items": ["shadow-removal", "lawn-touch-up"]},
        ],
    },
    "tool-sky-change": {
        "name": "AI Sky Change",
        "category": "exterior",
        "desc": "Replace the sky in an exterior photo, with optional flip.",
        "slots": ["tool-sky-change-input-image"],
        "widgets": [
            {"slug": "widget-variants-tool-sky-change", "purpose": "Sky variant",
             "select": "single", "required": True,
             "items": ["dusk-with-cloud-01", "clear-blue-01", "sunset-01"]},
            {"slug": "widget-style-tool-sky-change", "purpose": "Sky style/mood",
             "select": "single", "required": False,
             "items": ["dusk-with-cloud", "clear-blue", "sunset"]},
            {"slug": "widget-sky-change-options-tool-sky-change", "purpose": "Optional effects",
             "select": "single", "required": False,
             "items": ["flip-sky"]},
        ],
    },
}


def item_slug(widget_slug: str, value: str) -> str:
    """Build a full item slug from a short value, e.g. ('widget-space-tool-virtual-staging','bedroom')
    -> 'item-bedroom-widget-space-tool-virtual-staging'. If `value` already looks
    like a full item slug it is returned unchanged."""
    if value.startswith("item-"):
        return value
    return f"item-{value}-{widget_slug}"
