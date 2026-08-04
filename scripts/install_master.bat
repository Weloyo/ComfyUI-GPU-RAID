@echo off
chcp 65001 >nul
rem GPU RAID: установка на мастера (junction в custom_nodes, без прав администратора)

set "SRC=%~dp0.."
set "COMFY=D:\ComfyUI_windows_portable"
if not "%~1"=="" set "COMFY=%~1"
set "DST=%COMFY%\ComfyUI\custom_nodes\comfyui-gpu-raid"

if not exist "%COMFY%\ComfyUI\custom_nodes" (
    echo [ошибка] Не найден %COMFY%\ComfyUI\custom_nodes
    echo Использование: install_master.bat [путь_к_ComfyUI_windows_portable]
    pause
    exit /b 1
)

if exist "%DST%" (
    echo Уже установлено: %DST%
) else (
    mklink /J "%DST%" "%SRC%"
    if errorlevel 1 (
        echo [ошибка] mklink не сработал
        pause
        exit /b 1
    )
    echo Установлено: %DST% -^> %SRC%
)

echo.
echo Перезапустите ComfyUI ^(run_nvidia_gpu.bat^) — появится вкладка GPU RAID.
pause
