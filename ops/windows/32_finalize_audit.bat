@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "FAST="
set "SHADOW="
set "ROLLBACK="
for /f "delims=" %%F in ('dir /b /o-d docs\canary_shadow_fast15m*.json 2^>nul') do if not defined FAST set "FAST=docs\%%F"
for /f "delims=" %%F in ('dir /b /o-d docs\canary_shadow_24h*.json 2^>nul') do if not defined SHADOW set "SHADOW=docs\%%F"
if defined FXSTACK_ROLLBACK_EVIDENCE set "ROLLBACK=%FXSTACK_ROLLBACK_EVIDENCE%"
for /f "delims=" %%F in ('dir /b /o-d docs\rollback_drill_evidence*.json 2^>nul') do if not defined ROLLBACK set "ROLLBACK=docs\%%F"

if not defined FAST (
  echo [finalize] ERROR: no fast-gate artifact found in docs\
  exit /b 2
)
if not defined SHADOW (
  echo [finalize] ERROR: no 24h shadow artifact found in docs\
  exit /b 2
)
if not defined ROLLBACK (
  echo [finalize] ERROR: no rollback drill evidence found; set FXSTACK_ROLLBACK_EVIDENCE
  exit /b 2
)
if not exist "%ROLLBACK%" (
  echo [finalize] ERROR: rollback drill evidence not found: %ROLLBACK%
  exit /b 2
)
if not defined EVIDENCE_PAIR set "EVIDENCE_PAIR=EURUSD"
if not defined CANDIDATE_MODEL_MANIFEST set "CANDIDATE_MODEL_MANIFEST=%FXSTACK_CANDIDATE_MODEL_ACTIVATION_MANIFEST%"
if not defined CANDIDATE_MODEL_MANIFEST set "CANDIDATE_MODEL_MANIFEST=%FXSTACK_MODEL_ACTIVATION_MANIFEST%"
if not defined CANDIDATE_MODEL_MANIFEST set "CANDIDATE_MODEL_MANIFEST=fx-quant-stack\artifacts\active_models.json"
if not exist "%CANDIDATE_MODEL_MANIFEST%" (
  echo [finalize] ERROR: candidate model manifest not found: %CANDIDATE_MODEL_MANIFEST%
  exit /b 2
)

echo [finalize] fast=%FAST%
echo [finalize] shadow=%SHADOW%
echo [finalize] rollback=%ROLLBACK%
"%TRADER_PYTHON_EXE%" "%ROOT%\tools\finalize_build.py" --evidence-root docs/audit --fast-gate-artifact "%FAST%" --shadow-artifact "%SHADOW%" --rollback-evidence "%ROLLBACK%" --pair %EVIDENCE_PAIR% --model-manifest "%CANDIDATE_MODEL_MANIFEST%"
exit /b %ERRORLEVEL%
