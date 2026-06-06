@echo off
REM ============================================================
REM   FC Inventory Tool v3.0.0 - Production Build (Windows)
REM ============================================================
REM
REM   Refactored for the FastAPI rewrite (v3.0.0):
REM     - Entry point:   app/main.py  (was app.py)
REM     - Server:        uvicorn      (was waitress; no longer needed)
REM     - Config:        pyproject.toml (was requirements.txt)
REM     - Templates:     app/templates/ (was templates/)
REM     - Installer:     PyInstaller one-dir bundle + ZIP
REM
REM   Usage:  build.bat             (uses defaults)
REM           build.bat clean       (clean only)
REM           build.bat wheel      (build sdist + wheel only)
REM
REM   Requires:  Python 3.10+ on PATH
REM   Optional: uv (https://github.com/astral-sh/uv) for faster installs
REM ============================================================

setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

set "PROJECT_NAME=FCInventoryTool"
set "ENTRY_MODULE=app.main:app"
set "PYTHON_MIN=3.10"

REM ── arg parsing ────────────────────────────────────────────
set "MODE=exe"
if /I "%~1"=="clean" set "MODE=clean"
if /I "%~1"=="wheel" set "MODE=wheel"
if /I "%~1"=="sdist" set "MODE=sdist"

echo ============================================
echo   FC Inventory v3.0.0 - %MODE% build
echo ============================================
echo.

REM ── Python check ──────────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not on PATH. Install 3.10+ from https://python.org
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PY_VERSION=%%v"
echo [OK] Found Python !PY_VERSION!

REM ── Pick uv if available, else pip ────────────────────────
set "INSTALLER=pip"
where uv >nul 2>&1 && set "INSTALLER=uv"

echo [installer] Using !INSTALLER!
echo.

REM ── Clean step ────────────────────────────────────────────
echo [1/3] Cleaning previous build artefacts...
for %%d in (build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache __pycache__) do (
    if exist "%%d" rmdir /s /q "%%d" 2>nul
)
if exist "%PROJECT_NAME%.spec" del /f /q "%PROJECT_NAME%.spec"
if exist "fc-inventory.spec"   del /f /q "fc-inventory.spec"
echo.

REM ── Install deps ──────────────────────────────────────────
echo [2/3] Installing dependencies...
if /I "!INSTALLER!"=="uv" (
    uv sync --all-extras --dev
) else (
    python -m pip install --upgrade pip
    python -m pip install -e ".[dev]"
    python -m pip install pyinstaller
)
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    exit /b 1
)
echo.

REM ── Branch on mode ────────────────────────────────────────
if /I "%MODE%"=="clean" (
    echo [clean] Done.
    exit /b 0
)

if /I "%MODE%"=="wheel" goto :do_wheel
if /I "%MODE%"=="sdist" goto :do_sdist

REM ── PyInstaller .exe (default) ────────────────────────────
:do_exe
echo [3/3] Building %PROJECT_NAME%.exe (PyInstaller one-dir)...

REM Build the .spec via PyInstaller (lets PyInstaller auto-resolve imports).
pyinstaller --noconfirm --clean --onedir --name "%PROJECT_NAME%" ^
    --add-data "app/templates;app/templates" ^
    --add-data "static;static" ^
    --add-data "CHANGELOG.md;." ^
    --add-data "LICENSE;." ^
    --add-data "README.md;." ^
    --collect-submodules uvicorn ^
    --collect-submodules httpx ^
    --collect-submodules fastapi ^
    --collect-submodules pydantic ^
    --collect-submodules structlog ^
    --hidden-import uvicorn ^
    --hidden-import uvicorn.lifespan ^
    --hidden-import uvicorn.loops ^
    --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.protocols ^
    --hidden-import uvicorn.protocols.http ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.protocols.websockets ^
    --hidden-import anyio ^
    --hidden-import anyio._backends ^
    --hidden-import anyio._backends._asyncio ^
    --hidden-import watchfiles ^
    app/__main__.py

if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    exit /b 1
)

REM ── Zip the bundle (mirrors the GH Actions artefact) ──────
if not exist dist mkdir dist
echo.
echo   Packaging dist\%PROJECT_NAME% into a zip...
powershell -NoLogo -NoProfile -Command ^
  "Compress-Archive -Path 'dist\%PROJECT_NAME%' -DestinationPath 'dist\%PROJECT_NAME%-v3.0.0-windows.zip' -Force"
if errorlevel 1 (
    echo [WARN] Compress-Archive failed; the .exe is still at dist\%PROJECT_NAME%\%PROJECT_NAME%.exe
) else (
    echo   Created: dist\%PROJECT_NAME%-v3.0.0-windows.zip
)

echo.
echo ============================================
echo   Build complete!
echo.
echo   Output dir:   dist\%PROJECT_NAME%\
echo   Run:          dist\%PROJECT_NAME%\%PROJECT_NAME%.exe
echo   Zip:          dist\%PROJECT_NAME%-v3.0.0-windows.zip
echo.
echo   First-run tip: set FC_INVENTORY_BIND=0.0.0.0 to expose on the LAN.
echo ============================================
exit /b 0

REM ── Wheel build (sdist + wheel) ───────────────────────────
:do_wheel
echo [3/3] Building sdist + wheel...
if not exist dist mkdir dist
if /I "!INSTALLER!"=="uv" (
    uv build --out-dir dist
) else (
    python -m pip install --upgrade build
    python -m build --outdir dist
)
if errorlevel 1 (
    echo [ERROR] Wheel build failed.
    exit /b 1
)
echo.
echo   Built: dist\fc_inventory-3.0.0-py3-none-any.whl
echo         dist\fc_inventory-3.0.0.tar.gz
exit /b 0

REM ── sdist build only ──────────────────────────────────────
:do_sdist
echo [3/3] Building sdist only...
if not exist dist mkdir dist
if /I "!INSTALLER!"=="uv" (
    uv build --sdist --out-dir dist
) else (
    python -m pip install --upgrade build
    python -m build --sdist --outdir dist
)
if errorlevel 1 (
    echo [ERROR] sdist build failed.
    exit /b 1
)
echo.
echo   Built: dist\fc_inventory-3.0.0.tar.gz
exit /b 0
