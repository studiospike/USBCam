@echo off
rem Builds android\app\build\usbcam.apk using only the installed SDK (no Gradle).
setlocal EnableDelayedExpansion
cd /d "%~dp0.."

set "SDK=%LOCALAPPDATA%\Android\Sdk"
if not exist "%SDK%" set "SDK=%ANDROID_HOME%"
set "BT=%SDK%\build-tools\36.0.0"
set "PLATFORM=%SDK%\platforms\android-36"
set "OUT=%CD%\android\app\build"
set "SRC=%CD%\android\app\src\main\java"

if not exist "%BT%\aapt2.exe" ( echo build-tools 36.0.0 not found & exit /b 1 )

rem --- locate a full JDK (javapath shims lack keytool/jar) ---
set "JAVAHOME="
for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "java -XshowSettings:properties -version 2>&1 | Select-String 'java.home =' | ForEach-Object { $_.Line.Split('=')[1].Trim() }"`) do set "JAVAHOME=%%a"
if "%JAVAHOME%"=="" ( echo Could not locate a JDK via java.home & exit /b 1 )
set "JAVAC=%JAVAHOME%\bin\javac.exe"
set "KEYTOOL=%JAVAHOME%\bin\keytool.exe"
set "JAR=%JAVAHOME%\bin\jar.exe"

echo [1/5] aapt2 compile+link...
if not exist "%OUT%" mkdir "%OUT%"
"%BT%\aapt2.exe" compile --dir "%CD%\android\app\src\main\res" -o "%OUT%\res.zip" || exit /b 1
"%BT%\aapt2.exe" link -o "%OUT%\usbcam.apk" -I "%PLATFORM%\android.jar" --manifest "%CD%\android\app\src\main\AndroidManifest.xml" --min-sdk-version 26 --target-sdk-version 34 "%OUT%\res.zip" || exit /b 1

echo [2/5] javac...
if exist "%OUT%\classes" rmdir /s /q "%OUT%\classes"
mkdir "%OUT%\classes"
rem Quote every path and use forward slashes: the project folder may contain
rem spaces, and javac treats backslashes in @argfiles as escapes.
> "%OUT%\sources.txt" (
  for /f "delims=" %%f in ('dir /s /b "%SRC%\*.java"') do (
    set "P=%%f"
    set "P=!P:\=/!"
    echo "!P!"
  )
)
"%JAVAC%" -source 17 -target 17 -classpath "%PLATFORM%\android.jar" -d "%OUT%\classes" @"%OUT%\sources.txt" || exit /b 1

echo [3/5] d8...
"%JAR%" cf "%OUT%\classes.jar" -C "%OUT%\classes" . || exit /b 1
if exist "%BT%\d8.bat" (
  call "%BT%\d8.bat" --release --lib "%PLATFORM%\android.jar" --min-api 26 --output "%OUT%" "%OUT%\classes.jar" || exit /b 1
) else (
  "%JAVAHOME%\bin\java.exe" -cp "%BT%\lib\d8.jar" com.android.tools.r8.D8 --release --lib "%PLATFORM%\android.jar" --min-api 26 --output "%OUT%" "%OUT%\classes.jar" || exit /b 1
)

echo [4/5] package + zipalign...
cd /d "%OUT%"
"%BT%\aapt.exe" add usbcam.apk classes.dex || exit /b 1
"%BT%\zipalign.exe" -f 4 usbcam.apk usbcam-aligned.apk || exit /b 1
move /y usbcam-aligned.apk usbcam.apk >nul

echo [5/5] sign...
if not exist debug.keystore "%KEYTOOL%" -genkeypair -keystore debug.keystore -storepass android -keypass android -alias androiddebugkey -dname "CN=USBCam Debug" -keyalg RSA -keysize 2048 -validity 10000 || exit /b 1
call "%BT%\apksigner.bat" sign --ks debug.keystore --ks-pass pass:android --key-pass pass:android --out usbcam-signed.apk usbcam.apk || exit /b 1
move /y usbcam-signed.apk usbcam.apk >nul
echo APK ready: %OUT%\usbcam.apk
endlocal
