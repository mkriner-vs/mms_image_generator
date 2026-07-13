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

# Page config
st.set_page_config(page_title="Custom Image Generator", layout="wide")

st.title("🎨 Custom Image Generator")
st.write("Generate custom images by combining a template with overlays and text.")

# File uploaders
st.sidebar.header("Upload Files")
template_file = st.sidebar.file_uploader("Upload Template Image", type=['png', 'jpg', 'jpeg'])
if template_file:
    _template_preview = Image.open(template_file)
    st.sidebar.caption(f"📐 Template size: {_template_preview.width} × {_template_preview.height} px",
                        help="Set text_max_width to roughly the width of the template minus 100-150px.")
    template_file.seek(0)  # reset so it can be read again later
font_file = st.sidebar.file_uploader("Upload Font File (.ttf)", type=['ttf'])
overlay_files = st.sidebar.file_uploader("Upload Overlay Images", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True)

# Configuration inputs
st.sidebar.header("Overlay Configuration")
overlay_x = st.sidebar.number_input("Overlay X Position", value=1301, help="Horizontal position of the overlay; + to move right, - to move left")
overlay_y = st.sidebar.number_input("Overlay Y Position", value=770, help="Vertical position of the overlay; + to move down, - to move up")
overlay_max_w = st.sidebar.number_input("Overlay Max Width", value=395, help="Maximum width the overlay can be")
overlay_max_h = st.sidebar.number_input("Overlay Max Height", value=650, help="Maximum height the overlay can be")
overlay_auto_trim = st.sidebar.checkbox("Auto-trim transparent padding on overlay", value=True,
                                         help="Crops each overlay to its visible content before fitting it into the box above. Fixes overlays looking smaller than others when the source image has extra transparent margin (common with state shape PNGs).")
overlay_bottom_limit = st.sidebar.number_input(
    "Overlay Bottom Limit Y", value=1420,
    help="The overlay will never extend below this vertical point. Set it to where any text/graphics baked into your template (e.g. 'Share your perspective today!') begins, so tall overlays never grow into it."
)

st.sidebar.header("Text Configuration")
font_size = st.sidebar.slider("Font Size", 10, 300, 215)
auto_center_text = st.sidebar.checkbox("Auto-center text horizontally", value=True,
                                        help="Recentres the text based on its actual rendered width, so it stays centered no matter how long the text is (e.g. 'Alabama' vs 'California').")
text_center_x = st.sidebar.number_input("Text Center X (used when auto-center is on)", value=1590,
                                         help="The horizontal point the text should be centered around.")
text_x = st.sidebar.number_input("Text X Position (used when auto-center is off)", value=391, help="Horizontal position of the text")
text_y = st.sidebar.number_input("Text Y Position", value=542, help="Vertical position of the text")
text_spacing = st.sidebar.slider("Line Spacing", 0, 50, 6)
text_align = st.sidebar.selectbox("Text Alignment", ["left", "center", "right"], index=1)
auto_shrink_text = st.sidebar.checkbox("Auto-shrink font to fit width", value=True,
                                        help="If a line of text (e.g. a long county name) would render wider than the max width below, the font size is automatically reduced until it fits.")
text_max_width = st.sidebar.number_input("Text Max Width (px)", value=2800,
                                          help="Maximum allowed rendered text width before auto-shrink kicks in. Set text_max_width to roughly the width of the template minus 100-150px.")
text_min_font_size = st.sidebar.number_input("Minimum Font Size (auto-shrink floor)", value=80,
                                              help="Font will never shrink below this size, even if the text still doesn't fully fit.")

# Color picker
text_color = st.sidebar.color_picker("Text Color", "#000000")
text_color_rgb = tuple(int(text_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))


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
    paste_x = box_x + (box_w - ow) // 2
    paste_y = box_y + (box_h - oh) // 2
    canvas.alpha_composite(overlay_resized, (paste_x, paste_y))


def compute_safe_overlay_box(draw, custom_text, font, fallback_y, box_h, bottom_limit=None, padding=20):
    """
    Returns (box_top, box_h) for the overlay, guaranteeing:
    - the overlay starts below the bottom of custom_text (top limit), so tall
      shapes never overlap text drawn above them
    - the overlay ends above bottom_limit, if provided (bottom limit), so tall
      shapes never grow into text/graphics baked into the template below them
    """
    if custom_text:
        render_font = get_render_font(draw, custom_text)
        bbox = draw.multiline_textbbox((0, text_y), custom_text, font=render_font,
                                        spacing=text_spacing, align=text_align)
        text_bottom = bbox[3]
    else:
        text_bottom = text_y

    box_top = max(fallback_y, text_bottom + padding)

    if bottom_limit is not None:
        available_h = bottom_limit - box_top - padding
        box_h = min(box_h, max(available_h, 0))

    return box_top, box_h


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
    """Draw custom_text, auto-centering horizontally around text_center_x when
    enabled, and auto-shrinking the font so long lines never overflow the canvas."""
    if not custom_text:
        return
    render_font = get_render_font(draw, custom_text, debug_label=debug_label)
    if auto_center_text:
        bbox = draw.multiline_textbbox((0, 0), custom_text, font=render_font,
                                        spacing=text_spacing, align=text_align)
        text_w = bbox[2] - bbox[0]
        draw_x = text_center_x - text_w / 2 - bbox[0]
    else:
        draw_x = text_x
    draw.multiline_text((draw_x, text_y), custom_text, font=render_font,
                         fill=text_color_rgb, spacing=text_spacing, align=text_align)


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
if template_file and font_file:
    # Load template
    template = Image.open(template_file).convert("RGBA")
    
    # Load font (keep raw bytes so we can rebuild it at different sizes for auto-shrink)
    font_bytes = font_file.getvalue()
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
    
    if overlay_files or st.session_state['saved_overlays']:
        total_overlays = len(overlays_dict)
        st.success(f"✅ Loaded template, font, and {total_overlays} overlay images")
    else:
        st.success(f"✅ Loaded template and font")
    
    # Tab interface
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Single Image Generator", "Batch Generator", "Background Remover", "County Map Generator", "State Map Generator"])
    
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
                help="Maps Only: Just county maps. Complete Images: Combines maps with template and adds '[County] residents needed!' text."
            )
            
            # DPI setting
            dpi = st.slider("Image Quality (DPI)", 100, 600, 300, step=50)
            
            # Show template options if Complete Images is selected
            use_template = output_mode == "Complete Images (with template & text)"
            
            if use_template and not (template_file and font_file):
                st.warning("⚠️ Please upload a template image and font in the sidebar to use Complete Images mode.")
            
            # Generate button
            can_generate = use_local_file or (county_shapefile_source == "upload" and 'county_shapefile' in locals() and county_shapefile is not None)
            
            if use_template:
                can_generate = can_generate and template_file and font_file
            
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
                                
                                # Create color column
                                state_counties_copy = state_counties.copy()
                                state_counties_copy['color'] = state_counties_copy['GEOID'].apply(
                                    lambda x: highlight_color if x == county_fips else base_color
                                )
                                
                                # Create plot
                                fig, ax = plt.subplots(figsize=(8, 6))
                                state_counties_copy.plot(
                                    ax=ax,
                                    color=state_counties_copy['color'],
                                    edgecolor=outline_color,
                                    linewidth=0.5
                                )
                                
                                # Remove axes
                                ax.set_axis_off()
                                
                                # Save to BytesIO
                                buf = io.BytesIO()
                                plt.savefig(
                                    buf,
                                    format='PNG',
                                    dpi=dpi,
                                    bbox_inches='tight',
                                    transparent=True,
                                    facecolor='none'
                                )
                                plt.close(fig)
                                buf.seek(0)
                                
                                # If using template mode, combine with template and text
                                if use_template:
                                    # Load the county map as an overlay
                                    county_map_img = Image.open(buf).convert("RGBA")
                                    
                                    # Generate the complete image
                                    canvas = template.copy()
                                    draw = ImageDraw.Draw(canvas)
                                    
                                    # Determine subdivision type based on state
                                    if selected_state == 'AK':
                                        subdivision = 'borough'
                                    elif selected_state == 'LA':
                                        subdivision = 'parish'
                                    else:
                                        subdivision = 'county'
                                    
                                    # Build the text first so we can measure it when
                                    # computing a safe overlay box (must not overlap
                                    # the text above, and must not grow into
                                    # template graphics/text below)
                                    text = f"{county_name} {subdivision} residents needed!"
                                    
                                    # Apply special scaling for Alaska (it's geographically huge)
                                    if selected_state == 'AK':
                                        # Alaska gets 6x the normal max dimensions
                                        box_w, box_h = overlay_max_w * 6, overlay_max_h * 6
                                    else:
                                        box_w, box_h = overlay_max_w, overlay_max_h
                                    
                                    # Constrain the box so tall/thin shapes never
                                    # overlap the text above or below them
                                    box_top, box_h = compute_safe_overlay_box(
                                        draw, text, font, overlay_y, box_h,
                                        bottom_limit=overlay_bottom_limit, padding=20
                                    )
                                    
                                    county_map_resized = fit_into(county_map_img, box_w, box_h)
                                    
                                    # Paste county map overlay, centered within its box
                                    paste_centered(canvas, county_map_resized, overlay_x, box_top, box_w, box_h)
                                    
                                    # Draw text
                                    draw_text(draw, text, font, debug_label=county_name)
                                    
                                    # Save the complete image
                                    final_buf = io.BytesIO()
                                    canvas.save(final_buf, format='PNG')
                                    final_buf.seek(0)
                                    buf = final_buf
                                
                                # Clean filename
                                safe_county_name = "".join(c if c.isalnum() else "_" for c in county_name)
                                if use_template:
                                    filename = f"template_{state_name}_{safe_county_name}.png"
                                else:
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
                ### How to get county shapefiles:
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
            local_state_images_dir = 'dark_blue_state_images'
            use_local_state_images = os.path.isdir(local_state_images_dir)

            state_images_zip = None
            if use_local_state_images:
                st.success(f"✅ Using included state images folder ('{local_state_images_dir}')")
            else:
                state_images_zip = st.file_uploader(
                    "Upload State Images (ZIP)",
                    type=['zip'],
                    help="No 'dark_blue_state_images' folder found alongside the app. Upload a ZIP file containing one image per state, named by state name or abbreviation (e.g. 'Alabama.png' or 'AL.png').",
                    key="state_images_zip_uploader"
                )

            # Output mode selection
            state_output_mode = st.radio(
                "Output Mode:",
                options=["Maps Only", "Complete Images (with template & text)"],
                key="state_output_mode",
                help="Maps Only: Just the pre-made state images, packaged for download. Complete Images: Combines each state image with your template and adds '[State] residents needed!' text."
            )

            # Recolor option
            recolor_states = st.checkbox(
                "Recolor state images", value=False, key="recolor_states_checkbox",
                help="Replaces the color of the state shape with a custom color, while keeping its transparent background and edge anti-aliasing intact."
            )
            state_recolor_color = None
            if recolor_states:
                state_recolor_color = st.color_picker("State Image Color", "#06204a", key="state_recolor_color")
                state_recolor_rgb = tuple(int(state_recolor_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))

            # Show template options if Complete Images is selected
            use_state_template = state_output_mode == "Complete Images (with template & text)"

            if use_state_template and not (template_file and font_file):
                st.warning("⚠️ Please upload a template image and font in the sidebar to use Complete Images mode.")

            # Generate button
            can_generate_states = use_local_state_images or state_images_zip is not None

            if use_state_template:
                can_generate_states = can_generate_states and template_file and font_file

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
                                    canvas = template.copy()
                                    draw = ImageDraw.Draw(canvas)

                                    text = f"{state_full_name} residents needed!"

                                    # Apply special scaling for Alaska (it's geographically huge)
                                    if abbrev == 'AK':
                                        box_w, box_h = overlay_max_w * 6, overlay_max_h * 6
                                    else:
                                        box_w, box_h = overlay_max_w, overlay_max_h

                                    # Constrain the box so tall/thin shapes never
                                    # overlap the text above or below them
                                    box_top, box_h = compute_safe_overlay_box(
                                        draw, text, font, overlay_y, box_h,
                                        bottom_limit=overlay_bottom_limit, padding=20
                                    )

                                    state_map_resized = fit_into(state_map_img, box_w, box_h)

                                    # Paste state image, centered within its box
                                    paste_centered(canvas, state_map_resized, overlay_x, box_top, box_w, box_h)

                                    # Draw text
                                    draw_text(draw, text, font, debug_label=state_full_name)

                                    # Save the complete image
                                    final_buf = io.BytesIO()
                                    canvas.save(final_buf, format='PNG')
                                    final_buf.seek(0)
                                    out_bytes = final_buf.getvalue()
                                else:
                                    # Maps Only: just repackage the source image as PNG
                                    out_buf = io.BytesIO()
                                    state_map_img.save(out_buf, format='PNG')
                                    out_buf.seek(0)
                                    out_bytes = out_buf.getvalue()

                                # Clean filename
                                safe_state_name = "".join(c if c.isalnum() else "_" for c in state_full_name)
                                if use_state_template:
                                    filename = f"template_{safe_state_name}.png"
                                else:
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
                1. Place a folder named `dark_blue_state_images` next to the app, containing
                   one image per state (PNG or JPG), named by state name or abbreviation
                   (e.g. `Alabama.png`, `Alaska.png`, ... or `AL.png`, `AK.png`, ...).
                   If that folder isn't found, you can upload a ZIP of the same images instead.
                2. Choose "Maps Only" to just repackage those images, or "Complete Images"
                   to composite each one onto your template with the "[State] residents
                   needed!" text.
                3. Click "Generate All State Maps" to process all 50 states + DC in one batch.
                4. Download everything as a ZIP, or grab individual states below.

                Any state without a matching image file is skipped and listed in a warning
                so you can spot missing/misnamed files easily.
                """)

    with tab1:
        st.header("Single Image Generator")
        
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
                
                # Download button
                buf = io.BytesIO()
                st.session_state['preview_img'].save(buf, format='PNG')
                buf.seek(0)
                
                st.download_button(
                    label="⬇️ Download Image",
                    data=buf.getvalue(),
                    file_name="generated_image.png",
                    mime="image/png"
                )
    
    with tab2:
        st.header("Batch Generator")
        st.write("Generate multiple images at once with different text/overlay combinations.")
        
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
                    
                    # Convert to bytes
                    buf = io.BytesIO()
                    img.save(buf, format='PNG')
                    buf.seek(0)
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

else:
    st.info("👈 Please upload a template image and font file in the sidebar to get started.")
    
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
    1. If new county outlines are needed, download county shapefiles from [US Census Bureau](https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html)
    2. Upload the ZIP file
    3. Select your state and customize colors
    4. Generate individual maps for each county
    5. Download all as ZIP or individually
    
    **State Map Generator:**
    1. Place a folder named `dark_blue_state_images` next to the app with one pre-made image per state (named by state name or abbreviation), or upload a ZIP of the same
    2. Choose "Maps Only" or "Complete Images (with template & text)"
    3. Click "Generate All State Maps" to process all 50 states + DC in one batch
    4. Download all as ZIP or individually
    
    ### Tips:
    - Use the configuration sliders to position text and overlays
    - Text supports multiple lines (use line breaks in the text area)
    - Overlay images are automatically resized to fit within max dimensions, with transparent padding auto-trimmed for consistent sizing
    - Text auto-centers based on its actual width so different-length labels line up the same way
    - In batch mode, overlay reference is optional
    - County maps are generated with transparent backgrounds for easy compositing
    - The county map overlay is automatically kept between the text above it and the "Overlay Bottom Limit Y" setting, so tall/thin state shapes (like Alabama) won't overlap either
    - The State Map Generator pulls one pre-made image per state from a local `dark_blue_state_images` folder (or an uploaded ZIP), matching files by state name or abbreviation, and processes all 50 states + DC in one batch
    - In the State Map Generator, "Recolor state images" swaps the shape's color for one you choose while preserving its transparent background and edge smoothing — handy if the source images are all one color (e.g. dark blue) and you want a different one
    """)
