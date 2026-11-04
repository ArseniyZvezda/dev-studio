# Wait for the owned backend process, not its listening socket (dev spec §2.7).
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StopFile,
    [int]$WaitMilliseconds = 18000
)
$ErrorActionPreference = 'Stop'
$backend = $null
try {
    $ownerFile = $StopFile + '.owner.json'
    if (Test-Path -LiteralPath $ownerFile) {
        $owner = Get-Content -LiteralPath $ownerFile -Raw | ConvertFrom-Json
        try {
            $backend = [System.Diagnostics.Process]::GetProcessById([int]$owner.pid)
            # Acquire a handle before signalling stop; a recycled PID cannot be killed.
            $null = $backend.Handle
            if ($backend.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() -ne $owner.created) {
                $backend.Dispose()
                $backend = $null
            }
        } catch [System.ArgumentException] { $backend = $null }
        catch [System.InvalidOperationException] { $backend = $null }
    }
    [System.IO.File]::WriteAllText($StopFile, 'stop')
    if ($backend -and -not $backend.WaitForExit($WaitMilliseconds)) {
        # The backend's kill-on-close job also ends descendants whose direct
        # parent has already exited. Process.Kill uses the acquired handle.
        $backend.Kill()
        if (-not $backend.WaitForExit(5000)) { throw 'Backend did not exit' }
    }
} finally {
    if ($backend) { $backend.Dispose() }
}
