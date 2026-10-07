# Offline only: no native gh, HTTP, dispatch or private-key reads.
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$testRoot=Join-Path $PSScriptRoot ('.tmp-closeout-'+[guid]::NewGuid().ToString('N'))
$null=New-Item -ItemType Directory -Path $testRoot
$passes=0
$priorToken=$env:GH_TOKEN
function Assert($Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function MustFail([scriptblock]$Action,[string]$Message,[string]$Pattern) { try { & $Action } catch { if ($Pattern -and $_.Exception.Message -notlike $Pattern) { throw }; return }; throw $Message }
try {
    . (Join-Path $root 'scripts\backup_closeout\Later-Steps.ps1') -Step Status
    $CloseoutRoot=$testRoot
    $script:cloudCalls=0
    function gh { $script:cloudCalls++; throw 'No cloud request permitted in pending-plan test' }
    Write-NewCloseoutJson (Join-Path $testRoot 'backup-closeout-plan.json') @{
        final_deployment_sha='PENDING_FINAL_DEPLOYMENT_SHA';ob_source_commit='1'*40;
        client_commit='a78711f63609552fddb18f2d7128194476a9aff8';session_name='closeout-'+('2'*32)}
    MustFail { Invoke-Closeout Dispatch } 'Pending deployment accepted'
    Assert ($script:cloudCalls -eq 0) 'Pending plan queried cloud'
    Assert (@(Get-ChildItem $testRoot).Count -eq 1) 'Pending plan wrote a session'
    $passes++
    $receipt=Join-Path $testRoot 'existing.json'
    Write-NewCloseoutJson $receipt @{keep=$true}
    MustFail { Write-NewCloseoutJson $receipt @{replace=$true} } 'Receipt overwritten'
    Assert ((Get-Content $receipt -Raw | ConvertFrom-Json).keep -eq $true) 'Receipt content changed'
    $passes++
    $plan=[pscustomobject]@{session_name='closeout-'+('3'*32);final_deployment_sha='1'*40;
                           client_commit='a78711f63609552fddb18f2d7128194476a9aff8'}
    function Get-CloseoutPlan { return $plan }
    $global:LASTEXITCODE=0
    function gh {
        $global:LASTEXITCODE=0
        if ($args[0] -eq 'auth') { return 'synthetic-test-credential' }
        if ($args[1] -eq 'user') { return 'ALLFORTING' }
        if ($args[1] -like '*git/ref*') { return $plan.client_commit }
        if ($args[1] -like '*EXPECTED_COMMIT') { return $plan.final_deployment_sha }
        if ($args[1] -like '*ARMED') { return 'true' }
        throw 'Unexpected synthetic gh query'
    }
    function Get-OperatorSnapshot { return [pscustomobject]@{controller_busy=$false;coordinator=[pscustomobject]@{
        state='open';lease_present=$false;freeze_started_at=$null;freeze_deadline=$null;freeze_reason=$null}} }
    $script:transportCalls=0
    function Invoke-BackupTransport($Plan,[string]$Output,[string]$ExistingRun) {
        Assert (-not $ExistingRun) 'Unexpected run resume'
        $script:transportCalls++
        $null=New-Item -ItemType Directory -Path $Output
        Write-NewCloseoutJson (Join-Path $Output 'run-binding.json') @{run_id='42'}
        throw 'synthetic lost response or transport failure'
    }
    function Save-RequestEvidence([string]$Session,[string]$Download) {
        Write-NewCloseoutJson (Join-Path $Session 'request.json') @{
            request_id='11111111-1111-4111-8111-111111111111';oidc_run_id='42';oidc_run_attempt='1'}
    }
    MustFail { Invoke-Closeout Dispatch } 'Transport failure reported success'
    Assert ($script:transportCalls -eq 1) 'Automatic transport retry occurred'
    $session=Join-Path $testRoot $plan.session_name
    Assert (Test-Path (Join-Path $session 'request.json')) 'Failure lost request evidence'
    Assert (-not (Test-Path (Join-Path $session 'download-selection.json'))) 'Failure selected a successful download'
    MustFail { Invoke-Closeout Dispatch } 'Second dispatch accepted'
    Assert ($script:transportCalls -eq 1) 'Existing session dispatched again'
    $passes+=2
    # Restore the real parser, then feed only synthetic original-attempt log lines.
    . (Join-Path $root 'scripts\backup_closeout\Later-Steps.ps1') -Step Status
    $CloseoutRoot=$testRoot
    function gh {
        $global:LASTEXITCODE=0
        if ($args[0] -eq 'api') { return '{"id":42,"run_attempt":1}' }
        return 'Capture step 2026-10-07T00:00:00Z {"oidc_run_attempt": "1", "oidc_run_id": "42", "request_id": "11111111-1111-4111-8111-111111111111"}'
    }
    Save-RequestEvidence $session (Join-Path $session 'download')
    $identity=Get-Content (Join-Path $session 'run-identity.json') -Raw | ConvertFrom-Json
    Assert ($identity.run_id -eq '42' -and $identity.run_attempt -eq '1') 'Original attempt parser failed'
    $passes++
    function gh {
        $global:LASTEXITCODE=0
        if ($args[0] -eq 'api') { return '{"id":42,"run_attempt":2}' }
        throw 'Must reject changed attempt before logs'
    }
    MustFail { Save-RequestEvidence $session (Join-Path $session 'download') } 'Changed attempt accepted'
    $passes++
    $plan=[pscustomobject]@{session_name='closeout-'+('4'*32);final_deployment_sha='1'*40}
    function Get-CloseoutPlan { return $plan }
    function Get-OperatorSnapshot($Plan,[string]$RequestId,[string]$RunId,[string]$Attempt) {
        Assert ($RequestId -ceq '00000000-0000-4000-8000-000000000001' -and $RunId -ceq '1' -and $Attempt -ceq '1') 'Unknown request guessed an identity'
        return [pscustomobject]@{snapshot_consistent=$true;job_lookup='not_found';job=$null}
    }
    $unknownSession=Join-Path $testRoot $plan.session_name
    $null=New-Item -ItemType Directory -Path $unknownSession
    $null=Invoke-Closeout Status
    $statusFile=@(Get-ChildItem $unknownSession -Filter 'status-*.json')[0]
    $observation=Get-Content -LiteralPath $statusFile.FullName -Raw | ConvertFrom-Json
    Assert ($observation.request_binding -ceq 'unknown_query_placeholder' -and $observation.original_job_lease_release -ceq 'unknown') 'Placeholder confused with original job evidence'
    $passes++
    # Exercise the real resume/evidence functions with synthetic original receipts only.
    . (Join-Path $root 'scripts\backup_closeout\Later-Steps.ps1') -Step Status
    $CloseoutRoot=$testRoot
    function Get-CloseoutPlan { return $script:resumePlan }
    function New-ResumeCase([switch]$MissingAttempt) {
        $script:resumePlan=[pscustomobject]@{session_name='closeout-'+[guid]::NewGuid().ToString('N');
            client_commit='a78711f63609552fddb18f2d7128194476a9aff8';final_deployment_sha='1'*40}
        $script:resumeSession=Join-Path $testRoot $script:resumePlan.session_name
        $download=Join-Path $script:resumeSession 'download'
        $null=New-Item -ItemType Directory -Path $download
        $binding=@{run_id='42';workflow_commit=$script:resumePlan.client_commit}
        if (-not $MissingAttempt) { $binding.run_attempt=1 }
        Write-NewCloseoutJson (Join-Path $download 'run-binding.json') $binding
        $script:transportCalls=0; $script:runQueries=0
        $script:apiAttempt='1'; $script:logAttempt='1'; $script:transportMode='normal'
        $script:rerunDuringLogs=$false
    }
    function Assert-NoResumeSuccess {
        Assert (-not (Test-Path (Join-Path $script:resumeSession 'download-selection.json'))) 'Rejected download reported success'
        Assert (-not (Test-Path (Join-Path $script:resumeSession 'run-identity.json'))) 'Rejected evidence published identity'
        Assert (-not (Test-Path (Join-Path $script:resumeSession 'request.json'))) 'Rejected evidence published request'
    }
    function gh {
        $global:LASTEXITCODE=0
        if ($args[0] -eq 'auth') { return 'synthetic-test-credential' }
        if ($args[0] -eq 'api') {
            if ($args[1] -eq 'user') { return 'ALLFORTING' }
            if ($args[1] -like '*git/ref*') { return $script:resumePlan.client_commit }
            if ($args[1] -eq 'repos/ALLFORTING/ob-backup/actions/runs/42') {
                $script:runQueries++
                return (@{id=42;run_attempt=[int]$script:apiAttempt} | ConvertTo-Json -Compress)
            }
        }
        if ($args[0] -eq 'run') {
            Assert ($args[2] -ceq '42' -and $args[6] -ceq '1') 'Logs queried a substituted attempt'
            if ($script:rerunDuringLogs) { $script:apiAttempt='2' }
            return ('Capture step {"oidc_run_attempt":"'+$script:logAttempt+'","oidc_run_id":"42","request_id":"11111111-1111-4111-8111-111111111111"}')
        }
        throw 'Unexpected synthetic gh query'
    }
    function Invoke-BackupTransport($Plan,[string]$Output,[string]$ExistingRun) {
        Assert ($ExistingRun -ceq '42') 'Resume dispatched or changed the original run'
        $script:transportCalls++
        $null=New-Item -ItemType Directory -Path $Output
        $attempt=1
        if ($script:transportMode -eq 'wrong-receipt') { $attempt=2 }
        Write-NewCloseoutJson (Join-Path $Output 'run-binding.json') @{
            run_id='42';run_attempt=$attempt;workflow_commit=$Plan.client_commit}
        if ($script:transportMode -eq 'rerun') { $script:apiAttempt='2' }
    }
    New-ResumeCase
    $script:apiAttempt='2'
    MustFail { Invoke-Closeout ResumeDownload } 'Original attempt 1 replaced by API attempt 2' '*attempt changed*'
    Assert ($script:transportCalls -eq 0) 'Changed attempt invoked helper'
    Assert-NoResumeSuccess
    $passes++

    New-ResumeCase -MissingAttempt
    $message=$null
    try { Invoke-Closeout ResumeDownload } catch { $message=$_.Exception.Message }
    Assert ($message -like '*unknown*') 'Missing original attempt did not remain unknown'
    Assert ($script:transportCalls -eq 0 -and $script:runQueries -eq 0) 'Missing attempt downloaded or adopted API identity'
    Assert-NoResumeSuccess
    $passes++

    New-ResumeCase
    $bad=Join-Path $script:resumeSession 'synthetic-receipt'
    $null=New-Item -ItemType Directory -Path $bad
    Write-NewCloseoutJson (Join-Path $bad 'run-binding.json') @{run_id='42';run_attempt=2}
    MustFail { Save-RequestEvidence $script:resumeSession $bad } 'Mismatched receipt created first identity' '*Download attempt mismatch*'
    Assert-NoResumeSuccess
    $passes++

    New-ResumeCase
    $script:logAttempt='2'
    MustFail { Save-RequestEvidence $script:resumeSession (Join-Path $script:resumeSession 'download') } 'Mismatched request created first identity' '*Request evidence identity mismatch*'
    Assert-NoResumeSuccess
    $passes++

    New-ResumeCase
    $script:rerunDuringLogs=$true
    MustFail { Save-RequestEvidence $script:resumeSession (Join-Path $script:resumeSession 'download') } 'Rerun during logs created first identity' '*attempt changed*'
    Assert-NoResumeSuccess
    $passes++

    foreach ($mode in @('wrong-receipt','rerun')) {
        New-ResumeCase
        $script:transportMode=$mode
        $pattern=if ($mode -eq 'wrong-receipt') { '*Download attempt mismatch*' } else { '*attempt changed*' }
        MustFail { Invoke-Closeout ResumeDownload } 'Download receipt or current attempt changed but succeeded' $pattern
        Assert ($script:transportCalls -eq 1) 'Failed download automatically retried'
        Assert-NoResumeSuccess
        $passes++
    }

    New-ResumeCase
    $identityPath=Join-Path $script:resumeSession 'run-identity.json'
    Write-NewCloseoutJson $identityPath @{run_id='42';run_attempt='1';original_job_lease_release='unknown';keep='original bytes'}
    $before=(Get-FileHash -LiteralPath $identityPath).Hash
    Invoke-Closeout ResumeDownload
    Assert ($script:transportCalls -eq 1) 'Matching original attempt did not download exactly once'
    Assert (Test-Path (Join-Path $script:resumeSession 'download-selection.json')) 'Matching download not selected'
    Assert ((Get-FileHash -LiteralPath $identityPath).Hash -ceq $before) 'Existing identity was overwritten'
    $request=Get-Content (Join-Path $script:resumeSession 'request.json') -Raw | ConvertFrom-Json
    Assert ($request.oidc_run_id -ceq '42' -and $request.oidc_run_attempt -ceq '1') 'Successful request binding changed'
    $passes++

    Write-Output "Closeout PowerShell: $passes assertions passed; zero real cloud/dispatch/key operations"
} finally {
    $env:GH_TOKEN=$priorToken
    $resolved=[IO.Path]::GetFullPath($testRoot)
    $allowed=[IO.Path]::GetFullPath($PSScriptRoot)+'\'
    if (-not $resolved.StartsWith($allowed,[StringComparison]::OrdinalIgnoreCase)) { throw 'Synthetic cleanup escaped tests' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
