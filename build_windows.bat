@echo off
python -m venv venv
call venv\Scripts\activate
pip install flask requests beautifulsoup4 pywebview pyinstaller
pyinstaller --noconsole --onefile --name PerfumeCompare desktop_app.py
echo.
echo Done. Your app is: dist\PerfumeCompare.exe
pause
