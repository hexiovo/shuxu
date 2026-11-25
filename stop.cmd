@echo off
setlocal
py -3.12 -X utf8 -B "%~dp0shuxu\server.py" stop
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" pause
exit /b %RESULT%
