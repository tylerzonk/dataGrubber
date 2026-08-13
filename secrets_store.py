"""Credential storage for dataGrubber.

Tries the system keyring first (works on machines with a real backend).
On headless WSL there is none, so it falls back to Windows DPAPI via
PowerShell interop: secrets are encrypted with your Windows login and
stored under %USERPROFILE%\\.dataGrubber\\, decryptable only by your
Windows account on this machine. Nothing is ever written in plaintext.
"""

import subprocess

SERVICE = "umgc-d2l"
PS = "powershell.exe"


def _keyring():
    try:
        import keyring
        from keyring.backends.fail import Keyring as Fail
        if not isinstance(keyring.get_keyring(), Fail):
            return keyring
    except ImportError:
        pass
    return None


def _ps(script, stdin=None):
    try:
        r = subprocess.run(
            [PS, "-NoProfile", "-NonInteractive", "-Command", script],
            input=stdin, capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "No keyring backend and no PowerShell (not on Windows/WSL); "
            "install a Linux keyring backend, e.g. `pip install keyrings.alt`"
        )
    if r.returncode != 0:
        raise RuntimeError(f"PowerShell failed: {r.stderr.strip()}")
    return r.stdout.rstrip("\r\n")


def set_secret(name, value):
    kr = _keyring()
    if kr:
        kr.set_password(SERVICE, name, value)
        return "keyring"
    _ps(
        "$dir = Join-Path $env:USERPROFILE '.dataGrubber';"
        "New-Item -ItemType Directory -Force -Path $dir | Out-Null;"
        "$plain = [Console]::In.ReadToEnd().TrimEnd(\"`r\",\"`n\");"
        "$ss = ConvertTo-SecureString $plain -AsPlainText -Force;"
        f"ConvertFrom-SecureString $ss | Set-Content (Join-Path $dir '{name}.dpapi')",
        stdin=value,
    )
    return "dpapi"


def get_secret(name):
    kr = _keyring()
    if kr:
        v = kr.get_password(SERVICE, name)
        if v is not None:
            return v
    return _ps(
        f"$f = Join-Path $env:USERPROFILE '.dataGrubber\\{name}.dpapi';"
        "if (-not (Test-Path $f)) { exit 1 };"
        "$ss = Get-Content $f | ConvertTo-SecureString;"
        "$b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ss);"
        "[Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)"
    )


def get_or_prompt(name, hidden=False):
    """Return the stored secret; if absent, prompt in the terminal and save it."""
    try:
        v = get_secret(name)
        if v:
            return v
    except RuntimeError:
        pass
    import getpass
    prompt = f"UMGC {name}: "
    v = getpass.getpass(prompt) if hidden else input(prompt).strip()
    if not v:
        raise SystemExit(f"No {name} given; aborting.")
    backend = set_secret(name, v)
    print(f"{name} saved to {backend} for future runs.")
    return v


def credentials():
    """(username, password), prompting and saving on first use."""
    return get_or_prompt("username"), get_or_prompt("password", hidden=True)


if __name__ == "__main__":
    import getpass
    print("Storing UMGC credentials (leave blank to keep existing).")
    user = input("Username: ").strip()
    pw = getpass.getpass("Password: ")
    if user:
        print("username stored via", set_secret("username", user))
    if pw:
        print("password stored via", set_secret("password", pw))
    print("Verify -> username:", get_secret("username"))
