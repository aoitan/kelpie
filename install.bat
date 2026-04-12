@echo off
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

if "%KELPIE_HOME%"=="" set "KELPIE_HOME=%USERPROFILE%\.local\share\kelpie"
if "%KELPIE_CONFIG_HOME%"=="" set "KELPIE_CONFIG_HOME=%USERPROFILE%\.config\kelpie"
if "%KELPIE_BIN_DIR%"=="" set "KELPIE_BIN_DIR=%USERPROFILE%\.local\bin"

mkdir "%KELPIE_HOME%" 2>nul
mkdir "%KELPIE_CONFIG_HOME%" 2>nul
mkdir "%KELPIE_BIN_DIR%" 2>nul

call :copy_file "%SCRIPT_DIR%\AGENTS.md" "%KELPIE_HOME%\AGENTS.md"
call :copy_file "%SCRIPT_DIR%\Dockerfile.llm-base" "%KELPIE_HOME%\Dockerfile.llm-base"
call :copy_file "%SCRIPT_DIR%\compose.llm.yaml" "%KELPIE_HOME%\compose.llm.yaml"
call :copy_file "%SCRIPT_DIR%\README.md" "%KELPIE_HOME%\README.md"
call :copy_file "%SCRIPT_DIR%\llm-entrypoint.sh" "%KELPIE_HOME%\llm-entrypoint.sh"
call :copy_dir "%SCRIPT_DIR%\prompts" "%KELPIE_HOME%\prompts"
call :copy_dir "%SCRIPT_DIR%\skills" "%KELPIE_HOME%\skills"
call :copy_dir "%SCRIPT_DIR%\examples" "%KELPIE_HOME%\examples"
call :copy_dir "%SCRIPT_DIR%\scripts" "%KELPIE_HOME%\scripts"

if not exist "%KELPIE_CONFIG_HOME%\runner_config.json" call :copy_file "%SCRIPT_DIR%\examples\runner_config.json" "%KELPIE_CONFIG_HOME%\runner_config.json"
if not exist "%KELPIE_CONFIG_HOME%\instruction_staging.json" call :copy_file "%SCRIPT_DIR%\examples\instruction_staging.json" "%KELPIE_CONFIG_HOME%\instruction_staging.json"
if not exist "%KELPIE_CONFIG_HOME%\compose.local.yaml" call :copy_file "%SCRIPT_DIR%\compose.local.yaml" "%KELPIE_CONFIG_HOME%\compose.local.yaml"
if not exist "%KELPIE_CONFIG_HOME%\runner.env" call :copy_file "%SCRIPT_DIR%\examples\runner.env.example" "%KELPIE_CONFIG_HOME%\runner.env"

> "%KELPIE_BIN_DIR%\kelpie.cmd" (
  echo @echo off
  echo set "KELPIE_HOME=%KELPIE_HOME%"
  echo set "KELPIE_CONFIG_HOME=%KELPIE_CONFIG_HOME%"
  echo sh "%KELPIE_HOME%\scripts\run_issue_workflow_in_container.sh" %%*
)

echo Installed kelpie to:
echo   home:   %KELPIE_HOME%
echo   config: %KELPIE_CONFIG_HOME%
echo   bin:    %KELPIE_BIN_DIR%
echo.
echo Add this to PATH if needed:
echo   %KELPIE_BIN_DIR%
exit /b 0

:copy_file
set "SRC=%~1"
set "DST=%~2"
for %%I in ("%DST%") do mkdir "%%~dpI" 2>nul
copy /Y "%SRC%" "%DST%" >nul
exit /b 0

:copy_dir
set "SRC=%~1"
set "DST=%~2"
if exist "%DST%" rmdir /S /Q "%DST%"
xcopy "%SRC%" "%DST%\" /E /I /Y >nul
exit /b 0
