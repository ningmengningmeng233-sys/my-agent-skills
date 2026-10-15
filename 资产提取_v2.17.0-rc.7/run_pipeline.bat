@echo off
REM 一键入口（加速改法工单 T9，Windows 版）：只做编排，不改各步骤脚本行为。
REM 用法：run_pipeline.bat --lib <资产库> --input <小说文件> [--genre 古风] [--style 仙侠奇幻风] [--dry-run] [--force]
REM 任一步骤失败：立即停止，打印原因码与失败步骤名；再跑同一条命令即可续跑。
setlocal enabledelayedexpansion

set "LIB="
set "INPUT="
set "GENRE=古风"
set "STYLE=仙侠奇幻风"
set "DRY_RUN=0"
set "FORCE_FLAG="
set "NOVEL_OVERRIDE="
set "AUTHOR_OVERRIDE="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--lib" ( set "LIB=%~2" & shift & shift & goto parse )
if /i "%~1"=="--input" ( set "INPUT=%~2" & shift & shift & goto parse )
if /i "%~1"=="--genre" ( set "GENRE=%~2" & shift & shift & goto parse )
if /i "%~1"=="--style" ( set "STYLE=%~2" & shift & shift & goto parse )
if /i "%~1"=="--novel" ( set "NOVEL_OVERRIDE=%~2" & shift & shift & goto parse )
if /i "%~1"=="--author" ( set "AUTHOR_OVERRIDE=%~2" & shift & shift & goto parse )
if /i "%~1"=="--dry-run" ( set "DRY_RUN=1" & shift & goto parse )
if /i "%~1"=="--force" ( set "FORCE_FLAG=--force-retry" & shift & goto parse )
echo 未知参数: %~1
exit /b 2

:parsed
set "SKILL_DIR=%~dp0"
if not "%PYTHON%"=="" ( set "PY=%PYTHON%" ) else ( set "PY=python" )

if "%DRY_RUN%"=="1" (
  echo 【dry-run】一键链路将按以下顺序执行（不出网、不调用模型、不落账）：
  echo   0. 初始化      scripts/maintenance.py init（状态文件已存在时自动跳过）
  echo   1. 拆分        scripts/split_segments.py
  echo   2. 登记        scripts/scan_registry.py
  echo   3. 提取        scripts/extract_segments.py
  echo   4. 聚合        scripts/aggregate.py
  echo   5. 对账        scripts/scan_registry.py --include-claims
  echo   6. 服饰        scripts/prepare_costume_input.py
  echo   7. 描述        scripts/generate_prompts.py --enrich
  echo   8. 基础提示词  scripts/generate_prompts.py --base
  echo   9. 衍生提示词  scripts/generate_prompts.py --deriv
  echo  10. verify      scripts/maintenance.py verify
  exit /b 0
)

if "%LIB%"=="" ( echo 缺少 --lib ^<资产库^> & exit /b 2 )
if "%INPUT%"=="" ( echo 缺少 --input ^<小说文件^> & exit /b 2 )
if not exist "%INPUT%" ( echo 输入文件不存在: %INPUT% & exit /b 2 )
if "%NOVEL_OVERRIDE%"=="" (
  for %%I in ("%INPUT%") do set "NOVEL=%%~nI"
) else (
  set "NOVEL=%NOVEL_OVERRIDE%"
)
if "%AUTHOR_OVERRIDE%"=="" ( set "AUTHOR=佚名" ) else ( set "AUTHOR=%AUTHOR_OVERRIDE%" )
if not exist "%LIB%" mkdir "%LIB%"
set "ASSET_LIB_DIR=%LIB%"

set "STEP_NO="
set "STEP_NAME="

if not exist "%LIB%\.start-state.json" (
  set "STEP_NO=0" && set "STEP_NAME=初始化"
  %PY% "%SKILL_DIR%scripts\maintenance.py" init --lib "%LIB%" --input-source "%INPUT%" --genre "%GENRE%" --style "%STYLE%"
  if errorlevel 1 goto failed
)
set "STEP_NO=1" && set "STEP_NAME=拆分"
%PY% "%SKILL_DIR%scripts\split_segments.py" "%INPUT%" --novel "%NOVEL%" --author "%AUTHOR%" --lib "%LIB%" --overlap 1
if errorlevel 1 goto failed
set "STEP_NO=2" && set "STEP_NAME=登记"
%PY% "%SKILL_DIR%scripts\scan_registry.py" "%LIB%\原文段\%NOVEL%-%AUTHOR%" --lib "%LIB%"
if errorlevel 1 goto failed
set "STEP_NO=3" && set "STEP_NAME=提取"
%PY% "%SKILL_DIR%scripts\extract_segments.py" "%LIB%\原文段\%NOVEL%-%AUTHOR%" --lib "%LIB%" %FORCE_FLAG%
if errorlevel 1 goto failed
set "STEP_NO=4" && set "STEP_NAME=聚合"
%PY% "%SKILL_DIR%scripts\aggregate.py" "%LIB%\段产物\%NOVEL%-%AUTHOR%" "%LIB%\原文段\%NOVEL%-%AUTHOR%" --lib "%LIB%"
if errorlevel 1 goto failed
set "STEP_NO=5" && set "STEP_NAME=对账"
%PY% "%SKILL_DIR%scripts\scan_registry.py" --include-claims "%LIB%\.process\登记表对账报告.md" --lib "%LIB%"
if errorlevel 1 goto failed
set "STEP_NO=6" && set "STEP_NAME=服饰"
%PY% "%SKILL_DIR%scripts\prepare_costume_input.py" "%LIB%\反推_最终.md" --genre "%GENRE%" --lib "%LIB%"
if errorlevel 1 goto failed
set "STEP_NO=7" && set "STEP_NAME=描述"
%PY% "%SKILL_DIR%scripts\generate_prompts.py" --enrich "%LIB%\反推_最终.md" --genre "%GENRE%" --lib "%LIB%" --style "%STYLE%" --yes
if errorlevel 1 goto failed
set "STEP_NO=8" && set "STEP_NAME=基础提示词"
%PY% "%SKILL_DIR%scripts\generate_prompts.py" --base "%LIB%\反推_最终.md" --genre "%GENRE%" --lib "%LIB%" --style "%STYLE%" --yes --view-mode full
if errorlevel 1 goto failed
set "STEP_NO=9" && set "STEP_NAME=衍生提示词"
%PY% "%SKILL_DIR%scripts\generate_prompts.py" --init-schedule "%LIB%\反推_最终.md" --genre "%GENRE%" --lib "%LIB%"
if errorlevel 1 goto failed
%PY% "%SKILL_DIR%scripts\generate_prompts.py" --deriv --genre "%GENRE%" --lib "%LIB%" --style "%STYLE%" --yes --view-mode full
if errorlevel 1 goto failed
set "STEP_NO=10" && set "STEP_NAME=verify"
%PY% "%SKILL_DIR%scripts\maintenance.py" verify --lib "%LIB%" --style "%STYLE%"
if errorlevel 1 goto failed

echo ✅ 全部步骤完成：%LIB%
exit /b 0

:failed
echo.
echo ❌ 失败步骤: %STEP_NAME%（原因码 errorlevel 非 0）
echo    再跑同一条命令即可续跑：已完成步骤会按断点自动跳过。
exit /b 1
