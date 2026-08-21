from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_installer_rejects_broad_owned_directory_access_before_writes() -> None:
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    preflight = installer.split(
        "function Assert-OwnedDirectoryAclIsPrivate", 1
    )[1].split("function Assert-MasterKey", 1)[0]

    assert "Get-Acl -LiteralPath $Path" in preflight
    assert "AccessControlType]::Allow" in preflight
    assert "FileSystemRights]::FullControl" in preflight
    assert '"S-1-1-0" = "Everyone"' in preflight
    assert '"S-1-5-11" = "Authenticated Users"' in preflight
    assert '"S-1-5-32-545" = "BUILTIN\\Users"' in preflight
    assert '"S-1-5-32-546" = "BUILTIN\\Guests"' in preflight
    assert "use a private directory for the current user" in preflight
    assert "Set-Acl" not in preflight
    assert "SetAccessRuleProtection" not in preflight
    assert "DirectorySecurity" not in preflight

    calls = [
        "Assert-OwnedDirectoryAclIsPrivate $ConfigDir",
        "Assert-OwnedDirectoryAclIsPrivate $DataDir",
        "Assert-OwnedDirectoryAclIsPrivate $StateDir",
        "Assert-OwnedDirectoryAclIsPrivate $BackupDir",
    ]
    for call in calls:
        assert installer.count(call) == 1
    assert "Assert-OwnedDirectoryAclIsPrivate $BinDir" not in installer

    owned_root_creation = installer.index(
        "if (-not (Test-Path -LiteralPath $OwnedDir)) { New-Item -ItemType Directory",
        installer.index("function Assert-OwnedDirectoryAclIsPrivate"),
    )
    first_acl_call = min(installer.index(call) for call in calls)
    final_acl_call = max(installer.index(call) for call in calls)
    marker_write = installer.index(
        'Write-Utf8NoBom $Marker $InstallationId', final_acl_call
    )
    owned_child_write = installer.index(
        'New-Item -ItemType Directory -Force -Path (Join-Path $ConfigDir "secrets")',
        final_acl_call,
    )
    master_key_write = installer.index(
        "[IO.File]::WriteAllBytes($MasterKeyFile", final_acl_call
    )
    assert owned_root_creation < first_acl_call
    assert final_acl_call < marker_write
    assert final_acl_call < owned_child_write
    assert final_acl_call < master_key_write
