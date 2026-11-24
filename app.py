"""The public demo on Vercel: the same invented data as scripts/run_demo.py.

Vercel's file system is read-only except /tmp, so the demo database is built
there when a server instance starts. Each instance starts from the same
invented data; approvals last only as long as that instance runs.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from modules.file_handler import FileHandler
from scripts import run_demo

# The deployed code is read-only and already holds every folder the app reads;
# the demo writes only its database, under /tmp.
FileHandler.ensure_directories = lambda self: None

work = Path(os.environ.get("DEMO_WORK_DIR") or tempfile.gettempdir()) / "omega-demo"
work.mkdir(parents=True, exist_ok=True)
run_demo.DEMO_DB = work / "demo.db"
run_demo.DEMO_CLIENTS = work / "demo_clients.xlsx"

settings = run_demo.demo_settings()
run_demo.seed(settings)

import main as dashboard

app = dashboard.create_dashboard_app(settings)
app.config["TRUSTED_HOSTS"] = [
    *app.config["TRUSTED_HOSTS"],
    "group-8-omega.vercel.app",
    ".vercel.app",
]
