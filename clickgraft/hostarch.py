"""
clickgraft.hostarch — what kind of Mac is this, really?

Separate module because getting this wrong is worse than not checking at all.
ClickGraft's whole purpose is producing an arm64 build, so an Intel host is
worth warning about — but a warning shown to an Apple Silicon user is a bug
that makes the tool look broken to exactly the people it is for.

Target: Python 3.9+ (standard library only)
"""
import subprocess


def _sysctl(name):
    """One sysctl value, or None if the key does not exist on this machine."""
    try:
        r = subprocess.run(["/usr/sbin/sysctl", "-n", name],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def host_info():
    """Describe the machine, immune to Rosetta.

    Returns a dict:
        apple_silicon : bool  -- the HARDWARE has arm64 cores
        translated    : bool  -- this process is running under Rosetta
        reported_arch : str   -- what the process thinks it is ("x86_64"/"arm64")
        model         : str   -- e.g. "Mac16,6", "" if unreadable

    Do NOT use platform.machine() or `uname -m` for this decision. Both report
    the *process* architecture, so on an Apple Silicon Mac running under Rosetta
    they say "x86_64" — measured, not assumed:

        native : platform.machine() -> arm64   hw.optional.arm64 -> 1
        rosetta: platform.machine() -> x86_64  hw.optional.arm64 -> 1

    A tool that guessed from platform.machine() would tell an M-series owner
    they had an Intel Mac the moment anything launched it translated, which is
    a right-click away in Finder ("Open using Rosetta").

    hw.optional.arm64 asks the hardware and is 1 in both rows above. It is
    absent entirely on genuine Intel Macs, which is the signal we want.
    """
    import platform

    arm64_capable = _sysctl("hw.optional.arm64") == "1"
    translated = _sysctl("sysctl.proc_translated") == "1"

    return {
        "apple_silicon": arm64_capable,
        "translated": translated,
        "reported_arch": platform.machine(),
        "model": _sysctl("hw.model") or "",
    }


def is_apple_silicon():
    """True if this Mac has Apple Silicon hardware, however we are running."""
    return host_info()["apple_silicon"]
