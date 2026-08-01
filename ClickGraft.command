#!/bin/bash
# ClickGraft — double-clickable entry point.
# Uses Apple's /usr/bin/python3 (from Xcode Command Line Tools) rather than
# whatever `python3` resolves to on PATH: a Homebrew or pyenv python may lack
# tkinter, which would fail with an obscure ImportError instead of the wizard.
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"
exec /usr/bin/python3 -m clickgraft.cli gui
