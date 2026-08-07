# AGENT: ROLE: Verify and enable MT4's global AutoTrading toolbar state for the active IG terminal.
# AGENT: ENTRYPOINT: called by `ops/windows/19_start_mt4.ps1` after resolving or starting the exact terminal process.
# AGENT: STATE / SIDE EFFECTS: sends MT4's Ctrl+E shortcut only when the exact toolbar state is enabled but unchecked.
param(
    [string]$TerminalExe = ""
)

$ErrorActionPreference = "Stop"
$expectedPath = if ($TerminalExe.Trim()) {
    [IO.Path]::GetFullPath($TerminalExe)
} else {
    [IO.Path]::GetFullPath("${env:ProgramFiles(x86)}\IG MetaTrader 4 Terminal\terminal.exe")
}
if (-not (Test-Path -LiteralPath $expectedPath -PathType Leaf)) {
    throw "MT4 terminal executable not found: $expectedPath"
}

$terminals = @(Get-CimInstance Win32_Process -Filter "Name='terminal.exe'" -ErrorAction Stop | Where-Object {
    $_.ExecutablePath -and [IO.Path]::GetFullPath([string]$_.ExecutablePath) -eq $expectedPath
})
if ($terminals.Count -ne 1) {
    throw "Expected exactly one IG MT4 terminal process at $expectedPath; found $($terminals.Count)."
}
$terminalProcess = Get-Process -Id ([int]$terminals[0].ProcessId) -ErrorAction Stop
if ($terminalProcess.MainWindowHandle -eq [IntPtr]::Zero) {
    throw "IG MT4 terminal main window is unavailable."
}

Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;

public static class FxStackMt4AutoTrading {
    public delegate bool EnumWindowProc(IntPtr window, IntPtr parameter);
    private const int StandardToolbarId = 99;
    private const int AutoTradingCommandId = 33020;
    private const uint TbGetState = 0x0412;
    private const uint TbStateChecked = 0x01;
    private const uint TbStateEnabled = 0x04;
    private const uint KeyUp = 0x02;

    [DllImport("user32.dll")]
    private static extern bool EnumChildWindows(
        IntPtr parent,
        EnumWindowProc callback,
        IntPtr parameter
    );

    [DllImport("user32.dll")]
    private static extern int GetDlgCtrlID(IntPtr window);

    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    private static extern int GetClassName(IntPtr window, StringBuilder value, int maximum);

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);

    [DllImport("user32.dll")]
    private static extern void keybd_event(byte virtualKey, byte scanCode, uint flags, UIntPtr extraInfo);

    public static IntPtr FindStandardToolbar(IntPtr root) {
        IntPtr found = IntPtr.Zero;
        EnumChildWindows(root, (window, parameter) => {
            var className = new StringBuilder(64);
            GetClassName(window, className, className.Capacity);
            if (
                GetDlgCtrlID(window) == StandardToolbarId &&
                className.ToString() == "ToolbarWindow32"
            ) {
                found = window;
                return false;
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }

    public static uint AutoTradingState(IntPtr toolbar) {
        return unchecked((uint)SendMessage(
            toolbar,
            TbGetState,
            (IntPtr)AutoTradingCommandId,
            IntPtr.Zero
        ).ToInt64());
    }

    public static bool IsEnabled(uint state) { return (state & TbStateEnabled) != 0; }
    public static bool IsChecked(uint state) { return (state & TbStateChecked) != 0; }

    public static void SendCtrlE(IntPtr root) {
        if (!SetForegroundWindow(root)) {
            throw new InvalidOperationException("mt4_foreground_activation_failed");
        }
        System.Threading.Thread.Sleep(200);
        keybd_event(0x11, 0, 0, UIntPtr.Zero);
        keybd_event(0x45, 0, 0, UIntPtr.Zero);
        keybd_event(0x45, 0, KeyUp, UIntPtr.Zero);
        keybd_event(0x11, 0, KeyUp, UIntPtr.Zero);
    }
}
'@

$toolbar = [FxStackMt4AutoTrading]::FindStandardToolbar($terminalProcess.MainWindowHandle)
if ($toolbar -eq [IntPtr]::Zero) {
    throw "IG MT4 Standard toolbar was not found."
}
$state = [FxStackMt4AutoTrading]::AutoTradingState($toolbar)
if (-not [FxStackMt4AutoTrading]::IsEnabled($state)) {
    throw "IG MT4 AutoTrading toolbar command is unavailable or disabled (state=$state)."
}
if (-not [FxStackMt4AutoTrading]::IsChecked($state)) {
    [FxStackMt4AutoTrading]::SendCtrlE($terminalProcess.MainWindowHandle)
    $deadline = (Get-Date).AddSeconds(3)
    do {
        Start-Sleep -Milliseconds 100
        $state = [FxStackMt4AutoTrading]::AutoTradingState($toolbar)
        if ([FxStackMt4AutoTrading]::IsChecked($state)) {
            break
        }
    } while ((Get-Date) -lt $deadline)
}
if (-not [FxStackMt4AutoTrading]::IsChecked($state)) {
    throw "IG MT4 AutoTrading did not become enabled (state=$state)."
}

Write-Host ("[mt4] autotrading=enabled pid={0}" -f $terminalProcess.Id)
