# First-logon setup for the windows-server-2022-iso-boot box.
#
# Runs once, as Administrator, from the answer ISO (FirstLogonCommands).
# Every step runs even when an earlier one failed, and all of it is logged
# to C:\boxman-setup.log. The last line written is "boxman-setup: done" when
# every step worked, or "boxman-setup: FAILED: ..." naming those that did not.
#
# The CD-ROMs are install-only: boxman ejects them on the first `up` or
# `update` that finds the QEMU guest agent answering. So nothing is read off
# them after step 1 installs the agent; the public key is read first.

Start-Transcript -Path C:\boxman-setup.log -Append
$ErrorActionPreference = 'Continue'
$failed = @()

function Find-Media($relpath) {
    foreach ($d in 'D', 'E', 'F', 'G') {
        if (Test-Path "${d}:\$relpath") { return "${d}:\$relpath" }
    }
    return $null
}

# Record a step that did not complete, for the last line of the log.
function Fail($what) {
    Write-Output "FAILED: $what"
    $script:failed += $what
}

function Install-Msi($relpath) {
    $msi = Find-Media $relpath
    if (-not $msi) { Fail "$relpath not found"; return }
    Write-Output "installing $msi"
    $p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList '/i', $msi, '/qn', '/norestart'
    # 3010: installed, completed by the next reboot
    if ($p.ExitCode -notin 0, 3010) { Fail "$msi (msiexec exit code $($p.ExitCode))" }
}

# 0. The optional public key for Administrator, read now: the media may be
#    gone by step 4.
$authorizedKeys = $null
$key = Find-Media 'authorized_keys'
if ($key) { $authorizedKeys = Get-Content -Raw -Path $key }

# 1. virtio drivers for everything (balloon, serial, rng, ...) plus the QEMU
#    guest agent, last: the agent answering is what tells boxman the media
#    are no longer needed. WinPE only loaded viostor and NetKVM.
Install-Msi 'virtio-win-gt-x64.msi'
Install-Msi 'guest-agent\qemu-ga-x86_64.msi'

# 2. Network profile private, ping allowed.
Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private
Enable-NetFirewallRule -Name 'FPS-ICMP4-ERQ-In' -ErrorAction SilentlyContinue

# 3. WinRM over HTTP (5985) with the default Negotiate/NTLM auth and message
#    encryption -- no Basic, no AllowUnencrypted. Ansible: winrm + ntlm.
try {
    Enable-PSRemoting -Force -SkipNetworkProfileCheck -ErrorAction Stop
    Set-Item -Path WSMan:\localhost\Service\Auth\Basic -Value $false -ErrorAction Stop
} catch { Fail "WinRM: $_" }

# 4. OpenSSH server. On Server 2022 it is a Feature on Demand fetched from
#    Windows Update, so this needs the guest to reach the internet.
try {
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction Stop
    if ($cap.State -ne 'Installed') {
        Add-WindowsCapability -Online -Name $cap.Name -ErrorAction Stop
    }
} catch { Fail "OpenSSH capability: $_" }
if (Get-Service sshd -ErrorAction SilentlyContinue) {
    try {
        Set-Service sshd -StartupType Automatic -ErrorAction Stop
        Start-Service sshd -ErrorAction Stop
    } catch { Fail "sshd: $_" }
    New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Force `
        -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' | Out-Null
    if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    }
    # The public key read in step 0 (members of Administrators read this
    # file, not ~/.ssh/authorized_keys).
    if ($authorizedKeys) {
        $dst = 'C:\ProgramData\ssh\administrators_authorized_keys'
        Set-Content -Path $dst -Value $authorizedKeys -NoNewline -Encoding ascii
        icacls.exe $dst /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "authorized_keys (icacls exit code $LASTEXITCODE)" }
    }
} else { Fail 'sshd not available (OpenSSH capability did not install)' }

# 5. Auto-logon off. LogonCount 1 allows no second auto-logon, but it leaves
#    AutoAdminLogon set until Windows clears it at the next boot. Clear it
#    now, with the count and any stored password.
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
try {
    Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value '0' -ErrorAction Stop
} catch { Fail "auto-logon off: $_" }
Remove-ItemProperty -Path $winlogon -Name AutoLogonCount, DefaultPassword -ErrorAction SilentlyContinue

# Setup keeps the AutoLogon password as the LSA secret DefaultPassword, not
# in the registry value, and nothing removes it: not the last logon, not a
# reboot. LsaStorePrivateData with no data deletes a secret.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class BoxmanLsa {
    [StructLayout(LayoutKind.Sequential)]
    struct LSA_UNICODE_STRING { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
    [StructLayout(LayoutKind.Sequential)]
    struct LSA_OBJECT_ATTRIBUTES {
        public int Length; public IntPtr RootDirectory; public IntPtr ObjectName;
        public uint Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService;
    }
    [DllImport("advapi32.dll")]
    static extern uint LsaOpenPolicy(IntPtr system, ref LSA_OBJECT_ATTRIBUTES attrs, uint access, out IntPtr policy);
    [DllImport("advapi32.dll")]
    static extern uint LsaStorePrivateData(IntPtr policy, ref LSA_UNICODE_STRING key, IntPtr data);
    [DllImport("advapi32.dll")]
    static extern uint LsaClose(IntPtr policy);
    [DllImport("advapi32.dll")]
    static extern int LsaNtStatusToWinError(uint status);

    // The Win32 error code: 0 when deleted, 2 when there was no such secret.
    public static int DeleteSecret(string name) {
        var attrs = new LSA_OBJECT_ATTRIBUTES();
        attrs.Length = Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
        IntPtr policy;
        uint status = LsaOpenPolicy(IntPtr.Zero, ref attrs, 0x000F0FFF /* POLICY_ALL_ACCESS */, out policy);
        if (status != 0) return LsaNtStatusToWinError(status);
        var key = new LSA_UNICODE_STRING();
        key.Length = (ushort)(name.Length * 2);
        key.MaximumLength = (ushort)(key.Length + 2);
        key.Buffer = Marshal.StringToHGlobalUni(name);
        try {
            status = LsaStorePrivateData(policy, ref key, IntPtr.Zero);
        } finally {
            Marshal.FreeHGlobal(key.Buffer);
            LsaClose(policy);
        }
        return LsaNtStatusToWinError(status);
    }
}
'@
$err = [BoxmanLsa]::DeleteSecret('DefaultPassword')
if ($err -notin 0, 2) { Fail "LSA secret DefaultPassword (Win32 error $err)" }

if ($failed) {
    Write-Output "boxman-setup: FAILED: $($failed -join '; ')"
} else {
    Write-Output 'boxman-setup: done'
}
Stop-Transcript

# End the one auto-logon session: otherwise the console (VNC, no password)
# stays logged in as Administrator.
shutdown.exe /l /f
