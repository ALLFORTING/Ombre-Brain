#requires -Version 7.4
param([Parameter(Mandatory)][ValidateSet('Dispatch','ResumeDownload','Status','StageRecovery','Verify','Restore')][string]$Step)
$ErrorActionPreference='Stop'
$CloseoutRoot='D:\Codex\projects\OB-Backup-Recovery-20261004'

function Write-NewCloseoutJson([string]$Path, $Value) {
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 20)+"`n")
    $file=[IO.File]::Open($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write)
    try { $file.Write($bytes,0,$bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
}

function Get-CloseoutPlan {
    $plan=Get-Content -LiteralPath (Join-Path $CloseoutRoot 'backup-closeout-plan.json') -Raw | ConvertFrom-Json
    if ($plan.final_deployment_sha -cnotmatch '^[0-9a-f]{40}$') { throw 'PENDING_FINAL_DEPLOYMENT_SHA; stop before any operation' }
    if ($plan.ob_source_commit -cne $plan.final_deployment_sha) { throw 'Source and final deployment differ' }
    if ($plan.client_commit -cne 'a78711f63609552fddb18f2d7128194476a9aff8') { throw 'Client revision mismatch' }
    if ($plan.session_name -cnotmatch '^closeout-[0-9a-f]{32}$') { throw 'Invalid fresh session name' }
    foreach ($entry in @(@($plan.ob_source_worktree,$plan.ob_source_commit),@($plan.client_worktree,$plan.client_commit))) {
        $repo=[IO.Path]::GetFullPath($entry[0])
        if (-not $repo.StartsWith('D:\Codex\projects\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Worktree boundary mismatch' }
        $ancestor=Get-Item -LiteralPath $repo
        while ($null -ne $ancestor) {
            if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Worktree link denied' }
            if ($ancestor.FullName -ieq 'D:\Codex') { break }
            $ancestor=$ancestor.Parent
        }
        $head=& git -C $repo rev-parse HEAD
        if ($LASTEXITCODE -ne 0 -or $head -cne $entry[1]) { throw 'Source worktree revision mismatch' }
        $dirty=& git -C $repo status --porcelain
        if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'Source worktree changed' }
    }
    return $plan
}

function Get-OperatorSnapshot($Plan, [string]$RequestId, [string]$RunId, [string]$Attempt) {
    if ($RequestId -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' -or
        $RunId -cnotmatch '^[1-9][0-9]{0,19}$' -or $Attempt -cnotmatch '^[1-9][0-9]{0,19}$') { throw 'Invalid exact request identity' }
    if ($env:OMBRE_BACKUP_V2_STATUS_TOKEN -cnotmatch '^[A-Za-z0-9_-]{43}$') { throw 'Process-only dedicated status token required' }
    if ($Plan.operator_base_url -cne 'https://ombre-brain-forting.zeabur.app') { throw 'Status destination mismatch' }
    $uri=$Plan.operator_base_url+'/api/backup/v2/operator-status/'+$RequestId+'?original_run_id='+$RunId+'&original_run_attempt='+$Attempt
    $response=Invoke-WebRequest -Method Get -Uri $uri -MaximumRedirection 0 -Headers @{Authorization=('Bearer '+$env:OMBRE_BACKUP_V2_STATUS_TOKEN)}
    if ([int]$response.StatusCode -ne 200 -or $response.Headers['Cache-Control'] -notcontains 'no-store') { throw 'Operator status response rejected' }
    $snapshot=$response.Content | ConvertFrom-Json
    if ($snapshot.runtime_commit -cne $Plan.final_deployment_sha -or $snapshot.snapshot_consistent -ne $true) { throw 'Operator revision or consistency mismatch' }
    return $snapshot
}

function Assert-IdleSnapshot($Snapshot) {
    $c=$Snapshot.coordinator
    if ($Snapshot.controller_busy -ne $false -or $c.state -cne 'open' -or $c.lease_present -ne $false -or
        $null -ne $c.freeze_started_at -or $null -ne $c.freeze_deadline -or $null -ne $c.freeze_reason) {
        throw 'Current formal controller/coordinator not idle; no dispatch'
    }
}

function Assert-DownloadIdentity($Binding, $Original, [switch]$RequireAttempt) {
    if ([string]$Binding.run_id -cne $Original.run_id) { throw 'Download run identity mismatch; original identity unknown' }
    $attempt=[string]$Binding.run_attempt
    if (($RequireAttempt -or $attempt) -and $attempt -cne $Original.run_attempt) {
        throw 'Download attempt mismatch; no binding or success'
    }
}

function Get-OriginalRunIdentity([string]$Session, $Binding) {
    # Only saved original evidence establishes an attempt; never the current API value.
    $evidence=@()
    $identityPath=Join-Path $Session 'run-identity.json'
    if (Test-Path -LiteralPath $identityPath) {
        $saved=Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
        $evidence+=@{run_id=[string]$saved.run_id;run_attempt=[string]$saved.run_attempt}
    }
    $requestPath=Join-Path $Session 'request.json'
    if (Test-Path -LiteralPath $requestPath) {
        $saved=Get-Content -LiteralPath $requestPath -Raw | ConvertFrom-Json
        if ($saved.request_id -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') { throw 'Original request identity unknown' }
        $evidence+=@{run_id=[string]$saved.oidc_run_id;run_attempt=[string]$saved.oidc_run_attempt}
    }
    $originalPath=Join-Path $Session 'download\run-binding.json'
    if (-not (Test-Path -LiteralPath $originalPath)) { throw 'Original run/attempt evidence unknown; zero download' }
    $download=Get-Content -LiteralPath $originalPath -Raw | ConvertFrom-Json
    if ([string]$download.run_id -cnotmatch '^[1-9][0-9]{0,19}$') { throw 'Original run identity unknown; zero download' }
    if ($download.PSObject.Properties.Name -contains 'run_attempt') {
        $evidence+=@{run_id=[string]$download.run_id;run_attempt=[string]$download.run_attempt}
    }
    if ($evidence.Count -eq 0) { throw 'Original run/attempt evidence unknown; zero download' }
    $original=$evidence[0]
    foreach ($item in $evidence) {
        if ($item.run_id -cnotmatch '^[1-9][0-9]{0,19}$' -or $item.run_attempt -cnotmatch '^[1-9][0-9]{0,19}$' -or
            $item.run_id -cne $original.run_id -or $item.run_attempt -cne $original.run_attempt) {
            throw 'Original run/attempt evidence inconsistent; identity unknown'
        }
    }
    Assert-DownloadIdentity $download $original
    Assert-DownloadIdentity $Binding $original
    return $original
}

function Assert-CurrentRunIdentity($Original) {
    $raw=& gh api ('repos/ALLFORTING/ob-backup/actions/runs/'+$Original.run_id)
    if ($LASTEXITCODE -ne 0) { throw 'Original run lookup failed; identity unknown, no retry' }
    $run=($raw -join "`n") | ConvertFrom-Json
    if ([string]$run.id -cne $Original.run_id -or [string]$run.run_attempt -cne $Original.run_attempt) {
        throw 'Original run attempt changed; identity unknown, stop'
    }
}

function Save-RequestEvidence([string]$Session, [string]$Download) {
    $bindingPath=Join-Path $Download 'run-binding.json'
    if (-not (Test-Path -LiteralPath $bindingPath)) { throw 'No returned run ID; inspect GitHub without dispatching again' }
    $binding=Get-Content -LiteralPath $bindingPath -Raw | ConvertFrom-Json
    $original=Get-OriginalRunIdentity $Session $binding
    Assert-CurrentRunIdentity $original
    $logs=& gh run view $original.run_id --repo ALLFORTING/ob-backup --attempt $original.run_attempt --log
    if ($LASTEXITCODE -ne 0) { throw 'Original attempt log unavailable; request identity unknown' }
    $candidates=@{}
    foreach ($line in $logs) {
        if ($line -match '(\{"oidc_run_attempt":.*\})\s*$') {
            try { $item=$Matches[1] | ConvertFrom-Json -ErrorAction Stop } catch { continue }
            if ((($item.PSObject.Properties.Name | Sort-Object) -join ',') -cne 'oidc_run_attempt,oidc_run_id,request_id') { continue }
            if ($item.oidc_run_id -cne $original.run_id -or $item.oidc_run_attempt -cne $original.run_attempt -or
                $item.request_id -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') { throw 'Request evidence identity mismatch' }
            $candidates[$item.request_id]=$item
        }
    }
    if ($candidates.Count -ne 1) { throw 'Exact original request evidence unavailable or ambiguous; no resend' }
    $requestPath=Join-Path $Session 'request.json'
    $request=@($candidates.Values)[0]
    if (Test-Path -LiteralPath $requestPath) {
        $old=Get-Content -LiteralPath $requestPath -Raw | ConvertFrom-Json
        if ($old.request_id -cne $request.request_id -or $old.oidc_run_id -cne $request.oidc_run_id -or
            $old.oidc_run_attempt -cne $request.oidc_run_attempt) { throw 'Request evidence changed' }
    }
    # Validate everything, including a rerun during log retrieval, before first publication.
    Assert-CurrentRunIdentity $original
    if (-not (Test-Path -LiteralPath $requestPath)) { Write-NewCloseoutJson $requestPath $request }
    $identityPath=Join-Path $Session 'run-identity.json'
    if (-not (Test-Path -LiteralPath $identityPath)) {
        Write-NewCloseoutJson $identityPath @{run_id=$original.run_id;run_attempt=$original.run_attempt;original_job_lease_release='unknown'}
    }
}

function Invoke-BackupTransport($Plan, [string]$Output, [string]$ExistingRun) {
    $helper=Join-Path $Plan.client_worktree 'scripts\backup_v2_first_backup.ps1'
    if ($ExistingRun) { & $helper -RunId $ExistingRun -OutputDirectory $Output }
    else { & $helper -Dispatch -OutputDirectory $Output }
}

function Invoke-Closeout([string]$Action) {
    $plan=Get-CloseoutPlan
    $session=Join-Path $CloseoutRoot $plan.session_name
    if ($Action -in @('StageRecovery','Verify','Restore')) {
        $operation=@{StageRecovery='stage-recovery';Verify='verify';Restore='restore'}[$Action]
        & (Join-Path $CloseoutRoot 'recovery-venv\Scripts\python.exe') -B (Join-Path $CloseoutRoot 'local_prepare.py') $operation
        if ($LASTEXITCODE -ne 0) { throw 'Local stage failed; no complete success, no automatic retry' }
        return
    }
    if ($Action -eq 'Status') {
        if (-not (Test-Path -LiteralPath $session -PathType Container)) { throw 'No existing session for status evidence' }
        $requestPath=Join-Path $session 'request.json'
        if (Test-Path -LiteralPath $requestPath) {
            $request=Get-Content -LiteralPath $requestPath -Raw | ConvertFrom-Json
            $requestBinding='exact_original_attempt'
        } else {
            $request=[pscustomobject]@{request_id='00000000-0000-4000-8000-000000000001';oidc_run_id='1';oidc_run_attempt='1'}
            $requestBinding='unknown_query_placeholder'
        }
        $snapshot=Get-OperatorSnapshot $plan $request.request_id $request.oidc_run_id $request.oidc_run_attempt
        $observation=@{request_binding=$requestBinding;snapshot=$snapshot;original_job_lease_release='unknown'}
        Write-NewCloseoutJson (Join-Path $session ('status-'+[guid]::NewGuid().ToString('N')+'.json')) $observation
        $observation | ConvertTo-Json -Depth 12
        return
    }
    $priorToken=$env:GH_TOKEN
    try {
        $taskToken=& gh auth token --user ALLFORTING
        if ($LASTEXITCODE -ne 0 -or -not $taskToken) { throw 'ALLFORTING credential unavailable' }
        $env:GH_TOKEN=$taskToken
        $login=& gh api user --jq .login
        if ($LASTEXITCODE -ne 0 -or $login -cne 'ALLFORTING') { throw 'Account mismatch' }
        $main=& gh api repos/ALLFORTING/ob-backup/git/ref/heads/main --jq .object.sha
        if ($LASTEXITCODE -ne 0 -or $main -cne $plan.client_commit) { throw 'Client main changed' }
        $runId=$null
        $original=$null
        if ($Action -eq 'Dispatch') {
            if (Test-Path -LiteralPath $session) { throw 'Session already exists; do not dispatch again' }
            $expected=& gh api repos/ALLFORTING/ob-backup/actions/variables/OMBRE_BACKUP_V2_EXPECTED_COMMIT --jq .value
            if ($LASTEXITCODE -ne 0 -or $expected -cne $plan.final_deployment_sha) { throw 'Cloud expected commit mismatch' }
            $armed=& gh api repos/ALLFORTING/ob-backup/actions/variables/OMBRE_BACKUP_V2_ARMED --jq .value
            if ($LASTEXITCODE -ne 0 -or $armed -cne 'true') { throw 'Dispatch requires separately approved arming' }
            # Explicit query placeholder observes current state, never claims old-job binding.
            $snapshot=Get-OperatorSnapshot $plan '00000000-0000-4000-8000-000000000001' '1' '1'
            Assert-IdleSnapshot $snapshot
            $null=New-Item -ItemType Directory -Path $session
            Write-NewCloseoutJson (Join-Path $session 'preflight.json') $snapshot
            Write-NewCloseoutJson (Join-Path $session 'intent.json') @{client_commit=$plan.client_commit;runtime_commit=$plan.final_deployment_sha;original_job_lease_release='unknown'}
            $output=Join-Path $session 'download'
        } else {
            if (Test-Path -LiteralPath (Join-Path $session 'download-selection.json')) { throw 'A verified download is already selected' }
            $binding=Get-Content -LiteralPath (Join-Path $session 'download\run-binding.json') -Raw | ConvertFrom-Json
            $runId=[string]$binding.run_id
            $original=Get-OriginalRunIdentity $session $binding
            Assert-CurrentRunIdentity $original
            $runId=$original.run_id
            $output=Join-Path $session ('resume-'+[guid]::NewGuid().ToString('N'))
        }
        try { Invoke-BackupTransport $plan $output $runId } catch {
            try { Save-RequestEvidence $session $output } catch { Write-Warning $_.Exception.Message }
            throw
        }
        $record=Get-Content -LiteralPath (Join-Path $output 'run-binding.json') -Raw | ConvertFrom-Json
        $bound=Get-OriginalRunIdentity $session $record
        if ($null -ne $original) { Assert-DownloadIdentity $bound $original -RequireAttempt }
        else { $original=$bound }
        Assert-DownloadIdentity $record $original -RequireAttempt
        if ($record.workflow_commit -cne $plan.client_commit) { throw 'Downloaded workflow revision mismatch' }
        Save-RequestEvidence $session $output
        Assert-CurrentRunIdentity $original
        Write-NewCloseoutJson (Join-Path $session 'download-selection.json') @{download_directory=$output}
    } finally { $env:GH_TOKEN=$priorToken; $taskToken=$null }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-Closeout $Step }
