# AGENT: ROLE: Fail-before-spawn validation for runtime sizing and portfolio risk caps.
# AGENT: ENTRYPOINT: called by `21_start_runtime.bat` for paper and live postures.
# AGENT: PRIMARY INPUTS: exported FXSTACK sizing and hard-risk environment variables.
# AGENT: PRIMARY OUTPUTS: exit 0 for a bounded numeric contract, exit 2 with the offending variable otherwise.
# AGENT: DEPENDS ON: `ops/windows/_env.bat`.
# AGENT: CALLED BY: `ops/windows/21_start_runtime.bat`.
# AGENT: STATE / SIDE EFFECTS: none; reads process environment only.
# AGENT: HANDSHAKES: Windows launch posture -> `fxstack.settings.Settings.validate_for_startup`.
# AGENT: SEE: `docs/agents/ops-entrypoints.md` -> `fx-quant-stack/src/fxstack/settings.py` -> `fx-quant-stack/src/fxstack/risk/kernel.py`

$ErrorActionPreference = "Stop"
$InvariantCulture = [System.Globalization.CultureInfo]::InvariantCulture
$FloatStyle = [System.Globalization.NumberStyles]::Float

function Stop-RiskLimitValidation {
    param([Parameter(Mandatory = $true)][string]$Message)

    [Console]::Error.WriteLine("[runtime] ERROR: $Message")
    exit 2
}

$RequiredPositive = @(
    "FXSTACK_EQUITY_LOTS_PER_USD",
    "FXSTACK_DEFAULT_ORDER_LOTS",
    "FXSTACK_MIN_ORDER_LOTS",
    "FXSTACK_ORDER_LOT_STEP",
    "FXSTACK_MAX_ORDER_LOTS",
    "FXSTACK_RISK_MAX_DRAWDOWN_PCT",
    "FXSTACK_RISK_MAX_GROSS_EXPOSURE",
    "FXSTACK_RISK_MAX_NET_EXPOSURE"
)

$Values = @{}
foreach ($Name in $RequiredPositive) {
    $RawValue = [Environment]::GetEnvironmentVariable($Name)
    $ParsedValue = 0.0
    $Parsed = -not [string]::IsNullOrWhiteSpace($RawValue) -and [double]::TryParse(
        $RawValue,
        $FloatStyle,
        $InvariantCulture,
        [ref]$ParsedValue
    )
    if (
        -not $Parsed -or
        [double]::IsNaN($ParsedValue) -or
        [double]::IsInfinity($ParsedValue) -or
        $ParsedValue -le 0.0
    ) {
        Stop-RiskLimitValidation "$Name must be a finite number greater than zero (received '$RawValue')."
    }
    $Values[$Name] = $ParsedValue
}

if ($Values["FXSTACK_RISK_MAX_DRAWDOWN_PCT"] -gt 100.0) {
    Stop-RiskLimitValidation "FXSTACK_RISK_MAX_DRAWDOWN_PCT must be no greater than 100."
}
if ($Values["FXSTACK_MAX_ORDER_LOTS"] -lt $Values["FXSTACK_MIN_ORDER_LOTS"]) {
    Stop-RiskLimitValidation "FXSTACK_MAX_ORDER_LOTS must be greater than or equal to FXSTACK_MIN_ORDER_LOTS."
}
if (
    $Values["FXSTACK_DEFAULT_ORDER_LOTS"] -lt $Values["FXSTACK_MIN_ORDER_LOTS"] -or
    $Values["FXSTACK_DEFAULT_ORDER_LOTS"] -gt $Values["FXSTACK_MAX_ORDER_LOTS"]
) {
    Stop-RiskLimitValidation "FXSTACK_DEFAULT_ORDER_LOTS must be between FXSTACK_MIN_ORDER_LOTS and FXSTACK_MAX_ORDER_LOTS."
}
if ($Values["FXSTACK_RISK_MAX_NET_EXPOSURE"] -gt $Values["FXSTACK_RISK_MAX_GROSS_EXPOSURE"]) {
    Stop-RiskLimitValidation "FXSTACK_RISK_MAX_NET_EXPOSURE must be no greater than FXSTACK_RISK_MAX_GROSS_EXPOSURE."
}

exit 0
