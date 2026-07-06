@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  Claude Code 旧账号痕迹清理（Windows）
echo  第一次运行只看不删，确认后再真删
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo.

REM === 检测 Claude 进程 ===
tasklist /FI "IMAGENAME eq claude*" 2>nul | find /i "claude" >nul
if %errorlevel%==0 (
    echo [!] 检测到 Claude 进程还在跑，建议先退出：
    echo     右下角托盘 → 右键 Claude → 退出
    echo.
)

tasklist /FI "IMAGENAME eq Code.exe" 2>nul | find /i "Code.exe" >nul
if %errorlevel%==0 (
    echo [!] VS Code 还在运行，建议先关闭
    echo.
)

tasklist /FI "IMAGENAME eq chrome.exe" 2>nul | find /i "chrome.exe" >nul
if %errorlevel%==0 (
    echo [!] Chrome 还在运行，IndexedDB 可能删不干净
    echo     建议关掉 claude.ai 标签页或退出 Chrome
    echo.
)

echo ═══════════════════════════════════════════
echo  以下是将要清理的内容：
echo ═══════════════════════════════════════════
echo.

set "found=0"

REM --- 1. 账号身份文件 ---
echo -- 1. 账号身份文件 --
if exist "%USERPROFILE%\.claude.json" (
    echo [FOUND] %USERPROFILE%\.claude.json
    echo         内容：userID、设备指纹、账号UUID
    set /a found+=1
) else (
    echo [SKIP]  .claude.json 不存在
)
echo.

REM --- 2. Claude 数据目录 ---
echo -- 2. Claude 数据目录 --

if exist "%USERPROFILE%\.claude\usage-data" (
    echo [FOUND] %USERPROFILE%\.claude\usage-data\
    echo         内容：会话统计、token 用量
    set /a found+=1
) else echo [SKIP]  usage-data 不存在

if exist "%USERPROFILE%\.claude\session-env" (
    echo [FOUND] %USERPROFILE%\.claude\session-env\
    echo         内容：会话环境快照
    set /a found+=1
) else echo [SKIP]  session-env 不存在

if exist "%USERPROFILE%\.claude\debug" (
    echo [FOUND] %USERPROFILE%\.claude\debug\
    echo         内容：调试日志
    set /a found+=1
) else echo [SKIP]  debug 不存在

if exist "%USERPROFILE%\.claude\file-history" (
    echo [FOUND] %USERPROFILE%\.claude\file-history\
    echo         内容：文件编辑记录
    set /a found+=1
) else echo [SKIP]  file-history 不存在

if exist "%USERPROFILE%\.claude\projects" (
    echo [FOUND] %USERPROFILE%\.claude\projects\
    echo         内容：项目缓存
    set /a found+=1
) else echo [SKIP]  projects 不存在

if exist "%USERPROFILE%\.claude\plans" (
    echo [FOUND] %USERPROFILE%\.claude\plans\
    echo         内容：执行计划
    set /a found+=1
) else echo [SKIP]  plans 不存在

if exist "%USERPROFILE%\.claude\history.jsonl" (
    echo [FOUND] %USERPROFILE%\.claude\history.jsonl
    echo         内容：命令历史
    set /a found+=1
) else echo [SKIP]  history.jsonl 不存在

if exist "%USERPROFILE%\.claude\statsig" (
    echo [FOUND] %USERPROFILE%\.claude\statsig\
    echo         内容：分析数据
    set /a found+=1
) else echo [SKIP]  statsig 不存在

if exist "%USERPROFILE%\.claude\todos" (
    echo [FOUND] %USERPROFILE%\.claude\todos\
    echo         内容：待办事项
    set /a found+=1
) else echo [SKIP]  todos 不存在

if exist "%USERPROFILE%\.claude\credentials.json" (
    echo [FOUND] %USERPROFILE%\.claude\credentials.json
    echo         内容：凭证文件
    set /a found+=1
) else echo [SKIP]  credentials.json 不存在

if exist "%USERPROFILE%\.claude\settings.json" (
    echo [FOUND] %USERPROFILE%\.claude\settings.json
    echo         内容：配置文件（确认已备份）
    set /a found+=1
) else echo [SKIP]  settings.json 不存在

if exist "%USERPROFILE%\.claude\settings.local.json" (
    echo [FOUND] %USERPROFILE%\.claude\settings.local.json
    set /a found+=1
) else echo [SKIP]  settings.local.json 不存在

echo.

REM --- 3. Chrome IndexedDB ---
echo -- 3. Chrome IndexedDB（claude.ai 登录缓存） --
set "chrome_base=%LOCALAPPDATA%\Google\Chrome\User Data"

if exist "%chrome_base%\Default\IndexedDB\https_claude.ai_0.indexeddb.leveldb" (
    echo [FOUND] %chrome_base%\Default\IndexedDB\https_claude.ai_0.indexeddb.leveldb
    set /a found+=1
) else echo [SKIP]  Default Profile 无 claude.ai IndexedDB

for /d %%P in ("%chrome_base%\Profile *") do (
    if exist "%%P\IndexedDB\https_claude.ai_0.indexeddb.leveldb" (
        echo [FOUND] %%P\IndexedDB\https_claude.ai_0.indexeddb.leveldb
        set /a found+=1
    )
)
echo.

REM --- 4. VS Code Claude 扩展日志 ---
echo -- 4. VS Code Claude 扩展日志 --
set "vscode_logs=%APPDATA%\Code\logs"
if exist "%vscode_logs%" (
    set "log_found=0"
    for /d /r "%vscode_logs%" %%D in (*Anthropic.claude-code*) do (
        echo [FOUND] %%D
        set /a found+=1
        set /a log_found+=1
    )
    if !log_found!==0 echo [SKIP]  未找到 Claude 扩展日志
) else echo [SKIP]  VS Code logs 目录不存在
echo.

REM --- 5. VS Code 扩展安装 ---
echo -- 5. VS Code Claude 扩展安装 --
set "ext_found=0"
for /d %%E in ("%USERPROFILE%\.vscode\extensions\anthropic.claude-code-*") do (
    echo [FOUND] %%E
    set /a found+=1
    set /a ext_found+=1
)
if !ext_found!==0 echo [SKIP]  未找到 Claude 扩展安装
echo.

REM --- 6. Claude Desktop 应用数据 ---
echo -- 6. Claude Desktop 应用数据 --
if exist "%APPDATA%\Claude" (
    echo [FOUND] %APPDATA%\Claude\
    echo         内容：Desktop 应用的本地数据
    set /a found+=1
) else echo [SKIP]  Claude Desktop 数据不存在

if exist "%LOCALAPPDATA%\AnthropicClaude" (
    echo [FOUND] %LOCALAPPDATA%\AnthropicClaude\
    set /a found+=1
) else echo [SKIP]  AnthropicClaude 缓存不存在

if exist "%LOCALAPPDATA%\claude-desktop" (
    echo [FOUND] %LOCALAPPDATA%\claude-desktop\
    set /a found+=1
) else echo [SKIP]  claude-desktop 缓存不存在
echo.

REM --- 7. CLI 缓存 ---
echo -- 7. CLI 运行缓存 --
if exist "%LOCALAPPDATA%\claude-cli-nodejs" (
    echo [FOUND] %LOCALAPPDATA%\claude-cli-nodejs\
    set /a found+=1
) else echo [SKIP]  CLI 缓存不存在

if exist "%LOCALAPPDATA%\npm-cache\_npx\*claude*" (
    echo [FOUND] npm npx claude 缓存
    set /a found+=1
) else echo [SKIP]  npx claude 缓存不存在
echo.

REM --- 8. Temp ---
echo -- 8. 临时文件 --
set "tmp_found=0"
for /d %%T in ("%TEMP%\claude-*") do (
    echo [FOUND] %%T
    set /a found+=1
    set /a tmp_found+=1
)
if !tmp_found!==0 echo [SKIP]  无 claude 临时文件
echo.

echo ═══════════════════════════════════════════
echo  扫描完成，找到 !found! 个需要清理的项
echo ═══════════════════════════════════════════
echo.

if !found!==0 (
    echo 没有找到需要清理的内容，已经是干净的。
    pause
    exit /b 0
)

echo 以上是将要删除的内容。
echo.
choice /c YN /m "确认删除吗？(Y=删除 / N=取消)"
if errorlevel 2 (
    echo.
    echo 已取消，没有删除任何东西。
    pause
    exit /b 0
)

echo.
echo 开始清理...
echo.

REM --- 执行删除 ---

if exist "%USERPROFILE%\.claude.json" (
    del /f /q "%USERPROFILE%\.claude.json"
    echo [OK] 已删除 .claude.json
)

for %%D in (usage-data session-env debug file-history projects plans statsig todos) do (
    if exist "%USERPROFILE%\.claude\%%D" (
        rmdir /s /q "%USERPROFILE%\.claude\%%D"
        echo [OK] 已删除 .claude\%%D
    )
)

for %%F in (history.jsonl credentials.json settings.json settings.local.json) do (
    if exist "%USERPROFILE%\.claude\%%F" (
        del /f /q "%USERPROFILE%\.claude\%%F"
        echo [OK] 已删除 .claude\%%F
    )
)

if exist "%chrome_base%\Default\IndexedDB\https_claude.ai_0.indexeddb.leveldb" (
    rmdir /s /q "%chrome_base%\Default\IndexedDB\https_claude.ai_0.indexeddb.leveldb"
    echo [OK] 已删除 Chrome Default IndexedDB
)
for /d %%P in ("%chrome_base%\Profile *") do (
    if exist "%%P\IndexedDB\https_claude.ai_0.indexeddb.leveldb" (
        rmdir /s /q "%%P\IndexedDB\https_claude.ai_0.indexeddb.leveldb"
        echo [OK] 已删除 Chrome %%~nxP IndexedDB
    )
)

if exist "%vscode_logs%" (
    for /d /r "%vscode_logs%" %%D in (*Anthropic.claude-code*) do (
        rmdir /s /q "%%D"
        echo [OK] 已删除 VS Code 日志 %%D
    )
)

for /d %%E in ("%USERPROFILE%\.vscode\extensions\anthropic.claude-code-*") do (
    rmdir /s /q "%%E"
    echo [OK] 已删除 VS Code 扩展 %%~nxE
)

if exist "%APPDATA%\Claude" (
    rmdir /s /q "%APPDATA%\Claude"
    echo [OK] 已删除 Claude Desktop 数据
)
if exist "%LOCALAPPDATA%\AnthropicClaude" (
    rmdir /s /q "%LOCALAPPDATA%\AnthropicClaude"
    echo [OK] 已删除 AnthropicClaude 缓存
)
if exist "%LOCALAPPDATA%\claude-desktop" (
    rmdir /s /q "%LOCALAPPDATA%\claude-desktop"
    echo [OK] 已删除 claude-desktop 缓存
)

if exist "%LOCALAPPDATA%\claude-cli-nodejs" (
    rmdir /s /q "%LOCALAPPDATA%\claude-cli-nodejs"
    echo [OK] 已删除 CLI 缓存
)

for /d %%T in ("%TEMP%\claude-*") do (
    rmdir /s /q "%%T"
    echo [OK] 已删除临时文件 %%~nxT
)

echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  清理完成
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if exist "%USERPROFILE%\.claude" (
    echo.
    echo [INFO] .claude 目录还在（可能有残留文件）
    echo        如需彻底删除：在文件管理器里手动删除
    echo        %USERPROFILE%\.claude
)

echo.
pause
