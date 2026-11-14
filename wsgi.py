# WSGI entry for PythonAnywhere (free Beginner, no card).
# PythonAnywhere Web tab points its WSGI file at this app:
#   from wsgi import application
# or set the WSGI configuration file to import from this repo.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import app as application
