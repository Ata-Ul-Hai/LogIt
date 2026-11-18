import io
import json
import os
import re
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas
import PyPDF2
import pypdfium2 as pdfium

A4_WIDTH, A4_HEIGHT = A4  # 595.27, 841.89 pts

# Exact coordinates tuned for the OJL journal page (A4 layout)
FIELD_COORDS = {
    "date":                 {"x": 121, "y": 748, "max_width": 150, "font_size": 11, "font_weight": "Helvetica-Bold"},
    "ojt_timing_start":     {"x": 384, "y": 748, "max_width": 80,  "font_size": 11, "font_weight": "Helvetica-Bold"},
    "ojt_timing_end":       {"x": 480, "y": 748, "max_width": 80,  "font_size": 11, "font_weight": "Helvetica-Bold"},
    "department":           {"x": 150, "y": 718, "max_width": 150, "font_size": 11, "font_weight": "Helvetica-Bold"},
    "designation":          {"x": 404, "y": 718, "max_width": 150, "font_size": 11, "font_weight": "Helvetica-Bold"},
    
    # 5 Main boxes - large font, comfortable line height, bullet points
    "my_space":             {"x": 52,  "y": 650, "max_width": 490, "font_size": 10.5, "line_height": 15, "max_lines": 5},
    "tasks_carried_out":    {"x": 52,  "y": 460, "max_width": 490, "font_size": 10.5, "line_height": 15, "max_lines": 6},
    "key_learnings":        {"x": 52,  "y": 300, "max_width": 490, "font_size": 10.5, "line_height": 14, "max_lines": 4},
    "tools_used":           {"x": 54,  "y": 172, "max_width": 240, "font_size": 10.0, "line_height": 14, "max_lines": 5},
    "special_achievements": {"x": 322, "y": 172, "max_width": 230, "font_size": 10.0, "line_height": 14, "max_lines": 5},
}

# Precise coordinates for Page 3 (Learner's Details)
LEARNER_COORDS = {
    "learner_name":          {"x": 165, "y": 283.0, "max_width": 330, "font_size": 10.5, "font": "Helvetica-Bold"},
    "registration_no":       {"x": 165, "y": 253.0, "max_width": 145, "font_size": 10.5, "font": "Helvetica-Bold"},
    "start_date":            {"x": 372, "y": 253.0, "max_width": 124, "font_size": 10.5, "font": "Helvetica-Bold"},
    "program_name":          {"x": 162, "y": 222.9, "max_width": 330, "font_size": 9.5,  "font": "Helvetica-Bold"},
    "semester":              {"x": 134, "y": 192.7, "max_width": 66,  "font_size": 10.0, "font": "Helvetica-Bold"},
    "location":              {"x": 254, "y": 192.7, "max_width": 240, "font_size": 9.5,  "font": "Helvetica-Bold"},
    "industry_partner_name": {"x": 196, "y": 162.6, "max_width": 300, "font_size": 10.0, "font": "Helvetica-Bold"},
    "phone_no":              {"x": 120, "y": 81.7,  "max_width": 130, "font_size": 9.5,  "font": "Helvetica"},
    "email_id":              {"x": 296, "y": 81.7,  "max_width": 160, "font_size": 9.5,  "font": "Helvetica"},
}


def parse_ojt_timing(timing_str: str) -> tuple:
    """Parse OJT timing string and return (start_time, end_time)."""
    if not timing_str:
        return "", ""
    parts = re.split(r"[–\-]|\bto\b", timing_str, flags=re.IGNORECASE)
    if len(parts) >= 2:
        return parts[0].strip(), parts[1].strip()
    return timing_str.strip(), ""


def format_bullet_text(text: str) -> list:
    """Format any incoming text or multiline string into a clean list of bullet points."""
    if not text:
        return []
    lines = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^[\s•\-*]+", "", line).strip()
        if line:
            lines.append(f"•  {line}")
    return lines


def _wrap_bullet_items(items: list, font_name: str, font_size: float, max_width: float, max_lines: int) -> list:
    """Wrap each bullet item to fit max_width; indent continuation lines neatly."""
    output_lines = []
    for item in items:
        if len(output_lines) >= max_lines:
            break
        wrapped = simpleSplit(item, font_name, font_size, max_width)
        for i, sub_line in enumerate(wrapped):
            if len(output_lines) >= max_lines:
                break
            if i > 0:
                output_lines.append(f"   {sub_line}")
            else:
                output_lines.append(sub_line)
    return output_lines


def _build_learner_details_overlay_page(c_canvas, learner_details: dict, page_width: float, page_height: float):
    """Draw all Page 3 fields onto the reportlab canvas with sub-pixel alignment."""
    if not learner_details:
        return

    scale_x = page_width / A4_WIDTH
    scale_y = page_height / A4_HEIGHT
    c_canvas.setFillColorRGB(0.1, 0.1, 0.1)

    for key, cfg in LEARNER_COORDS.items():
        val = learner_details.get(key, "")
        if not val:
            continue
        val_str = str(val).strip()
        font_name = cfg["font"]
        font_size = cfg["font_size"]
        max_width = cfg["max_width"] * scale_x
        x = cfg["x"] * scale_x
        y = cfg["y"] * scale_y

        c_canvas.setFont(font_name, font_size)
        wrapped = simpleSplit(val_str, font_name, font_size, max_width)
        c_canvas.drawString(x, y, wrapped[0] if wrapped else val_str)


def _build_journal_overlay_page(c_canvas, page_data: dict, page_width: float, page_height: float):
    """Draw all header and box fields onto the reportlab canvas for a single journal page."""
    scale_x = page_width / A4_WIDTH
    scale_y = page_height / A4_HEIGHT

    ojt_timing_str = page_data.get("ojt_timing", "")
    timing_start, timing_end = parse_ojt_timing(ojt_timing_str)

    # 1. Header fields (Single line, bold)
    header_fields = {
        "date": page_data.get("date", ""),
        "ojt_timing_start": timing_start,
        "ojt_timing_end": timing_end,
        "department": page_data.get("department", ""),
        "designation": page_data.get("designation", ""),
    }

    c_canvas.setFillColorRGB(0.1, 0.1, 0.1)

    for field_key, val in header_fields.items():
        if not val:
            continue
        cfg = FIELD_COORDS[field_key]
        x = cfg["x"] * scale_x
        y = cfg["y"] * scale_y
        max_width = cfg["max_width"] * scale_x
        font_size = cfg["font_size"]
        font_name = cfg.get("font_weight", "Helvetica-Bold")

        c_canvas.setFont(font_name, font_size)
        wrapped = simpleSplit(str(val), font_name, font_size, max_width)
        c_canvas.drawString(x, y, wrapped[0] if wrapped else str(val))

    # 2. Box fields (Large font, bullet points, strict line height)
    box_keys = ["my_space", "tasks_carried_out", "key_learnings", "tools_used", "special_achievements"]

    for box_key in box_keys:
        raw_val = page_data.get(box_key, "")
        if not raw_val:
            continue

        cfg = FIELD_COORDS[box_key]
        x = cfg["x"] * scale_x
        start_y = cfg["y"] * scale_y
        max_width = cfg["max_width"] * scale_x
        font_size = cfg["font_size"]
        line_height = cfg["line_height"] * scale_y
        max_lines = cfg["max_lines"]
        font_name = "Helvetica"

        bullets = format_bullet_text(raw_val)
        wrapped_lines = _wrap_bullet_items(bullets, font_name, font_size, max_width, max_lines)

        c_canvas.setFont(font_name, font_size)
        current_y = start_y
        for line in wrapped_lines:
            c_canvas.drawString(x, current_y, line)
            current_y -= line_height


def fill_pdf_with_overlay(
    pdf_bytes: bytes,
    pages_data: list,
    learner_details: dict = None,
    journal_start_page: int = 8,
) -> bytes:
    """
    Fill the PDF template with:
    - Learner details on Page 3 (index 2)
    - Untouched pages on 1, 2, 4, 5, 6, 7
    - Daily journal entries starting on Page 8 (Day 1)
    """
    original_reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
    num_template_pages = len(original_reader.pages)

    journal_start_idx = max(0, journal_start_page - 1)
    total_needed_pages = journal_start_idx + len(pages_data)
    num_output_pages = max(num_template_pages, total_needed_pages)

    ref_page = original_reader.pages[min(journal_start_idx, num_template_pages - 1)]
    page_width = float(ref_page.mediabox.width)
    page_height = float(ref_page.mediabox.height)

    # 1. Build transparent overlay PDF
    overlay_buffer = io.BytesIO()
    c = canvas.Canvas(overlay_buffer, pagesize=(page_width, page_height))

    for page_idx in range(num_output_pages):
        # Page 3 (0-indexed 2): Fill Learner Details
        if page_idx == 2 and learner_details:
            _build_learner_details_overlay_page(c, learner_details, page_width, page_height)

        # Page 8+ (0-indexed 7+): Fill Daily Journal Entries
        elif page_idx >= journal_start_idx:
            day_idx = page_idx - journal_start_idx
            if day_idx < len(pages_data):
                _build_journal_overlay_page(c, pages_data[day_idx], page_width, page_height)

        c.showPage()

    c.save()
    overlay_buffer.seek(0)
    overlay_reader = PyPDF2.PdfReader(overlay_buffer)

    # 2. Merge overlay with template pages
    writer = PyPDF2.PdfWriter()

    for page_idx in range(num_output_pages):
        if page_idx < num_template_pages:
            base_page = original_reader.pages[page_idx]
        else:
            extra_reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
            base_page = extra_reader.pages[min(journal_start_idx, num_template_pages - 1)]

        # Merge overlay if it contains drawn text
        if (page_idx == 2 and learner_details) or (page_idx >= journal_start_idx):
            if page_idx < len(overlay_reader.pages):
                base_page.merge_page(overlay_reader.pages[page_idx])

        writer.add_page(base_page)

    output_buffer = io.BytesIO()
    writer.write(output_buffer)
    output_buffer.seek(0)
    return output_buffer.read()


def generate_preview_png(pdf_bytes: bytes, page_number: int, learner_details: dict = None, sample_day_data: dict = None) -> bytes:
    """
    Generate an in-memory PNG image preview for a given page number (e.g. Page 3 or Page 8).
    """
    original_reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
    page_idx = page_number - 1
    if page_idx >= len(original_reader.pages):
        page_idx = len(original_reader.pages) - 1

    orig_page = original_reader.pages[page_idx]
    page_width = float(orig_page.mediabox.width)
    page_height = float(orig_page.mediabox.height)

    # Build overlay for this single page
    overlay_buf = io.BytesIO()
    c = canvas.Canvas(overlay_buf, pagesize=(page_width, page_height))

    if page_number == 3 and learner_details:
        _build_learner_details_overlay_page(c, learner_details, page_width, page_height)
    elif page_number >= 8 and sample_day_data:
        _build_journal_overlay_page(c, sample_day_data, page_width, page_height)

    c.showPage()
    c.save()
    overlay_buf.seek(0)

    # Merge
    ov_reader = PyPDF2.PdfReader(overlay_buf)
    merged_writer = PyPDF2.PdfWriter()
    orig_page.merge_page(ov_reader.pages[0])
    merged_writer.add_page(orig_page)

    merged_pdf_buf = io.BytesIO()
    merged_writer.write(merged_pdf_buf)
    merged_pdf_buf.seek(0)

    # Render with pypdfium2
    doc = pdfium.PdfDocument(merged_pdf_buf)
    img = doc[0].render(scale=2).to_pil()
    png_buf = io.BytesIO()
    img.save(png_buf, format="PNG")
    return png_buf.getvalue()
