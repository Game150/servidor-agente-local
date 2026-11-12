@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 goto ruta_no_disponible

set "PYTHON=%CD%\.venv\Scripts\python.exe"

if not exist "%PYTHON%" goto entorno_no_encontrado

echo Iniciando Servidor Agente Local...
echo API: http://127.0.0.1:8000
echo Para detenerlo, presiona Ctrl+C.
echo.

"%PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo El servidor se detuvo con codigo %EXIT_CODE%.
pause
popd
exit /b %EXIT_CODE%

:entorno_no_encontrado
echo No se encontro el entorno virtual del proyecto:
echo %PYTHON%
echo.
echo Crea el entorno e instala las dependencias con:
echo python -m venv .venv
echo .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
popd
exit /b 1

:ruta_no_disponible
echo No se pudo acceder a la carpeta del proyecto:
echo %~dp0
echo.
pause
exit /b 1
