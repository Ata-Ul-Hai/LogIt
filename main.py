import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

import PyPDF2
from dateutil.parser import parse as parse_date
from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from gemini_helper import format_date_ordinal, generate_all_days_progressive
from github_helper import (
    fetch_github_commits,
    fetch_github_readme,
    fetch_local_commits,
    fetch_local_readme,
    parse_repo_source,
)
from pdf_filler import fill_pdf_with_overlay, generate_preview_png

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(BASE_DIR, "input")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="OJL Book Maker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    if exc.status_code == 413:
        return JSONResponse(
            status_code=413,
            content={"error": "File too large. Maximum upload size is 50MB."}
        )
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


# In-memory task storage
tasks: dict = {}
task_files: dict = {}
task_timestamps: dict = {}

TASK_TTL_SECONDS = 7200  # 2 hours retention


def cleanup_old_tasks():
    now = time.time()
    stale = [tid for tid, ts in list(task_timestamps.items()) if now - ts > TASK_TTL_SECONDS]
    for tid in stale:
        path = task_files.pop(tid, None)
        if path and os.path.exists(path) and "output" not in path:
            try:
                os.unlink(path)
            except OSError:
                pass
        tasks.pop(tid, None)
        task_timestamps.pop(tid, None)


def get_working_days(start_dt: datetime, end_dt: datetime, skip_sundays: bool = True) -> list:
    """Return working days list (YYYY-MM-DD) between start and end, skipping Sundays."""
    days = []
    curr = start_dt.date()
    end_date = end_dt.date()
    while curr <= end_date:
        if skip_sundays and curr.weekday() == 6:
            curr += timedelta(days=1)
            continue
        days.append(curr.strftime("%Y-%m-%d"))
        curr += timedelta(days=1)
    return days


def find_input_pdf() -> tuple:
    """Look for an existing PDF inside the input/ folder."""
    if os.path.exists(INPUT_DIR):
        for fname in sorted(os.listdir(INPUT_DIR)):
            if fname.lower().endswith(".pdf") and not fname.startswith("."):
                fpath = os.path.join(INPUT_DIR, fname)
                with open(fpath, "rb") as f:
                    return f.read(), fname
    return None, None


# ---------------------------------------------------------------------------
# Background PDF Generation Task
# ---------------------------------------------------------------------------

def generate_pdf_background(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return

    try:
        task["status"] = "generating"
        task["progress"] = 10
        task["message"] = "Preparing journal pages & learner details..."

        daily_work = task["daily_work"]
        pdf_bytes = task["pdf_bytes"]
        ojt_timing = task["ojt_timing"]
        department = task["department"]
        designation = task["designation"]
        learner_details = task.get("learner_details", {})

        pages_data = []
        for i, item in enumerate(daily_work):
            pages_data.append({
                "date": item.get("date", ""),
                "ojt_timing": ojt_timing,
                "department": department,
                "designation": designation,
                "my_space": item.get("my_space", ""),
                "tasks_carried_out": item.get("tasks_carried_out", ""),
                "key_learnings": item.get("key_learnings", ""),
                "tools_used": item.get("tools_used", ""),
                "special_achievements": item.get("special_achievements", ""),
            })

        task["progress"] = 40
        task["message"] = "Overlaying Page 3 & Day 1-N on Page 8+ (Pages 1,2,4-7 untouched)..."

        # Generate filled PDF
        filled_pdf = fill_pdf_with_overlay(
            pdf_bytes,
            pages_data,
            learner_details=learner_details,
            journal_start_page=8
        )

        task["progress"] = 80
        task["message"] = "Saving completed PDF to output/ folder..."

        # Save to output/ folder
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_filename = f"final_ojt_journal_{timestamp_str}.pdf"
        out_path = os.path.join(OUTPUT_DIR, out_filename)
        with open(out_path, "wb") as f:
            f.write(filled_pdf)

        # Also write/update the canonical output/final_ojt_journal.pdf
        canonical_path = os.path.join(OUTPUT_DIR, "final_ojt_journal.pdf")
        with open(canonical_path, "wb") as f:
            f.write(filled_pdf)

        task_files[task_id] = out_path
        task["output_filename"] = out_filename
        task["status"] = "done"
        task["progress"] = 100
        task["message"] = "Journal created successfully in output/!"

    except Exception as e:
        task["status"] = "error"
        task["message"] = str(e)
        print(f"[Task {task_id}] Error: {traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------

class FinalizeRequest(BaseModel):
    task_id: str
    daily_work: list
    learner_details: dict = None


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/status/{task_id}")
async def get_status(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {
        "status": task.get("status", "unknown"),
        "progress": task.get("progress", 0),
        "message": task.get("message", ""),
        "output_filename": task.get("output_filename", ""),
    }


@app.get("/preview/{task_id}/{page_num}")
async def get_page_preview(task_id: str, page_num: int):
    """Return a real-time PNG preview of Page 3 or Page 8 for in-browser visual verification."""
    task = tasks.get(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})

    pdf_bytes = task.get("pdf_bytes")
    if not pdf_bytes:
        return JSONResponse(status_code=404, content={"error": "PDF template missing"})

    learner_details = task.get("learner_details", {})
    daily_work = task.get("daily_work", [])
    sample_day = daily_work[0] if daily_work else {}

    try:
        sample_page_data = {
            "date": sample_day.get("date", ""),
            "ojt_timing": task.get("ojt_timing", ""),
            "department": task.get("department", ""),
            "designation": task.get("designation", ""),
            "my_space": sample_day.get("my_space", ""),
            "tasks_carried_out": sample_day.get("tasks_carried_out", ""),
            "key_learnings": sample_day.get("key_learnings", ""),
            "tools_used": sample_day.get("tools_used", ""),
            "special_achievements": sample_day.get("special_achievements", ""),
        }
        png_bytes = generate_preview_png(
            pdf_bytes,
            page_number=page_num,
            learner_details=learner_details,
            sample_day_data=sample_page_data
        )
        return Response(content=png_bytes, media_type="image/png")
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Preview generation failed: {e}"})


@app.get("/export-backup/{task_id}")
async def export_backup(task_id: str):
    """Export complete task data as JSON for local backup and portability."""
    task = tasks.get(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})

    backup_data = {
        "export_date": datetime.now().isoformat(),
        "task_id": task_id,
        "ojt_timing": task.get("ojt_timing"),
        "department": task.get("department"),
        "designation": task.get("designation"),
        "learner_details": task.get("learner_details"),
        "working_days": task.get("working_days"),
        "daily_work": task.get("daily_work"),
    }
    return Response(
        content=json.dumps(backup_data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="ojt_journal_backup.json"'}
    )


@app.post("/import-backup")
async def import_backup(backup_file: UploadFile = File(...)):
    """Import a previously exported JSON backup file."""
    try:
        raw = await backup_file.read()
        data = json.loads(raw.decode("utf-8"))
        
        pdf_bytes, template_name = find_input_pdf()
        if not pdf_bytes:
            return JSONResponse(status_code=400, content={"error": "No template found in input/ folder to pair with backup."})

        task_id = str(uuid.uuid4())
        tasks[task_id] = {
            "status": "ready",
            "pdf_bytes": pdf_bytes,
            "working_days": data.get("working_days", []),
            "daily_work": data.get("daily_work", []),
            "ojt_timing": data.get("ojt_timing", "9:30 AM to 7:30 PM"),
            "department": data.get("department", "Computer Science Engineering"),
            "designation": data.get("designation", "GenAI - AI Engineer Trainee"),
            "learner_details": data.get("learner_details", {}),
            "progress": 0,
            "message": "Backup loaded successfully.",
        }
        task_timestamps[task_id] = time.time()

        return {
            "task_id": task_id,
            "total_days": len(data.get("daily_work", [])),
            "daily_work": data.get("daily_work", []),
            "learner_details": data.get("learner_details", {}),
        }
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": f"Failed to import backup: {e}"})


@app.get("/download/{task_id}")
async def download(task_id: str):
    path = task_files.get(task_id)
    if not path or not os.path.exists(path):
        canonical_path = os.path.join(OUTPUT_DIR, "final_ojt_journal.pdf")
        if os.path.exists(canonical_path):
            path = canonical_path
        else:
            return JSONResponse(status_code=404, content={"error": "PDF file not found."})

    return FileResponse(
        path,
        media_type="application/pdf",
        filename="final_ojt_journal.pdf",
    )


@app.post("/generate-from-github")
async def generate_from_github(
    github_url: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    ojt_timing: str = Form(...),
    department: str = Form(...),
    designation: str = Form(...),
    learner_name: str = Form(default="Ata Ul Hai"),
    registration_no: str = Form(default="251810700121"),
    program_name: str = Form(default="Integrated B.Tech in Computer Science Engineering (AI and ML) [2025-2029]"),
    semester: str = Form(default="3rd Sem"),
    location: str = Form(default="Polaris School of Technology, Bangalore, Karnataka"),
    industry_partner_name: str = Form(default="Futuretalks Services Private Limited"),
    phone_no: str = Form(default=""),
    email_id: str = Form(default=""),
    author_filter: str = Form(default=""),
    gemini_api_key: str = Form(default=""),
    github_token: str = Form(default=""),
    pdf_file: UploadFile = File(default=None),
):
    cleanup_old_tasks()

    try:
        start_dt = parse_date(start_date)
        end_dt = parse_date(end_date)
        if end_dt < start_dt:
            return JSONResponse(status_code=400, content={"error": "End date must be on or after Start date."})

        # Calculate working days skipping all Sundays
        working_days = get_working_days(start_dt, end_dt, skip_sundays=True)
        if not working_days:
            return JSONResponse(status_code=400, content={"error": "No working days found (all dates were Sundays or invalid range)."})

        # Get PDF Template
        pdf_bytes = None
        template_name = ""
        if pdf_file and pdf_file.filename:
            pdf_bytes = await pdf_file.read()
            template_name = pdf_file.filename
            input_dest = os.path.join(INPUT_DIR, pdf_file.filename)
            with open(input_dest, "wb") as f:
                f.write(pdf_bytes)
        else:
            pdf_bytes, template_name = find_input_pdf()

        if not pdf_bytes:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "No OJT PDF template found! Please upload your PDF in the form or place your empty template inside the 'input/' folder."
                }
            )

        try:
            reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
            page_count = len(reader.pages)
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": f"Invalid PDF template: {e}"})

        # Parse source (URL or local)
        source_type, path_or_owner, repo = parse_repo_source(github_url)
        token_to_use = github_token.strip() or os.environ.get("GITHUB_TOKEN")
        author_filter_clean = author_filter.strip() or None

        if source_type == "local":
            repo_name = repo
            readme = fetch_local_readme(path_or_owner)
            commits = fetch_local_commits(path_or_owner, author_filter=author_filter_clean)
        else:
            owner = path_or_owner
            repo_name = f"{owner}/{repo}"
            readme = fetch_github_readme(owner, repo, token=token_to_use)
            commits = fetch_github_commits(owner, repo, token=token_to_use, author_filter=author_filter_clean)

        api_key_to_use = gemini_api_key.strip() or os.environ.get("GEMINI_API_KEY")
        daily_entries = generate_all_days_progressive(api_key_to_use, repo_name, readme, commits, working_days)

        formatted_start_date = format_date_ordinal(start_date)
        learner_details = {
            "learner_name": learner_name.strip(),
            "registration_no": registration_no.strip(),
            "start_date": formatted_start_date,
            "program_name": program_name.strip(),
            "semester": semester.strip(),
            "location": location.strip(),
            "industry_partner_name": industry_partner_name.strip(),
            "phone_no": phone_no.strip(),
            "email_id": email_id.strip(),
        }

        task_id = str(uuid.uuid4())
        tasks[task_id] = {
            "status": "ready",
            "pdf_bytes": pdf_bytes,
            "working_days": working_days,
            "daily_work": daily_entries,
            "ojt_timing": ojt_timing,
            "department": department,
            "designation": designation,
            "learner_details": learner_details,
            "progress": 0,
            "message": "Ready to build PDF.",
        }
        task_timestamps[task_id] = time.time()

        return {
            "task_id": task_id,
            "repo_name": repo_name,
            "total_days": len(working_days),
            "template_name": template_name,
            "template_pages": page_count,
            "daily_work": daily_entries,
            "learner_details": learner_details,
        }

    except ValueError as ve:
        return JSONResponse(status_code=400, content={"error": str(ve)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e), "trace": traceback.format_exc()})


@app.post("/generate-pdf")
async def generate_pdf(req: FinalizeRequest):
    task = tasks.get(req.task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})

    task["daily_work"] = req.daily_work
    if req.learner_details:
        task["learner_details"] = req.learner_details
    task["status"] = "pending"
    task["progress"] = 0
    task["message"] = "Starting generation..."

    thread = threading.Thread(
        target=generate_pdf_background,
        args=(req.task_id,),
        daemon=True,
    )
    thread.start()

    return {"status": "started", "task_id": req.task_id}


# Mount static files
app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True), name="static")
