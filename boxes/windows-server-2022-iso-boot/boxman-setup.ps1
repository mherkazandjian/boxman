# First-logon setup for the windows-server-2022-iso-boot box.
#
# Runs once, as Administrator, from the answer ISO (FirstLogonCommands).
# Every step is best-effort and logged to C:\boxman-setup.log; the last line
# written is "boxman-setup: done", which is what to look for.

Start-Transcript -Path C:\boxman-setup.log -Append
$ErrorActionPreference = 'Continue'

function Find-Media($relpath) {
    foreach ($d in 'D', 'E', 'F', 'G') {
        if (Test-Path "${d}:\$relpath") { return "${d}:\$relpath" }
    }
    return $null
}

# 1. virtio drivers for everything (balloon, serial, rng, ...) plus the QEMU
#    guest agent. WinPE only loaded viostor and NetKVM.
$gt = Find-Media 'virtio-win-gt-x64.msi'
if ($gt) {
    Write-Output "installing $gt"
    Start-Process msiexec.exe -Wait -ArgumentList '/i', $gt, '/qn', '/norestart'
} else { Write-Output 'virtio-win-gt-x64.msi not found' }

$ga = Find-Media 'guest-agent\qemu-ga-x86_64.msi'
if ($ga) {
    Write-Output "installing $ga"
    Start-Process msiexec.exe -Wait -ArgumentList '/i', $ga, '/qn', '/norestart'
} else { Write-Output 'qemu-ga-x86_64.msi not found' }

# 2. Network profile private, ping allowed.
Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private
Enable-NetFirewallRule -Name 'FPS-ICMP4-ERQ-In' -ErrorAction SilentlyContinue

# 3. WinRM over HTTP (5985) with the default Negotiate/NTLM auth and message
#    encryption -- no Basic, no AllowUnencrypted. Ansible: winrm + ntlm.
Enable-PSRemoting -Force -SkipNetworkProfileCheck
Set-Item -Path WSMan:\localhost\Service\Auth\Basic -Value $false

# 4. OpenSSH server. On Server 2022 it is a Feature on Demand fetched from
#    Windows Update, so this needs the guest to reach the internet.
$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*'
if ($cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name }
if (Get-Service sshd -ErrorAction SilentlyContinue) {
    Set-Service sshd -StartupType Automatic
    Start-Service sshd
    New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Force `
        -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' | Out-Null
    if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    }
    # Optional public key for Administrator (members of Administrators read
    # this file, not ~/.ssh/authorized_keys).
    $key = Find-Media 'authorized_keys'
    if ($key) {
        $dst = 'C:\ProgramData\ssh\administrators_authorized_keys'
        Copy-Item $key $dst -Force
        icacls.exe $dst /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
    }
} else { Write-Output 'sshd not available (OpenSSH capability did not install)' }

Write-Output 'boxman-setup: done'
Stop-Transcript
