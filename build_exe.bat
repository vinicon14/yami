@echo off
setlocal
set "PYTHON=C:\Users\vinim\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

"%PYTHON%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --name YamiPortable ^
  yami_portable.py

if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)

if not exist data mkdir data
if not exist models mkdir models
if not exist bin mkdir bin
copy /Y dist\YamiPortable.exe YamiPortable.exe >nul
echo.
echo Built: YamiPortable.exe
pause
