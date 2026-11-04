# Guard for installer replacement/uninstall; termination requires explicit consent (dev spec §2.7).
param(
    [Parameter(Mandatory = $true)][string]$Directory,
    [ValidateRange(0, 60)][int]$WaitSeconds = 0,
    [switch]$Terminate,
    [int]$InstallerProcessId = 0
)
$ErrorActionPreference = 'Stop'
try {
    $InstallRoot = [IO.Path]::GetFullPath($Directory).TrimEnd('\') + '\'
    if ($InstallRoot -eq [IO.Path]::GetPathRoot($InstallRoot)) { exit 2 }

    # Protect the helper and NSIS itself, including its outer launcher when setup
    # was saved in the installation folder. Never exempt the app launching an update.
    $Excluded = [Collections.Generic.HashSet[int]]::new()
    [void]$Excluded.Add($PID)
    if ($InstallerProcessId -gt 0) {
        $Installer = Get-CimInstance Win32_Process -Filter "ProcessId = $InstallerProcessId"
        if (-not $Installer -or -not $Installer.ExecutablePath) { exit 2 }
        $InstallerPath = $Installer.ExecutablePath
        while ($Installer -and $Installer.ExecutablePath -eq $InstallerPath) {
            if (-not $Excluded.Add([int]$Installer.ProcessId)) { break }
            $Parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($Installer.ParentProcessId)"
            if ($Parent -and $Parent.CreationDate -gt $Installer.CreationDate) { break }
            $Installer = $Parent
        }
    }
    $Deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
    do {
        $Busy = @(Get-CimInstance Win32_Process | Where-Object {
            -not $Excluded.Contains([int]$_.ProcessId) -and $_.ExecutablePath -and
                $_.ExecutablePath.StartsWith($InstallRoot, [StringComparison]::OrdinalIgnoreCase)
        })
        if ($Busy.Count -eq 0) { exit 0 }
        if ($Terminate) {
            foreach ($Candidate in $Busy) {
                # Recheck identity before stopping: a process may exit (and its PID
                # may be reused) between enumeration and the user's confirmation.
                $Process = Get-Process -Id $Candidate.ProcessId -ErrorAction SilentlyContinue
                if (-not $Process) { continue }
                try {
                    # CIM timestamps have microsecond precision; Process has 100ns ticks.
                    $StartedTicks = $Process.StartTime.ToUniversalTime().Ticks
                    $StartedTicks -= $StartedTicks % 10
                    # The CIM snapshot already checked the executable path. Process.Path
                    # is null for a 64-bit worker in NSIS's 32-bit PowerShell host;
                    # its start time still identifies the exact enumerated process.
                    if ($StartedTicks -eq $Candidate.CreationDate.ToUniversalTime().Ticks) {
                        Stop-Process -InputObject $Process -Force
                    }
                } catch {
                    # A concurrent normal exit is fine. Access denied / a surviving
                    # process remains busy on the next pass and cannot unlock setup.
                    if (-not $Process.HasExited) { Write-Verbose $_ }
                } finally { $Process.Dispose() }
            }
        }
        if ([DateTime]::UtcNow -ge $Deadline) { exit 1 }
        Start-Sleep -Milliseconds 250
    } while ($true)
} catch { exit 2 }
