<#
.SYNOPSIS
    Copies new DJI Fly flight records from any USB-attached Android device
    (DJI RC 2, phone) to a target folder, e.g. the Home Assistant /share.
    Optionally also copies the videos/photos into a folder (OneDrive, NAS share).

.DESCRIPTION
    Android devices show up in Windows as MTP "portable devices", not as drive
    letters, so this script walks them through the Shell COM API. It looks for
        <storage>\Android\data\dji.go.v5\files\FlightRecord   (DJI Fly, Android 11+)
        <storage>\DJI\dji.go.v5\FlightRecord                  (older DJI Fly)
    on every storage of every attached device and copies *.txt files that do
    not exist in the target yet (compared by name and size).

    With -MediaTarget the script also collects the recordings (DJI_*.MP4,
    .OSV, .LRF, .JPG, .DNG, .SRT) from
        - DCIM folders of attached MTP devices (drone, goggles, RC),
        - DCIM folders of removable drives (SD card reader),
        - every folder given with -MediaSource,
    and files them as <MediaTarget>\<yyyy>\<yyyy-MM-dd>\<name>. Point
    -MediaTarget into the local OneDrive folder and the OneDrive client
    uploads them; the Home Assistant integration then links them to flights.

    Per recording:
        .LRF (low-res proxy of the camera)  -> <name>_proxy.mp4 (plays in browsers
                                               with H.265 support), unless -NoProxy
        .OSV (360° original, Avata 360)     -> copied as is, plus <name>_cover.jpg
                                               (the cover frame embedded in the file;
                                               needs ffmpeg), unless -NoCover
        -Stitch360                          -> the proxy of a 360° recording is
                                               re-encoded from dual fisheye to an
                                               equirectangular H.264 video (ffmpeg,
                                               slow: roughly real time)

    Run it manually, or register a scheduled task that polls every few minutes
    (`-Register`). When nothing is attached the script exits immediately.

.PARAMETER Target
    Destination folder of the flight records. Default is the HAOS Samba share.

.PARAMETER MediaTarget
    Destination root of the recordings, e.g. "$env:OneDrive\Drohne\Medien" or a
    share like '\\nas\drohne\medien'.
    Empty (default) = recordings are not copied.

.PARAMETER MediaSource
    Additional folders to take recordings from (searched recursively).

.PARAMETER Register / Unregister
    Create / remove a scheduled task "DJI Flight Record Sync" that runs this
    script every -IntervalMinutes minutes for the current user.

.EXAMPLE
    .\Sync-DjiFlightRecords.ps1 -Target '\\homeassistant\share\dji\flightrecords'
    .\Sync-DjiFlightRecords.ps1 -MediaTarget "$env:OneDrive\Drohne\Medien" -MediaSource 'E:\DJI Avata 360' -WhatIf
    .\Sync-DjiFlightRecords.ps1 -Register -IntervalMinutes 10 -MediaTarget "$env:OneDrive\Drohne\Medien"
#>
[CmdletBinding()]
param(
    [string]$Target = '\\homeassistant\share\dji\flightrecords',
    [string]$MediaTarget = '',
    [string[]]$MediaSource = @(),
    [switch]$NoProxy,
    [switch]$NoCover,
    [switch]$Stitch360,
    [int]$FisheyeFov = 190,
    [string]$FfmpegPath = '',
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
$MediaExtensions = @('.mp4', '.mov', '.osv', '.lrf', '.jpg', '.jpeg', '.dng', '.srt')
# powershell.exe -File cannot pass arrays, so the scheduled task joins them with ';'.
$MediaSource = @($MediaSource | ForEach-Object { $_ -split ';' } | Where-Object { $_ })
$DjiName = '^(DJI_(\d{8})\d{6}_\d{3,4}(?:_[A-Z])?)\.'

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
    $argList = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -Target `"$Target`""
    if ($MediaTarget) {
        $argList += " -MediaTarget `"$MediaTarget`""
        if ($MediaSource.Count) { $argList += " -MediaSource `"$($MediaSource -join ';')`"" }
        if ($NoProxy) { $argList += ' -NoProxy' }
        if ($NoCover) { $argList += ' -NoCover' }
        if ($Stitch360) { $argList += " -Stitch360 -FisheyeFov $FisheyeFov" }
        if ($FfmpegPath) { $argList += " -FfmpegPath `"$FfmpegPath`"" }
    }
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argList
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
    # Copying 360° originals (several GB each) over MTP takes a while.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description 'Copies DJI Fly flight records (and recordings) from USB-attached devices' -Force | Out-Null
    Write-Log "Scheduled task '$TaskName' registered (every $IntervalMinutes min, target $Target, media $MediaTarget)."
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

function Get-MtpStorages {
    <# Yields [pscustomobject]@{Device; Storage; Folder} for every storage of every portable device. #>
    $shell = New-Object -ComObject Shell.Application
    $thisPc = $shell.NameSpace(17)   # ssfDRIVES = "This PC", includes portable devices
    foreach ($device in $thisPc.Items()) {
        # Portable devices have a "::{...}" path and are folders but not drives.
        if (-not $device.IsFolder -or $device.Path -notlike '::{*') { continue }
        $deviceFolder = $device.GetFolder
        if ($null -eq $deviceFolder) { continue }
        foreach ($storage in $deviceFolder.Items()) {
            if (-not $storage.IsFolder) { continue }
            [pscustomobject]@{ Device = $device.Name; Storage = $storage.Name; Folder = $storage.GetFolder }
        }
    }
}

function Resolve-ShellPath {
    param($Folder, [string[]]$Segments)
    foreach ($segment in $Segments) {
        $child = Get-ShellChild -Folder $Folder -Name $segment
        if ($null -eq $child -or -not $child.IsFolder) { return $null }
        $Folder = $child.GetFolder
    }
    return $Folder
}

function Get-FlightRecordFolders {
    param($Storages)
    foreach ($s in $Storages) {
        foreach ($rel in $RelativePaths) {
            $folder = Resolve-ShellPath -Folder $s.Folder -Segments $rel
            if ($folder) { [pscustomobject]@{ Device = $s.Device; Storage = $s.Storage; Folder = $folder } }
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
    # At least 2 minutes, otherwise assume >= 5 MB/s over USB.
    $deadline = (Get-Date).AddSeconds([Math]::Max(120, $ExpectedSize / 5MB))
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if ((Test-Path $dest) -and ((Get-Item $dest).Length -ge $ExpectedSize)) { return $dest }
    }
    throw "Timeout copying $($Item.Name)"
}

$staging = Join-Path $env:TEMP 'DjiFlightSync'
if (-not (Test-Path $staging)) { New-Item -ItemType Directory -Path $staging -Force | Out-Null }

# ---------------------------------------------------------------------------
# Flight records
# ---------------------------------------------------------------------------
function Sync-FlightRecords {
    param($Storages)
    $folders = @(Get-FlightRecordFolders -Storages $Storages)
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
    Write-Log "Flight records done, $copied new file(s)."
}

# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------
function Find-Ffmpeg {
    if ($FfmpegPath) { return $FfmpegPath }
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-MediaFiles {
    <# Yields [pscustomobject]@{Name; Size; Path (file system) | Item (MTP); Source} #>
    param($Storages)
    $fsRoots = @($MediaSource)
    # SD card readers and drones that mount as a drive letter.
    foreach ($disk in Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=2' -ErrorAction SilentlyContinue) {
        $dcim = Join-Path "$($disk.DeviceID)\" 'DCIM'
        if (Test-Path $dcim) { $fsRoots += $dcim }
    }
    foreach ($root in $fsRoots) {
        if (-not (Test-Path $root)) { Write-Log "Media source $root not found"; continue }
        Get-ChildItem -Path $root -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'DJI_*' -and $MediaExtensions -contains $_.Extension.ToLower() } |
            ForEach-Object { [pscustomobject]@{ Name = $_.Name; Size = $_.Length; Path = $_.FullName; Item = $null; Source = $root } }
    }
    # Drone / goggles / RC attached over USB (MTP): <storage>\DCIM\<subfolder>\DJI_*
    foreach ($s in $Storages) {
        $dcim = Resolve-ShellPath -Folder $s.Folder -Segments @('DCIM')
        if (-not $dcim) { continue }
        $queue = New-Object System.Collections.Queue
        $queue.Enqueue($dcim)
        while ($queue.Count) {
            $folder = $queue.Dequeue()
            foreach ($item in $folder.Items()) {
                if ($item.IsFolder) { $queue.Enqueue($item.GetFolder); continue }
                $ext = [IO.Path]::GetExtension($item.Name).ToLower()
                if ($item.Name -notlike 'DJI_*' -or $MediaExtensions -notcontains $ext) { continue }
                [pscustomobject]@{
                    Name = $item.Name; Size = [long]$item.ExtendedProperty('System.Size'); Path = $null; Item = $item
                    Source = "$($s.Device)/$($s.Storage)"
                }
            }
        }
    }
}

function Get-MediaDestDir {
    param([string]$Name)
    if ($Name -match $DjiName) {
        $day = [datetime]::ParseExact($Matches[2], 'yyyyMMdd', $null)
    } else {
        $day = Get-Date
    }
    return Join-Path $MediaTarget (Join-Path $day.ToString('yyyy') $day.ToString('yyyy-MM-dd'))
}

function Get-LocalCopy {
    <# A local path of the source file: the file itself, or an MTP copy in staging. #>
    param($File)
    if ($File.Path) { return $File.Path }
    return Copy-MtpFile -Item $File.Item -Destination $staging -ExpectedSize $File.Size
}

function Invoke-Ffmpeg {
    param([string[]]$Arguments)
    $output = & $script:Ffmpeg -hide_banner -loglevel error -y @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed: $output" }
}

function Save-Cover {
    <# <base>_cover.jpg: the cover frame embedded in 360° originals, else a frame of the video. #>
    param([string]$VideoPath, [string]$CoverPath)
    $ffprobe = Join-Path (Split-Path $script:Ffmpeg) 'ffprobe.exe'
    $mjpeg = $null
    if (Test-Path $ffprobe) {
        $streams = & $ffprobe -v error -select_streams v -show_entries stream=index,codec_name -of csv=p=0 $VideoPath
        $mjpeg = $streams | Where-Object { $_ -match '^(\d+),mjpeg' } | ForEach-Object { $Matches[1] } | Select-Object -First 1
    }
    $tmp = Join-Path $staging ([IO.Path]::GetFileName($CoverPath))
    if ($mjpeg) {
        Invoke-Ffmpeg @('-i', $VideoPath, '-map', "0:$mjpeg", '-frames:v', '1', '-c', 'copy', $tmp)
    } else {
        Invoke-Ffmpeg @('-ss', '2', '-i', $VideoPath, '-frames:v', '1', '-vf', 'scale=960:-2', '-q:v', '4', $tmp)
    }
    Move-Item -Path $tmp -Destination $CoverPath -Force
}

function Save-StitchedProxy {
    <# Dual fisheye LRF -> equirectangular H.264 (the lenses point up and down, hence pitch=90). #>
    param([string]$LrfPath, [string]$DestPath)
    $tmp = Join-Path $staging ([IO.Path]::GetFileName($DestPath))
    Invoke-Ffmpeg @(
        '-i', $LrfPath, '-map', '0:v:0', '-map', '0:a?',
        '-vf', "v360=input=dfisheye:output=equirect:ih_fov=${FisheyeFov}:iv_fov=${FisheyeFov}:pitch=90",
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-movflags', '+faststart', $tmp
    )
    Move-Item -Path $tmp -Destination $DestPath -Force
}

function Sync-Media {
    param($Storages)
    if (-not $MediaTarget) { return }
    $files = @(Get-MediaFiles -Storages $Storages)
    if ($files.Count -eq 0) {
        Write-Verbose 'No recordings found.'
        return
    }
    $script:Ffmpeg = Find-Ffmpeg
    if (-not $script:Ffmpeg -and (-not $NoCover -or $Stitch360)) {
        Write-Log 'ffmpeg not found (PATH or -FfmpegPath): no cover images, no stitching.'
    }
    # Stems of 360° originals, to recognise their proxies.
    $stems360 = @{}
    foreach ($f in $files) {
        if ($f.Name -match '\.osv$') { $stems360[[IO.Path]::GetFileNameWithoutExtension($f.Name)] = $true }
    }

    $copied = 0
    foreach ($f in $files) {
        $stem = [IO.Path]::GetFileNameWithoutExtension($f.Name)
        $ext = [IO.Path]::GetExtension($f.Name).ToLower()
        $destDir = Get-MediaDestDir -Name $f.Name
        $isProxy = $ext -eq '.lrf'
        if ($isProxy -and $NoProxy) { continue }
        $destName = if ($isProxy) { "${stem}_proxy.mp4" } else { $f.Name }
        $dest = Join-Path $destDir $destName
        $stitch = $isProxy -and $Stitch360 -and $script:Ffmpeg -and $stems360.ContainsKey($stem)

        # A stitched proxy has another size than the LRF; existence is enough.
        $done = (Test-Path $dest) -and ($stitch -or (Get-Item $dest).Length -eq $f.Size)
        $wantCover = (-not $NoCover) -and $script:Ffmpeg -and ($ext -in '.osv', '.mp4', '.mov')
        $cover = Join-Path $destDir "${stem}_cover.jpg"
        $needCover = $wantCover -and -not (Test-Path $cover)
        if ($done -and -not $needCover) { continue }

        if ($WhatIf) {
            if (-not $done) { Write-Log "Would copy $($f.Name) ($([math]::Round($f.Size / 1MB)) MB) -> $dest$(if ($stitch) { ' (stitched)' })" }
            if ($needCover) { Write-Log "Would extract $cover" }
            continue
        }
        try {
            if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
            $local = $null
            if (-not $done) {
                $local = Get-LocalCopy -File $f
                if ($stitch) {
                    Write-Log "Stitching $($f.Name) (this takes a while)"
                    Save-StitchedProxy -LrfPath $local -DestPath $dest
                } elseif ($f.Path) {
                    # Copy next to the target first so OneDrive never uploads a half-written file.
                    $tmp = Join-Path $staging $destName
                    Copy-Item -LiteralPath $f.Path -Destination $tmp -Force
                    Move-Item -LiteralPath $tmp -Destination $dest -Force
                } else {
                    Move-Item -LiteralPath $local -Destination $dest -Force
                    $local = $dest
                }
                $copied++
                Write-Log "Copied $($f.Name) from $($f.Source) -> $dest"
            }
            if ($needCover) {
                Save-Cover -VideoPath $(if ($local -and (Test-Path $local)) { $local } else { $dest }) -CoverPath $cover
                Write-Log "Cover $cover"
            }
            # Drop MTP staging copies that were only needed for ffmpeg.
            if ($local -and -not $f.Path -and $local -ne $dest -and (Test-Path $local)) { Remove-Item -LiteralPath $local -Force }
        } catch {
            Write-Log "ERROR $($f.Name): $($_.Exception.Message)"
        }
    }
    Write-Log "Recordings done, $copied new file(s)."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
$storages = @(Get-MtpStorages)
Sync-FlightRecords -Storages $storages
Sync-Media -Storages $storages
