# Offline only: no native gh, HTTP, dispatch or private-key reads.
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$testRoot=Join-Path $PSScriptRoot ('.tmp-closeout-'+[guid]::NewGuid().ToString('N'))
$null=New-Item -ItemType Directory -Path $testRoot
$passes=0
$priorToken=$env:GH_TOKEN
function Assert($Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function MustFail([scriptblock]$Action,[string]$Message) { try { & $Action } catch { return }; throw $Message }
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
    Write-Output "Closeout PowerShell: $passes assertions passed; zero real cloud/dispatch/key operations"
} finally {
    $env:GH_TOKEN=$priorToken
    $resolved=[IO.Path]::GetFullPath($testRoot)
    $allowed=[IO.Path]::GetFullPath($PSScriptRoot)+'\'
    if (-not $resolved.StartsWith($allowed,[StringComparison]::OrdinalIgnoreCase)) { throw 'Synthetic cleanup escaped tests' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
