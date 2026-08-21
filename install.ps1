[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$DefaultVersion = "@TRACEABLE_RAG_VERSION@"
$UnrenderedVersion = "@" + "TRACEABLE_RAG_VERSION" + "@"
$Repository = if ($env:TRACEABLE_RAG_REPOSITORY) { $env:TRACEABLE_RAG_REPOSITORY } else { "yo-solo/traceable-rag" }
$ImageRepository = if ($env:TRACEABLE_RAG_IMAGE_REPOSITORY) { $env:TRACEABLE_RAG_IMAGE_REPOSITORY } else { "ghcr.io/yo-solo/traceable-rag-public" }
$SourceScriptPath = $MyInvocation.MyCommand.Path
$SourceScriptDir = if ($SourceScriptPath) { Split-Path -Parent $SourceScriptPath } else { "" }

function Fail([string]$Message) { throw "Traceable RAG installer: $Message" }
function Write-Utf8NoBom([string]$Path, [string]$Content) { [IO.File]::WriteAllText($Path, $Content, $script:Utf8NoBom) }

function Set-EnvSettingAtomic([string]$Path, [string]$Key, [string]$Value) {
    $Lines = [IO.File]::ReadAllLines($Path)
    $Indexes = @()
    for ($Index = 0; $Index -lt $Lines.Count; $Index++) {
        if ($Lines[$Index].StartsWith("$Key=", [StringComparison]::Ordinal)) { $Indexes += $Index }
    }
    if ($Indexes.Count -ne 1) { Fail "existing deployment must contain exactly one $Key setting" }
    $Lines[$Indexes[0]] = "$Key=$Value"
    $TemporaryPath = "$Path.port.$PID.$([guid]::NewGuid().ToString('N'))"
    try {
        [IO.File]::WriteAllLines($TemporaryPath, $Lines, $script:Utf8NoBom)
        [IO.File]::Replace($TemporaryPath, $Path, $null)
    } finally {
        if (Test-Path -LiteralPath $TemporaryPath) { Remove-Item -LiteralPath $TemporaryPath -Force -ErrorAction SilentlyContinue }
    }
}

function Write-PortRetryHint {
    $RetryPort = if ($script:Port -lt 65535) { $script:Port + 1 } else { 8000 }
    Write-Warning "The service did not start. If local port $($script:Port) is occupied, rerun this same-version installer on another loopback port:"
    Write-Output ('  $env:TRACEABLE_RAG_PORT = "{0}"' -f $RetryPort)
    if ($script:SourceScriptPath) {
        Write-Output "  powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File `"$($script:SourceScriptPath)`""
    } else {
        Write-Output "  irm https://github.com/$($script:Repository)/releases/download/$($script:Version)/install.ps1 | iex"
    }
    Write-Warning "The rerun keeps the existing data, master key, admin token, and installation ID."
}

function New-RandomBytes([int]$Count) {
    $Bytes = New-Object byte[] $Count
    $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Generator.GetBytes($Bytes) } finally { $Generator.Dispose() }
    return $Bytes
}

function ConvertTo-Hex([byte[]]$Bytes) { return ([BitConverter]::ToString($Bytes)).Replace("-", "").ToLowerInvariant() }

function Assert-NotReparsePoint([string]$Path) {
    $Item = Get-Item -LiteralPath $Path -Force
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail "secret path must not be a reparse point: $Path" }
}

function Set-SecretAcl([string]$Path) {
    $CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $SystemSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
    $Acl = New-Object Security.AccessControl.FileSecurity
    $Acl.SetOwner($CurrentSid)
    $Acl.SetAccessRuleProtection($true, $false)
    $Acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($CurrentSid, "FullControl", "Allow")))
    $Acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($SystemSid, "FullControl", "Allow")))
    Set-Acl -LiteralPath $Path -AclObject $Acl
    $Allowed = @($CurrentSid.Value, $SystemSid.Value)
    foreach ($Rule in (Get-Acl -LiteralPath $Path).Access) {
        if ($Rule.AccessControlType -ne "Allow") { continue }
        $Sid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($Sid -notin $Allowed) { Fail "secret ACL contains an unexpected principal: $Sid" }
    }
}

function Assert-OwnedDirectoryAclIsPrivate([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { Fail "owned directory is missing: $Path" }
    Assert-NotReparsePoint $Path
    $BroadPrincipals = @{
        "S-1-1-0" = "Everyone"
        "S-1-5-11" = "Authenticated Users"
        "S-1-5-32-545" = "BUILTIN\Users"
        "S-1-5-32-546" = "BUILTIN\Guests"
    }
    $DataAccessMask = [Security.AccessControl.FileSystemRights]::FullControl
    foreach ($Rule in @((Get-Acl -LiteralPath $Path).Access)) {
        if ($Rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        if (($Rule.FileSystemRights -band $DataAccessMask) -eq 0) { continue }
        try { $Sid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }
        catch { Fail "could not verify an access rule on owned directory: $Path" }
        if ($BroadPrincipals.ContainsKey($Sid)) {
            Fail "owned directory grants data access to $($BroadPrincipals[$Sid]); use a private directory for the current user or tighten the ACL yourself before rerunning: $Path"
        }
    }
}

function Assert-MasterKey([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "master key is missing: $Path" }
    Assert-NotReparsePoint $Path
    if ((Get-Item -LiteralPath $Path).Length -ne 32) { Fail "master key must contain exactly 32 raw bytes" }
    Set-SecretAcl $Path
}

function Assert-AdminToken([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "admin token is missing: $Path" }
    Assert-NotReparsePoint $Path
    if ([IO.File]::ReadAllText($Path) -notmatch '^[0-9a-f]{64}$') { Fail "admin token must encode 32 random bytes as 64 hex characters" }
    Set-SecretAcl $Path
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
    while ($Full.Length -gt $RootPath.Length -and ($Full.EndsWith("\") -or $Full.EndsWith("/"))) {
        $Full = $Full.Substring(0, $Full.Length - 1)
    }
    return $Full
}

function Read-ValidatedReleaseManifest([string]$Path, [string]$ExpectedVersion, [string]$ExpectedImageRepository) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "release manifest is missing: $Path" }
    Assert-NotReparsePoint $Path
    try { $Manifest = [IO.File]::ReadAllText($Path) | ConvertFrom-Json } catch { Fail "release manifest is not valid JSON" }
    if ($Manifest.schema_version -ne 1) { Fail "unsupported release manifest schema version" }
    if ($Manifest.version -ne $ExpectedVersion) { Fail "release manifest version mismatch" }
    if ($Manifest.image -ne $ExpectedImageRepository) { Fail "release manifest image repository mismatch" }
    if ($Manifest.index_digest -notmatch '^sha256:[0-9a-f]{64}$') { Fail "release manifest index digest is invalid" }
    if ($Manifest.source_commit -notmatch '^[0-9a-f]{40}$') { Fail "release manifest source commit is invalid" }
    if ($Manifest.platforms.'linux/amd64' -notmatch '^sha256:[0-9a-f]{64}$') { Fail "release manifest linux/amd64 digest is invalid" }
    if ($Manifest.platforms.'linux/arm64' -notmatch '^sha256:[0-9a-f]{64}$') { Fail "release manifest linux/arm64 digest is invalid" }
    return $Manifest
}

function Get-LauncherContent([string]$InstallationId, [string]$ConfigPath, [string]$ManagerPath) {
    $EscapedConfig = $ConfigPath.Replace("'", "''")
    $EscapedManager = $ManagerPath.Replace("'", "''")
    return "# Traceable-RAG installation: $InstallationId`r`n`$env:TRACEABLE_RAG_CONFIG_DIR = '$EscapedConfig'`r`n& '$EscapedManager' @args`r`n"
}

function Get-WrapperContent([string]$InstallationId) {
    return "@echo off`r`nrem Traceable-RAG installation: $InstallationId`r`n@chcp 65001 >nul`r`npowershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File `"%~dp0traceable-rag-launcher.ps1`" %*`r`n"
}

$Version = if ($env:TRACEABLE_RAG_VERSION) { $env:TRACEABLE_RAG_VERSION } else { $DefaultVersion }
if ($Version -eq $UnrenderedVersion) {
    $ProjectFile = if ($SourceScriptDir) { Join-Path $SourceScriptDir "pyproject.toml" } else { "" }
    if ($ProjectFile -and (Test-Path -LiteralPath $ProjectFile -PathType Leaf)) {
        $Match = Select-String -LiteralPath $ProjectFile -Pattern '^version = "([^"]+)"' | Select-Object -First 1
        if (-not $Match) { Fail "could not read the project version" }
        $Version = "v$($Match.Matches[0].Groups[1].Value)"
    } else { Fail "use the versioned install.ps1 attached to a GitHub Release" }
}
if (-not $Version.StartsWith("v")) { $Version = "v$Version" }
if ($Version -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$') { Fail "TRACEABLE_RAG_VERSION must be an exact release such as v0.4.2" }

$Root = if ($env:TRACEABLE_RAG_HOME) { $env:TRACEABLE_RAG_HOME } else { Join-Path $env:LOCALAPPDATA "TraceableRAG" }
$ExplicitDataDir = Test-Path Env:TRACEABLE_RAG_DATA_DIR
$ExplicitStateDir = Test-Path Env:TRACEABLE_RAG_STATE_DIR
$ExplicitBackupDir = Test-Path Env:TRACEABLE_RAG_BACKUP_DIR
$ExplicitBinDir = Test-Path Env:TRACEABLE_RAG_BIN_DIR
$ExplicitProjectName = Test-Path Env:TRACEABLE_RAG_PROJECT_NAME
$ExplicitBindAddress = Test-Path Env:TRACEABLE_RAG_BIND_ADDRESS
$ExplicitPort = Test-Path Env:TRACEABLE_RAG_PORT
$ExplicitReadyTimeout = Test-Path Env:TRACEABLE_RAG_READY_TIMEOUT_SECONDS
$ExplicitImageRepository = Test-Path Env:TRACEABLE_RAG_IMAGE_REPOSITORY
$ExplicitReleaseRepository = Test-Path Env:TRACEABLE_RAG_REPOSITORY
$ConfigDir = Get-NormalizedRootedPath "TRACEABLE_RAG_CONFIG_DIR" $(if ($env:TRACEABLE_RAG_CONFIG_DIR) { $env:TRACEABLE_RAG_CONFIG_DIR } else { Join-Path $Root "config" })
$DataDir = Get-NormalizedRootedPath "TRACEABLE_RAG_DATA_DIR" $(if ($env:TRACEABLE_RAG_DATA_DIR) { $env:TRACEABLE_RAG_DATA_DIR } else { Join-Path $Root "data" })
$StateDir = Get-NormalizedRootedPath "TRACEABLE_RAG_STATE_DIR" $(if ($env:TRACEABLE_RAG_STATE_DIR) { $env:TRACEABLE_RAG_STATE_DIR } else { Join-Path $Root "state" })
$BackupDir = Get-NormalizedRootedPath "TRACEABLE_RAG_BACKUP_DIR" $(if ($env:TRACEABLE_RAG_BACKUP_DIR) { $env:TRACEABLE_RAG_BACKUP_DIR } else { Join-Path $Root "backups" })
$BinDir = Get-NormalizedRootedPath "TRACEABLE_RAG_BIN_DIR" $(if ($env:TRACEABLE_RAG_BIN_DIR) { $env:TRACEABLE_RAG_BIN_DIR } else { Join-Path $Root "bin" })
$BinLink = Join-Path $BinDir "traceable-rag.cmd"
$MasterKeyFile = Join-Path $ConfigDir "secrets\master.key"
$AdminTokenFile = Join-Path $ConfigDir "secrets\admin-token"
$EnvFile = Join-Path $ConfigDir "deployment.env"
$ProjectName = if ($env:TRACEABLE_RAG_PROJECT_NAME) { $env:TRACEABLE_RAG_PROJECT_NAME } else { "traceable-rag-user" }
$BindAddress = if ($env:TRACEABLE_RAG_BIND_ADDRESS) { $env:TRACEABLE_RAG_BIND_ADDRESS } else { "127.0.0.1" }
if ($BindAddress -eq "localhost") { $BindAddress = "127.0.0.1" }
$Port = if ($env:TRACEABLE_RAG_PORT) { [int]$env:TRACEABLE_RAG_PORT } else { 8000 }
$ReadyTimeout = if ($env:TRACEABLE_RAG_READY_TIMEOUT_SECONDS) { [int]$env:TRACEABLE_RAG_READY_TIMEOUT_SECONDS } else { 900 }
$Image = "${ImageRepository}:$Version"
$ReleaseSourceCommit = ""
$StrictReleaseIdentity = $false
$PortChangeRequested = $false

if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
    $Stored = @{}
    foreach ($Line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
        if (-not $Line -or $Line.StartsWith("#")) { continue }
        $Parts = $Line.Split("=", 2)
        if ($Parts.Count -eq 2) { $Stored[$Parts[0]] = $Parts[1] }
    }
    if ($Stored.TRACEABLE_RAG_RELEASE_VERSION -ne $Version) { Fail "installed release is $($Stored.TRACEABLE_RAG_RELEASE_VERSION); use traceable-rag upgrade $Version instead of rerunning the installer" }
    $StoredDataDir = Get-NormalizedRootedPath "stored TRACEABLE_RAG_DATA_DIR" $Stored.TRACEABLE_RAG_DATA_DIR
    $StoredStateDir = Get-NormalizedRootedPath "stored TRACEABLE_RAG_STATE_DIR" $Stored.TRACEABLE_RAG_STATE_DIR
    $StoredBackupDir = Get-NormalizedRootedPath "stored TRACEABLE_RAG_BACKUP_DIR" $Stored.TRACEABLE_RAG_BACKUP_DIR
    $StoredBinLink = Get-NormalizedRootedPath "stored TRACEABLE_RAG_BIN_LINK" $Stored.TRACEABLE_RAG_BIN_LINK
    if ((Split-Path -Leaf $StoredBinLink) -ne "traceable-rag.cmd") { Fail "stored command entry is invalid" }
    $StoredBinDir = Get-NormalizedRootedPath "stored TRACEABLE_RAG_BIN_DIR" (Split-Path -Parent $StoredBinLink)
    if ($ExplicitDataDir -and -not [string]::Equals($DataDir, $StoredDataDir, [StringComparison]::OrdinalIgnoreCase)) { Fail "TRACEABLE_RAG_DATA_DIR differs from the installed layout" }
    if ($ExplicitStateDir -and -not [string]::Equals($StateDir, $StoredStateDir, [StringComparison]::OrdinalIgnoreCase)) { Fail "TRACEABLE_RAG_STATE_DIR differs from the installed layout" }
    if ($ExplicitBackupDir -and -not [string]::Equals($BackupDir, $StoredBackupDir, [StringComparison]::OrdinalIgnoreCase)) { Fail "TRACEABLE_RAG_BACKUP_DIR differs from the installed layout" }
    if ($ExplicitBinDir -and -not [string]::Equals($BinDir, $StoredBinDir, [StringComparison]::OrdinalIgnoreCase)) { Fail "TRACEABLE_RAG_BIN_DIR differs from the installed layout" }
    if ($ExplicitProjectName -and $env:TRACEABLE_RAG_PROJECT_NAME -ne $Stored.TRACEABLE_RAG_PROJECT_NAME) { Fail "TRACEABLE_RAG_PROJECT_NAME differs from the installed layout" }
    $RequestedBind = if ($env:TRACEABLE_RAG_BIND_ADDRESS -eq "localhost") { "127.0.0.1" } else { $env:TRACEABLE_RAG_BIND_ADDRESS }
    if ($ExplicitBindAddress -and $RequestedBind -ne $Stored.TRACEABLE_RAG_BIND_ADDRESS) { Fail "TRACEABLE_RAG_BIND_ADDRESS differs from the installed layout" }
    if ($ExplicitReadyTimeout -and "$($env:TRACEABLE_RAG_READY_TIMEOUT_SECONDS)" -ne $Stored.TRACEABLE_RAG_READY_TIMEOUT_SECONDS) { Fail "TRACEABLE_RAG_READY_TIMEOUT_SECONDS differs from the installed layout" }
    if ($ExplicitImageRepository -and $env:TRACEABLE_RAG_IMAGE_REPOSITORY -ne $Stored.TRACEABLE_RAG_IMAGE_REPOSITORY) { Fail "TRACEABLE_RAG_IMAGE_REPOSITORY differs from the installed layout" }
    if ($ExplicitReleaseRepository -and $env:TRACEABLE_RAG_REPOSITORY -ne $Stored.TRACEABLE_RAG_RELEASE_REPOSITORY) { Fail "TRACEABLE_RAG_REPOSITORY differs from the installed layout" }

    $DataDir = $StoredDataDir
    $StateDir = $StoredStateDir
    $BackupDir = $StoredBackupDir
    $BinLink = $StoredBinLink
    $BinDir = $StoredBinDir
    $ProjectName = $Stored.TRACEABLE_RAG_PROJECT_NAME
    $BindAddress = $Stored.TRACEABLE_RAG_BIND_ADDRESS
    if ($Stored.TRACEABLE_RAG_PORT -notmatch '^[0-9]+$') { Fail "existing deployment has an invalid TRACEABLE_RAG_PORT" }
    $StoredPort = [int]$Stored.TRACEABLE_RAG_PORT
    if ($ExplicitPort) {
        if ($Port -ne $StoredPort) { $PortChangeRequested = $true }
    } else { $Port = $StoredPort }
    $ReadyTimeout = [int]$Stored.TRACEABLE_RAG_READY_TIMEOUT_SECONDS
    $ImageRepository = $Stored.TRACEABLE_RAG_IMAGE_REPOSITORY
    $Repository = $Stored.TRACEABLE_RAG_RELEASE_REPOSITORY
    $Image = $Stored.TRACEABLE_RAG_IMAGE
    if ($Stored.ContainsKey("TRACEABLE_RAG_RELEASE_SOURCE_COMMIT")) {
        $ReleaseSourceCommit = $Stored.TRACEABLE_RAG_RELEASE_SOURCE_COMMIT
        $StrictReleaseIdentity = $true
    }
    $MasterKeyFile = Get-NormalizedRootedPath "stored TRACEABLE_RAG_MASTER_KEY_HOST_FILE" $Stored.TRACEABLE_RAG_MASTER_KEY_HOST_FILE
    $AdminTokenFile = Get-NormalizedRootedPath "stored TRACEABLE_RAG_ADMIN_TOKEN_HOST_FILE" $Stored.TRACEABLE_RAG_ADMIN_TOKEN_HOST_FILE
    if (-not [string]::Equals($MasterKeyFile, (Join-Path $ConfigDir "secrets\master.key"), [StringComparison]::OrdinalIgnoreCase)) { Fail "stored master key path differs from the installed config layout" }
    if (-not [string]::Equals($AdminTokenFile, (Join-Path $ConfigDir "secrets\admin-token"), [StringComparison]::OrdinalIgnoreCase)) { Fail "stored admin token path differs from the installed config layout" }
}

if ($ConfigDir -eq $DataDir -or $ConfigDir -eq $StateDir -or $DataDir -eq $StateDir) { Fail "config, data and state directories must be distinct" }
$ConfigFull = $ConfigDir
$DataFull = $DataDir
$StateFull = $StateDir
$BackupFull = $BackupDir
if ($ConfigFull.StartsWith("$DataFull\", [StringComparison]::OrdinalIgnoreCase) -or $ConfigFull.StartsWith("$StateFull\", [StringComparison]::OrdinalIgnoreCase)) { Fail "config directory must not be nested inside data or state" }
if ($DataFull.StartsWith("$ConfigFull\", [StringComparison]::OrdinalIgnoreCase) -or $DataFull.StartsWith("$StateFull\", [StringComparison]::OrdinalIgnoreCase) -or $DataFull.StartsWith("$BackupFull\", [StringComparison]::OrdinalIgnoreCase)) { Fail "data directory must remain independent from config, state and backups" }
if ($StateFull.StartsWith("$ConfigFull\", [StringComparison]::OrdinalIgnoreCase) -or $StateFull.StartsWith("$DataFull\", [StringComparison]::OrdinalIgnoreCase)) { Fail "state directory must remain independent from config and data" }
if ($BackupFull.StartsWith("$ConfigFull\", [StringComparison]::OrdinalIgnoreCase) -or $BackupFull.StartsWith("$DataFull\", [StringComparison]::OrdinalIgnoreCase)) { Fail "backup directory must not be inside config or data" }

if ($ProjectName -notmatch '^[a-z0-9][a-z0-9_-]*$') { Fail "TRACEABLE_RAG_PROJECT_NAME is invalid" }
if ($BindAddress -notin @("127.0.0.1", "::1")) { Fail "the bundled deployment supports loopback binding only" }
if ($Port -lt 1 -or $Port -gt 65535) { Fail "TRACEABLE_RAG_PORT must be between 1 and 65535" }
if ($ReadyTimeout -lt 30 -or $ReadyTimeout -gt 86400) { Fail "TRACEABLE_RAG_READY_TIMEOUT_SECONDS must be between 30 and 86400" }
foreach ($Value in @($ConfigDir, $DataDir, $StateDir, $BackupDir, $BinDir, $ProjectName, $BindAddress, "$Port", "$ReadyTimeout")) {
    Assert-NoControlCharacters "installation value" $Value
    if ($Value -match '[#$]') { Fail "installation values cannot contain dollar signs or # because Compose env parsing would be ambiguous" }
}

$Temporary = ""
try {
    $LocalBundle = if ($SourceScriptDir) { Join-Path $SourceScriptDir "deploy" } else { "" }
    if ($LocalBundle -and (Test-Path -LiteralPath (Join-Path $LocalBundle "compose.yaml")) -and (Test-Path -LiteralPath (Join-Path $LocalBundle "traceable-rag.ps1"))) {
        $BundleDir = $LocalBundle
        $BundleManifestPath = Join-Path $BundleDir "release-manifest.json"
        if (Test-Path -LiteralPath $BundleManifestPath -PathType Leaf) {
            $Manifest = Read-ValidatedReleaseManifest $BundleManifestPath $Version $ImageRepository
            $Image = "$($Manifest.image)@$($Manifest.index_digest)"
            $ReleaseSourceCommit = $Manifest.source_commit
            $StrictReleaseIdentity = $true
        } elseif ($StrictReleaseIdentity) { Fail "installed release identity requires a bundle release-manifest.json" }
    } else {
        $Temporary = Join-Path ([IO.Path]::GetTempPath()) "traceable-rag-install-$([guid]::NewGuid())"
        New-Item -ItemType Directory -Force -Path $Temporary | Out-Null
        $Asset = "traceable-rag-deployment-$Version.zip"
        $ReleaseBase = if ($env:TRACEABLE_RAG_RELEASE_BASE_URL) { $env:TRACEABLE_RAG_RELEASE_BASE_URL } else { "https://github.com/$Repository/releases/download/$Version" }
        $Archive = Join-Path $Temporary $Asset
        $ChecksumFile = Join-Path $Temporary "SHA256SUMS"
        Invoke-WebRequest -UseBasicParsing -Uri "$ReleaseBase/$Asset" -OutFile $Archive
        $ManifestPath = Join-Path $Temporary "release-manifest.json"
        Invoke-WebRequest -UseBasicParsing -Uri "$ReleaseBase/release-manifest.json" -OutFile $ManifestPath
        Invoke-WebRequest -UseBasicParsing -Uri "$ReleaseBase/SHA256SUMS" -OutFile $ChecksumFile
        $EscapedAsset = [regex]::Escape($Asset)
        $ChecksumLine = Get-Content -LiteralPath $ChecksumFile | Where-Object { $_ -match "^[0-9a-fA-F]{64}\s+\*?$EscapedAsset$" } | Select-Object -First 1
        if (-not $ChecksumLine) { Fail "release checksum does not list $Asset" }
        $Expected = ($ChecksumLine -split '\s+')[0].ToLowerInvariant()
        $Actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Expected -ne $Actual) { Fail "deployment bundle checksum mismatch" }
        $ManifestLine = Get-Content -LiteralPath $ChecksumFile | Where-Object { $_ -match '^[0-9a-fA-F]{64}\s+\*?release-manifest\.json$' } | Select-Object -First 1
        if (-not $ManifestLine) { Fail "release checksum does not list release-manifest.json" }
        $ManifestExpected = ($ManifestLine -split '\s+')[0].ToLowerInvariant()
        $ManifestActual = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ManifestExpected -ne $ManifestActual) { Fail "release manifest checksum mismatch" }
        $Manifest = Read-ValidatedReleaseManifest $ManifestPath $Version $ImageRepository
        $Image = "$($Manifest.image)@$($Manifest.index_digest)"
        $ReleaseSourceCommit = $Manifest.source_commit
        $StrictReleaseIdentity = $true
        $Unpacked = Join-Path $Temporary "unpacked"
        Expand-Archive -LiteralPath $Archive -DestinationPath $Unpacked -Force
        $BundleDir = Join-Path $Unpacked "deploy"
        $BundleManifestPath = Join-Path $BundleDir "release-manifest.json"
        if (-not (Test-Path -LiteralPath $BundleManifestPath -PathType Leaf)) { Fail "deployment bundle is missing release-manifest.json" }
        $BundleManifestHash = (Get-FileHash -LiteralPath $BundleManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($BundleManifestHash -ne $ManifestActual) { Fail "deployment bundle release manifest does not match the verified release asset" }
    }

    foreach ($Required in @("compose.yaml", "traceable-rag.ps1")) {
        if (-not (Test-Path -LiteralPath (Join-Path $BundleDir $Required) -PathType Leaf)) { Fail "deployment bundle is missing $Required" }
    }
    $InstallationId = ""
    if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
        $ExistingId = Select-String -LiteralPath $EnvFile -Pattern '^TRACEABLE_RAG_INSTALLATION_ID=(.+)$' | Select-Object -First 1
        if ($ExistingId) { $InstallationId = $ExistingId.Matches[0].Groups[1].Value }
    }
    if (-not $InstallationId) {
        foreach ($OwnedDir in @($ConfigDir, $DataDir, $StateDir, $BackupDir)) {
            $Marker = Join-Path $OwnedDir ".traceable-rag-installation"
            if (Test-Path -LiteralPath $Marker -PathType Leaf) {
                Assert-NotReparsePoint $Marker
                $MarkerId = [IO.File]::ReadAllText($Marker)
                if (-not $InstallationId) { $InstallationId = $MarkerId }
                elseif ($InstallationId -ne $MarkerId) { Fail "partial installation markers disagree; inspect the target directories" }
            }
        }
    }
    if (-not $InstallationId) { $InstallationId = ConvertTo-Hex (New-RandomBytes 16) }
    if ($InstallationId -notmatch '^[0-9a-f]{32}$') { Fail "installation ID is malformed" }
    foreach ($OwnedDir in @($ConfigDir, $DataDir, $StateDir, $BackupDir)) {
        if (Test-Path -LiteralPath $OwnedDir) {
            Assert-NotReparsePoint $OwnedDir
            $Marker = Join-Path $OwnedDir ".traceable-rag-installation"
            $HasContent = Get-ChildItem -LiteralPath $OwnedDir -Force | Select-Object -First 1
            if (-not (Test-Path -LiteralPath $Marker) -and $HasContent) { Fail "refusing to claim a non-empty unmarked directory: $OwnedDir" }
        }
        $Marker = Join-Path $OwnedDir ".traceable-rag-installation"
        if (Test-Path -LiteralPath $Marker) {
            Assert-NotReparsePoint $Marker
            if ([IO.File]::ReadAllText($Marker) -ne $InstallationId) { Fail "installation marker mismatch: $OwnedDir" }
        }
    }
    $ManagerPath = Join-Path $ConfigDir "traceable-rag.ps1"
    $LauncherPath = Join-Path $BinDir "traceable-rag-launcher.ps1"
    $ExpectedLauncher = Get-LauncherContent $InstallationId $ConfigDir $ManagerPath
    $ExpectedWrapper = Get-WrapperContent $InstallationId
    $ExistingLauncher = Get-Item -LiteralPath $LauncherPath -Force -ErrorAction SilentlyContinue
    $ExistingWrapper = Get-Item -LiteralPath $BinLink -Force -ErrorAction SilentlyContinue
    if ($ExistingLauncher -or $ExistingWrapper) {
        if (-not $ExistingLauncher -or -not $ExistingWrapper) { Fail "refusing to replace a partial command entry in $BinDir; choose another TRACEABLE_RAG_BIN_DIR" }
        if ($ExistingLauncher.PSIsContainer -or $ExistingWrapper.PSIsContainer) { Fail "refusing to replace an unmanaged command entry in $BinDir; choose another TRACEABLE_RAG_BIN_DIR" }
        if (($ExistingLauncher.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or ($ExistingWrapper.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail "refusing to replace a redirected command entry in $BinDir; choose another TRACEABLE_RAG_BIN_DIR" }
        if ([IO.File]::ReadAllText($LauncherPath) -ne $ExpectedLauncher -or [IO.File]::ReadAllText($BinLink) -ne $ExpectedWrapper) { Fail "refusing to replace an unmanaged command entry in $BinDir; choose another TRACEABLE_RAG_BIN_DIR" }
    }
    foreach ($OwnedDir in @($ConfigDir, $DataDir, $StateDir, $BackupDir)) {
        if (-not (Test-Path -LiteralPath $OwnedDir)) { New-Item -ItemType Directory -Force -Path $OwnedDir | Out-Null }
    }
    Assert-OwnedDirectoryAclIsPrivate $ConfigDir
    Assert-OwnedDirectoryAclIsPrivate $DataDir
    Assert-OwnedDirectoryAclIsPrivate $StateDir
    Assert-OwnedDirectoryAclIsPrivate $BackupDir
    foreach ($OwnedDir in @($ConfigDir, $DataDir, $StateDir, $BackupDir)) {
        $Marker = Join-Path $OwnedDir ".traceable-rag-installation"
        if (-not (Test-Path -LiteralPath $Marker)) { Write-Utf8NoBom $Marker $InstallationId }
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $ConfigDir "secrets"), (Join-Path $DataDir "home"), (Join-Path $DataDir "managed"), (Join-Path $StateDir "upgrades"), $BinDir | Out-Null

    if (-not (Test-Path -LiteralPath $MasterKeyFile)) { [IO.File]::WriteAllBytes($MasterKeyFile, (New-RandomBytes 32)) }
    Assert-MasterKey $MasterKeyFile
    if (-not (Test-Path -LiteralPath $AdminTokenFile)) { Write-Utf8NoBom $AdminTokenFile (ConvertTo-Hex (New-RandomBytes 32)) }
    Assert-AdminToken $AdminTokenFile

    function Ensure-Setting([string]$Key, [string]$Value) {
        $Text = if (Test-Path -LiteralPath $EnvFile) { [IO.File]::ReadAllText($EnvFile) } else { "" }
        if ($Text -notmatch "(?m)^$([regex]::Escape($Key))=") {
            if ($Text -and -not $Text.EndsWith("`n")) { $Text += "`n" }
            Write-Utf8NoBom $EnvFile ($Text + "$Key=$Value`n")
        }
    }

    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        $Settings = @(
            "TRACEABLE_RAG_IMAGE_REPOSITORY=$ImageRepository", "TRACEABLE_RAG_RELEASE_REPOSITORY=$Repository", "TRACEABLE_RAG_RELEASE_VERSION=$Version",
            $(if ($StrictReleaseIdentity) { "TRACEABLE_RAG_RELEASE_SOURCE_COMMIT=$ReleaseSourceCommit" }), "TRACEABLE_RAG_IMAGE=$Image",
            "TRACEABLE_RAG_PROJECT_NAME=$ProjectName", "TRACEABLE_RAG_BIND_ADDRESS=$BindAddress", "TRACEABLE_RAG_PORT=$Port",
            "TRACEABLE_RAG_READY_TIMEOUT_SECONDS=$ReadyTimeout",
            "TRACEABLE_RAG_DATA_DIR=$DataDir", "TRACEABLE_RAG_MASTER_KEY_HOST_FILE=$MasterKeyFile", "TRACEABLE_RAG_ADMIN_TOKEN_HOST_FILE=$AdminTokenFile",
            "TRACEABLE_RAG_STATE_DIR=$StateDir", "TRACEABLE_RAG_BACKUP_DIR=$BackupDir", "TRACEABLE_RAG_BIN_LINK=$BinLink",
            "TRACEABLE_RAG_CONTAINER_UID=10001", "TRACEABLE_RAG_CONTAINER_GID=10001", "TRACEABLE_RAG_INSTALLATION_ID=$InstallationId"
        ) -join "`n"
        Write-Utf8NoBom $EnvFile "$Settings`n"
    } else {
        Ensure-Setting "TRACEABLE_RAG_RELEASE_REPOSITORY" $Repository
        Ensure-Setting "TRACEABLE_RAG_RELEASE_VERSION" $Version
        if ($StrictReleaseIdentity) { Ensure-Setting "TRACEABLE_RAG_RELEASE_SOURCE_COMMIT" $ReleaseSourceCommit }
        Ensure-Setting "TRACEABLE_RAG_ADMIN_TOKEN_HOST_FILE" $AdminTokenFile
        Ensure-Setting "TRACEABLE_RAG_READY_TIMEOUT_SECONDS" "$ReadyTimeout"
        Ensure-Setting "TRACEABLE_RAG_BIN_LINK" $BinLink
        Ensure-Setting "TRACEABLE_RAG_CONTAINER_UID" "10001"
        Ensure-Setting "TRACEABLE_RAG_CONTAINER_GID" "10001"
        Ensure-Setting "TRACEABLE_RAG_INSTALLATION_ID" $InstallationId
        if ($PortChangeRequested) { Set-EnvSettingAtomic $EnvFile "TRACEABLE_RAG_PORT" "$Port" }
    }

    $ComposeTemplate = [IO.File]::ReadAllText((Join-Path $BundleDir "compose.yaml"))
    $RenderedCompose = $ComposeTemplate.Replace("@TRACEABLE_RAG_CONTAINER_UID@", "10001").Replace("@TRACEABLE_RAG_CONTAINER_GID@", "10001")
    if ($RenderedCompose -notmatch 'user:\s*"10001:10001"') { Fail "could not render the non-root Compose user" }
    Write-Utf8NoBom (Join-Path $ConfigDir "compose.yaml") $RenderedCompose
    if ($StrictReleaseIdentity) {
        Copy-Item -LiteralPath (Join-Path $BundleDir "release-manifest.json") -Destination (Join-Path $ConfigDir "release-manifest.json") -Force
        Set-SecretAcl (Join-Path $ConfigDir "release-manifest.json")
    }
    Copy-Item -LiteralPath (Join-Path $BundleDir "traceable-rag.ps1") -Destination $ManagerPath -Force
    Unblock-File -LiteralPath $ManagerPath -ErrorAction SilentlyContinue
    Set-SecretAcl $ManagerPath

    [IO.File]::WriteAllText($LauncherPath, $ExpectedLauncher, [Text.Encoding]::Unicode)
    Unblock-File -LiteralPath $LauncherPath -ErrorAction SilentlyContinue
    Set-SecretAcl $LauncherPath
    [IO.File]::WriteAllText($BinLink, $ExpectedWrapper, [Text.Encoding]::ASCII)
    Set-SecretAcl $BinLink
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $PathEntries = @($UserPath -split ';' | Where-Object { $_ })
    if ($BinDir -notin $PathEntries) { [Environment]::SetEnvironmentVariable("Path", ((@($PathEntries) + $BinDir) -join ';'), "User") }
    if ($BinDir -notin ($env:Path -split ';')) { $env:Path = "$BinDir;$env:Path" }

    Write-Output "Traceable RAG $Version deployment files installed."
    Write-Output "  command: $BinLink"
    Write-Output "  config:  $ConfigDir"
    Write-Output "  data:    $DataDir"
    Write-Output "  backups: $BackupDir"
    if ($env:TRACEABLE_RAG_NO_START -eq "1") { Write-Output "Automatic start skipped. Run: traceable-rag start" }
    else {
        & powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File $ManagerPath start
        if ($LASTEXITCODE -ne 0) {
            Write-PortRetryHint
            Fail "the installed service did not start"
        }
        & powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File $ManagerPath setup-url
    }
} finally {
    if ($Temporary -and (Test-Path -LiteralPath $Temporary)) { Remove-Item -LiteralPath $Temporary -Recurse -Force -ErrorAction SilentlyContinue }
}
