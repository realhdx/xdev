param(
    [Parameter(Mandatory)][ValidateSet('install','status','restart','remove')][string]$Action,
    [Parameter(Mandatory)][string]$State,
    [Parameter(Mandatory)][string]$XdevRoot,
    [Parameter(Mandatory)][string]$Pythonw
)
$ErrorActionPreference = 'Stop'
$description = 'xdev enrolled folder notification receiver v1'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

function Get-FullFile([string]$Path, [bool]$Directory) {
    if (-not [IO.Path]::IsPathRooted($Path)) { throw 'Use absolute local installation and state paths' }
    $item = Get-Item -LiteralPath $Path
    if ($item.PSIsContainer -ne $Directory -or $item.FullName.Contains('"')) { throw 'Unexpected path' }
    return [string]$item.FullName
}

$statePath = Get-FullFile $State $true
$rootPath = Get-FullFile $XdevRoot $true
$program = Get-FullFile $Pythonw $false
if ([IO.Path]::GetFileName($program) -ne 'pythonw.exe') { throw 'Use the configured Windows pythonw.exe' }
$worker = Get-FullFile (Join-Path $PSScriptRoot 'paired-folder-worker.py') $false
$workerArgs = '-I -B -X utf8 "' + $worker + '" --follow --state "' + $statePath + '" --xdev-root "' + $rootPath + '"'
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $suffix = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($statePath.ToUpperInvariant()))).Replace('-','').Substring(0,20)
} finally { $sha.Dispose() }
$taskName = 'xdev paired folder ' + $sid + ' ' + $suffix
$statusPath = Join-Path $statePath 'paired-folder-worker-status.json'

function Get-OwnedTask {
    $task = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
    if (-not $task) { return $null }
    $owner = $task.Principal.UserId
    if ($owner -ne $sid) {
        $owner = ([Security.Principal.NTAccount]::new($owner)).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    if ($task.Description -ne $description -or $owner -ne $sid -or
        $task.Actions.Count -ne 1 -or $task.Actions[0].Execute -ne $program -or $task.Actions[0].Arguments -cne $workerArgs) {
        throw 'Existing task does not match this receiver'
    }
    return $task
}

function Get-Status {
    $task = Get-OwnedTask
    $record = $null
    if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
        $record = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    $live = $false
    if ($task -and $task.State -eq 'Running' -and $record -and $record.pid -and $record.startup_id -match '^[0-9a-f]{32}$') {
        $proc = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$record.heartbeat_at
        if ($proc -and $proc.Path -eq $program -and $age -ge -2 -and $age -le 40) {
            $born = ([DateTimeOffset]$proc.StartTime).ToUnixTimeMilliseconds() / 1000.0
            $live = ([double]$record.started_at -ge $born - .1 -and [double]$record.started_at -le $born + 30)
        }
    }
    return [ordered]@{
        registered=[bool]$task; running=[bool]$live; cycle_ok=($live -and $record.cycle_ok -eq $true)
        phase=$record.phase; startup_id=$record.startup_id; pid=$record.pid; heartbeat_at=$record.heartbeat_at
        last_cycle_at=$record.last_cycle_at; last_notification=$record.last_notification
        pending_count=$record.pending_count; unknown_count=$record.unknown_count; retry_count=$record.retry_count
    }
}

function Stop-Receiver($task) {
    if ($task.State -in 'Running','Queued') {
        Stop-ScheduledTask -TaskName $taskName -TaskPath '\'
        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        while ((Get-OwnedTask).State -in 'Running','Queued') {
            if ([DateTime]::UtcNow -ge $deadline) { throw 'Receiver has not stopped' }
            Start-Sleep -Milliseconds 250
        }
    }
}

$task = Get-OwnedTask
if ($Action -eq 'status') { Get-Status | ConvertTo-Json -Compress; exit 0 }
if ($Action -eq 'remove') {
    if ($task) {
        Stop-Receiver $task
        Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false
    }
    Get-Status | ConvertTo-Json -Compress
    exit 0
}
if ($Action -eq 'restart' -and -not $task) { throw 'Install the receiver before restarting it' }
foreach ($path in @((Join-Path $statePath 'mail.sqlite'), (Join-Path $rootPath 'launch.py'),
                     (Join-Path $PSScriptRoot 'paired-folder-mail.py'), (Join-Path $PSScriptRoot 'github-pr-notice.py'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'An enrolled mailbox or installed adapter is missing' }
}
$old = Get-Status
if ($task) {
    if ($Action -eq 'install' -and $old.running) { $old | ConvertTo-Json -Compress; exit 0 }
    if ($task.State -eq 'Disabled') { throw 'Receiver is disabled' }
    Stop-Receiver $task
} else {
    $run = New-ScheduledTaskAction -Execute $program -Argument $workerArgs
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid
    $owner = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
    $policy = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -Hidden
    Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $run -Trigger $trigger -Principal $owner -Settings $policy -Description $description | Out-Null
}
Start-ScheduledTask -TaskName $taskName -TaskPath '\'
$deadline = [DateTime]::UtcNow.AddSeconds(60)
do {
    Start-Sleep -Milliseconds 750
    $current = Get-Status
    if ($current.running -and $current.startup_id -ne $old.startup_id -and $current.cycle_ok) {
        $current | ConvertTo-Json -Compress
        exit 0
    }
} while ([DateTime]::UtcNow -lt $deadline)
$current | ConvertTo-Json -Compress
exit 1
