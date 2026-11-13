#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Dec 15 14:17:17 2025

@author: mikekriner
"""
import streamlit as st
from PIL import Image, ImageDraw, ImageFont
import io
import zipfile
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import os
import hashlib
import json
import base64
import requests

# Page config
st.set_page_config(page_title="Custom Image Generator", layout="wide")

st.title("🎨 Custom Image Generator")
st.write("Generate custom images by combining a template with overlays and text.")

logo = Image.open("static/logo_white.png").resize((166,252))
st.sidebar.image(logo)

# --- Per-template layout presets --------------------------------------------
# Saved to a JSON file, keyed by a hash of the template's bytes, so the
# overlay/text position & size settings tuned for a given template are
# remembered and auto-loaded the next time that same template is selected or
# uploaded again.
#
# When a [github] section is present in st.secrets (as on Streamlit Community
# Cloud), the file is read from and committed to the GitHub repo through the
# contents API, so layouts survive redeploys and restarts. Otherwise (e.g.
# running locally) it's just a file next to the app.
PRESETS_FILE = 'template_layouts.json'
GITHUB_API = 'https://api.github.com'


def _github_config():
    """Return the [github] secrets (token, repo, optional branch), or None
    if they aren't configured."""
    try:
        cfg = st.secrets["github"]
        if cfg.get("token") and cfg.get("repo"):
            return cfg
    except Exception:  # no secrets file / no [github] section
        pass
    return None


def _github_contents_url(cfg):
    return f"{GITHUB_API}/repos/{cfg['repo']}/contents/{PRESETS_FILE}"


def _github_headers(cfg):
    return {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/vnd.github+json",
    }


def _fetch_github_presets(cfg):
    """Fetch the presets file from GitHub. Returns (presets, sha); sha is
    None if the file doesn't exist in the repo yet."""
    resp = requests.get(_github_contents_url(cfg), headers=_github_headers(cfg),
                        params={"ref": cfg.get("branch", "main")}, timeout=15)
    if resp.status_code == 404:
        return {}, None
    resp.raise_for_status()
    body = resp.json()
    return json.loads(base64.b64decode(body["content"]).decode("utf-8")), body["sha"]


@st.cache_data(ttl=300, show_spinner=False)
def _cached_github_presets(repo, branch):
    # repo/branch are only the cache key; the config is re-read inside.
    return _fetch_github_presets(_github_config())[0]


def _write_github_presets(cfg, update, message):
    """Apply `update` (a function that edits the presets dict in place) to the
    latest copy on GitHub and commit it. Retries if someone else saved in
    between (GitHub rejects the write when the sha is stale)."""
    for _attempt in range(3):
        presets, sha = _fetch_github_presets(cfg)
        update(presets)
        payload = {
            "message": message,
            "content": base64.b64encode(json.dumps(presets, indent=2).encode("utf-8")).decode("ascii"),
            "branch": cfg.get("branch", "main"),
        }
        if sha:
            payload["sha"] = sha
        resp = requests.put(_github_contents_url(cfg), headers=_github_headers(cfg), json=payload, timeout=15)
        if resp.status_code in (409, 422) and _attempt < 2:
            continue
        resp.raise_for_status()
        _cached_github_presets.clear()
        return


def _modify_presets(update, message):
    cfg = _github_config()
    if cfg:
        _write_github_presets(cfg, update, message)
        return
    presets = load_presets()
    update(presets)
    with open(PRESETS_FILE, 'w') as f:
        json.dump(presets, f, indent=2)

LAYOUT_FIELDS = [
    "overlay_x", "overlay_y", "overlay_max_w", "overlay_max_h",
    "overlay_auto_trim", "overlay_zone_max_w", "overlay_zone_max_h",
    "font_size", "auto_center_text", "text_center_x", "text_x", "text_y",
    "text_spacing", "text_align", "auto_shrink_text", "text_max_width",
    "text_min_font_size", "name_text_color", "static_text_color",
]


def load_presets():
    cfg = _github_config()
    if cfg:
        try:
            return _cached_github_presets(cfg["repo"], cfg.get("branch", "main"))
        except (requests.RequestException, ValueError, KeyError):
            st.sidebar.warning("⚠️ Couldn't load saved layouts from GitHub — using defaults for now.")
            return {}
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_preset(template_hash, template_name, values):
    _modify_presets(
        lambda presets: presets.__setitem__(template_hash, {"template_name": template_name, **values}),
        f"Save layout for {template_name}",
    )


def delete_preset(template_hash):
    _modify_presets(
        lambda presets: presets.pop(template_hash, None),
        "Delete saved template layout",
    )


# ----------------------------------------------------------------------
# Template selection: pick from a local 'templates/' folder if one exists
# next to the app (same pattern as the State Map Generator's state_images
# folder), otherwise fall back to a plain file uploader. Either path ends
# up setting `template_bytes` to the raw file bytes.
#
# Templates are grouped into one subfolder of 'templates/' per brand; the
# Brand dropdown (above "Upload Files") picks the folder, and the Template
# dropdown picks an image within it.
# ----------------------------------------------------------------------
local_templates_dir = 'templates'
TEMPLATE_FOLDER_ORDER = ["verasight", "baseline", "statewide", "calibrate"]
use_local_templates = os.path.isdir(local_templates_dir)
template_folders = []
if use_local_templates:
    _found_folders = [
        d for d in os.listdir(local_templates_dir)
        if os.path.isdir(os.path.join(local_templates_dir, d)) and not d.startswith('.')
    ]
    # Known folders first in their set order, then any others alphabetically.
    template_folders = [d for d in TEMPLATE_FOLDER_ORDER if d in _found_folders] + \
                       sorted(d for d in _found_folders if d not in TEMPLATE_FOLDER_ORDER)


def list_template_images(folder):
    folder_path = os.path.join(local_templates_dir, folder)
    return sorted(
        f for f in os.listdir(folder_path)
        if f.lower().endswith(('.png', '.jpg', '.jpeg'))
    )


chosen_brand = None
if template_folders:
    chosen_brand = st.sidebar.selectbox("Brand", template_folders, key="brand_select",
                                        format_func=str.title)

# File uploaders
st.sidebar.header("Upload Files")

template_bytes = None
template_display_name = None
if template_folders:
    template_source_mode = st.sidebar.radio(
        "Template source", ["Choose from library", "Upload custom"],
        horizontal=True, key="template_source_mode"
    )
    if template_source_mode == "Choose from library":
        template_options = list_template_images(chosen_brand)
        if template_options:
            chosen_template_name = st.sidebar.selectbox("Template", template_options,
                                                        key=f"template_select_{chosen_brand}")
            with open(os.path.join(local_templates_dir, chosen_brand, chosen_template_name), 'rb') as f:
                template_bytes = f.read()
            template_display_name = chosen_template_name
        else:
            st.sidebar.caption(f"No {chosen_brand.title()} templates yet — add images to "
                               f"templates/{chosen_brand}/ or upload one instead.")
    else:
        _uploaded_template = st.sidebar.file_uploader("Upload Template Image", type=['png', 'jpg', 'jpeg'])
        if _uploaded_template:
            template_bytes = _uploaded_template.getvalue()
            template_display_name = _uploaded_template.name
else:
    if use_local_templates:
        st.sidebar.caption("No template folders found in 'templates' — upload one instead.")
    _uploaded_template = st.sidebar.file_uploader("Upload Template Image", type=['png', 'jpg', 'jpeg'])
    if _uploaded_template:
        template_bytes = _uploaded_template.getvalue()
        template_display_name = _uploaded_template.name

# Look up (or fall back to defaults for) this template's saved layout before
# the config widgets below are created. Widget keys are suffixed by the
# template's hash so each template gets its own independent set of
# remembered values in Streamlit's session state — switching templates swaps
# in that template's saved layout instead of leaving the previous one's
# numbers in place.
template_hash = None
saved_layout = {}
if template_bytes:
    template_hash = hashlib.sha256(template_bytes).hexdigest()
    saved_layout = load_presets().get(template_hash, {})

    _template_preview = Image.open(io.BytesIO(template_bytes))
    st.sidebar.caption(f"📐 Template size: {_template_preview.width} × {_template_preview.height} px",
                        help="Set text_max_width to roughly the width of the template minus 100-150px.")

    if saved_layout:
        st.sidebar.success(f"✅ Saved layout loaded for '{saved_layout.get('template_name', template_display_name)}'")
        if st.sidebar.button("🗑️ Forget this layout", key="forget_layout_btn"):
            try:
                delete_preset(template_hash)
            except requests.RequestException as e:
                st.sidebar.error(f"Couldn't delete the layout on GitHub: {e}")
            else:
                for _field in LAYOUT_FIELDS:
                    st.session_state.pop(f"{_field}_{template_hash}", None)
                st.rerun()
    else:
        st.sidebar.info("ℹ️ No saved layout for this template yet — tune the settings below, then save them.")

_key_suffix = template_hash or "default"


def pref(name, default):
    return saved_layout.get(name, default)

# ----------------------------------------------------------------------
# Font selection: same local-folder-first pattern, reading from 'fonts/'
# if present. Ends up setting `font_bytes` to the raw .ttf bytes.
# ----------------------------------------------------------------------
local_fonts_dir = 'fonts'
use_local_fonts = os.path.isdir(local_fonts_dir)
font_options = []
if use_local_fonts:
    font_options = sorted([f for f in os.listdir(local_fonts_dir) if f.lower().endswith('.ttf')])

font_bytes = None
if font_options:
    font_source_mode = st.sidebar.radio(
        "Font source", ["Choose from library", "Upload custom"],
        horizontal=True, key="font_source_mode"
    )
    if font_source_mode == "Choose from library":
        chosen_font_name = st.sidebar.selectbox("Font", font_options, key="font_select")
        with open(os.path.join(local_fonts_dir, chosen_font_name), 'rb') as f:
            font_bytes = f.read()
    else:
        _uploaded_font = st.sidebar.file_uploader("Upload Font File (.ttf)", type=['ttf'])
        if _uploaded_font:
            font_bytes = _uploaded_font.getvalue()
else:
    if use_local_fonts:
        st.sidebar.caption("No .ttf files found in 'fonts' folder — upload one instead.")
    _uploaded_font = st.sidebar.file_uploader("Upload Font File (.ttf)", type=['ttf'])
    if _uploaded_font:
        font_bytes = _uploaded_font.getvalue()

overlay_files = st.sidebar.file_uploader("Upload Overlay Images", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True)

# Configuration inputs
st.sidebar.header("Overlay Configuration")
overlay_x = st.sidebar.number_input("Overlay X Position", value=pref("overlay_x", 2072), key=f"overlay_x_{_key_suffix}", help="Horizontal position of the overlay box (top-left corner); matches the top-right badge area on the July 2026 template")
overlay_y = st.sidebar.number_input("Overlay Y Position", value=pref("overlay_y", 40), key=f"overlay_y_{_key_suffix}", help="Vertical position of the overlay box (top-left corner)")
overlay_max_w = st.sidebar.number_input("Overlay Max Width", value=pref("overlay_max_w", 880), key=f"overlay_max_w_{_key_suffix}", help="Maximum width the overlay can be")
overlay_max_h = st.sidebar.number_input("Overlay Max Height", value=pref("overlay_max_h", 720), key=f"overlay_max_h_{_key_suffix}", help="Maximum height the overlay can be")
overlay_auto_trim = st.sidebar.checkbox("Auto-trim transparent padding on overlay", value=pref("overlay_auto_trim", True),
                                         key=f"overlay_auto_trim_{_key_suffix}",
                                         help="Crops each overlay to its visible content before fitting it into the box above. Fixes overlays looking smaller than others when the source image has extra transparent margin (common with state shape PNGs).")
overlay_zone_max_w = st.sidebar.number_input(
    "Oversized-Shape Zone Max Width", value=pref("overlay_zone_max_w", 1400), key=f"overlay_zone_max_w_{_key_suffix}",
    help="Caps how wide an oversized shape (e.g. Alaska, scaled up below) is allowed to grow, so it never overflows the canvas or the text area. The shape stays centered on the normal overlay box."
)
overlay_zone_max_h = st.sidebar.number_input(
    "Oversized-Shape Zone Max Height", value=pref("overlay_zone_max_h", 900), key=f"overlay_zone_max_h_{_key_suffix}",
    help="Caps how tall an oversized shape (e.g. Alaska, scaled up below) is allowed to grow, so it never overflows the canvas."
)

st.sidebar.header("Text Configuration")
font_size = st.sidebar.slider("Font Size", 100, 400, pref("font_size", 265), key=f"font_size_{_key_suffix}")
auto_center_text = st.sidebar.checkbox("Auto-center text horizontally", value=pref("auto_center_text", False),
                                        key=f"auto_center_text_{_key_suffix}",
                                        help="Recentres the text based on its actual rendered width, so it stays centered no matter how long the text is (e.g. 'Alabama' vs 'California'). The July 2026 template is left-aligned, so this defaults off.")
text_center_x = st.sidebar.number_input("Text Center X (used when auto-center is on)", value=pref("text_center_x", 1590),
                                         key=f"text_center_x_{_key_suffix}",
                                         help="The horizontal point the text should be centered around.")
text_x = st.sidebar.number_input("Text X Position (left edge)", value=pref("text_x", 230), key=f"text_x_{_key_suffix}", help="Horizontal position of the text")
text_y = st.sidebar.number_input("Text Y Position (top of first line)", value=pref("text_y", 930), key=f"text_y_{_key_suffix}", help="Vertical position of the text")
text_spacing = st.sidebar.slider("Line Spacing Adjustment", -50, 50, pref("text_spacing", 0), key=f"text_spacing_{_key_suffix}",
                                  help="Pixels added to (positive) or removed from (negative) the font's natural line height between lines.")
_align_options = ["left", "center", "right"]
text_align = st.sidebar.selectbox("Text Alignment", _align_options, index=_align_options.index(pref("text_align", "left")),
                                   key=f"text_align_{_key_suffix}",
                                   help="How each line is aligned within Text Max Width when auto-center is off.")
auto_shrink_text = st.sidebar.checkbox("Auto-shrink font to fit width", value=pref("auto_shrink_text", True),
                                        key=f"auto_shrink_text_{_key_suffix}",
                                        help="If a line of text (e.g. a long county name) would render wider than the max width below, the font size is automatically reduced until it fits.")
text_max_width = st.sidebar.number_input("Text Max Width (px)", value=pref("text_max_width", 1750),
                                          key=f"text_max_width_{_key_suffix}",
                                          help="Maximum allowed rendered text width before auto-shrink kicks in. On the July 2026 template, keep this narrow enough that text doesn't run into the state/county badge in the top-right corner.")
text_min_font_size = st.sidebar.number_input("Minimum Font Size (auto-shrink floor)", value=pref("text_min_font_size", 100),
                                              key=f"text_min_font_size_{_key_suffix}",
                                              help="Font will never shrink below this size, even if the text still doesn't fully fit.")

# Colors: the July 2026 template uses two text colors — one for the first line
# (the dynamic state/county name) and one for the remaining static line(s)
# (e.g. "voices needed!"), matching the teal/blue two-tone look in the example.
name_text_color = st.sidebar.color_picker(
    "First Line Color (state/county name)", pref("name_text_color", "#31BAEC"),
    key=f"name_text_color_{_key_suffix}",
    help="Color for the first line of text — typically the dynamic state or county name."
)
name_text_color_rgb = tuple(int(name_text_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))

static_text_color = st.sidebar.color_picker(
    "Remaining Lines Color (e.g. 'voices needed!')", pref("static_text_color", "#FFFFFF"),
    key=f"static_text_color_{_key_suffix}",
    help="Color for any additional lines of text after the first."
)
static_text_color_rgb = tuple(int(static_text_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))

if template_bytes:
    if st.sidebar.button("💾 Save current values as this template's layout", key="save_layout_btn"):
        try:
            save_preset(template_hash, template_display_name, {
                "overlay_x": overlay_x, "overlay_y": overlay_y,
                "overlay_max_w": overlay_max_w, "overlay_max_h": overlay_max_h,
                "overlay_auto_trim": overlay_auto_trim,
                "overlay_zone_max_w": overlay_zone_max_w, "overlay_zone_max_h": overlay_zone_max_h,
                "font_size": font_size, "auto_center_text": auto_center_text,
                "text_center_x": text_center_x, "text_x": text_x, "text_y": text_y,
                "text_spacing": text_spacing, "text_align": text_align,
                "auto_shrink_text": auto_shrink_text, "text_max_width": text_max_width,
                "text_min_font_size": text_min_font_size,
                "name_text_color": name_text_color, "static_text_color": static_text_color,
            })
        except requests.RequestException as e:
            st.sidebar.error(f"Couldn't save the layout to GitHub: {e}")
        else:
            st.sidebar.success(f"Layout saved for '{template_display_name}'!")


def trim_transparent(img):
    """Crop image to the bounding box of its non-transparent content."""
    if img.mode != "RGBA":
        return img
    alpha = img.split()[-1]
    bbox = alpha.getbbox()
    return img.crop(bbox) if bbox else img


def fit_into(img, max_w, max_h, trim=None):
    """Scale img to fit within (max_w, max_h), optionally trimming transparent
    padding first so differently-padded source images render at a consistent size."""
    if trim is None:
        trim = overlay_auto_trim
    if trim:
        img = trim_transparent(img)
    w, h = img.size
    scale = min(max_w / w, max_h / h)
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def paste_centered(canvas, overlay_resized, box_x, box_y, box_w, box_h):
    """Alpha-composite overlay_resized centered within the given box."""
    ow, oh = overlay_resized.size
    paste_x = int(box_x + (box_w - ow) / 2)
    paste_y = int(box_y + (box_h - oh) / 2)
    canvas.alpha_composite(overlay_resized, (paste_x, paste_y))


def clamp_box_to_zone(center_x, center_y, desired_w, desired_h, zone_max_w, zone_max_h,
                       canvas_w=None, canvas_h=None):
    """
    Given a desired (possibly oversized) box size and the center point of the
    *normal* overlay box, caps the size to zone_max_w/zone_max_h and returns a
    box centered on that same center point. This keeps oversized shapes
    (e.g. Alaska scaled 6x) growing evenly outward from the badge's center
    instead of drifting off-canvas, which happens if you naively expand a box
    from a fixed top-left corner.

    If canvas_w/canvas_h are given, the box is additionally shifted (without
    resizing) so it never extends past the edges of the canvas — a safety
    net for when the badge sits close to a corner.
    """
    capped_w = min(desired_w, zone_max_w)
    capped_h = min(desired_h, zone_max_h)
    box_x = center_x - capped_w / 2
    box_y = center_y - capped_h / 2

    if canvas_w is not None:
        box_x = max(0, min(box_x, canvas_w - capped_w))
    if canvas_h is not None:
        box_y = max(0, min(box_y, canvas_h - capped_h))

    return box_x, box_y, capped_w, capped_h


def build_county_overlay_image(state_counties_gdf, county_fips, highlight_color, base_color, outline_color, dpi):
    """Render a single county-highlight map with matplotlib and return it as a
    transparent-background PIL RGBA image. Shared by the batch County Map
    Generator and its single-county preview so the two always match exactly."""
    state_counties_copy = state_counties_gdf.copy()
    state_counties_copy['color'] = state_counties_copy['GEOID'].apply(
        lambda x: highlight_color if x == county_fips else base_color
    )
    fig, ax = plt.subplots(figsize=(8, 6))
    state_counties_copy.plot(
        ax=ax,
        color=state_counties_copy['color'],
        edgecolor=outline_color,
        linewidth=0.5
    )
    ax.set_axis_off()
    buf = io.BytesIO()
    plt.savefig(buf, format='PNG', dpi=dpi, bbox_inches='tight', transparent=True, facecolor='none')
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGBA")


def composite_complete_image(template, font, overlay_img, name_part, second_part,
                              overlay_x, overlay_y, overlay_max_w, overlay_max_h,
                              overlay_zone_max_w, overlay_zone_max_h, one_line=False, oversized=False):
    """Composite one overlay + text onto the template — the exact same logic
    used inside the County and State Map Generators' batch loops. `oversized=True`
    reproduces the Alaska 6x-from-center scaling. `one_line=True` draws
    name_part and second_part side by side on a single row instead of
    stacked. Shared by the batch generators and their previews so a preview
    is always a true representation of the final output."""
    canvas = template.copy()
    draw = ImageDraw.Draw(canvas)

    if oversized:
        center_x = overlay_x + overlay_max_w / 2
        center_y = overlay_y + overlay_max_h / 2
        box_x, box_y_, box_w, box_h = clamp_box_to_zone(
            center_x, center_y,
            overlay_max_w * 6, overlay_max_h * 6,
            overlay_zone_max_w, overlay_zone_max_h,
            canvas_w=canvas.width, canvas_h=canvas.height
        )
    else:
        box_w, box_h = overlay_max_w, overlay_max_h
        box_x, box_y_ = overlay_x, overlay_y

    overlay_resized = fit_into(overlay_img, box_w, box_h)
    paste_centered(canvas, overlay_resized, box_x, box_y_, box_w, box_h)
    draw_two_part_text(draw, name_part, second_part, font, one_line=one_line)
    return canvas


def get_font(size):
    """Create a font instance at the given size from the uploaded font bytes."""
    return ImageFont.truetype(io.BytesIO(font_bytes), size)


def get_render_font(draw, text, debug_label=None):
    """Return a font sized for `text`, auto-shrinking from font_size down to
    text_min_font_size (in steps of 5) until the rendered width fits within
    text_max_width. Falls back to the base font unchanged if auto-shrink is
    off or the text already fits.

    If debug_label is given, records the measured width, threshold, and final
    font size to st.session_state['shrink_debug'] for later inspection."""
    if not text or not auto_shrink_text:
        if debug_label is not None:
            _log_shrink_debug(debug_label, text, font_size, font_size, None)
        return font
    size = font_size
    candidate = font
    bbox = draw.multiline_textbbox((0, 0), text, font=candidate,
                                    spacing=text_spacing, align=text_align)
    width = bbox[2] - bbox[0]
    original_width = width
    while width > text_max_width and size > text_min_font_size:
        size -= 5
        candidate = get_font(size)
        bbox = draw.multiline_textbbox((0, 0), text, font=candidate,
                                        spacing=text_spacing, align=text_align)
        width = bbox[2] - bbox[0]
    if debug_label is not None:
        _log_shrink_debug(debug_label, text, font_size, size, original_width)
    return candidate


def _log_shrink_debug(label, text, base_size, final_size, original_width):
    """Append a row describing whether/how much a given text got shrunk."""
    if 'shrink_debug' not in st.session_state:
        st.session_state['shrink_debug'] = []
    st.session_state['shrink_debug'].append({
        'label': label,
        'text': text,
        'original_width_px': original_width,
        'base_font_size': base_size,
        'final_font_size': final_size,
        'shrunk': final_size < base_size,
    })


def draw_text(draw, custom_text, font, debug_label=None):
    """Draw custom_text line by line, auto-shrinking the font so long lines
    never overflow the canvas. The first line (typically the dynamic
    state/county name) is drawn in name_text_color_rgb; any remaining
    line(s) (e.g. "voices needed!") are drawn in static_text_color_rgb,
    matching the two-tone look of the July 2026 template. Lines are spaced
    using the font's own ascent+descent metrics plus text_spacing, and each
    line is positioned per text_align (or auto-centered around
    text_center_x when auto_center_text is on)."""
    if not custom_text:
        return
    render_font = get_render_font(draw, custom_text, debug_label=debug_label)
    ascent, descent = render_font.getmetrics()
    line_height = ascent + descent + text_spacing
    lines = custom_text.split('\n')
    y_cursor = text_y
    for i, line in enumerate(lines):
        color = name_text_color_rgb if i == 0 else static_text_color_rgb
        bbox = draw.textbbox((0, 0), line, font=render_font)
        line_w = bbox[2] - bbox[0]
        if auto_center_text:
            draw_x = text_center_x - line_w / 2 - bbox[0]
        elif text_align == "center":
            draw_x = text_x + (text_max_width - line_w) / 2 - bbox[0]
        elif text_align == "right":
            draw_x = text_x + (text_max_width - line_w) - bbox[0]
        else:  # left
            draw_x = text_x - bbox[0]
        draw.text((draw_x, y_cursor), line, font=render_font, fill=color)
        y_cursor += line_height


def _combine_two_part_text(name_part, second_part, one_line):
    """Join the dynamic name portion and the static second-line portion used
    by the County/State Map Generators into a single string — on one line
    (space-joined) or two (newline-joined) — for auto-shrink sizing and for
    the shrink-debug log."""
    if not second_part:
        return name_part
    return f"{name_part} {second_part}" if one_line else f"{name_part}\n{second_part}"


def draw_two_part_text(draw, name_part, second_part, font, one_line=False, debug_label=None):
    """Draw the County/State Map Generator's two text parts — the dynamic
    name (e.g. a state or county name) and the static second line (e.g.
    "voices needed!"). When one_line is False, they stack as two lines via
    draw_text (name in name_text_color_rgb, second part in
    static_text_color_rgb). When one_line is True, they're placed side by
    side on a single row instead, still in their respective colors, so the
    two-tone look is preserved either way."""
    combined = _combine_two_part_text(name_part, second_part, one_line)
    if not combined:
        return
    if not one_line or not second_part:
        draw_text(draw, combined, font, debug_label=debug_label)
        return

    render_font = get_render_font(draw, combined, debug_label=debug_label)
    bbox = draw.textbbox((0, 0), combined, font=render_font)
    combined_w = bbox[2] - bbox[0]
    if auto_center_text:
        draw_x = text_center_x - combined_w / 2 - bbox[0]
    elif text_align == "center":
        draw_x = text_x + (text_max_width - combined_w) / 2 - bbox[0]
    elif text_align == "right":
        draw_x = text_x + (text_max_width - combined_w) - bbox[0]
    else:  # left
        draw_x = text_x - bbox[0]

    draw.text((draw_x, text_y), name_part, font=render_font, fill=name_text_color_rgb)
    name_advance = draw.textlength(name_part + " ", font=render_font)
    draw.text((draw_x + name_advance, text_y), second_part, font=render_font, fill=static_text_color_rgb)


def make_color_transparent(img, target_color, threshold=50):
    """Make a specific color transparent in an image"""
    img = img.convert("RGBA")
    datas = img.getdata()
    new_data = []
    
    for item in datas:
        # Calculate Euclidean distance between pixel color and target color
        diff = sum((item[i] - target_color[i])**2 for i in range(3))**0.5
        
        # If color is within threshold, make transparent
        if diff < threshold:
            new_data.append((item[0], item[1], item[2], 0))  # alpha = 0
        else:
            new_data.append(item)
    
    img.putdata(new_data)
    return img


def recolor_visible_pixels(img, target_color_rgb):
    """Replace the RGB of every visible pixel with target_color_rgb, keeping
    the original alpha channel untouched. Ideal for recoloring a solid-shape
    PNG (e.g. a state silhouette) on a transparent background, since it
    preserves anti-aliased edges instead of hard-thresholding them."""
    img = img.convert("RGBA")
    alpha = img.split()[-1]
    solid = Image.new("RGBA", img.size, target_color_rgb + (255,))
    solid.putalpha(alpha)
    return solid


def save_optimized_png(img, max_bytes=490 * 1024):
    """Save a full template-composited image as PNG, progressively reducing
    file size (via palette quantization, then downscaling as a last resort)
    until it fits under max_bytes. Intended for the "Complete Images" outputs
    (template + overlay + text), which are large photographic canvases —
    NOT for the raw "Maps Only" county/state shape exports, which stay
    untouched at full quality/transparency.

    Returns a BytesIO positioned at 0."""
    img = img.convert("RGBA")

    def try_save(im, **kwargs):
        b = io.BytesIO()
        im.save(b, format='PNG', optimize=True, **kwargs)
        return b

    # 1. Plain optimized save at full color depth
    buf = try_save(img, compress_level=9)
    if buf.tell() <= max_bytes:
        buf.seek(0)
        return buf

    # 2. Palette quantization (keeps transparency via tRNS, reduces color depth)
    quantized = None
    for colors in (256, 192, 128, 96, 64, 48, 32):
        quantized = img.quantize(colors=colors, method=Image.FASTOCTREE)
        buf = try_save(quantized)
        if buf.tell() <= max_bytes:
            buf.seek(0)
            return buf

    # 3. Downscale as a last resort, using the smallest palette from step 2
    base_w, base_h = img.size
    scale = 0.9
    while buf.tell() > max_bytes and scale > 0.25:
        new_size = (max(1, int(base_w * scale)), max(1, int(base_h * scale)))
        resized = img.resize(new_size, Image.LANCZOS).quantize(colors=32, method=Image.FASTOCTREE)
        buf = try_save(resized)
        scale -= 0.1

    buf.seek(0)
    return buf


def generate_image(template, overlay, custom_text, font):
    canvas = template.copy()
    draw = ImageDraw.Draw(canvas)
    
    # Paste overlay, centered within the configured box so trimmed shapes of
    # different sizes/aspect ratios all sit in the same visual spot
    if overlay:
        overlay_resized = fit_into(overlay, overlay_max_w, overlay_max_h)
        paste_centered(canvas, overlay_resized, overlay_x, overlay_y, overlay_max_w, overlay_max_h)
    
    # Draw text
    draw_text(draw, custom_text, font)
    
    return canvas

# Main app logic
# Load template (optional here: Single Image/Batch tabs require it, but County/State
# Map Generators can run in "Maps Only" mode without a template or font).
# template_bytes was set in the sidebar, either from the 'templates/' library
# folder or from a manual upload.
template = None
if template_bytes:
    template = Image.open(io.BytesIO(template_bytes)).convert("RGBA")

# Load font (keep raw bytes so we can rebuild it at different sizes for
# auto-shrink). font_bytes was set in the sidebar, either from the 'fonts/'
# library folder or from a manual upload.
font = None
if font_bytes:
    font = ImageFont.truetype(io.BytesIO(font_bytes), font_size)

# Load overlay images
overlays_dict = {}
if overlay_files:
    for overlay_file in overlay_files:
        name = overlay_file.name
        overlays_dict[name] = Image.open(overlay_file).convert("RGBA")

# Add any processed overlays from session state
if 'saved_overlays' not in st.session_state:
    st.session_state['saved_overlays'] = {}

overlays_dict.update(st.session_state['saved_overlays'])

if template and font:
    if overlay_files or st.session_state['saved_overlays']:
        total_overlays = len(overlays_dict)
        st.success(f"✅ Loaded template, font, and {total_overlays} overlay images")
    else:
        st.success(f"✅ Loaded template and font")
else:
    st.info(
        "👈 Upload a template image and font file in the sidebar to use the Single Image "
        "and Batch Generator tabs, and to enable \"Complete Images\" mode in the County/State "
        "Map Generators. \"Maps Only\" mode works without them."
    )

# Tab interface
tab4, tab5, tab1, tab2, tab3 = st.tabs(["County Map Generator", "State Map Generator", "Single Image Generator", "Batch Generator", "Background Remover"])

with tab3:
    st.header("🖼️ Background Remover")
    st.write("Remove a specific color from your overlay images to make them transparent.")
    
    if not overlays_dict:
        st.info("Upload overlay images in the sidebar first to use the background remover.")
    else:
        bg_overlay_choice = st.selectbox("Select image to process:", 
                                       list(overlays_dict.keys()),
                                       key="bg_removal_select")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Settings")
            
            # Color picker for background color
            bg_color = st.color_picker("Select background color to remove", "#FFFFFF",
                                      help="Pick the color you want to make transparent")
            bg_color_rgb = tuple(int(bg_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
            
            # Threshold slider
            threshold = st.slider("Threshold", 0, 255, 50,
                                help="Higher values remove more similar colors. Lower values are more precise.")
            
            # Process button
            if st.button("🎨 Remove Background", type="primary", key="remove_bg_btn"):
                original_img = overlays_dict[bg_overlay_choice]
                processed_img = make_color_transparent(original_img.copy(), bg_color_rgb, threshold)
                st.session_state['processed_overlay'] = processed_img
                st.session_state['processed_overlay_name'] = bg_overlay_choice
        
        with col2:
            st.subheader("Preview")
            
            # Show before/after
            if 'processed_overlay' in st.session_state:
                # Create a checkerboard background to show transparency
                checker_size = 20
                w, h = st.session_state['processed_overlay'].size
                checker = Image.new('RGB', (w, h), (200, 200, 200))
                draw = ImageDraw.Draw(checker)
                for y in range(0, h, checker_size):
                    for x in range(0, w, checker_size):
                        if (x // checker_size + y // checker_size) % 2:
                            draw.rectangle([x, y, x + checker_size, y + checker_size], 
                                         fill=(255, 255, 255))
                
                # Composite the processed image over checker
                checker.paste(st.session_state['processed_overlay'], (0, 0), 
                            st.session_state['processed_overlay'])
                
                st.image(checker, caption="Processed (transparent areas show checkered)", 
                       use_container_width=True)
                
                # Download button
                buf = io.BytesIO()
                st.session_state['processed_overlay'].save(buf, format='PNG')
                buf.seek(0)
                
                st.download_button(
                    label="⬇️ Download Processed Image",
                    data=buf.getvalue(),
                    file_name=f"transparent_{st.session_state['processed_overlay_name']}",
                    mime="image/png",
                    key="download_processed"
                )
                
                # Option to use in generator
                if st.button("✅ Use This in Generator", key="use_processed"):
                    processed_name = f"processed_{bg_overlay_choice}"
                    st.session_state['saved_overlays'][processed_name] = st.session_state['processed_overlay']
                    st.success(f"✅ Added as '{processed_name}' to overlay list! Switch to the generator tabs to use it.")
                    st.rerun()
            else:
                st.info("Click 'Remove Background' to see the result")
    
with tab4:
    st.header("🗺️ County Map Generator")
    st.write("Generate individual county maps for US states with highlighted counties.")
    
    # State selection
    us_states = {
        'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
        'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
        'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
        'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
        'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
        'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
        'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
        'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
        'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
        'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
        'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
        'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
        'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'Washington DC'
    }
    
    col1, col2 = st.columns([1, 1])
    
    with col1:
        st.subheader("Settings")
        
        # State selection
        selected_state = st.selectbox(
            "Select State:",
            options=list(us_states.keys()),
            format_func=lambda x: f"{x} - {us_states[x]}"
        )
        
        # Check if local file exists
        local_shapefile_path = 'cb_2020_us_county_20m.zip'
        
        use_local_file = os.path.exists(local_shapefile_path)
        
        if use_local_file:
            st.success("✅ Using included shapefile")
            county_shapefile_source = "local"
        else:
            # Upload county shapefile
            county_shapefile = st.file_uploader(
                "Upload County Shapefile (ZIP)",
                type=['zip'],
                help="Upload a ZIP file containing county shapefiles (e.g., cb_2020_us_county_20m.zip from Census Bureau)"
            )
            county_shapefile_source = "upload"
        
        # Color settings
        highlight_color = st.color_picker("Highlight Color (selected county)", "#FFA500")
        base_color = st.color_picker("Base Color (other counties)", "#06204a")

        # County outline color
        outline_color = st.color_picker("County Outline Color", "#06204a")
        
        # Output mode selection
        output_mode = st.radio(
            "Output Mode:",
            options=["Maps Only", "Complete Images (with template & text)"],
            key="county_output_mode",
            help="Maps Only: Just county maps. Complete Images: Combines maps with template and adds '[County] [subdivision] / voices needed!' text."
        )
        
        # DPI setting
        dpi = st.slider("Image Quality (DPI)", 100, 600, 300, step=50)
        
        # Show template options if Complete Images is selected
        use_template = output_mode == "Complete Images (with template & text)"
        
        if use_template and not (template_bytes and font_bytes):
            st.warning("⚠️ Please upload a template image and font in the sidebar to use Complete Images mode.")

        county_second_line = "voices needed!"
        county_one_line = False
        if use_template:
            county_second_line = st.text_input(
                "Second Line Text", value="voices needed!", key="county_second_line_text",
                help="Static text drawn below the county/parish/borough name, in the \"Remaining Lines Color\"."
            )
            county_one_line = st.checkbox(
                "Combine onto one line", value=False, key="county_one_line",
                help="Draws the county/parish/borough name and the second line text side by side on one row instead of stacked on two. Colors are preserved either way."
            )

        # ------------------------------------------------------------------
        # Preview: let the user check one county's Complete Image (or map)
        # before committing to generating the whole state's batch. Reuses
        # build_county_overlay_image / composite_complete_image so the
        # preview is pixel-for-pixel what the batch run will produce.
        # ------------------------------------------------------------------
        st.divider()
        st.subheader("Preview")
        st.caption("Check one county's image before generating the whole state.")

        preview_can_load = use_local_file or (
            county_shapefile_source == "upload" and 'county_shapefile' in locals() and county_shapefile is not None
        )

        if st.button("Load Counties for Preview", key="load_preview_counties", disabled=not preview_can_load):
            try:
                if use_local_file:
                    _preview_gdf_full = gpd.read_file(local_shapefile_path)
                else:
                    _preview_gdf_full = gpd.read_file(county_shapefile)
                st.session_state['preview_county_gdf'] = _preview_gdf_full[_preview_gdf_full['STUSPS'] == selected_state]
                st.session_state['preview_county_state'] = selected_state
                # Clear any preview image from a previously-loaded state
                st.session_state.pop('county_preview_img', None)
            except Exception as e:
                st.error(f"Error loading shapefile for preview: {str(e)}")

        if st.session_state.get('preview_county_state') == selected_state and 'preview_county_gdf' in st.session_state:
            _preview_gdf = st.session_state['preview_county_gdf']

            if len(_preview_gdf) == 0:
                st.warning(f"No counties found for {selected_state} in the loaded shapefile.")
            else:
                county_names_list = sorted(_preview_gdf['NAME'].tolist())
                preview_county_name = st.selectbox("County to preview:", county_names_list, key="preview_county_select")

                preview_needs_template = use_template and not (template and font)
                if preview_needs_template:
                    st.caption("⚠️ Complete Images mode needs a template + font; previewing the map only.")

                if st.button("Generate Preview", key="generate_county_preview"):
                    with st.spinner(f"Rendering preview for {preview_county_name}..."):
                        county_row_preview = _preview_gdf[_preview_gdf['NAME'] == preview_county_name].iloc[0]
                        county_fips_preview = county_row_preview['GEOID']

                        map_img = build_county_overlay_image(
                            _preview_gdf, county_fips_preview,
                            highlight_color, base_color, outline_color, dpi
                        )

                        if use_template and template and font:
                            if selected_state == 'AK':
                                subdivision = 'Borough'
                            elif selected_state == 'LA':
                                subdivision = 'Parish'
                            else:
                                subdivision = 'County'
                            name_part = f"{preview_county_name} {subdivision}"
                            preview_canvas = composite_complete_image(
                                template, font, map_img, name_part, county_second_line,
                                overlay_x, overlay_y, overlay_max_w, overlay_max_h,
                                overlay_zone_max_w, overlay_zone_max_h,
                                one_line=county_one_line,
                                oversized=(selected_state == 'AK')
                            )
                            st.session_state['county_preview_img'] = preview_canvas
                        else:
                            st.session_state['county_preview_img'] = map_img
                        st.session_state['county_preview_label'] = preview_county_name
        else:
            st.caption("Click 'Load Counties for Preview' above to pick a county to check.")

        # Generate button
        can_generate = use_local_file or (county_shapefile_source == "upload" and 'county_shapefile' in locals() and county_shapefile is not None)
        
        if use_template:
            can_generate = can_generate and template_bytes and font_bytes
        
        if st.button("🗺️ Generate County Maps", type="primary", key="generate_counties", disabled=not can_generate):
            with st.spinner(f"Generating county maps for {us_states[selected_state]}..."):
                try:
                    # Read the shapefile
                    if use_local_file:
                        counties_gdf = gpd.read_file(local_shapefile_path)
                    else:
                        counties_gdf = gpd.read_file(county_shapefile)
                    
                    # Filter to selected state
                    state_counties = counties_gdf[counties_gdf['STUSPS'] == selected_state]
                    
                    if len(state_counties) == 0:
                        st.error(f"No counties found for {selected_state}. Please check your shapefile.")
                    else:
                        state_name = state_counties.iloc[0]['STATE_NAME']
                        
                        # Create a dictionary to store generated images
                        county_images = {}
                        st.session_state['shrink_debug'] = []  # reset debug log for this batch
                        progress_bar = st.progress(0)
                        
                        total_counties = len(state_counties)
                        
                        for idx, (_, county_row) in enumerate(state_counties.iterrows()):
                            county_name = county_row['NAME']
                            county_fips = county_row['GEOID']

                            # Render the county-highlight map (shared helper —
                            # identical logic to the preview above)
                            county_map_img = build_county_overlay_image(
                                state_counties, county_fips,
                                highlight_color, base_color, outline_color, dpi
                            )
                            
                            # If using template mode, combine with template and text
                            if use_template:
                                # Determine subdivision type based on state
                                if selected_state == 'AK':
                                    subdivision = 'Borough'
                                elif selected_state == 'LA':
                                    subdivision = 'Parish'
                                else:
                                    subdivision = 'County'
                                
                                # Name portion (drawn in the highlight color)
                                # plus the user-editable second line (drawn
                                # in the secondary color) — stacked on two
                                # lines or combined onto one, per
                                # "Combine onto one line" — matches the
                                # July 2026 template.
                                name_part = f"{county_name} {subdivision}"

                                canvas = composite_complete_image(
                                    template, font, county_map_img, name_part, county_second_line,
                                    overlay_x, overlay_y, overlay_max_w, overlay_max_h,
                                    overlay_zone_max_w, overlay_zone_max_h,
                                    one_line=county_one_line,
                                    oversized=(selected_state == 'AK')
                                )

                                # Text-sizing debug log needs its own entry
                                # (composite_complete_image doesn't take a
                                # debug_label, so log it directly here)
                                draw_dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
                                get_render_font(draw_dummy, _combine_two_part_text(name_part, county_second_line, county_one_line), debug_label=county_name)

                                # Save the complete image, capped to ~490KB since
                                # this is the full template-composited output
                                buf = save_optimized_png(canvas)
                            else:
                                buf = io.BytesIO()
                                county_map_img.save(buf, format='PNG')
                                buf.seek(0)
                            
                            # Clean filename
                            safe_county_name = "".join(c if c.isalnum() else "_" for c in county_name)
                            filename = f"{state_name}_{safe_county_name}.png"
                            
                            county_images[filename] = buf.getvalue()
                            
                            # Update progress
                            progress_bar.progress((idx + 1) / total_counties)
                        
                        st.session_state['county_images'] = county_images
                        st.session_state['county_state_name'] = state_name
                        st.session_state['county_mode_saved'] = output_mode
                        st.success(f"✅ Generated {len(county_images)} county {'images' if use_template else 'maps'} for {state_name}!")
                
                except Exception as e:
                    st.error(f"Error processing shapefile: {str(e)}")
    
    with col2:
        st.subheader("Results")

        # Single-county preview, shown above the batch results
        if 'county_preview_img' in st.session_state:
            st.image(
                st.session_state['county_preview_img'],
                caption=f"Preview: {st.session_state.get('county_preview_label', '')}",
                use_container_width=True
            )
            st.divider()
        
        if 'county_images' in st.session_state and st.session_state['county_images']:
            county_images = st.session_state['county_images']
            state_name = st.session_state['county_state_name']
            output_mode = st.session_state.get('county_mode_saved', 'Maps Only')
            
            # Preview first county
            first_image = list(county_images.values())[0]
            first_filename = list(county_images.keys())[0]
            st.image(first_image, caption=f"Preview: {first_filename}", use_container_width=True)
            
            # Create ZIP file
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for filename, img_bytes in county_images.items():
                    zip_file.writestr(filename, img_bytes)
            
            zip_buf.seek(0)
            
            # Download ZIP
            label_text = f"⬇️ Download All {len(county_images)} County {'Images' if output_mode == 'Complete Images (with template & text)' else 'Maps'} (ZIP)"
            st.download_button(
                label=label_text,
                data=zip_buf.getvalue(),
                file_name=f"{state_name}_county_{'images' if output_mode == 'Complete Images (with template & text)' else 'maps'}.zip",
                mime="application/zip",
                key="download_county_zip"
            )
            
            # Text-sizing debug info: shows exactly which county names got
            # shrunk, from what width, and to what final font size, so
            # text_max_width can be tuned precisely instead of guessed.
            shrink_log = st.session_state.get('shrink_debug', [])
            if shrink_log:
                shrunk_count = sum(1 for r in shrink_log if r['shrunk'])
                with st.expander(f"🔍 Text sizing debug ({shrunk_count}/{len(shrink_log)} shrunk)"):
                    st.caption(
                        f"Current threshold: text_max_width = {text_max_width}px, "
                        f"base font size = {font_size}, floor = {text_min_font_size}"
                    )
                    for row in shrink_log:
                        if row['original_width_px'] is None:
                            continue
                        status = "🔻 shrunk" if row['shrunk'] else "✅ fits as-is"
                        st.write(
                            f"**{row['label']}** — rendered width: {row['original_width_px']:.0f}px "
                            f"→ {status} (font size {row['base_font_size']} → {row['final_font_size']})"
                        )
            
            # Individual downloads
            with st.expander("Download Individual County Maps"):
                cols = st.columns(3)
                for idx, (filename, img_bytes) in enumerate(county_images.items()):
                    col = cols[idx % 3]
                    with col:
                        # Extract county name for display
                        county_display = filename.replace(f"{state_name}_", "").replace(".png", "").replace("_", " ")
                        st.download_button(
                            label=county_display,
                            data=img_bytes,
                            file_name=filename,
                            mime="image/png",
                            key=f"download_county_{idx}"
                        )
        else:
            st.info("Configure settings and click 'Generate County Maps' to create maps.")
            st.markdown("""
                ### If new shapefiles are needed, how to get county shapefiles:
                1. Visit the [US Census Bureau TIGER/Line Shapefiles](https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html)
                2. Download the county shapefile (e.g., `cb_2020_us_county_20m.zip`)
                3. Upload the ZIP file here
                
                The generator will create individual maps for each county in your selected state, 
                with each county highlighted in turn.
                """)
    
with tab5:
    st.header("🇺🇸 State Map Generator")
    st.write("Generate one image per US state (all 50 + DC) from a folder of pre-made state images, optionally composited onto your template with text.")

    col1, col2 = st.columns([1, 1])

    with col1:
        st.subheader("Settings")

        def normalize_key(s):
            """Lowercase and strip everything but letters/digits, so
                'New York.png', 'new_york.PNG', and 'NEW-YORK.jpg' all match."""
            return "".join(ch for ch in s.lower() if ch.isalnum())

        # Check for a local folder of pre-made per-state images first
        local_state_images_dir = 'state_images'
        use_local_state_images = os.path.isdir(local_state_images_dir)

        state_images_zip = None
        if use_local_state_images:
            st.success(f"✅ Using included state images folder ('{local_state_images_dir}')")
        else:
            state_images_zip = st.file_uploader(
                "Upload State Images (ZIP)",
                type=['zip'],
                help="No 'state_images' folder found alongside the app. Upload a ZIP file containing one image per state, named by state name or abbreviation (e.g. 'Alabama.png' or 'AL.png').",
                key="state_images_zip_uploader"
            )

        # Output mode selection
        state_output_mode = st.radio(
            "Output Mode:",
            options=["Maps Only", "Complete Images (with template & text)"],
            key="state_output_mode",
            help="Maps Only: Just the pre-made state images, packaged for download. Complete Images: Combines each state image with your template and adds '[State] / voices needed!' text."
        )

        # Recolor option
        recolor_states = st.checkbox(
            "Recolor state images", value=False, key="recolor_states_checkbox",
            help="Replaces the color of the state shape with a custom color, while keeping its transparent background and edge anti-aliasing intact."
        )
        state_recolor_color = None
        if recolor_states:
            state_recolor_color = st.color_picker("State Image Color", "#FFFFFF", key="state_recolor_color")
            state_recolor_rgb = tuple(int(state_recolor_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))

        # Show template options if Complete Images is selected
        use_state_template = state_output_mode == "Complete Images (with template & text)"

        if use_state_template and not (template_bytes and font_bytes):
            st.warning("⚠️ Please upload a template image and font in the sidebar to use Complete Images mode.")

        state_second_line = "voices needed!"
        state_one_line = False
        if use_state_template:
            state_second_line = st.text_input(
                "Second Line Text", value="voices needed!", key="state_second_line_text",
                help="Static text drawn below the state name, in the \"Remaining Lines Color\"."
            )
            state_one_line = st.checkbox(
                "Combine onto one line", value=False, key="state_one_line",
                help="Draws the state name and the second line text side by side on one row instead of stacked on two. Colors are preserved either way."
            )

        # ------------------------------------------------------------------
        # Preview: let the user check one state's Complete Image (or map)
        # before committing to generating all 50 + DC. Reuses
        # composite_complete_image so the preview matches the batch exactly.
        # ------------------------------------------------------------------
        st.divider()
        st.subheader("Preview")
        st.caption("Check one state's image before generating the full batch.")

        preview_can_load_states = use_local_state_images or state_images_zip is not None

        if st.button("Load State Images for Preview", key="load_preview_states", disabled=not preview_can_load_states):
            try:
                _image_lookup_preview = {}
                if use_local_state_images:
                    for fname in sorted(os.listdir(local_state_images_dir)):
                        fpath = os.path.join(local_state_images_dir, fname)
                        if not os.path.isfile(fpath):
                            continue
                        stem, ext = os.path.splitext(fname)
                        if ext.lower() not in ('.png', '.jpg', '.jpeg'):
                            continue
                        with open(fpath, 'rb') as f:
                            _image_lookup_preview[normalize_key(stem)] = f.read()
                else:
                    with zipfile.ZipFile(state_images_zip) as zf:
                        for info in zf.infolist():
                            if info.is_dir():
                                continue
                            fname = os.path.basename(info.filename)
                            stem, ext = os.path.splitext(fname)
                            if ext.lower() not in ('.png', '.jpg', '.jpeg'):
                                continue
                            _image_lookup_preview[normalize_key(stem)] = zf.read(info)
                st.session_state['preview_state_image_lookup'] = _image_lookup_preview
                st.session_state.pop('state_preview_img', None)
            except Exception as e:
                st.error(f"Error loading state images for preview: {str(e)}")

        if 'preview_state_image_lookup' in st.session_state:
            _lookup = st.session_state['preview_state_image_lookup']
            available_preview_states = [
                us_states[ab] for ab in us_states
                if _lookup.get(normalize_key(us_states[ab])) is not None or _lookup.get(normalize_key(ab)) is not None
            ]
            if not available_preview_states:
                st.warning("No matching state images found for preview.")
            else:
                preview_state_choice = st.selectbox("State to preview:", available_preview_states, key="preview_state_select")

                preview_needs_template = use_state_template and not (template and font)
                if preview_needs_template:
                    st.caption("⚠️ Complete Images mode needs a template + font; previewing the map only.")

                if st.button("Generate Preview", key="generate_state_preview"):
                    with st.spinner(f"Rendering preview for {preview_state_choice}..."):
                        preview_abbrev = [ab for ab, name in us_states.items() if name == preview_state_choice][0]
                        match_bytes = _lookup.get(normalize_key(preview_state_choice)) or _lookup.get(normalize_key(preview_abbrev))
                        state_map_img_preview = Image.open(io.BytesIO(match_bytes)).convert("RGBA")

                        if recolor_states:
                            state_map_img_preview = recolor_visible_pixels(state_map_img_preview, state_recolor_rgb)

                        if use_state_template and template and font:
                            preview_canvas = composite_complete_image(
                                template, font, state_map_img_preview, preview_state_choice, state_second_line,
                                overlay_x, overlay_y, overlay_max_w, overlay_max_h,
                                overlay_zone_max_w, overlay_zone_max_h,
                                one_line=state_one_line,
                                oversized=(preview_abbrev == 'AK')
                            )
                            st.session_state['state_preview_img'] = preview_canvas
                        else:
                            st.session_state['state_preview_img'] = state_map_img_preview
                        st.session_state['state_preview_label'] = preview_state_choice
        else:
            st.caption("Click 'Load State Images for Preview' above to pick a state to check.")

        # Generate button
        can_generate_states = use_local_state_images or state_images_zip is not None

        if use_state_template:
            can_generate_states = can_generate_states and template_bytes and font_bytes

        if st.button("🗺️ Generate All State Maps", type="primary", key="generate_states", disabled=not can_generate_states):
            with st.spinner("Loading state images and generating outputs..."):
                try:
                    # Build a lookup of available images: normalized filename (no
                    # extension) -> raw image bytes
                    image_lookup = {}

                    if use_local_state_images:
                        for fname in sorted(os.listdir(local_state_images_dir)):
                            fpath = os.path.join(local_state_images_dir, fname)
                            if not os.path.isfile(fpath):
                                continue
                            stem, ext = os.path.splitext(fname)
                            if ext.lower() not in ('.png', '.jpg', '.jpeg'):
                                continue
                            with open(fpath, 'rb') as f:
                                image_lookup[normalize_key(stem)] = f.read()
                    else:
                        with zipfile.ZipFile(state_images_zip) as zf:
                            for info in zf.infolist():
                                if info.is_dir():
                                    continue
                                fname = os.path.basename(info.filename)
                                stem, ext = os.path.splitext(fname)
                                if ext.lower() not in ('.png', '.jpg', '.jpeg'):
                                    continue
                                image_lookup[normalize_key(stem)] = zf.read(info)

                    if not image_lookup:
                        st.error("No image files found. Please check the folder/ZIP.")
                    else:
                        state_images = {}
                        missing_states = []
                        st.session_state['shrink_debug'] = []  # reset debug log for this batch
                        progress_bar = st.progress(0)

                        all_abbrevs = list(us_states.keys())
                        total_states = len(all_abbrevs)

                        for idx, abbrev in enumerate(all_abbrevs):
                            state_full_name = us_states[abbrev]

                            # Match by full state name first, then abbreviation
                            match_bytes = image_lookup.get(normalize_key(state_full_name))
                            if match_bytes is None:
                                match_bytes = image_lookup.get(normalize_key(abbrev))

                            if match_bytes is None:
                                missing_states.append(state_full_name)
                                progress_bar.progress((idx + 1) / total_states)
                                continue

                            state_map_img = Image.open(io.BytesIO(match_bytes)).convert("RGBA")

                            if recolor_states:
                                state_map_img = recolor_visible_pixels(state_map_img, state_recolor_rgb)

                            # If using template mode, combine with template and text
                            if use_state_template:
                                # State name (drawn in the highlight color)
                                # plus the user-editable second line (drawn
                                # in the secondary color) — stacked on two
                                # lines or combined onto one, per
                                # "Combine onto one line" — matches the
                                # July 2026 template.
                                canvas = composite_complete_image(
                                    template, font, state_map_img, state_full_name, state_second_line,
                                    overlay_x, overlay_y, overlay_max_w, overlay_max_h,
                                    overlay_zone_max_w, overlay_zone_max_h,
                                    one_line=state_one_line,
                                    oversized=(abbrev == 'AK')
                                )

                                draw_dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
                                get_render_font(draw_dummy, _combine_two_part_text(state_full_name, state_second_line, state_one_line), debug_label=state_full_name)

                                # Save the complete image, capped to ~490KB since
                                # this is the full template-composited output
                                final_buf = save_optimized_png(canvas)
                                out_bytes = final_buf.getvalue()
                            else:
                                # Maps Only: just repackage the source image as PNG,
                                # untouched — no size cap needed here
                                out_buf = io.BytesIO()
                                state_map_img.save(out_buf, format='PNG')
                                out_buf.seek(0)
                                out_bytes = out_buf.getvalue()

                            # Clean filename
                            safe_state_name = "".join(c if c.isalnum() else "_" for c in state_full_name)
                            filename = f"{safe_state_name}.png"

                            state_images[filename] = out_bytes

                            # Update progress
                            progress_bar.progress((idx + 1) / total_states)

                        st.session_state['state_images'] = state_images
                        st.session_state['state_mode_saved'] = state_output_mode

                        if state_images:
                            st.success(f"✅ Generated {len(state_images)} state {'images' if use_state_template else 'maps'}!")
                        if missing_states:
                            st.warning(
                                f"⚠️ No image found for: {', '.join(missing_states)}. "
                                "Filenames should match the state name or abbreviation "
                                "(e.g. 'Alabama.png' or 'AL.png')."
                            )

                except Exception as e:
                    st.error(f"Error processing state images: {str(e)}")

    with col2:
        st.subheader("Results")

        # Single-state preview, shown above the batch results
        if 'state_preview_img' in st.session_state:
            st.image(
                st.session_state['state_preview_img'],
                caption=f"Preview: {st.session_state.get('state_preview_label', '')}",
                use_container_width=True
            )
            st.divider()

        if 'state_images' in st.session_state and st.session_state['state_images']:
            state_images = st.session_state['state_images']
            state_output_mode_saved = st.session_state.get('state_mode_saved', 'Maps Only')

            # Preview first state
            first_image = list(state_images.values())[0]
            first_filename = list(state_images.keys())[0]
            st.image(first_image, caption=f"Preview: {first_filename}", use_container_width=True)

            # Create ZIP file
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for filename, img_bytes in state_images.items():
                    zip_file.writestr(filename, img_bytes)

            zip_buf.seek(0)

            # Download ZIP
            label_text = f"⬇️ Download All {len(state_images)} State {'Images' if state_output_mode_saved == 'Complete Images (with template & text)' else 'Maps'} (ZIP)"
            st.download_button(
                label=label_text,
                data=zip_buf.getvalue(),
                file_name=f"us_state_{'images' if state_output_mode_saved == 'Complete Images (with template & text)' else 'maps'}.zip",
                mime="application/zip",
                key="download_state_zip"
            )

            # Text-sizing debug info (shared with the County Map Generator's log)
            shrink_log = st.session_state.get('shrink_debug', [])
            if shrink_log:
                shrunk_count = sum(1 for r in shrink_log if r['shrunk'])
                with st.expander(f"🔍 Text sizing debug ({shrunk_count}/{len(shrink_log)} shrunk)"):
                    st.caption(
                        f"Current threshold: text_max_width = {text_max_width}px, "
                        f"base font size = {font_size}, floor = {text_min_font_size}"
                    )
                    for row in shrink_log:
                        if row['original_width_px'] is None:
                            continue
                        status = "🔻 shrunk" if row['shrunk'] else "✅ fits as-is"
                        st.write(
                            f"**{row['label']}** — rendered width: {row['original_width_px']:.0f}px "
                            f"→ {status} (font size {row['base_font_size']} → {row['final_font_size']})"
                        )

            # Individual downloads
            with st.expander("Download Individual State Maps"):
                cols = st.columns(3)
                for idx, (filename, img_bytes) in enumerate(state_images.items()):
                    col = cols[idx % 3]
                    with col:
                        state_display = filename.replace("template_", "").replace(".png", "").replace("_", " ")
                        st.download_button(
                            label=state_display,
                            data=img_bytes,
                            file_name=filename,
                            mime="image/png",
                            key=f"download_state_{idx}"
                        )
        else:
            st.info("Configure settings and click 'Generate All State Maps' to create maps.")
            st.markdown("""
                ### How this works:
                1. If you need new maps, place a folder named `state_images` next to the app, containing
                   one image per state (PNG or JPG), named by state name or abbreviation
                   (e.g. `Alabama.png`, `Alaska.png`, ... or `AL.png`, `AK.png`, ...).
                   If that folder isn't found, you can upload a ZIP of the same images instead.
                2. Choose "Maps Only" to just repackage those images, or "Complete Images"
                   to composite each one onto your template with the "[State] residents
                   needed!" text.
                3. Click "Generate All State Maps" to process all 50 states + DC in one batch.
                4. Download all as ZIP or individually.

                Any state without a matching image file is skipped and listed in a warning
                so you can spot missing/misnamed files easily.
                """)

with tab1:
    st.header("Single Image Generator")

    if not (template and font):
        st.info("👈 Upload a template image and font file in the sidebar to use this tab.")
    else:
        col1, col2 = st.columns([1, 1])

        with col1:
            # Text input
            custom_text = st.text_area(
                "Enter your text (use line breaks for multiple lines):",
                value="Your text here\ngoes on multiple lines!",
                height=150
            )

            # Overlay selection
            selected_overlay = None
            if overlays_dict:
                overlay_choice = st.selectbox("Select overlay image (optional):",
                                             ["None"] + list(overlays_dict.keys()))
                if overlay_choice != "None":
                    selected_overlay = overlays_dict[overlay_choice]

            # Generate button
            if st.button("🎨 Generate Preview", type="primary"):
                preview_img = generate_image(template, selected_overlay, custom_text, font)
                st.session_state['preview_img'] = preview_img

        with col2:
            # Display preview
            if 'preview_img' in st.session_state:
                st.image(st.session_state['preview_img'], caption="Preview", use_container_width=True)

                # Download button — full template composite, so cap to ~490KB
                buf = save_optimized_png(st.session_state['preview_img'])

                st.download_button(
                    label="⬇️ Download Image",
                    data=buf.getvalue(),
                    file_name="generated_image.png",
                    mime="image/png"
                )

with tab2:
    st.header("Batch Generator")
    st.write("Generate multiple images at once with different text/overlay combinations.")

    if not (template and font):
        st.info("👈 Upload a template image and font file in the sidebar to use this tab.")
    else:
        # Input area for batch data
        st.subheader("Enter Image Data")
        st.write("Format: `filename | text line 1 | text line 2 | ... | overlay_image_name (optional)`")

        batch_input = st.text_area(
            "Enter one image per line:",
            value="""image1 | First line | Second line | overlay1.png
image2 | Different text | Another line
image3 | Just one line | overlay2.png""",
            height=200,
            help="Each line creates one image. Separate values with | symbol."
        )

        if st.button("🚀 Generate Batch Images", type="primary"):
            lines = [line.strip() for line in batch_input.split('\n') if line.strip()]

            if not lines:
                st.error("Please enter at least one line of data")
            else:
                progress_bar = st.progress(0)
                generated_images = {}

                for idx, line in enumerate(lines):
                    parts = [p.strip() for p in line.split('|')]

                    if len(parts) < 2:
                        st.warning(f"Skipping line {idx+1}: needs at least filename and one text part")
                        continue

                    filename = parts[0]

                    # Check if last part is an overlay reference
                    overlay = None
                    if parts[-1] in overlays_dict:
                        overlay = overlays_dict[parts[-1]]
                        text_parts = parts[1:-1]
                    else:
                        text_parts = parts[1:]

                    custom_text = '\n'.join(text_parts)

                    # Generate image
                    img = generate_image(template, overlay, custom_text, font)

                    # Convert to bytes — full template composite, so cap to ~490KB
                    buf = save_optimized_png(img)
                    generated_images[f"{filename}.png"] = buf.getvalue()

                    progress_bar.progress((idx + 1) / len(lines))

                st.success(f"✅ Generated {len(generated_images)} images!")

                # Create ZIP file
                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    for filename, img_bytes in generated_images.items():
                        zip_file.writestr(filename, img_bytes)

                zip_buf.seek(0)

                # Download ZIP
                st.download_button(
                    label="⬇️ Download All as ZIP",
                    data=zip_buf.getvalue(),
                    file_name="generated_images.zip",
                    mime="application/zip"
                )

                # Individual downloads
                with st.expander("Download Individual Images"):
                    cols = st.columns(4)
                    for idx, (filename, img_bytes) in enumerate(generated_images.items()):
                        col = cols[idx % 4]
                        with col:
                            st.download_button(
                                label=filename,
                                data=img_bytes,
                                file_name=filename,
                                mime="image/png",
                                key=f"download_{idx}"
                            )

if not (template and font):
    st.info("👈 Upload a template image and font file in the sidebar to unlock the Single Image, Batch, and \"Complete Images\" tabs. County/State Map Generator \"Maps Only\" mode works right away.")

with st.expander("ℹ️ How to Use"):
    st.markdown("""
    ### How to Use:
    
    **Single Image Mode:**
    1. Upload a template image and font file
    2. Optionally upload overlay images (logos, icons, shapes, etc.)
    3. Enter your custom text
    4. Select an overlay (optional)
    5. Click "Generate Preview"
    6. Download your image
    
    **Batch Mode:**
    1. Upload template, font, and overlays
    2. Enter image data in the format: `filename | text line 1 | text line 2 | overlay_name`
    3. Generate all images at once
    4. Download as a ZIP file
    
    **Background Remover:**
    1. Upload overlay images
    2. Select an image and pick the background color to remove
    3. Adjust threshold to fine-tune transparency
    4. Download or add to your overlay collection
    
    **County Map Generator:**
    1. County images are already available. If the images need to change, follow the steps below.
    2. Download county shapefiles from [US Census Bureau](https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html)
    3. Upload the ZIP file
    4. Select your state and customize colors
    5. Use the "Preview" section to check one county's image before running the full batch
    6. Generate individual maps for each county
    7. Download all as ZIP or individually
    
    **State Map Generator:**
    1. State images are already included and can change colors. If you need new images, follow steps 2-5.
    2. Place a folder named `state_images` next to the app with one pre-made image per state (named by state name or abbreviation), or upload a ZIP of the same
    3. Choose "Maps Only" or "Complete Images (with template & text)"
    4. Use the "Preview" section to check one state's image before running the full batch
    5. Click "Generate All State Maps" to process all 50 states + DC in one batch
    6. Download all as ZIP or individually
    
    ### Tips:
    - Use the configuration sliders to position text and overlays
    - Text supports multiple lines (use line breaks in the text area); the first line is drawn in the "First Line Color" and any remaining lines are drawn in the "Remaining Lines Color", matching the two-tone look of the July 2026 template
    - Overlay images are automatically resized to fit within max dimensions, with transparent padding auto-trimmed for consistent sizing
    - On the July 2026 template, the state/county shape sits in a fixed corner badge box (top-right) and text is left-aligned lower on the card — turn "Auto-center text horizontally" on if you're using an older, center-aligned template instead
    - In batch mode, overlay reference is optional
    - County maps are generated with transparent backgrounds for easy compositing
    - The county/state overlay stays in its fixed corner box; Alaska is scaled up 6x from that box's own center but capped by "Oversized-Shape Zone Max Width/Height" so it never overflows the canvas
    - The State Map Generator pulls one pre-made image per state from a local `state_images` folder (or an uploaded ZIP), matching files by state name or abbreviation, and processes all 50 states + DC in one batch
    - In the State Map Generator, "Recolor state images" swaps the shape's color for one you choose while preserving its transparent background and edge smoothing — handy if the source images are all one color (e.g. dark blue) and you want a different one
    - Full "Complete Images" downloads (template + overlay + text) are automatically size-optimized to stay under ~490KB per file; raw "Maps Only" exports are left untouched at full quality
    - Both the County and State Map Generators now have a "Preview" section so you can check one county/state's exact output — including text sizing and overlay placement — before committing to the full batch run
    """)
