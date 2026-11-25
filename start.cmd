@echo off
setlocal
py -3.12 -X utf8 -B "%~dp0shuxu\server.py" start --open-browser
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" pause
exit /b %RESULT%
