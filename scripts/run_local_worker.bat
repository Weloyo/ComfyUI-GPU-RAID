@echo off
chcp 65001 >nul
rem GPU RAID: второй локальный инстанс ComfyUI как тестовый воркер (порт 8189).
rem Режим CPU (для тестов логики без нагрузки на GPU):  set EXTRA_ARGS=--cpu
rem Строка подключения для панели:
rem     gpuraid://devtoken@127.0.0.1:8189?tls=0^&name=local2

set "COMFY=D:\ComfyUI_windows_portable"
if not "%~1"=="" set "COMFY=%~1"

set "GPURAID_TOKEN=devtoken"
set "GPURAID_AUTH_STRICT=1"

echo Запускаю воркера на 127.0.0.1:8189 (токен: devtoken, строгий режим auth)...
rem --disable-auto-launch: воркеру браузер не нужен, а под токеном он открыл бы
rem окно с ответом {"error": "unauthorized"}
"%COMFY%\python_embeded\python.exe" -s "%COMFY%\ComfyUI\main.py" --windows-standalone-build --port 8189 --listen 127.0.0.1 --disable-auto-launch %EXTRA_ARGS%
pause
