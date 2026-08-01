"""
clickgraft.printerinfo — the printer facts worth putting in a bug report.

Only ever used with the user's explicit consent, and only ever the fields named
below. The user sees the exact result before it is sent.

WHY AN ALLOWLIST, NOT A DENYLIST
--------------------------------
HP Click's printers.json holds real secrets beside the useful bits:

    snmpCredentials : {snmpAuthPass, snmpPrivacyPass, snmpUsername}
    password        : the printer's password
    address         : the printer's address on the user's network
    displayName     : user-chosen, and print shops name printers after clients
    printerInfo.serialNo : identifies one physical machine

A denylist would ship every one of those the moment HP renames a key or adds a
field — and HP ships new versions of this file's schema whenever they like. An
allowlist can only ever leak something someone deliberately added to it.

If you are adding a field here, the test is not "is this useful" but "would I
be happy for this to appear in a public GitHub issue".

Target: Python 3.9+ (standard library only)
"""
import json
import os

CONFIG = "~/Library/Application Support/hpclick/printers.json"

# Printer identity and capability. Nothing here identifies a person, a company,
# a network, or one specific physical machine.
SAFE_PRINTER_INFO = (
    "productName",           # "HP DesignJet T1600 36-in PostScript"
    "productNo",             # model number
    "firmware",              # firmware version — a prime suspect in print bugs
    "minFirmware",
    "recommendedFirmware",
    "deviceRegion",
    "width",                 # carriage width in inches
)

SAFE_TOP_LEVEL = (
    "family",
    "costRecovery",
    "supportsBorderless",
    "supportsGlossEnhancer",
    "supportsYCutter",
    "margin",
)

# Deliberately excluded, recorded so nobody "helpfully" adds them back:
#   address, password, snmpCredentials, displayName, hasCustomName,
#   printerInfo.serialNo


def _media_shape(inputs):
    """Roll sizes and types, without whatever the user called their media.

    Media matters for nesting and rotation bugs, but a shop's custom media
    names are their business — "Client X proofing stock" is not ours to
    collect.
    """
    out = []
    for m in inputs or []:
        if not isinstance(m, dict):
            continue
        out.append({k: m.get(k) for k in ("type", "width", "length", "loaded")
                    if k in m})
    return out


def collect(path=None):
    """Allowlisted printer facts, or {"available": False} if there are none.

    Never raises: a bug report must still send when this cannot be read.
    """
    p = os.path.expanduser(path or CONFIG)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:                                              # noqa: BLE001
        return {"available": False, "why": "no readable HP Click printer config"}

    printers = data.get("printers") or []
    if not printers:
        return {"available": False, "why": "no printers configured"}

    out = []
    for pr in printers:
        if not isinstance(pr, dict):
            continue
        info = pr.get("printerInfo") or {}
        entry = {k: info[k] for k in SAFE_PRINTER_INFO
                 if isinstance(info, dict) and info.get(k) not in (None, "")}
        entry.update({k: pr[k] for k in SAFE_TOP_LEVEL if k in pr})
        media = _media_shape(pr.get("mediaInputs"))
        if media:
            entry["mediaInputs"] = media
        if pr.get("cutters"):
            entry["cutterCount"] = len(pr["cutters"])
        out.append(entry)

    return {"available": True, "printers": out}


def as_text(path=None):
    """The same thing, formatted for a report a human is about to read."""
    d = collect(path)
    if not d.get("available"):
        return f"(not included: {d.get('why', 'unavailable')})"
    lines = []
    for i, pr in enumerate(d["printers"], 1):
        lines.append(f"printer {i}:")
        for k, v in sorted(pr.items()):
            lines.append(f"  {k}: {json.dumps(v) if isinstance(v, (dict, list)) else v}")
    return "\n".join(lines)
