#!/usr/bin/env python3
"""
Test script to verify PDF filler and input/output pipeline.
"""

import os
from main import find_input_pdf
from pdf_filler import fill_pdf_with_overlay

def test_pdf_filler():
    pdf_bytes, filename = find_input_pdf()
    if not pdf_bytes:
        print("❌ No template found in input/ folder")
        return False

    print(f"✓ Found template: {filename} ({len(pdf_bytes)} bytes)")

    test_data = [
        {
            "date": "1st September 2026",
            "ojt_timing": "9:30 AM to 7:30 PM",
            "department": "Computer Science Engineering",
            "designation": "GenAI - AI Engineer Trainee",
            "my_space": "•  Traced async event loop dispatching in WebSocket handler.\n•  Planned regression test checklist.",
            "tasks_carried_out": "•  Added context usage bar to PromptBox.tsx.\n•  Configured token calculation with tiktoken.",
            "key_learnings": "•  Understood SVG stroke-dasharray math for circular progress meters.",
            "tools_used": "•  React\n•  Tailwind CSS\n•  TypeScript",
            "special_achievements": "•  Verified zero-latency UI re-rendering under live streaming.",
        }
    ]

    try:
        filled_pdf = fill_pdf_with_overlay(pdf_bytes, test_data, journal_start_page=8)
        print(f"✓ PDF overlay merged successfully ({len(filled_pdf)} bytes)")
        
        os.makedirs("output", exist_ok=True)
        out_path = os.path.join("output", "test_verification.pdf")
        with open(out_path, "wb") as f:
            f.write(filled_pdf)
        print(f"✓ Saved verification PDF to {out_path}")
        return True
    except Exception as e:
        print(f"❌ Error during test: {e}")
        return False

if __name__ == "__main__":
    test_pdf_filler()
