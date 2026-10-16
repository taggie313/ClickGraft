#!/usr/bin/env python3
"""Render the public version-history table from measured data.

HP publishes no changelog for HP Click: no notes field in the update feed, no
notes file on the server, and the download page lists one installer at a time.
Everything on this page was therefore read out of the builds themselves.

Two sources, deliberately separate so a reader can tell them apart:

  clickgraft/data/capabilities.json  measured by packaging/measure_capabilities.py
                                     from real bundles -- the numbers
  site/version-notes.json            written by a person -- the sentences

A number on the page was measured. A sentence was written. Nothing here invents
a number, and the generator fails rather than emit a row it cannot measure,
because a version-history page that quietly guesses is worse than none.

    python3 site/deploy/make-version-history.py > fragment.html
"""
import html
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from clickgraft import capabilities as C   # noqa: E402

NOTES = os.path.join(ROOT, "site", "version-notes.json")
PLACEHOLDER = "{{VERSION_HISTORY}}"


def _key(v):
    return [int(p) for p in v.split(".")]


def rows():
    notes = json.load(open(NOTES, encoding="utf-8"))["versions"]
    measured = {r["version"]: r for r in C.table()}
    missing = sorted(set(notes) - set(measured), key=_key)
    if missing:
        raise SystemExit(
            f"✗ version-notes.json describes {', '.join(missing)}, which "
            f"capabilities.json has not measured. Measure the bundle first:\n"
            f"    python3 packaging/measure_capabilities.py <bundles...>")
    out = []
    for v in sorted(measured, key=_key, reverse=True):
        m, n = measured[v], notes.get(v)
        out.append({
            "version": v,
            "published": (n or {}).get("published") or "",
            "note": (n or {}).get("note") or "",
            "floor": m["declared_floor"],
            "library_floor": m["library_floor"],
            "archs": m["archs"],
            "electron": m["electron"],
            "printers": m["printers"],
            "native": m["hp_native"],
        })
    return out


def render(rs):
    e = html.escape
    parts = ['<div class="tablewrap">', '<table class="versions history">',
             "<thead><tr><th>Version</th><th>Published</th><th>macOS</th>"
             "<th>Electron</th><th>Built for</th><th>Printers</th>"
             "<th>What changed</th></tr></thead>", "<tbody>"]
    for r in rs:
        archs = "Apple Silicon + Intel" if "arm64" in r["archs"] else "Intel only"
        note = e(r["note"]) if r["note"] else '<span class="none">not examined here</span>'
        parts.append(
            f'<tr><td>{e(r["version"])}</td>'
            f'<td>{e(r["published"]) or "&mdash;"}</td>'
            f'<td>{e(str(r["floor"]))}</td>'
            f'<td>{e(str(r["electron"]))}</td>'
            f'<td>{archs}</td>'
            f'<td>{r["printers"]}</td>'
            f'<td>{note}</td></tr>')
    parts += ["</tbody>", "</table>", "</div>"]
    return "\n".join(parts)


def main(argv):
    frag = render(rows())
    if len(argv) >= 2 and argv[0] == "--into":
        page = os.path.abspath(argv[1])
        with open(page, encoding="utf-8") as f:
            t = f.read()
        n = t.count(PLACEHOLDER)
        if n != 1:
            raise SystemExit(f"\u2717 {page} has {n} {PLACEHOLDER} placeholders, expected 1")
        with open(page, "w", encoding="utf-8") as f:
            f.write(t.replace(PLACEHOLDER, frag))
        print(f"    version history: {frag.count('<tr>') - 1} releases")
        return 0
    print(frag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
