@echo off
REM Genera ReceptorVermicompost.exe a partir de receptor_vermicompost.py
REM Ejecutar con doble clic en la misma carpeta donde esta el .py

cd /d "%~dp0"
if not exist icono.ico (
  echo Falta icono.ico en esta carpeta.
  goto error
)
echo Instalando dependencias...
python -m pip install --upgrade paho-mqtt pyinstaller
if errorlevel 1 goto error

echo.
echo Construyendo el ejecutable (tarda unos minutos)...
python -m PyInstaller --noconfirm --onefile --windowed --name ReceptorVermicompost --icon icono.ico receptor_vermicompost.py
if errorlevel 1 goto error

echo.
echo Listo. El ejecutable quedo en: %~dp0dist\ReceptorVermicompost.exe
pause
exit /b 0

:error
echo.
echo Hubo un error. Revisa los mensajes de arriba.
pause
exit /b 1
