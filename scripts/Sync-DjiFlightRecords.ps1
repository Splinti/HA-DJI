<#
.SYNOPSIS
    Copies new DJI Fly flight records from any USB-attached Android device
    (DJI RC 2, phone) to a target folder, e.g. the Home Assistant /share.

.DESCRIPTION
    Android devices show up in Windows as MTP "portable devices", not as drive
    letters, so this script walks them through the Shell COM API. It looks for
        <storage>\Android\data\dji.go.v5\files\FlightRecord   (DJI Fly, Android 11+)
        <storage>\DJI\dji.go.v5\FlightRecord                  (older DJI Fly)
    on every storage of every attached device and copies *.txt files that do
    not exist in the target yet (compared by name and size).

    Run it manually, or register a scheduled task that polls every few minutes
    (`-Register`). When nothing is attached the script exits immediately.

.PARAMETER Target
    Destination folder. Default is the HAOS Samba share.

.PARAMETER Register / Unregister
    Create / remove a scheduled task "DJI Flight Record Sync" that runs this
    script every -IntervalMinutes minutes for the current user.

.EXAMPLE
    .\Sync-DjiFlightRecords.ps1 -Target '\\homeassistant\share\dji\flightrecords'
    .\Sync-DjiFlightRecords.ps1 -Register -IntervalMinutes 10
#>
[CmdletBinding()]
param(
    [string]$Target = '\\homeassistant\share\dji\flightrecords',
    [switch]$Register,
    [switch]$Unregister,
    [int]$IntervalMinutes = 10,
    [string]$LogFile = (Join-Path $env:LOCALAPPDATA 'DjiFlightSync\sync.log'),
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$TaskName = 'DJI Flight Record Sync'
$RelativePaths = @(
    @('Android', 'data', 'dji.go.v5', 'files', 'FlightRecord'),
    @('DJI', 'dji.go.v5', 'FlightRecord')
)

function Write-Log {
    param([string]$Message)
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    Write-Host $line
    try {
        $dir = Split-Path $LogFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    } catch { }
}

# ---------------------------------------------------------------------------
# Scheduled task management
# ---------------------------------------------------------------------------
if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Log "Scheduled task '$TaskName' removed."
    return
}
if ($Register) {
    $scriptPath = $MyInvocation.MyCommand.Path
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
        "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -Target `"$Target`""
    )
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description 'Copies DJI Fly flight records from USB-attached devices to Home Assistant' -Force | Out-Null
    Write-Log "Scheduled task '$TaskName' registered (every $IntervalMinutes min, target $Target)."
    return
}

# ---------------------------------------------------------------------------
# MTP helpers
# ---------------------------------------------------------------------------
function Get-ShellChild {
    param($Folder, [string]$Name)
    if ($null -eq $Folder) { return $null }
    foreach ($item in $Folder.Items()) {
        if ($item.Name -eq $Name) { return $item }
    }
    return $null
}

function Get-FlightRecordFolders {
    <# Yields [pscustomobject]@{Device; Storage; Folder} for every FlightRecord folder found. #>
    $shell = New-Object -ComObject Shell.Application
    $thisPc = $shell.NameSpace(17)   # ssfDRIVES = "This PC", includes portable devices
    foreach ($device in $thisPc.Items()) {
        # Portable devices have a "::{...}" path and are folders but not drives.
        if (-not $device.IsFolder -or $device.Path -notlike '::{*') { continue }
        $deviceFolder = $device.GetFolder
        if ($null -eq $deviceFolder) { continue }
        foreach ($storage in $deviceFolder.Items()) {
            if (-not $storage.IsFolder) { continue }
            foreach ($rel in $RelativePaths) {
                $folder = $storage.GetFolder
                $ok = $true
                foreach ($segment in $rel) {
                    $child = Get-ShellChild -Folder $folder -Name $segment
                    if ($null -eq $child -or -not $child.IsFolder) { $ok = $false; break }
                    $folder = $child.GetFolder
                }
                if ($ok) {
                    [pscustomobject]@{ Device = $device.Name; Storage = $storage.Name; Folder = $folder }
                }
            }
        }
    }
}

function Copy-MtpFile {
    <# CopyHere is asynchronous; wait until the file is complete. #>
    param($Item, [string]$Destination, [long]$ExpectedSize)
    $shell = New-Object -ComObject Shell.Application
    $destFolder = $shell.NameSpace($Destination)
    if ($null -eq $destFolder) { throw "Cannot open destination $Destination" }
    # 0x4 no progress UI, 0x10 yes to all, 0x400 no UI on error
    $destFolder.CopyHere($Item, 0x4 -bor 0x10 -bor 0x400)
    $dest = Join-Path $Destination $Item.Name
    $deadline = (Get-Date).AddMinutes(2)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 300
        if ((Test-Path $dest) -and ((Get-Item $dest).Length -ge $ExpectedSize)) { return $dest }
    }
    throw "Timeout copying $($Item.Name)"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
$folders = @(Get-FlightRecordFolders)
if ($folders.Count -eq 0) {
    Write-Verbose 'No attached device with a FlightRecord folder.'
    return
}

if (-not (Test-Path $Target)) {
    if ($WhatIf) { Write-Log "Would create $Target" } else { New-Item -ItemType Directory -Path $Target -Force | Out-Null }
}
$existing = @{}
if (Test-Path $Target) {
    Get-ChildItem -Path $Target -Filter '*.txt' -File -Recurse | ForEach-Object { $existing[$_.Name] = $_.Length }
}

$staging = Join-Path $env:TEMP 'DjiFlightSync'
if (-not (Test-Path $staging)) { New-Item -ItemType Directory -Path $staging -Force | Out-Null }

$copied = 0
foreach ($entry in $folders) {
    Write-Log "Scanning $($entry.Device) / $($entry.Storage)"
    foreach ($item in $entry.Folder.Items()) {
        if ($item.IsFolder -or $item.Name -notlike '*.txt') { continue }
        $size = [long]$item.ExtendedProperty('System.Size')
        if ($existing.ContainsKey($item.Name) -and $existing[$item.Name] -eq $size) { continue }
        if ($WhatIf) { Write-Log "Would copy $($item.Name) ($size bytes)"; continue }
        try {
            $tmp = Copy-MtpFile -Item $item -Destination $staging -ExpectedSize $size
            Move-Item -Path $tmp -Destination (Join-Path $Target $item.Name) -Force
            $existing[$item.Name] = $size
            $copied++
            Write-Log "Copied $($item.Name) ($size bytes) from $($entry.Device)"
        } catch {
            Write-Log "ERROR $($item.Name): $($_.Exception.Message)"
        }
    }
}
Write-Log "Done, $copied new file(s)."
