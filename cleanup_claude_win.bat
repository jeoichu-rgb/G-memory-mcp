@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

echo =========================================
echo  Claude Code - cleanup old account traces
echo  First run: scan only. Press Y to delete.
echo =========================================
echo.

set "found=0"

echo -- 1. Account identity file --
if exist "%USERPROFILE%\.claude.json" (
    echo [FOUND] %USERPROFILE%\.claude.json
    set /a found+=1
) else (
    echo [SKIP]  .claude.json not found
)
echo.

echo -- 2. Claude data directories --
for %%D in (usage-data session-env debug file-history projects plans statsig todos) do (
    if exist "%USERPROFILE%\.claude\%%D" (
        echo [FOUND] .claude\%%D
        set /a found+=1
    ) else (
        echo [SKIP]  .claude\%%D not found
    )
)
for %%F in (history.jsonl credentials.json settings.json settings.local.json) do (
    if exist "%USERPROFILE%\.claude\%%F" (
        echo [FOUND] .claude\%%F
        set /a found+=1
    ) else (
        echo [SKIP]  .claude\%%F not found
    )
)
echo.

echo -- 3. Chrome IndexedDB (claude.ai login cache) --
set "chrome_base=%LOCALAPPDATA%\Google\Chrome\User Data"
set "idb_found=0"
if exist "%chrome_base%\Default\IndexedDB\https_claude.ai_0.indexeddb.leveldb" (
    echo [FOUND] Chrome Default - claude.ai IndexedDB
    set /a found+=1
    set /a idb_found+=1
)
for /d %%P in ("%chrome_base%\Profile *") do (
    if exist "%%P\IndexedDB\https_claude.ai_0.indexeddb.leveldb" (
        echo [FOUND] Chrome %%~nxP - claude.ai IndexedDB
        set /a found+=1
        set /a idb_found+=1
    )
)
if !idb_found!==0 echo [SKIP]  No claude.ai IndexedDB found
echo.

echo -- 4. VS Code Claude extension logs --
set "vscode_logs=%APPDATA%\Code\logs"
set "log_found=0"
if exist "%vscode_logs%" (
    for /d /r "%vscode_logs%" %%D in (*Anthropic.claude-code*) do (
        echo [FOUND] %%D
        set /a found+=1
        set /a log_found+=1
    )
)
if !log_found!==0 echo [SKIP]  No extension logs found
echo.

echo -- 5. VS Code Claude extension install --
set "ext_found=0"
for /d %%E in ("%USERPROFILE%\.vscode\extensions\anthropic.claude-code-*") do (
    echo [FOUND] %%~nxE
    set /a found+=1
    set /a ext_found+=1
)
if !ext_found!==0 echo [SKIP]  No extension install found
echo.

echo -- 6. Claude Desktop app data --
if exist "%APPDATA%\Claude" (
    echo [FOUND] %APPDATA%\Claude
    set /a found+=1
) else (
    echo [SKIP]  Claude Desktop data not found
)
if exist "%LOCALAPPDATA%\AnthropicClaude" (
    echo [FOUND] %LOCALAPPDATA%\AnthropicClaude
    set /a found+=1
) else (
    echo [SKIP]  AnthropicClaude cache not found
)
if exist "%LOCALAPPDATA%\claude-desktop" (
    echo [FOUND] %LOCALAPPDATA%\claude-desktop
    set /a found+=1
) else (
    echo [SKIP]  claude-desktop cache not found
)
echo.

echo -- 7. CLI cache --
if exist "%LOCALAPPDATA%\claude-cli-nodejs" (
    echo [FOUND] %LOCALAPPDATA%\claude-cli-nodejs
    set /a found+=1
) else (
    echo [SKIP]  CLI cache not found
)
echo.

echo -- 8. Temp files --
set "tmp_found=0"
for /d %%T in ("%TEMP%\claude-*") do (
    echo [FOUND] %%~nxT
    set /a found+=1
    set /a tmp_found+=1
)
if !tmp_found!==0 echo [SKIP]  No claude temp files
echo.

echo =========================================
echo  Scan done. Found !found! items to clean.
echo =========================================
echo.

if !found!==0 (
    echo Nothing to clean.
    pause
    exit /b 0
)

choice /c YN /m "Delete all found items? Y=yes N=cancel"
if errorlevel 2 (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
echo Deleting...
echo.

if exist "%USERPROFILE%\.claude.json" (
    del /f /q "%USERPROFILE%\.claude.json"
    echo [OK] .claude.json
)

for %%D in (usage-data session-env debug file-history projects plans statsig todos) do (
    if exist "%USERPROFILE%\.claude\%%D" (
        rmdir /s /q "%USERPROFILE%\.claude\%%D"
        echo [OK] .claude\%%D
    )
)

for %%F in (history.jsonl credentials.json settings.json settings.local.json) do (
    if exist "%USERPROFILE%\.claude\%%F" (
        del /f /q "%USERPROFILE%\.claude\%%F"
        echo [OK] .claude\%%F
    )
)

if exist "%chrome_base%\Default\IndexedDB\https_claude.ai_0.indexeddb.leveldb" (
    rmdir /s /q "%chrome_base%\Default\IndexedDB\https_claude.ai_0.indexeddb.leveldb"
    echo [OK] Chrome Default IndexedDB
)
for /d %%P in ("%chrome_base%\Profile *") do (
    if exist "%%P\IndexedDB\https_claude.ai_0.indexeddb.leveldb" (
        rmdir /s /q "%%P\IndexedDB\https_claude.ai_0.indexeddb.leveldb"
        echo [OK] Chrome %%~nxP IndexedDB
    )
)

if exist "%vscode_logs%" (
    for /d /r "%vscode_logs%" %%D in (*Anthropic.claude-code*) do (
        rmdir /s /q "%%D"
        echo [OK] VS Code log %%D
    )
)

for /d %%E in ("%USERPROFILE%\.vscode\extensions\anthropic.claude-code-*") do (
    rmdir /s /q "%%E"
    echo [OK] %%~nxE
)

if exist "%APPDATA%\Claude" (
    rmdir /s /q "%APPDATA%\Claude"
    echo [OK] Claude Desktop data
)
if exist "%LOCALAPPDATA%\AnthropicClaude" (
    rmdir /s /q "%LOCALAPPDATA%\AnthropicClaude"
    echo [OK] AnthropicClaude cache
)
if exist "%LOCALAPPDATA%\claude-desktop" (
    rmdir /s /q "%LOCALAPPDATA%\claude-desktop"
    echo [OK] claude-desktop cache
)

if exist "%LOCALAPPDATA%\claude-cli-nodejs" (
    rmdir /s /q "%LOCALAPPDATA%\claude-cli-nodejs"
    echo [OK] CLI cache
)

for /d %%T in ("%TEMP%\claude-*") do (
    rmdir /s /q "%%T"
    echo [OK] temp %%~nxT
)

echo.
echo =========================================
echo  Done!
echo =========================================

if exist "%USERPROFILE%\.claude" (
    echo.
    echo [INFO] .claude folder still exists, may have leftover files.
    echo        To fully remove: delete %USERPROFILE%\.claude manually.
)

echo.
pause
