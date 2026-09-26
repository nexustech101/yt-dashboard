@echo off
REM Double-click this file. First run sets up a virtual environment
REM and installs dependencies; every run after that just starts the app.
cd /d "%~dp0"

if not exist ".venv" (
    echo First run - setting up, this takes a minute...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo Could not find Python. Install Python 3.11+ from python.org, then run this again.
        pause
        exit /b 1
    )
    .venv\Scripts\pip install --upgrade pip
    .venv\Scripts\pip install .
)

.venv\Scripts\streamlit run app.py
pause