param(
    [Parameter(Mandatory=$true)][string]$PlanPath,
    [switch]$NoRestart
)
$ErrorActionPreference = 'Stop'
$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($plan.appId -ne 'ReimburseOrganizer.Desktop') { throw 'Wrong application update plan' }
$installRoot = [IO.Path]::GetFullPath($plan.root).TrimEnd('\')
$stageRoot = [IO.Path]::GetFullPath($plan.staging).TrimEnd('\')
$backupRoot = [IO.Path]::GetFullPath($plan.backup).TrimEnd('\')

function Get-InsidePath([string]$base, [string]$relative) {
    if ([IO.Path]::IsPathRooted($relative) -or $relative.Contains(':') -or $relative -match '(^|[\\/])\.\.?([\\/]|$)') { throw 'Unsafe relative update path' }
    $target = [IO.Path]::GetFullPath((Join-Path $base $relative))
    if (-not $target.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Update path escapes root' }
    $current = $target
    while ($current -and $current -ne $base) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Update target contains a reparse point' }
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
    return $target
}

function Ensure-Parent([string]$path) {
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path)) | Out-Null
}

function Get-Sha256([string]$path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($path)
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}

$lockPath = Join-Path $installRoot '.update-lock'
$lockStream = $null
$originals = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$attempted = [Collections.Generic.List[string]]::new()
$success = $false
$rolledBack = $false
$failure = ''
$rollbackFailure = ''
$uninstallEntry = $null
$previousDisplayVersion = $null
try {
    if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) { throw 'Installation directory does not exist' }
    if ($plan.pid -gt 0) {
        try { $oldProcess = [Diagnostics.Process]::GetProcessById([int]$plan.pid) } catch { $oldProcess = $null }
        if ($oldProcess -and -not $oldProcess.WaitForExit(60000)) { throw 'Application did not exit; no installation files changed' }
    }
    $lockStream = [IO.File]::Open($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $touched = @(@($plan.changes) + @($plan.removed) + @('install-manifest.json') | Select-Object -Unique)
    foreach ($relative in $touched) {
        $target = Get-InsidePath $installRoot $relative
        $backup = Get-InsidePath $backupRoot $relative
        if (Test-Path -LiteralPath $target) {
            if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw 'A managed file is a directory' }
            Ensure-Parent $backup
            [IO.File]::Copy($target, $backup, $false)
            $originals.Add($relative) | Out-Null
        }
    }
    foreach ($relative in $plan.changes) {
        $source = Get-InsidePath $stageRoot $relative
        $target = Get-InsidePath $installRoot $relative
        Ensure-Parent $target
        $attempted.Add($relative)
        [IO.File]::Copy($source, $target, $true)
    }
    foreach ($relative in $plan.removed) {
        $target = Get-InsidePath $installRoot $relative
        $attempted.Add($relative)
        if (Test-Path -LiteralPath $target -PathType Leaf) { [IO.File]::Delete($target) }
    }
    foreach ($property in $plan.files.PSObject.Properties) {
        $target = Get-InsidePath $installRoot $property.Name
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw ('Installed file missing: ' + $property.Name) }
        if ((Get-Item -LiteralPath $target).Length -ne $property.Value.size -or (Get-Sha256 $target) -ne $property.Value.sha256) { throw ('Installed file hash mismatch: ' + $property.Name) }
    }
    $attempted.Add('install-manifest.json')
    [IO.File]::Copy($plan.manifest, (Join-Path $installRoot 'install-manifest.json'), $true)
    # Keep Windows' installed-app version consistent with an in-app update.
    $uninstallRoot = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Uninstall', $false)
    if ($null -ne $uninstallRoot) {
        foreach ($keyName in $uninstallRoot.GetSubKeyNames()) {
            $candidate = $uninstallRoot.OpenSubKey($keyName, $false)
            $location = [string]$candidate.GetValue('InstallLocation', '')
            $candidate.Dispose()
            if ($location.TrimEnd('\').Equals($installRoot, [StringComparison]::OrdinalIgnoreCase)) {
                $uninstallEntry = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey(('Software\Microsoft\Windows\CurrentVersion\Uninstall\' + $keyName), $true)
                $previousDisplayVersion = $uninstallEntry.GetValue('DisplayVersion', '')
                $uninstallEntry.SetValue('DisplayVersion', [string]$plan.version)
                break
            }
        }
        $uninstallRoot.Dispose()
    }
    $success = $true
} catch {
    $failure = $_.Exception.Message
    try {
        for ($index = $attempted.Count - 1; $index -ge 0; $index--) {
            $relative = $attempted[$index]
            $target = Get-InsidePath $installRoot $relative
            if ($originals.Contains($relative)) {
                [IO.File]::Copy((Get-InsidePath $backupRoot $relative), $target, $true)
            } elseif (Test-Path -LiteralPath $target -PathType Leaf) {
                [IO.File]::Delete($target)
            }
        }
        $rolledBack = $attempted.Count -gt 0
        if ($null -ne $uninstallEntry) { $uninstallEntry.SetValue('DisplayVersion', $previousDisplayVersion) }
    } catch { $rollbackFailure = $_.Exception.Message }
} finally {
    if ($null -ne $uninstallEntry) { $uninstallEntry.Dispose() }
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
        [IO.File]::Delete($lockPath)
    }
    $result = @{ success=$success; rolledBack=$rolledBack; version=$plan.version; error=$failure; rollbackError=$rollbackFailure; backup=$backupRoot; seen=$false; time=[DateTime]::UtcNow.ToString('o') }
    Ensure-Parent $plan.status
    $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $plan.status -Encoding UTF8
}
if (-not $NoRestart -and (Test-Path -LiteralPath (Join-Path $installRoot $plan.exe) -PathType Leaf)) {
    Start-Process -FilePath (Join-Path $installRoot $plan.exe) -WorkingDirectory $installRoot
}
if (-not $success) { exit 1 }
