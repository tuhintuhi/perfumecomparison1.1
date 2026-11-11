#!/bin/bash
python3 -m venv venv && source venv/bin/activate
pip install flask requests beautifulsoup4 pywebview pyinstaller
pyinstaller --windowed --name PerfumeCompare desktop_app.py
echo "Done. Your app is: dist/PerfumeCompare.app"
