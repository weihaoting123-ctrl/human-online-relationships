# Read only this installation's exact executable and main command. Never emit
# paths, PIDs, command lines, titles, account properties, or exception details.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Executable,
    [Parameter(Mandatory=$true)][string]$Main,
    [Parameter(Mandatory=$true)][ValidateRange(1024,65535)][int]$Port
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Get-FixedDesktopState {
    try {
        if (-not ('CopilotDesktopArguments' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CopilotDesktopArguments {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CommandLineToArgvW(string command, out int count);
    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr pointer);
    public static string[] Split(string command) {
        if (String.IsNullOrWhiteSpace(command) || command.Length > 32768) return null;
        int count;
        IntPtr pointer = CommandLineToArgvW(command, out count);
        if (pointer == IntPtr.Zero) return null;
        try {
            if (count < 1 || count > 64) return null;
            var args = new string[count];
            for (int i = 0; i < count; i++)
                args[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(pointer, i * IntPtr.Size));
            return args;
        } finally { LocalFree(pointer); }
    }
}
'@ | Out-Null
        }
        $expectedExe = [System.IO.Path]::GetFullPath($Executable)
        $expectedMain = [System.IO.Path]::GetFullPath($Main)
        # WQL escaping applies to a value, never to executable PowerShell text.
        $escapedExe = $expectedExe.Replace('\', '\\').Replace("'", "\'")
        $filter = "ExecutablePath = '$escapedExe'"
        $rows = @(Get-CimInstance -ClassName Win32_Process -Filter $filter -Property ExecutablePath,CommandLine -OperationTimeoutSec 2)
        if ($rows.Count -gt 64) { return 'unknown' }
        $uncertain = $false
        foreach ($row in $rows) {
            if (-not [string]::Equals([string]$row.ExecutablePath, $expectedExe, [StringComparison]::OrdinalIgnoreCase)) {
                $uncertain = $true
                continue
            }
            $parts = [CopilotDesktopArguments]::Split([string]$row.CommandLine)
            if ($null -eq $parts -or $parts.Length -lt 2) {
                $uncertain = $true
                continue
            }
            if (-not [string]::Equals($parts[0], $expectedExe, [StringComparison]::OrdinalIgnoreCase)) {
                $uncertain = $true
                continue
            }
            if (-not [string]::Equals($parts[1], $expectedMain, [StringComparison]::OrdinalIgnoreCase)) {
                continue  # Renderer/utility or another app using this runtime.
            }
            $seenPort = $false
            $seenShow = $false
            $actualPort = 8765
            $valid = $parts.Length -le 4
            for ($index = 2; $index -lt $parts.Length; $index++) {
                $part = $parts[$index]
                if ($part -ceq '--show' -and -not $seenShow) {
                    $seenShow = $true
                } elseif ($part -cmatch '^--port=([0-9]{4,5})$' -and -not $seenPort) {
                    $seenPort = $true
                    $actualPort = [int]$Matches[1]
                } else {
                    $valid = $false
                }
            }
            if ($valid -and $actualPort -eq $Port) { return 'running' }
            $uncertain = $true
        }
        if ($uncertain) { return 'unknown' }
        return 'stopped'
    } catch {
        return 'unknown'
    }
}

Get-FixedDesktopState
