[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = "help",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CommandArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    [IO.File]::WriteAllText($Path, $Content, $script:Utf8NoBom)
}

function Fail([string]$Message) {
    throw "traceable-rag: $Message"
}

function Assert-NoControlCharacters([string]$Name, [string]$Value) {
    foreach ($Character in $Value.ToCharArray()) {
        if ([char]::IsControl($Character)) { Fail "$Name cannot contain control characters" }
    }
}

function Get-NormalizedRootedPath([string]$Name, [string]$Value) {
    Assert-NoControlCharacters $Name $Value
    if (-not [IO.Path]::IsPathRooted($Value)) { Fail "$Name must be an absolute path: $Value" }
    try { $Full = [IO.Path]::GetFullPath($Value) } catch { Fail "$Name is not a valid path: $Value" }
    $RootPath = [IO.Path]::GetPathRoot($Full)
    while ($Full.Length -gt $RootPath.Length -and ($Full.EndsWith("\") -or $Full.EndsWith("/"))) { $Full = $Full.Substring(0, $Full.Length - 1) }
    return $Full
}

function Read-ValidatedReleaseManifest([string]$Path, [string]$ExpectedVersion, [string]$ExpectedImageRepository) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "release manifest is missing: $Path" }
    $Item = Get-Item -LiteralPath $Path -Force
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail "release manifest must not be a reparse point: $Path" }
    try { $Manifest = [IO.File]::ReadAllText($Path) | ConvertFrom-Json } catch { Fail "release manifest is not valid JSON" }
    if ($Manifest.schema_version -ne 1) { Fail "unsupported release manifest schema version" }
    if ($Manifest.version -ne $ExpectedVersion) { Fail "release manifest version mismatch" }
    if ($Manifest.image -ne $ExpectedImageRepository) { Fail "release manifest image repository mismatch" }
    if ($Manifest.index_digest -notmatch '^sha256:[0-9a-f]{64}$') { Fail "release manifest index digest is invalid" }
    if ($Manifest.source_commit -notmatch '^[0-9a-f]{40}$') { Fail "release manifest source commit is invalid" }
    if ($Manifest.platforms.'linux/amd64' -notmatch '^sha256:[0-9a-f]{64}$' -or $Manifest.platforms.'linux/arm64' -notmatch '^sha256:[0-9a-f]{64}$') { Fail "release manifest platform digests are invalid" }
    return $Manifest
}

function Get-ManagedLauncherContent {
    $EscapedConfig = $ConfigDir.Replace("'", "''")
    $EscapedManager = (Join-Path $ConfigDir "traceable-rag.ps1").Replace("'", "''")
    return "# Traceable-RAG installation: $InstallationId`r`n`$env:TRACEABLE_RAG_CONFIG_DIR = '$EscapedConfig'`r`n& '$EscapedManager' @args`r`n"
}

function Get-ManagedWrapperContent {
    return "@echo off`r`nrem Traceable-RAG installation: $InstallationId`r`n@chcp 65001 >nul`r`npowershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File `"%~dp0traceable-rag-launcher.ps1`" %*`r`n"
}

function Show-Help {
    @"
Usage: traceable-rag <command> [options]

Commands:
  start
  stop
  status
  setup-url
  doctor [--json] [--test-provider ROLE ...]
  logs [compose logs options]
  backup [label]
  upgrade <vX.Y.Z>
  rollback
  recover
  agent install-codex [--knowledge-base-id ID] [--adopt-existing]
  agent doctor-codex
  admin rotate-token
  uninstall [--purge --yes]
  help

Provider tests run only for explicitly named roles: mineru, embedding,
reranker and llm.
"@
}

if ($Command -in @("help", "-h", "--help")) {
    Show-Help
    exit 0
}

$ConfigDir = Get-NormalizedRootedPath "TRACEABLE_RAG_CONFIG_DIR" $(if ($env:TRACEABLE_RAG_CONFIG_DIR) { $env:TRACEABLE_RAG_CONFIG_DIR } else { $PSScriptRoot })
$EnvFile = Join-Path $ConfigDir "deployment.env"
$ComposeFile = Join-Path $ConfigDir "compose.yaml"
$ReleaseManifestFile = Join-Path $ConfigDir "release-manifest.json"
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { Fail "missing $EnvFile; run install.ps1 first" }
if (-not (Test-Path -LiteralPath $ComposeFile -PathType Leaf)) { Fail "missing $ComposeFile; run install.ps1 again" }

$AllowedSettings = @(
    "TRACEABLE_RAG_IMAGE_REPOSITORY",
    "TRACEABLE_RAG_RELEASE_REPOSITORY",
    "TRACEABLE_RAG_RELEASE_VERSION",
    "TRACEABLE_RAG_RELEASE_SOURCE_COMMIT",
    "TRACEABLE_RAG_IMAGE",
    "TRACEABLE_RAG_PROJECT_NAME",
    "TRACEABLE_RAG_BIND_ADDRESS",
    "TRACEABLE_RAG_PORT",
    "TRACEABLE_RAG_READY_TIMEOUT_SECONDS",
    "TRACEABLE_RAG_DATA_DIR",
    "TRACEABLE_RAG_MASTER_KEY_HOST_FILE",
    "TRACEABLE_RAG_ADMIN_TOKEN_HOST_FILE",
    "TRACEABLE_RAG_STATE_DIR",
    "TRACEABLE_RAG_BACKUP_DIR",
    "TRACEABLE_RAG_BIN_LINK",
    "TRACEABLE_RAG_CONTAINER_UID",
    "TRACEABLE_RAG_CONTAINER_GID",
    "TRACEABLE_RAG_INSTALLATION_ID"
)
$Config = @{}
foreach ($Line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
    if ([string]::IsNullOrWhiteSpace($Line) -or $Line.StartsWith("#")) { continue }
    $Parts = $Line.Split("=", 2)
    if ($Parts.Count -ne 2) { Fail "invalid deployment.env line" }
    if ($Parts[0] -notin $AllowedSettings) { Fail "unsupported deployment setting: $($Parts[0])" }
    if ($Parts[1] -match '[#$]') { Fail "deployment values cannot contain dollar signs or #" }
    $Config[$Parts[0]] = $Parts[1]
    [Environment]::SetEnvironmentVariable($Parts[0], $Parts[1], "Process")
}
foreach ($Required in $AllowedSettings | Where-Object { $_ -notin @("TRACEABLE_RAG_BIN_LINK", "TRACEABLE_RAG_RELEASE_SOURCE_COMMIT") }) {
    if (-not $Config.ContainsKey($Required) -or [string]::IsNullOrWhiteSpace($Config[$Required])) {
        Fail "missing $Required"
    }
}

$ImageRepository = $Config.TRACEABLE_RAG_IMAGE_REPOSITORY
$ReleaseRepository = $Config.TRACEABLE_RAG_RELEASE_REPOSITORY
$ReleaseVersion = $Config.TRACEABLE_RAG_RELEASE_VERSION
$ReleaseSourceCommit = if ($Config.ContainsKey("TRACEABLE_RAG_RELEASE_SOURCE_COMMIT")) { $Config.TRACEABLE_RAG_RELEASE_SOURCE_COMMIT } else { "" }
$Image = $Config.TRACEABLE_RAG_IMAGE
$ProjectName = $Config.TRACEABLE_RAG_PROJECT_NAME
$BindAddress = $Config.TRACEABLE_RAG_BIND_ADDRESS
$Port = [int]$Config.TRACEABLE_RAG_PORT
$ReadyTimeout = [int]$Config.TRACEABLE_RAG_READY_TIMEOUT_SECONDS
$DataDir = Get-NormalizedRootedPath "TRACEABLE_RAG_DATA_DIR" $Config.TRACEABLE_RAG_DATA_DIR
$MasterKeyFile = Get-NormalizedRootedPath "TRACEABLE_RAG_MASTER_KEY_HOST_FILE" $Config.TRACEABLE_RAG_MASTER_KEY_HOST_FILE
$AdminTokenFile = Get-NormalizedRootedPath "TRACEABLE_RAG_ADMIN_TOKEN_HOST_FILE" $Config.TRACEABLE_RAG_ADMIN_TOKEN_HOST_FILE
$StateDir = Get-NormalizedRootedPath "TRACEABLE_RAG_STATE_DIR" $Config.TRACEABLE_RAG_STATE_DIR
$BackupDir = Get-NormalizedRootedPath "TRACEABLE_RAG_BACKUP_DIR" $Config.TRACEABLE_RAG_BACKUP_DIR
$InstallationId = $Config.TRACEABLE_RAG_INSTALLATION_ID
$PendingOperationFile = Join-Path $StateDir "pending-operation"
$HasPendingOperation = Test-Path -LiteralPath $PendingOperationFile -PathType Leaf
$ConfigFull = $ConfigDir
$DataFull = $DataDir
$StateFull = $StateDir
$BackupFull = $BackupDir
if ($BackupFull.StartsWith("$ConfigFull\", [StringComparison]::OrdinalIgnoreCase) -or $BackupFull.StartsWith("$DataFull\", [StringComparison]::OrdinalIgnoreCase)) { Fail "backup directory must not be inside config or data" }
if ($DataFull.StartsWith("$ConfigFull\", [StringComparison]::OrdinalIgnoreCase) -or $DataFull.StartsWith("$StateFull\", [StringComparison]::OrdinalIgnoreCase) -or $DataFull.StartsWith("$BackupFull\", [StringComparison]::OrdinalIgnoreCase)) { Fail "data directory must remain independent from config, state and backups" }

function Assert-SafeSecret([string]$Path, [int]$ExpectedLength, [switch]$Hex) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "secret is missing: $Path" }
    $Item = Get-Item -LiteralPath $Path -Force
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail "secret path must not be a reparse point: $Path" }
    if ($Item.Length -ne $ExpectedLength) { Fail "secret has an invalid length: $Path" }
    if ($Hex -and [IO.File]::ReadAllText($Path) -notmatch '^[0-9a-f]{64}$') { Fail "admin token must encode 32 random bytes as 64 hex characters" }
    $CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $Allowed = @($CurrentSid, "S-1-5-18")
    foreach ($Rule in (Get-Acl -LiteralPath $Path).Access) {
        if ($Rule.AccessControlType -ne "Allow") { continue }
        $Sid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($Sid -notin $Allowed) { Fail "secret ACL contains an unexpected principal: $Sid" }
    }
}

function Assert-InstallationMarker([string]$OwnedDir) {
    if (-not (Test-Path -LiteralPath $OwnedDir -PathType Container)) { Fail "installation directory does not exist: $OwnedDir" }
    $OwnedItem = Get-Item -LiteralPath $OwnedDir -Force
    if (($OwnedItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail "installation directory must not be a reparse point: $OwnedDir" }
    $Marker = Join-Path $OwnedDir ".traceable-rag-installation"
    if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) { Fail "missing safe uninstall marker: $Marker" }
    $MarkerItem = Get-Item -LiteralPath $Marker -Force
    if (($MarkerItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail "installation marker must not be a reparse point: $Marker" }
    if ([IO.File]::ReadAllText($Marker) -ne $InstallationId) { Fail "installation marker mismatch: $OwnedDir" }
}

function Read-PendingOperation {
    if (-not (Test-Path -LiteralPath $PendingOperationFile -PathType Leaf)) { Fail "pending operation journal is missing: $PendingOperationFile" }
    $PendingItem = Get-Item -LiteralPath $PendingOperationFile -Force
    if (($PendingItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail "pending operation journal must not be a reparse point" }
    $Pending = @{}
    foreach ($Line in Get-Content -LiteralPath $PendingOperationFile -Encoding UTF8) {
        $Parts = $Line.Split("=", 2)
        if ($Parts.Count -ne 2 -or $Parts[0] -notin @("operation", "pid", "record", "was_running") -or $Pending.ContainsKey($Parts[0])) {
            Fail "pending operation journal is invalid"
        }
        $Pending[$Parts[0]] = $Parts[1]
    }
    foreach ($Required in @("operation", "pid", "record", "was_running")) {
        if (-not $Pending.ContainsKey($Required)) { Fail "pending operation journal is missing $Required" }
    }
    if ($Pending.operation -notin @("backup", "upgrade", "rollback")) { Fail "pending operation type is invalid: $($Pending.operation)" }
    if ($Pending.pid -notmatch '^[0-9]+$') { Fail "pending operation has invalid pid" }
    if ($Pending.was_running -notin @("0", "1")) { Fail "pending operation has invalid running state" }
    return $Pending
}

function Get-InterruptedRestoreDisplacement {
    $Pending = Read-PendingOperation
    if ($Pending.operation -notin @("upgrade", "rollback")) { Fail "data directory is missing outside a restorable pending operation" }
    $Parent = Split-Path -Parent $DataDir
    $Base = Split-Path -Leaf $DataDir
    if (-not (Test-Path -LiteralPath $Parent -PathType Container)) { Fail "data parent directory does not exist: $Parent" }
    $CandidatePattern = '^\.' + [Regex]::Escape($Base) + '\.displaced\.[0-9]+$'
    $Candidates = @(Get-ChildItem -LiteralPath $Parent -Directory -Force | Where-Object { $_.Name -match $CandidatePattern })
    if ($Candidates.Count -ne 1) { Fail "data directory is missing and interrupted restore state is ambiguous under $Parent" }
    $Candidate = $Candidates[0]
    if (($Candidate.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail "interrupted restore displacement is a reparse point" }
    Assert-InstallationMarker $Candidate.FullName
    return $Candidate
}

function Assert-InstallationMarkers([switch]$RequireActiveData) {
    foreach ($OwnedDir in @($ConfigDir, $StateDir, $BackupDir)) {
        Assert-InstallationMarker $OwnedDir
    }
    if (Test-Path -LiteralPath $DataDir -PathType Container) {
        Assert-InstallationMarker $DataDir
    } elseif (Test-Path -LiteralPath $DataDir) {
        Fail "data path is not a safe installation directory: $DataDir"
    } elseif ($HasPendingOperation -and -not $RequireActiveData) {
        Get-InterruptedRestoreDisplacement | Out-Null
    } else {
        Fail "data directory does not exist: $DataDir"
    }
}

if ($ImageRepository -notmatch '^ghcr\.io/[a-z0-9._/-]+$') { Fail "image repository must be on GHCR" }
if ($ReleaseRepository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { Fail "release repository must be owner/name" }
if ($Image -notmatch '^ghcr\.io/[a-z0-9._/-]+(:v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?|@sha256:[0-9a-f]{64})$') {
    Fail "image must use an exact vX.Y.Z tag or sha256 digest; latest is forbidden"
}
if (-not ($Image.StartsWith("${ImageRepository}:") -or $Image.StartsWith("${ImageRepository}@"))) {
    Fail "configured image does not belong to $ImageRepository"
}
if ($ReleaseVersion -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$') { Fail "release version must be an exact vX.Y.Z value" }
if ($ProjectName -notmatch '^[a-z0-9][a-z0-9_-]*$') { Fail "invalid Compose project name" }
if ($BindAddress -notin @("127.0.0.1", "::1")) { Fail "non-loopback binding is not supported by the bundled deployment" }
if ($Port -lt 1 -or $Port -gt 65535) { Fail "port must be between 1 and 65535" }
if ($ReadyTimeout -lt 30 -or $ReadyTimeout -gt 86400) { Fail "readiness timeout must be between 30 and 86400 seconds" }
Assert-SafeSecret $MasterKeyFile 32
Assert-SafeSecret $AdminTokenFile 64 -Hex
$AdminToken = [IO.File]::ReadAllText($AdminTokenFile)
if ($Config.TRACEABLE_RAG_CONTAINER_UID -ne "10001" -or $Config.TRACEABLE_RAG_CONTAINER_GID -ne "10001") { Fail "Windows deployment must retain the image non-root UID/GID" }
if ([IO.File]::ReadAllText($ComposeFile) -notmatch 'user:\s*"10001:10001"') { Fail "Compose user does not match the installed non-root UID/GID" }
if ($InstallationId -notmatch '^[0-9a-f]{32}$') { Fail "installation ID is invalid" }
foreach ($PathPair in @(
    @("TRACEABLE_RAG_DATA_DIR", $DataDir), @("TRACEABLE_RAG_MASTER_KEY_HOST_FILE", $MasterKeyFile),
    @("TRACEABLE_RAG_ADMIN_TOKEN_HOST_FILE", $AdminTokenFile), @("TRACEABLE_RAG_STATE_DIR", $StateDir),
    @("TRACEABLE_RAG_BACKUP_DIR", $BackupDir)
)) {
    if (-not [string]::Equals($Config[$PathPair[0]], $PathPair[1], [StringComparison]::OrdinalIgnoreCase)) { Fail "$($PathPair[0]) is not stored as a normalized absolute path" }
}
if ((Get-Content -LiteralPath $EnvFile -Raw) -match '(?m)^(MINERU_TOKEN|MINERU_API_KEY|OPENAI_EMBEDDING_API_KEY|TRACEABLE_RAG_EMBEDDING_API_KEY|OPENAI_API_KEY)=') {
    Fail "Provider credentials are forbidden in deployment.env"
}
Assert-InstallationMarkers

function Assert-InstalledReleaseIdentity {
    if ($script:ReleaseSourceCommit -or (Test-Path -LiteralPath $ReleaseManifestFile)) {
        if (-not $script:ReleaseSourceCommit) { Fail "release manifest exists but deployment.env has no source commit" }
        $InstalledManifest = Read-ValidatedReleaseManifest $ReleaseManifestFile $script:ReleaseVersion $ImageRepository
        if ($InstalledManifest.source_commit -ne $script:ReleaseSourceCommit) { Fail "release source commit does not match the installed manifest" }
        if ($script:Image -ne "$($InstalledManifest.image)@$($InstalledManifest.index_digest)") { Fail "configured image digest does not match the installed release manifest" }
    }
}
if (-not $HasPendingOperation) { Assert-InstalledReleaseIdentity }

$script:OperationMutex = $null
function Acquire-OperationLock {
    $Mutex = New-Object Threading.Mutex($false, "Local\TraceableRAG-$InstallationId")
    try { $Acquired = $Mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $Acquired = $true }
    if (-not $Acquired) { $Mutex.Dispose(); Fail "another deployment-changing command holds the installation lock" }
    $script:OperationMutex = $Mutex
}

function Release-OperationLock {
    if ($script:OperationMutex) {
        try { $script:OperationMutex.ReleaseMutex() } catch { }
        $script:OperationMutex.Dispose()
        $script:OperationMutex = $null
    }
}

function Assert-NoPendingOperation {
    if (Test-Path -LiteralPath $PendingOperationFile -PathType Leaf) {
        $Pending = [IO.File]::ReadAllText($PendingOperationFile)
        Fail "an incomplete deployment operation requires recovery; inspect $PendingOperationFile ($($Pending.Trim()))"
    }
}

function Write-PendingOperation([string]$Operation, [string]$Record, [bool]$WasRunning) {
    $RunningValue = if ($WasRunning) { "1" } else { "0" }
    $TemporaryPending = "$PendingOperationFile.tmp.$PID"
    Write-Utf8NoBom $TemporaryPending "operation=$Operation`npid=$PID`nrecord=$Record`nwas_running=$RunningValue`n"
    Move-Item -LiteralPath $TemporaryPending -Destination $PendingOperationFile -Force
}

function Begin-PendingOperation([string]$Operation, [string]$Record) {
    $State = Get-ContainerState
    if ($State -eq "removing") { Fail "lifecycle operation refused while container removal is in progress" }
    $WasRunning = $State -in @("running", "restarting", "paused")
    Write-PendingOperation $Operation $Record $WasRunning
    return $WasRunning
}

function Clear-PendingOperation {
    if (Test-Path -LiteralPath $PendingOperationFile -PathType Leaf) {
        Remove-Item -LiteralPath $PendingOperationFile -Force -ErrorAction Stop
    }
    if (Test-Path -LiteralPath $PendingOperationFile) { Fail "could not clear pending operation journal" }
}

function Ensure-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "Docker Desktop is not installed" }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) { Fail "Docker Compose v2 is required" }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop is not running" }
    $DockerOs = & docker info --format '{{.OSType}}' 2>$null
    if ($LASTEXITCODE -ne 0 -or $DockerOs -ne "linux") { Fail "Docker Desktop must use Linux containers; choose Switch to Linux containers" }
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$ComposeArgs,
        [switch]$AllowFailure
    )
    & docker compose --project-name $ProjectName --env-file $EnvFile --file $ComposeFile @ComposeArgs
    $Code = $LASTEXITCODE
    if ($Code -ne 0 -and -not $AllowFailure) { Fail "docker compose failed with exit code $Code" }
    return $Code
}

function Get-BaseUrl {
    if ($BindAddress -eq "::1") { return "http://[::1]:$Port" }
    return "http://${BindAddress}:$Port"
}

function Show-SetupUrl([string]$Token) {
    $CanReveal = [Environment]::UserInteractive -and $Host.Name -eq "ConsoleHost" -and -not [Console]::IsOutputRedirected -and -not [Console]::IsErrorRedirected
    if (-not $CanReveal) {
        Write-Warning "setup URL is only shown on an unredirected local console; rerun traceable-rag setup-url interactively"
        return $false
    }
    [Console]::WriteLine("Admin configuration URL: $(Get-BaseUrl)/setup#token=$Token")
    return $true
}

function Get-HttpStatus([string]$Path) {
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri "$(Get-BaseUrl)$Path" -TimeoutSec 5 -Method Get
        return [int]$Response.StatusCode
    } catch {
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            return [int]$_.Exception.Response.StatusCode
        }
        return 0
    }
}

function Get-ContainerId {
    $Output = @(& docker ps --all --quiet --filter "label=com.docker.compose.project=$ProjectName" --filter "label=com.docker.compose.service=traceable-rag" 2>$null)
    if ($LASTEXITCODE -ne 0) { Fail "could not query Docker labels for the Traceable RAG container; refusing an ambiguous lifecycle operation" }
    $Ids = @($Output | ForEach-Object { $Value = ([string]$_).Trim(); if ($Value) { $Value } })
    if ($Ids.Count -gt 1) { Fail "multiple Traceable RAG containers make lifecycle state ambiguous" }
    if (-not $Ids.Count) { return "" }
    return $Ids[0]
}

function Get-ContainerStateById([string]$Id) {
    $Output = @(& docker inspect --format '{{.State.Status}}' $Id 2>$null)
    if ($LASTEXITCODE -ne 0) { Fail "could not inspect the Traceable RAG container; refusing an ambiguous lifecycle operation" }
    $States = @($Output | ForEach-Object { $Value = ([string]$_).Trim(); if ($Value) { $Value } })
    if ($States.Count -ne 1 -or $States[0] -notin @("created", "running", "paused", "restarting", "removing", "exited", "dead")) {
        Fail "Docker returned an unknown Traceable RAG container state"
    }
    return $States[0]
}

function Get-ContainerState {
    $Id = Get-ContainerId
    if (-not $Id) { return "absent" }
    $State = Get-ContainerStateById $Id
    return $State
}

function Test-WriterStopped([string]$State) {
    return $State -in @("absent", "created", "exited", "dead")
}

function Assert-WriterStopped {
    $State = Get-ContainerState
    if (-not (Test-WriterStopped $State)) { Fail "Traceable RAG writer is not definitively stopped (state: $State)" }
}

function Stop-WriterAndConfirm {
    $Id = Get-ContainerId
    if (-not $Id) { return }
    $State = Get-ContainerStateById $Id
    if (Test-WriterStopped $State) { return }
    if ($State -eq "removing") { Fail "Traceable RAG container removal is in progress; writer shutdown cannot be confirmed" }
    & docker compose --project-name $ProjectName --env-file $EnvFile --file $ComposeFile stop --timeout 45 traceable-rag *> $null
    $ComposeStopCode = $LASTEXITCODE
    $NeedsDirectStop = $ComposeStopCode -ne 0
    if (-not $NeedsDirectStop) {
        $CurrentId = Get-ContainerId
        if (-not $CurrentId) { return }
        if ($CurrentId -ne $Id) { Fail "Traceable RAG container identity changed during shutdown" }
        $NeedsDirectStop = -not (Test-WriterStopped (Get-ContainerStateById $Id))
    }
    if ($NeedsDirectStop) {
        & docker stop --time 45 $Id *> $null
        if ($LASTEXITCODE -ne 0) { Fail "could not stop the uniquely identified Traceable RAG writer" }
    }
    Assert-WriterStopped
}

function Wait-Ready {
    for ($Attempt = 0; $Attempt -lt $ReadyTimeout; $Attempt++) {
        if ((Get-HttpStatus "/readyz") -eq 200) { return $true }
        if ((Get-ContainerState) -in @("exited", "dead")) {
            Invoke-Compose -ComposeArgs @("logs", "--tail", "100", "traceable-rag") -AllowFailure | Out-Null
            return $false
        }
        if ($Attempt -gt 0 -and ($Attempt % 30) -eq 0) {
            Write-Warning "Waiting for readiness ($Attempt/$ReadyTimeout seconds): livez=$(Get-HttpStatus '/livez') readyz=$(Get-HttpStatus '/readyz')"
        }
        Start-Sleep -Seconds 1
    }
    Invoke-Compose -ComposeArgs @("logs", "--tail", "100", "traceable-rag") -AllowFailure | Out-Null
    return $false
}

function Get-Version {
    return $script:ReleaseVersion
}

function New-Snapshot([string]$Label) {
    Assert-WriterStopped
    $SafeLabel = $Label -replace '[^A-Za-z0-9._-]', '_'
    $Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $Destination = Join-Path $BackupDir "$Stamp-$SafeLabel"
    $Suffix = 0
    while (Test-Path -LiteralPath $Destination) {
        $Suffix++
        $Destination = Join-Path $BackupDir "$Stamp-$SafeLabel-$Suffix"
    }
    $Temporary = "$Destination.tmp.$PID"
    New-Item -ItemType Directory -Force -Path (Join-Path $Temporary "data"), (Join-Path $Temporary "config") | Out-Null
    Get-ChildItem -LiteralPath $DataDir -Force | Copy-Item -Destination (Join-Path $Temporary "data") -Recurse -Force
    Copy-Item -LiteralPath $EnvFile -Destination (Join-Path $Temporary "config\deployment.env") -Force
    $Manifest = @(
        "created_at=$((Get-Date).ToUniversalTime().ToString('o'))",
        "image=$Image",
        "project=$ProjectName",
        "data_dir=$DataDir"
    ) -join "`n"
    Write-Utf8NoBom (Join-Path $Temporary "manifest") "$Manifest`n"
    $VerifyScript = @'
import hashlib, pathlib, sqlite3, sys
root = pathlib.Path("/snapshot")
integrity = []
for name in ("traceable_v3.db", "providers.db"):
    db = root / "data" / name
    if db.exists():
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"{name} integrity_check failed: {result}")
        integrity.append(f"{name}: ok")
    else:
        integrity.append(f"{name}: not present")
(root / "integrity.txt").write_text("\n".join(integrity) + "\n", encoding="utf-8")
entries = []
for path in sorted(root.rglob("*")):
    if not path.is_file() or path.name == "checksums.sha256":
        continue
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    entries.append(f"{digest.hexdigest()}  {path.relative_to(root).as_posix()}")
(root / "checksums.sha256").write_text("\n".join(entries) + "\n", encoding="utf-8")
'@
    & docker run --rm --user "10001:10001" --volume "${Temporary}:/snapshot" $script:Image python -c $VerifyScript
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $Temporary -Recurse -Force -ErrorAction SilentlyContinue
        Fail "snapshot SQLite or checksum verification failed"
    }
    Move-Item -LiteralPath $Temporary -Destination $Destination
    return $Destination
}

function Restart-IfNeeded([bool]$WasRunning) {
    if (-not $WasRunning) { return }
    Invoke-Compose -ComposeArgs @("up", "--detach", "--no-build", "--remove-orphans", "traceable-rag") | Out-Null
    if (-not (Wait-Ready)) { Fail "service did not recover after the snapshot" }
}

function Backup-Command([string]$Label) {
    if (-not $Label) { $Label = "manual-$(Get-Version)" }
    $WasRunning = Begin-PendingOperation "backup" "none"
    Stop-WriterAndConfirm
    $Snapshot = New-Snapshot $Label
    Restart-IfNeeded $WasRunning
    Assert-InstallationMarkers -RequireActiveData
    Assert-InstalledReleaseIdentity
    Clear-PendingOperation
    Write-Output "Backup created: $Snapshot"
    return $Snapshot
}

function Set-ConfigValue([string]$Key, [string]$Value) {
    $Found = $false
    $Lines = foreach ($Line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
        if ($Line.StartsWith("$Key=")) {
            $Found = $true
            "$Key=$Value"
        } else { $Line }
    }
    if (-not $Found) { $Lines += "$Key=$Value" }
    $Temporary = "$EnvFile.tmp.$PID"
    Write-Utf8NoBom $Temporary (($Lines -join "`n") + "`n")
    Move-Item -LiteralPath $Temporary -Destination $EnvFile -Force
    $Config[$Key] = $Value
    [Environment]::SetEnvironmentVariable($Key, $Value, "Process")
    if ($Key -eq "TRACEABLE_RAG_IMAGE") {
        $script:Image = $Value
    }
    if ($Key -eq "TRACEABLE_RAG_RELEASE_VERSION") {
        $script:ReleaseVersion = $Value
    }
}

function Pull-AndResolveDigest([string]$Reference) {
    & docker pull $Reference 2>&1 | ForEach-Object { Write-Host $_ }
    $PullExitCode = $LASTEXITCODE
    if ($PullExitCode -ne 0) { Fail "could not pull $Reference" }
    if ($Reference -match '@sha256:[0-9a-f]{64}$') { return [string]$Reference }
    $Raw = & docker image inspect --format '{{json .RepoDigests}}' $Reference
    if ($LASTEXITCODE -ne 0 -or -not $Raw) { Fail "Docker did not report RepoDigests for $Reference" }
    $Digests = $Raw | ConvertFrom-Json
    $Resolved = @($Digests | Where-Object { $_ -match "^$([regex]::Escape($ImageRepository))@sha256:[0-9a-f]{64}$" }) | Select-Object -First 1
    if (-not $Resolved) { Fail "Docker did not report an immutable RepoDigest for $Reference" }
    Write-Output -NoEnumerate ([string]$Resolved)
}

function Download-ReleaseBundle([string]$Version) {
    $Root = Join-Path $StateDir "upgrades\.download-$Version-$PID"
    if (Test-Path -LiteralPath $Root) { Fail "release download staging path already exists: $Root" }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $Asset = "traceable-rag-deployment-$Version.zip"
    $ReleaseBase = if ($env:TRACEABLE_RAG_RELEASE_BASE_URL) { $env:TRACEABLE_RAG_RELEASE_BASE_URL } else { "https://github.com/$ReleaseRepository/releases/download/$Version" }
    Assert-NoControlCharacters "release base URL" $ReleaseBase
    $Archive = Join-Path $Root $Asset
    $ManifestPath = Join-Path $Root "release-manifest.json"
    $ChecksumFile = Join-Path $Root "SHA256SUMS"
    $GhAuthenticated = $false
    if (-not $env:TRACEABLE_RAG_RELEASE_BASE_URL -and (Get-Command gh -ErrorAction SilentlyContinue)) {
        & gh auth status --hostname github.com *> $null
        $GhAuthenticated = $LASTEXITCODE -eq 0
    }
    if ($GhAuthenticated) {
        & gh release download $Version --repo $ReleaseRepository --pattern $Asset --pattern release-manifest.json --pattern SHA256SUMS --dir $Root
        if ($LASTEXITCODE -ne 0) { Fail "gh could not download the authenticated release assets" }
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri "$ReleaseBase/$Asset" -OutFile $Archive
        Invoke-WebRequest -UseBasicParsing -Uri "$ReleaseBase/release-manifest.json" -OutFile $ManifestPath
        Invoke-WebRequest -UseBasicParsing -Uri "$ReleaseBase/SHA256SUMS" -OutFile $ChecksumFile
    }
    $EscapedAsset = [regex]::Escape($Asset)
    $ChecksumLine = Get-Content -LiteralPath $ChecksumFile | Where-Object { $_ -match "^[0-9a-fA-F]{64}\s+\*?$EscapedAsset$" } | Select-Object -First 1
    if (-not $ChecksumLine) { Fail "release checksum does not list $Asset" }
    if ((($ChecksumLine -split '\s+')[0]).ToLowerInvariant() -ne (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()) { Fail "deployment bundle checksum mismatch" }
    $ManifestLine = Get-Content -LiteralPath $ChecksumFile | Where-Object { $_ -match '^[0-9a-fA-F]{64}\s+\*?release-manifest\.json$' } | Select-Object -First 1
    if (-not $ManifestLine) { Fail "release checksum does not list release-manifest.json" }
    $ManifestHash = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ((($ManifestLine -split '\s+')[0]).ToLowerInvariant() -ne $ManifestHash) { Fail "release manifest checksum mismatch" }
    $Manifest = Read-ValidatedReleaseManifest $ManifestPath $Version $ImageRepository
    $Unpacked = Join-Path $Root "unpacked"
    Expand-Archive -LiteralPath $Archive -DestinationPath $Unpacked -Force
    $Bundle = Join-Path $Unpacked "deploy"
    foreach ($Required in @("compose.yaml", "traceable-rag.ps1", "release-manifest.json")) {
        $RequiredPath = Join-Path $Bundle $Required
        if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) { Fail "deployment bundle is missing $Required" }
        $Item = Get-Item -LiteralPath $RequiredPath -Force
        if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail "deployment bundle redirects $Required" }
    }
    if ((Get-FileHash -LiteralPath (Join-Path $Bundle "release-manifest.json") -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ManifestHash) { Fail "deployment bundle release manifest does not match the verified release asset" }
    return [pscustomobject]@{ Root = $Root; Bundle = $Bundle; Image = "$($Manifest.image)@$($Manifest.index_digest)"; SourceCommit = $Manifest.source_commit }
}

function Write-EnvIdentity([string]$Source, [string]$Destination, [string]$Version, [string]$TargetImage, [string]$SourceCommit) {
    $FoundVersion = $false; $FoundImage = $false; $FoundCommit = $false
    $Lines = foreach ($Line in Get-Content -LiteralPath $Source -Encoding UTF8) {
        if ($Line.StartsWith("TRACEABLE_RAG_RELEASE_VERSION=")) { $FoundVersion = $true; "TRACEABLE_RAG_RELEASE_VERSION=$Version" }
        elseif ($Line.StartsWith("TRACEABLE_RAG_RELEASE_SOURCE_COMMIT=")) { $FoundCommit = $true; "TRACEABLE_RAG_RELEASE_SOURCE_COMMIT=$SourceCommit" }
        elseif ($Line.StartsWith("TRACEABLE_RAG_IMAGE=")) { $FoundImage = $true; "TRACEABLE_RAG_IMAGE=$TargetImage" }
        else { $Line }
    }
    if (-not $FoundVersion) { $Lines += "TRACEABLE_RAG_RELEASE_VERSION=$Version" }
    if (-not $FoundCommit) { $Lines += "TRACEABLE_RAG_RELEASE_SOURCE_COMMIT=$SourceCommit" }
    if (-not $FoundImage) { $Lines += "TRACEABLE_RAG_IMAGE=$TargetImage" }
    Write-Utf8NoBom $Destination (($Lines -join "`n") + "`n")
}

function Activate-ControlPlane([string]$ControlDir) {
    foreach ($Required in @("traceable-rag.ps1", "compose.yaml", "deployment.env")) {
        if (-not (Test-Path -LiteralPath (Join-Path $ControlDir $Required) -PathType Leaf)) { Fail "upgrade control plane is missing $Required" }
    }
    Copy-Item -LiteralPath (Join-Path $ControlDir "traceable-rag.ps1") -Destination "$ConfigDir\traceable-rag.ps1.activate.$PID" -Force
    Copy-Item -LiteralPath (Join-Path $ControlDir "compose.yaml") -Destination "$ComposeFile.activate.$PID" -Force
    Copy-Item -LiteralPath (Join-Path $ControlDir "deployment.env") -Destination "$EnvFile.activate.$PID" -Force
    $ControlManifest = Join-Path $ControlDir "release-manifest.json"
    if (Test-Path -LiteralPath $ControlManifest -PathType Leaf) { Copy-Item -LiteralPath $ControlManifest -Destination "$ReleaseManifestFile.activate.$PID" -Force; Move-Item -LiteralPath "$ReleaseManifestFile.activate.$PID" -Destination $ReleaseManifestFile -Force }
    else { Remove-Item -LiteralPath $ReleaseManifestFile -Force -ErrorAction SilentlyContinue }
    Move-Item -LiteralPath "$ConfigDir\traceable-rag.ps1.activate.$PID" -Destination (Join-Path $ConfigDir "traceable-rag.ps1") -Force
    Move-Item -LiteralPath "$ComposeFile.activate.$PID" -Destination $ComposeFile -Force
    Move-Item -LiteralPath "$EnvFile.activate.$PID" -Destination $EnvFile -Force
}

function Reload-IdentityFromEnv {
    $script:ReleaseSourceCommit = ""
    $Config.Remove("TRACEABLE_RAG_RELEASE_SOURCE_COMMIT")
    foreach ($Line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
        if ($Line.StartsWith("TRACEABLE_RAG_IMAGE=")) { $script:Image = $Line.Substring("TRACEABLE_RAG_IMAGE=".Length); $Config.TRACEABLE_RAG_IMAGE = $script:Image }
        elseif ($Line.StartsWith("TRACEABLE_RAG_RELEASE_VERSION=")) { $script:ReleaseVersion = $Line.Substring("TRACEABLE_RAG_RELEASE_VERSION=".Length); $Config.TRACEABLE_RAG_RELEASE_VERSION = $script:ReleaseVersion }
        elseif ($Line.StartsWith("TRACEABLE_RAG_RELEASE_SOURCE_COMMIT=")) { $script:ReleaseSourceCommit = $Line.Substring("TRACEABLE_RAG_RELEASE_SOURCE_COMMIT=".Length); $Config.TRACEABLE_RAG_RELEASE_SOURCE_COMMIT = $script:ReleaseSourceCommit }
    }
    [Environment]::SetEnvironmentVariable("TRACEABLE_RAG_IMAGE", $script:Image, "Process")
    [Environment]::SetEnvironmentVariable("TRACEABLE_RAG_RELEASE_VERSION", $script:ReleaseVersion, "Process")
    [Environment]::SetEnvironmentVariable("TRACEABLE_RAG_RELEASE_SOURCE_COMMIT", $script:ReleaseSourceCommit, "Process")
}

function Restore-Snapshot([string]$Snapshot) {
    Assert-WriterStopped
    $SnapshotData = Join-Path $Snapshot "data"
    if (-not (Test-Path -LiteralPath $SnapshotData -PathType Container)) { Fail "snapshot data is missing: $Snapshot" }
    $VerifyScript = @'
import hashlib, pathlib, sqlite3
root = pathlib.Path("/snapshot")
manifest = root / "checksums.sha256"
if not manifest.is_file():
    raise SystemExit("snapshot checksum manifest is missing")
for line in manifest.read_text(encoding="utf-8").splitlines():
    expected, separator, relative = line.partition("  ")
    if not separator or not relative or relative.startswith("/") or ".." in pathlib.PurePosixPath(relative).parts:
        raise SystemExit("invalid snapshot checksum entry")
    path = root / pathlib.PurePosixPath(relative)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise SystemExit(f"snapshot checksum mismatch: {relative}")
for name in ("traceable_v3.db", "providers.db"):
    db = root / "data" / name
    if db.exists():
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"{name} integrity_check failed: {result}")
'@
    $VerifierLine = Get-Content -LiteralPath (Join-Path $Snapshot "manifest") | Where-Object { $_.StartsWith("image=") } | Select-Object -First 1
    $VerifierImage = if ($VerifierLine) { $VerifierLine.Substring(6) } else { "" }
    if ($VerifierImage -notmatch '^ghcr\.io/[a-z0-9._/-]+(:v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?|@sha256:[0-9a-f]{64})$') { Fail "snapshot verifier image is missing or invalid: $Snapshot" }
    & docker run --rm --user "10001:10001" --volume "${Snapshot}:/snapshot:ro" $VerifierImage python -c $VerifyScript
    if ($LASTEXITCODE -ne 0) { Fail "snapshot verification failed: $Snapshot" }
    Assert-WriterStopped
    $Parent = Split-Path -Parent $DataDir
    $Base = Split-Path -Leaf $DataDir
    if (-not (Test-Path -LiteralPath $DataDir)) {
        $DisplacedCandidate = Get-InterruptedRestoreDisplacement
        $DisplacedPrefix = ".$Base.displaced."
        $DisplacedSuffix = $DisplacedCandidate.Name.Substring($DisplacedPrefix.Length)
        $InterruptedStage = Join-Path $Parent ".$Base.restore.$DisplacedSuffix"
        Move-Item -LiteralPath $DisplacedCandidate.FullName -Destination $DataDir
        Assert-InstallationMarker $DataDir
        if (Test-Path -LiteralPath $InterruptedStage) {
            Assert-InstallationMarker $InterruptedStage
            Remove-Item -LiteralPath $InterruptedStage -Recurse -Force -ErrorAction Stop
        }
    }
    $Stage = Join-Path $Parent ".$Base.restore.$PID"
    $Displaced = Join-Path $Parent ".$Base.displaced.$PID"
    if (Test-Path -LiteralPath $Stage) { Fail "restore staging path already exists: $Stage" }
    if (Test-Path -LiteralPath $Displaced) { Fail "restore displacement path already exists: $Displaced" }
    New-Item -ItemType Directory -Path $Stage | Out-Null
    Get-ChildItem -LiteralPath $SnapshotData -Force | Copy-Item -Destination $Stage -Recurse -Force
    Assert-InstallationMarker $Stage
    Assert-InstallationMarker $DataDir
    Assert-WriterStopped
    Move-Item -LiteralPath $DataDir -Destination $Displaced
    try {
        Move-Item -LiteralPath $Stage -Destination $DataDir
        Assert-InstallationMarker $DataDir
        Remove-Item -LiteralPath $Displaced -Recurse -Force
    } catch {
        if (-not (Test-Path -LiteralPath $DataDir) -and (Test-Path -LiteralPath $Displaced)) {
            Move-Item -LiteralPath $Displaced -Destination $DataDir
        }
        throw
    }
}

function Normalize-Version([string]$Version) {
    if (-not $Version.StartsWith("v")) { $Version = "v$Version" }
    if ($Version -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$') { Fail "version must be exact, for example v0.4.2" }
    return $Version
}

function Upgrade-Command([string]$Version) {
    $Version = Normalize-Version $Version
    $Previous = $script:Image
    $PreviousVersion = $script:ReleaseVersion
    if ($PreviousVersion -eq $Version) { Write-Output "Already configured for $Version"; return }
    $Downloaded = Download-ReleaseBundle $Version
    $Target = $Downloaded.Image
    & docker pull $Target
    if ($LASTEXITCODE -ne 0) { Remove-Item -LiteralPath $Downloaded.Root -Recurse -Force -ErrorAction SilentlyContinue; Fail "could not pull $Target" }
    $Record = Join-Path $StateDir "upgrades\$((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))-$Version"
    $PreviousControl = Join-Path $Record "control\previous"
    $TargetControl = Join-Path $Record "control\target"
    New-Item -ItemType Directory -Force -Path $PreviousControl, $TargetControl | Out-Null
    Copy-Item -LiteralPath (Join-Path $ConfigDir "traceable-rag.ps1") -Destination (Join-Path $PreviousControl "traceable-rag.ps1") -Force
    Copy-Item -LiteralPath $ComposeFile -Destination (Join-Path $PreviousControl "compose.yaml") -Force
    Copy-Item -LiteralPath $EnvFile -Destination (Join-Path $PreviousControl "deployment.env") -Force
    if (Test-Path -LiteralPath $ReleaseManifestFile -PathType Leaf) { Copy-Item -LiteralPath $ReleaseManifestFile -Destination (Join-Path $PreviousControl "release-manifest.json") -Force }
    Copy-Item -LiteralPath (Join-Path $Downloaded.Bundle "traceable-rag.ps1") -Destination (Join-Path $TargetControl "traceable-rag.ps1") -Force
    $Tokens = $null; $Errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $TargetControl "traceable-rag.ps1"), [ref]$Tokens, [ref]$Errors) | Out-Null
    if ($Errors.Count) { Fail "target release manager has invalid PowerShell syntax" }
    $ComposeTemplate = [IO.File]::ReadAllText((Join-Path $Downloaded.Bundle "compose.yaml"))
    $RenderedCompose = $ComposeTemplate.Replace("@TRACEABLE_RAG_CONTAINER_UID@", "10001").Replace("@TRACEABLE_RAG_CONTAINER_GID@", "10001")
    if ($RenderedCompose -notmatch 'user:\s*"10001:10001"') { Fail "could not render the target release Compose user" }
    Write-Utf8NoBom (Join-Path $TargetControl "compose.yaml") $RenderedCompose
    Copy-Item -LiteralPath (Join-Path $Downloaded.Bundle "release-manifest.json") -Destination (Join-Path $TargetControl "release-manifest.json") -Force
    Write-EnvIdentity $EnvFile (Join-Path $TargetControl "deployment.env") $Version $Target $Downloaded.SourceCommit
    Write-Utf8NoBom (Join-Path $Record "previous-image") $Previous
    Write-Utf8NoBom (Join-Path $Record "previous-release") $PreviousVersion
    Write-Utf8NoBom (Join-Path $Record "target-image") $Target
    Write-Utf8NoBom (Join-Path $Record "target-release") $Version
    $WasRunning = Begin-PendingOperation "upgrade" $Record
    Stop-WriterAndConfirm
    $Snapshot = New-Snapshot "pre-upgrade-$(Get-Version)-to-$Version"
    Write-Utf8NoBom (Join-Path $Record "pre-upgrade-snapshot") $Snapshot
    Activate-ControlPlane $TargetControl
    Reload-IdentityFromEnv
    Remove-Item -LiteralPath $Downloaded.Root -Recurse -Force -ErrorAction SilentlyContinue
    try {
        Invoke-Compose -ComposeArgs @("up", "--detach", "--no-build", "--remove-orphans", "traceable-rag") | Out-Null
        if (-not (Wait-Ready)) { throw "new release failed readiness" }
        Assert-InstallationMarkers -RequireActiveData
        Assert-InstalledReleaseIdentity
        Write-Utf8NoBom (Join-Path $StateDir "last-upgrade") $Record
        Clear-PendingOperation
        Write-Output "Upgraded to $Version ($Target). Pre-upgrade backup: $Snapshot"
    } catch {
        Write-Warning "Upgrade failed; restoring previous image and data."
        Stop-WriterAndConfirm
        Restore-Snapshot $Snapshot
        Activate-ControlPlane $PreviousControl
        Reload-IdentityFromEnv
        Invoke-Compose -ComposeArgs @("up", "--detach", "--no-build", "--remove-orphans", "traceable-rag") | Out-Null
        if (-not (Wait-Ready)) { Fail "automatic recovery failed; inspect $Snapshot" }
        Assert-InstallationMarkers -RequireActiveData
        Assert-InstalledReleaseIdentity
        Clear-PendingOperation
        Fail "upgrade failed; previous release was restored"
    }
}

function Rollback-Command {
    $Pointer = Join-Path $StateDir "last-upgrade"
    if (-not (Test-Path -LiteralPath $Pointer -PathType Leaf)) { Fail "no successful upgrade record is available" }
    $Record = [IO.File]::ReadAllText($Pointer)
    $Previous = [IO.File]::ReadAllText((Join-Path $Record "previous-image"))
    $PreviousVersion = [IO.File]::ReadAllText((Join-Path $Record "previous-release"))
    $Target = [IO.File]::ReadAllText((Join-Path $Record "target-image"))
    $TargetVersion = [IO.File]::ReadAllText((Join-Path $Record "target-release"))
    $UpgradeSnapshot = [IO.File]::ReadAllText((Join-Path $Record "pre-upgrade-snapshot"))
    if ($script:Image -ne $Target) { Fail "current image differs from the last upgrade target; refusing ambiguous rollback" }
    $PreviousControl = Join-Path $Record "control\previous"
    $TargetControl = Join-Path $Record "control\target"
    if (-not (Test-Path -LiteralPath $PreviousControl -PathType Container) -or -not (Test-Path -LiteralPath $TargetControl -PathType Container)) { Fail "upgrade record has no restorable control plane" }
    & docker pull $Previous
    if ($LASTEXITCODE -ne 0) { Fail "could not pull rollback image $Previous" }
    $WasRunning = Begin-PendingOperation "rollback" $Record
    Stop-WriterAndConfirm
    $Safety = New-Snapshot "pre-rollback-$(Get-Version)"
    Write-Utf8NoBom (Join-Path $Record "pre-rollback-snapshot") $Safety
    Restore-Snapshot $UpgradeSnapshot
    Activate-ControlPlane $PreviousControl
    Reload-IdentityFromEnv
    try {
        Invoke-Compose -ComposeArgs @("up", "--detach", "--no-build", "--remove-orphans", "traceable-rag") | Out-Null
        if (-not (Wait-Ready)) { throw "rollback release failed readiness" }
        Assert-InstallationMarkers -RequireActiveData
        Assert-InstalledReleaseIdentity
        Write-Utf8NoBom (Join-Path $StateDir "last-rollback") $Record
        Remove-Item -LiteralPath $Pointer -Force
        Clear-PendingOperation
        Write-Output "Rolled back to $PreviousVersion ($Previous). Safety backup: $Safety"
    } catch {
        Write-Warning "Rollback failed; restoring pre-rollback state."
        Stop-WriterAndConfirm
        Restore-Snapshot $Safety
        Activate-ControlPlane $TargetControl
        Reload-IdentityFromEnv
        Invoke-Compose -ComposeArgs @("up", "--detach", "--no-build", "--remove-orphans", "traceable-rag") | Out-Null
        if (-not (Wait-Ready)) { Fail "rollback recovery failed; inspect $UpgradeSnapshot and $Safety" }
        Assert-InstallationMarkers -RequireActiveData
        Assert-InstalledReleaseIdentity
        Clear-PendingOperation
        Fail "rollback failed; pre-rollback state was restored"
    }
}

function Recover-Command {
    if (-not (Test-Path -LiteralPath $PendingOperationFile -PathType Leaf)) { Write-Output "No interrupted deployment operation was found."; return }
    $Pending = Read-PendingOperation
    Stop-WriterAndConfirm
    switch ($Pending.operation) {
        "backup" { }
        "upgrade" {
            $PreviousControl = Join-Path $Pending.record "control\previous"
            if (-not (Test-Path -LiteralPath $PreviousControl -PathType Container)) { Fail "pending upgrade has no previous control plane" }
            $SnapshotFile = Join-Path $Pending.record "pre-upgrade-snapshot"
            if (Test-Path -LiteralPath $SnapshotFile -PathType Leaf) {
                $Snapshot = [IO.File]::ReadAllText($SnapshotFile)
                if ($Snapshot -ne "pending" -and (Test-Path -LiteralPath $Snapshot -PathType Container)) { Restore-Snapshot $Snapshot }
            }
            Activate-ControlPlane $PreviousControl
            Reload-IdentityFromEnv
        }
        "rollback" {
            $TargetControl = Join-Path $Pending.record "control\target"
            if (-not (Test-Path -LiteralPath $TargetControl -PathType Container)) { Fail "pending rollback has no target control plane" }
            $SafetyFile = Join-Path $Pending.record "pre-rollback-snapshot"
            if (Test-Path -LiteralPath $SafetyFile -PathType Leaf) {
                $Safety = [IO.File]::ReadAllText($SafetyFile)
                if (Test-Path -LiteralPath $Safety -PathType Container) { Restore-Snapshot $Safety }
            }
            Activate-ControlPlane $TargetControl
            Reload-IdentityFromEnv
        }
        default { Fail "pending operation type is invalid: $($Pending.operation)" }
    }
    if ($Pending.was_running -eq "1") {
        Invoke-Compose -ComposeArgs @("up", "--detach", "--no-build", "--remove-orphans", "traceable-rag") | Out-Null
        if (-not (Wait-Ready)) { Fail "recovery restored the prior control plane, but the service did not become ready" }
    }
    Invoke-Compose -ComposeArgs @("config", "--quiet") | Out-Null
    Assert-InstallationMarkers -RequireActiveData
    Assert-InstalledReleaseIdentity
    Clear-PendingOperation
    Write-Output "Recovered interrupted $($Pending.operation) operation to the last known-good side."
}

function Invoke-SafeJsonGet([string]$Path) {
    try {
        $Headers = @{ "X-Traceable-RAG-Admin-Token" = $script:AdminToken }
        return Invoke-RestMethod -Uri "$(Get-BaseUrl)$Path" -TimeoutSec 8 -Headers $Headers -Method Get
    } catch { return $null }
}

function Invoke-SafeJsonPost([string]$Path, [hashtable]$Body) {
    try {
        $Headers = @{ "X-Traceable-RAG-Admin-Token" = $script:AdminToken }
        return Invoke-RestMethod -Uri "$(Get-BaseUrl)$Path" -TimeoutSec 45 -Headers $Headers -Method Post -ContentType "application/json" -Body ($Body | ConvertTo-Json -Compress)
    } catch { return $null }
}

function Test-AdminTokenAccepted([string]$Token) {
    try {
        $Headers = @{ "X-Traceable-RAG-Admin-Token" = $Token }
        Invoke-WebRequest -UseBasicParsing -Uri "$(Get-BaseUrl)/api/provider-profiles/mineru" -TimeoutSec 8 -Headers $Headers -Method Put -ContentType "application/json" -Body "{}" | Out-Null
        return $false
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 422) { return $true }
        return $false
    }
}

function Doctor-Command([string[]]$Args) {
    $AsJson = $false
    $TestRoles = New-Object System.Collections.Generic.List[string]
    for ($Index = 0; $Index -lt $Args.Count; $Index++) {
        if ($Args[$Index] -eq "--json") { $AsJson = $true; continue }
        if ($Args[$Index] -eq "--test-provider" -and $Index + 1 -lt $Args.Count) {
            $Role = $Args[++$Index]
            if ($Role -notin @("mineru", "embedding", "reranker", "llm")) { Fail "unsupported Provider role: $Role" }
            $TestRoles.Add($Role)
            continue
        }
        Fail "unknown doctor option: $($Args[$Index])"
    }
    $DockerOk = $false
    $ComposeOk = $false
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        & docker info *> $null
        $DockerOk = $LASTEXITCODE -eq 0
        if ($DockerOk) {
            & docker compose version *> $null
            $ComposeOk = $LASTEXITCODE -eq 0
        }
    }
    $State = if ($DockerOk -and $ComposeOk) { Get-ContainerState } else { "unavailable" }
    $Live = Get-HttpStatus "/livez"
    $Ready = Get-HttpStatus "/readyz"
    $Legacy = Get-HttpStatus "/api/health"
    $Providers = Invoke-SafeJsonGet "/api/provider-profiles"
    $ProviderTests = [ordered]@{}
    foreach ($Role in $TestRoles) {
        $ProviderTests[$Role] = Invoke-SafeJsonPost "/api/provider-profiles/$Role/test" @{}
    }
    $ProviderTestResults = if ($TestRoles.Count) { $ProviderTests } else { $null }
    $Result = [ordered]@{
        installation = [ordered]@{ ok = $true; config_dir = $ConfigDir; data_dir = $DataDir; project = $ProjectName; loopback = $BindAddress -in @("127.0.0.1", "localhost", "::1") }
        version = [ordered]@{ configured = (Get-Version); image = $script:Image }
        container = [ordered]@{ state = $State; docker_ok = $DockerOk; compose_ok = $ComposeOk }
        livez = [ordered]@{ ok = $Live -eq 200; http_status = $Live }
        readyz = [ordered]@{ ok = $Ready -eq 200; http_status = $Ready; legacy_health_status = $Legacy }
        provider_summary = $Providers
        provider_tests = [ordered]@{ requested_roles = @($TestRoles); results = $ProviderTestResults }
        skill = [ordered]@{ command = "traceable-rag agent install-codex --knowledge-base-id default" }
    }
    if ($AsJson) { $Result | ConvertTo-Json -Depth 20 -Compress }
    else {
        Write-Output "Installation: ok"
        Write-Output "  config: $ConfigDir"
        Write-Output "  data: $DataDir"
        Write-Output "  image: $($script:Image)"
        Write-Output "Container: $State (Docker: $DockerOk, Compose: $ComposeOk)"
        Write-Output "Live: HTTP $Live; Ready: HTTP $Ready; legacy health: HTTP $Legacy"
        Write-Output "Provider profiles are read-only; explicit test roles: $($TestRoles -join ', ')"
        Write-Output "Codex Skill command: traceable-rag agent install-codex --knowledge-base-id default"
    }
    if (-not ($DockerOk -and $ComposeOk -and $Live -eq 200 -and $Ready -eq 200)) { exit 1 }
}

function Get-CodexSkillTarget {
    $CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
    return Join-Path $CodexHome "skills\traceable-rag"
}

function Assert-NoReparseIfExists([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $Item = Get-Item -LiteralPath $Path -Force
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail "refusing Codex Skill path redirected by a reparse point: $Path" }
}

function Install-CodexSkill([string[]]$Args) {
    $KnowledgeBaseId = ""
    $AdoptExisting = $false
    for ($Index = 0; $Index -lt $Args.Count; $Index++) {
        if ($Args[$Index] -eq "--knowledge-base-id" -and $Index + 1 -lt $Args.Count) { $KnowledgeBaseId = $Args[++$Index] }
        elseif ($Args[$Index] -eq "--adopt-existing") { $AdoptExisting = $true }
        else { Fail "unknown agent install-codex option: $($Args[$Index])" }
    }
    if ($KnowledgeBaseId -and $KnowledgeBaseId -notmatch '^[A-Za-z0-9._-]{1,128}$') { Fail "knowledge base ID may contain only letters, digits, dot, underscore and hyphen" }
    if ((Get-ContainerState) -ne "running") { Fail "start Traceable RAG before installing the Codex Skill" }
    $ContainerId = Get-ContainerId
    $Target = Get-CodexSkillTarget
    $SkillMarker = Join-Path $Target ".traceable-rag-installation"
    $Runtime = Join-Path $Target "references\runtime.md"
    foreach ($Guarded in @($Target, (Join-Path $Target "agents"), (Join-Path $Target "references"), (Join-Path $Target "SKILL.md"), (Join-Path $Target "agents\openai.yaml"), (Join-Path $Target "references\http-contract.md"), $Runtime, $SkillMarker)) {
        Assert-NoReparseIfExists $Guarded
    }
    if (Test-Path -LiteralPath $Target -PathType Container) {
        $HasContent = Get-ChildItem -LiteralPath $Target -Force | Select-Object -First 1
        if ($HasContent -and (-not (Test-Path -LiteralPath $SkillMarker -PathType Leaf) -or [IO.File]::ReadAllText($SkillMarker) -ne $InstallationId)) {
            if (-not $AdoptExisting) { Fail "refusing to overwrite an unmanaged Codex Skill: $Target; rerun with agent install-codex --adopt-existing after reviewing it" }
            $SkillBackup = "$Target.backup.$((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))"
            if (Test-Path -LiteralPath $SkillBackup) { Fail "Codex Skill adoption backup already exists: $SkillBackup" }
            Copy-Item -LiteralPath $Target -Destination $SkillBackup -Recurse -Force
            Write-Output "Existing Codex Skill backed up to $SkillBackup"
        }
    }
    if (-not $KnowledgeBaseId -and (Test-Path -LiteralPath $Runtime)) {
        $Existing = Select-String -LiteralPath $Runtime -Pattern '^knowledge_base_id:\s*(.+)$' | Select-Object -First 1
        if ($Existing) { $KnowledgeBaseId = $Existing.Matches[0].Groups[1].Value }
    }
    if (-not $KnowledgeBaseId) { $KnowledgeBaseId = "default" }
    if ($KnowledgeBaseId -notmatch '^[A-Za-z0-9._-]{1,128}$') { Fail "existing knowledge base ID is unsafe for runtime.md" }
    $Temporary = Join-Path ([IO.Path]::GetTempPath()) "traceable-rag-skill-$([guid]::NewGuid())"
    New-Item -ItemType Directory -Force -Path $Temporary | Out-Null
    try {
        & docker cp "${ContainerId}:/app/integrations/codex-skill/traceable-rag/." $Temporary
        if ($LASTEXITCODE -ne 0) { Fail "could not copy the Codex Skill from the image" }
        foreach ($Required in @("SKILL.md", "agents\openai.yaml", "references\http-contract.md")) {
            if (-not (Test-Path -LiteralPath (Join-Path $Temporary $Required) -PathType Leaf)) { Fail "image is missing Codex Skill file: $Required" }
        }
        New-Item -ItemType Directory -Force -Path (Join-Path $Target "agents"), (Join-Path $Target "references") | Out-Null
        Copy-Item -LiteralPath (Join-Path $Temporary "SKILL.md") -Destination (Join-Path $Target "SKILL.md") -Force
        Copy-Item -LiteralPath (Join-Path $Temporary "agents\openai.yaml") -Destination (Join-Path $Target "agents\openai.yaml") -Force
        Copy-Item -LiteralPath (Join-Path $Temporary "references\http-contract.md") -Destination (Join-Path $Target "references\http-contract.md") -Force
        Write-Utf8NoBom $Runtime ((@("# Runtime configuration", "", "base_url: $(Get-BaseUrl)", "knowledge_base_id: $KnowledgeBaseId") -join "`n") + "`n")
        Write-Utf8NoBom $SkillMarker $InstallationId
    } finally {
        Remove-Item -LiteralPath $Temporary -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Output "Codex Skill installed at $Target"
    Write-Output "Runtime: base_url=$(Get-BaseUrl) knowledge_base_id=$KnowledgeBaseId"
}

function Doctor-CodexSkill {
    $Target = Get-CodexSkillTarget
    foreach ($Required in @("SKILL.md", "agents\openai.yaml", "references\http-contract.md", "references\runtime.md")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Target $Required) -PathType Leaf)) { Fail "missing Codex Skill file: $Required" }
    }
    $Runtime = Join-Path $Target "references\runtime.md"
    $InstalledUrl = (Select-String -LiteralPath $Runtime -Pattern '^base_url:\s*(.+)$' | Select-Object -First 1).Matches[0].Groups[1].Value
    $InstalledKb = (Select-String -LiteralPath $Runtime -Pattern '^knowledge_base_id:\s*(.+)$' | Select-Object -First 1).Matches[0].Groups[1].Value
    if ($InstalledUrl -ne (Get-BaseUrl)) { Fail "Codex Skill base_url is $InstalledUrl, expected $(Get-BaseUrl)" }
    if (-not $InstalledKb) { Fail "Codex Skill has no default knowledge_base_id" }
    if ((Get-HttpStatus "/readyz") -ne 200) { Fail "Traceable RAG readiness endpoint is unavailable" }
    try { $OpenApi = (Invoke-WebRequest -UseBasicParsing -Uri "$(Get-BaseUrl)/openapi.json" -TimeoutSec 15).Content }
    catch { Fail "could not read the OpenAPI contract" }
    foreach ($Marker in @("/api/retrieval", "pipeline_used", "source_url", "/api/sources/{source_id}/context")) {
        if (-not $OpenApi.Contains($Marker)) { Fail "OpenAPI is missing required Agent contract marker: $Marker" }
    }
    Write-Output "Codex Skill: ok"
    Write-Output "  target: $Target"
    Write-Output "  base_url: $InstalledUrl"
    Write-Output "  knowledge_base_id: $InstalledKb"
    Write-Output "  retrieval/source contract: ok"
}

function Install-AdminTokenFile([string]$Token, [object]$TokenAcl) {
    $Temporary = "$AdminTokenFile.tmp.$PID"
    try {
        Write-Utf8NoBom $Temporary $Token
        Set-Acl -LiteralPath $Temporary -AclObject $TokenAcl
        Assert-SafeSecret $Temporary 64 -Hex
        Move-Item -LiteralPath $Temporary -Destination $AdminTokenFile -Force
        Assert-SafeSecret $AdminTokenFile 64 -Hex
    } finally {
        Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
    }
}

function Recreate-And-VerifyAdminToken([string]$Token) {
    Invoke-Compose -ComposeArgs @("up", "--detach", "--force-recreate", "--no-build", "--remove-orphans", "traceable-rag") | Out-Null
    if (-not (Wait-Ready)) { Fail "recreated service did not become ready after admin token rotation" }
    if (-not (Test-AdminTokenAccepted $Token)) { Fail "recreated service rejected the expected admin token" }
}

function Rotate-AdminToken {
    $InitialState = Get-ContainerState
    if ($InitialState -eq "removing") { Fail "admin token rotation is unsafe while container removal is in progress" }
    $NeedsRecreate = $InitialState -in @("running", "restarting", "paused")
    $PreviousToken = $script:AdminToken
    $TokenAcl = Get-Acl -LiteralPath $AdminTokenFile
    $Bytes = New-Object byte[] 32
    $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Generator.GetBytes($Bytes) } finally { $Generator.Dispose() }
    $NewToken = ([BitConverter]::ToString($Bytes)).Replace("-", "").ToLowerInvariant()
    if ($NeedsRecreate) { Stop-WriterAndConfirm }
    if ($NeedsRecreate) {
        try {
            Install-AdminTokenFile $NewToken $TokenAcl
            $script:AdminToken = $NewToken
            Recreate-And-VerifyAdminToken $NewToken
        } catch {
            $RotationFailure = $_
            try {
                Install-AdminTokenFile $PreviousToken $TokenAcl
                $script:AdminToken = $PreviousToken
                Recreate-And-VerifyAdminToken $PreviousToken
            } catch {
                Fail "admin token rotation failed and the previous token could not be restored consistently: $($_.Exception.Message)"
            }
            Fail "admin token rotation failed; the previous token was restored: $($RotationFailure.Exception.Message)"
        }
    } else {
        Install-AdminTokenFile $NewToken $TokenAcl
        $script:AdminToken = $NewToken
    }
    Write-Output "Admin token rotated. Previous setup links are invalid."
    Show-SetupUrl $NewToken | Out-Null
}

function Test-ManagedCommandEntry {
    if (-not $Config.ContainsKey("TRACEABLE_RAG_BIN_LINK")) { return $false }
    $BinLinkPath = $Config.TRACEABLE_RAG_BIN_LINK
    $LauncherPath = Join-Path (Split-Path -Parent $BinLinkPath) "traceable-rag-launcher.ps1"
    $WrapperItem = Get-Item -LiteralPath $BinLinkPath -Force -ErrorAction SilentlyContinue
    $LauncherItem = Get-Item -LiteralPath $LauncherPath -Force -ErrorAction SilentlyContinue
    if (-not $WrapperItem -or -not $LauncherItem -or $WrapperItem.PSIsContainer -or $LauncherItem.PSIsContainer) { return $false }
    if (($WrapperItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or ($LauncherItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
    return ([IO.File]::ReadAllText($BinLinkPath) -eq (Get-ManagedWrapperContent) -and [IO.File]::ReadAllText($LauncherPath) -eq (Get-ManagedLauncherContent))
}

function Uninstall-Command([string[]]$Args) {
    $Purge = $Args -contains "--purge"
    $Confirmed = $Args -contains "--yes"
    foreach ($Arg in $Args) { if ($Arg -notin @("--purge", "--yes")) { Fail "unknown uninstall option: $Arg" } }
    if ($Purge) {
        if (-not $Confirmed) { Fail "purge is destructive; repeat with uninstall --purge --yes" }
        Assert-InstallationMarkers
        foreach ($PurgePath in @($ConfigDir, $DataDir, $StateDir, $BackupDir)) {
            if ([string]::IsNullOrWhiteSpace($PurgePath) -or $PurgePath -eq [IO.Path]::GetPathRoot($PurgePath) -or $PurgePath -eq $HOME) { Fail "refusing unsafe purge path: $PurgePath" }
        }
    }

    Invoke-Compose -ComposeArgs @("down", "--remove-orphans") | Out-Null

    if ($Purge) {
        foreach ($PurgePath in @($DataDir, $BackupDir, $StateDir, $ConfigDir)) {
            if (Test-Path -LiteralPath $PurgePath) { Remove-Item -LiteralPath $PurgePath -Recurse -Force -ErrorAction Stop }
        }
    }
    if (Test-ManagedCommandEntry) {
        $BinLinkPath = $Config.TRACEABLE_RAG_BIN_LINK
        Remove-Item -LiteralPath (Join-Path (Split-Path -Parent $BinLinkPath) "traceable-rag-launcher.ps1") -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $BinLinkPath -Force -ErrorAction SilentlyContinue
    } elseif ($Config.ContainsKey("TRACEABLE_RAG_BIN_LINK") -and (Test-Path -LiteralPath $Config.TRACEABLE_RAG_BIN_LINK)) {
        Write-Warning "command entry no longer matches this installation and was preserved: $($Config.TRACEABLE_RAG_BIN_LINK)"
    }
    if ($Purge) { Write-Output "Runtime, data, configuration and backups were deleted." }
    else { Write-Output "Runtime removed. Data, configuration and backups were preserved." }
}

$ReadOnlyWhilePending = $Command -in @("status", "doctor", "logs", "setup-url", "recover")
if ($HasPendingOperation -and -not $ReadOnlyWhilePending) { Assert-NoPendingOperation }
if ($HasPendingOperation -and $Command -ne "recover") { Write-Warning "an interrupted deployment operation is pending; run traceable-rag recover before making changes" }
$NeedsOperationLock = $Command -in @("start", "stop", "backup", "upgrade", "rollback", "recover", "uninstall", "admin") -or ($Command -eq "agent" -and $CommandArgs.Count -gt 0 -and $CommandArgs[0] -eq "install-codex")
if ($NeedsOperationLock) { Acquire-OperationLock }
try {
switch ($Command) {
    "start" {
        if ($CommandArgs.Count) { Fail "start does not accept arguments" }
        Ensure-Docker
        $ResolvedImage = Pull-AndResolveDigest $script:Image
        if ($ResolvedImage -ne $script:Image) { Set-ConfigValue "TRACEABLE_RAG_IMAGE" $ResolvedImage }
        Invoke-Compose -ComposeArgs @("config", "--quiet") | Out-Null
        Invoke-Compose -ComposeArgs @("up", "--detach", "--no-build", "--remove-orphans", "traceable-rag") | Out-Null
        if (-not (Wait-Ready)) { Fail "service did not become ready" }
        Write-Output "Traceable RAG is ready at $(Get-BaseUrl)"
    }
    "stop" { Ensure-Docker; Invoke-Compose -ComposeArgs @("stop", "--timeout", "45", "traceable-rag") | Out-Null }
    "status" {
        Ensure-Docker
        Invoke-Compose -ComposeArgs @("ps") | Out-Null
        Write-Output "livez=$(Get-HttpStatus '/livez') readyz=$(Get-HttpStatus '/readyz') health=$(Get-HttpStatus '/api/health') url=$(Get-BaseUrl)"
    }
    "setup-url" { Show-SetupUrl $AdminToken | Out-Null }
    "doctor" { Doctor-Command $CommandArgs }
    "logs" {
        Ensure-Docker
        if ($CommandArgs.Count) { Invoke-Compose -ComposeArgs (@("logs") + $CommandArgs + @("traceable-rag")) | Out-Null }
        else { Invoke-Compose -ComposeArgs @("logs", "--tail", "200", "traceable-rag") | Out-Null }
    }
    "backup" {
        Ensure-Docker
        if ($CommandArgs.Count -gt 1) { Fail "usage: traceable-rag backup [label]" }
        Backup-Command $(if ($CommandArgs.Count) { $CommandArgs[0] } else { "" })
    }
    "upgrade" {
        Ensure-Docker
        if ($CommandArgs.Count -ne 1) { Fail "usage: traceable-rag upgrade <vX.Y.Z>" }
        Upgrade-Command $CommandArgs[0]
    }
    "rollback" { Ensure-Docker; Rollback-Command }
    "recover" {
        if ($CommandArgs.Count) { Fail "recover does not accept arguments" }
        Ensure-Docker
        Recover-Command
    }
    "agent" {
        Ensure-Docker
        if (-not $CommandArgs.Count) { Fail "usage: traceable-rag agent <install-codex|doctor-codex>" }
        $Subcommand = $CommandArgs[0]
        $SubArgs = if ($CommandArgs.Count -gt 1) { $CommandArgs[1..($CommandArgs.Count - 1)] } else { @() }
        if ($Subcommand -eq "install-codex") { Install-CodexSkill $SubArgs }
        elseif ($Subcommand -eq "doctor-codex") { if ($SubArgs.Count) { Fail "agent doctor-codex takes no arguments" }; Doctor-CodexSkill }
        else { Fail "unknown agent command: $Subcommand" }
    }
    "admin" {
        Ensure-Docker
        if ($CommandArgs.Count -ne 1 -or $CommandArgs[0] -ne "rotate-token") { Fail "usage: traceable-rag admin rotate-token" }
        Rotate-AdminToken
    }
    "uninstall" { Ensure-Docker; Uninstall-Command $CommandArgs }
    default { Show-Help; Fail "unknown command: $Command" }
}
} finally {
    Release-OperationLock
}
