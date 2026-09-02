@echo off
chcp 65001 >nul
title نظام إدارة المبيعات
cd /d "%~dp0"

rem ===================================================================
rem  تشغيل نظام إدارة المبيعات في المتصفح — بضغطة واحدة، بلا أي تثبيت.
rem  اترك هذه النافذة مفتوحة أثناء العمل. إغلاقها يوقف البرنامج.
rem ===================================================================

set "PYEXE=%~dp0runtime\python-win-x64\python.exe"
set "PYFLAGS=-s"

if not exist "%PYEXE%" (
  rem بيئة التشغيل المضمّنة غير موجودة — نجرّب بايثون المثبّت على الجهاز
  where python >nul 2>&1 && set "PYEXE=python" && set "PYFLAGS="
)
if not exist "%PYEXE%" if not "%PYEXE%"=="python" (
  echo.
  echo [خطأ] لم يُعثر على بيئة التشغيل.
  echo المسار المتوقع: %~dp0runtime\python-win-x64\python.exe
  echo تأكد من فك ضغط المجلد بالكامل.
  echo.
  pause
  exit /b 1
)

rem قاعدة البيانات بجوار البرنامج. لقاعدة مشتركة على الشبكة غيّر السطر التالي:
rem set "SALES_DB=\\\\SERVER\\Shared\\Mabee3at\\sales.db"
if not defined SALES_DB set "SALES_DB=%~dp0data\sales.db"

set "MABEE3AT_OPEN_BROWSER=1"
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "PYTHONHOME="

echo.
echo   نظام إدارة المبيعات
echo   ---------------------------------------------
echo   جارٍ التشغيل... سيفتح المتصفح تلقائياً خلال ثوانٍ.
echo   اترك هذه النافذة مفتوحة أثناء العمل.
echo   للإيقاف: أغلق النافذة أو اضغط Ctrl+C
echo.

cd app
"%PYEXE%" %PYFLAGS% server.py

echo.
echo تم إيقاف البرنامج.
pause
