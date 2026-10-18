#!/bin/bash
# Starts the beta backend on the Mac mini. Run by launchd
# (com.aboyeur.app.plist, next to this file); also safe to run by hand for a
# foreground start — the two then do exactly the same thing.
#
# Deliberately not `uv run`: that wrapper stays alive as a parent of the real
# server, so launchd would supervise the wrapper and `kill -9` on the uvicorn
# PID would not be what KeepAlive sees. .venv is a symlink to
# ~/.venvs/aboyeur, i.e. the same interpreter the manual command used.
set -u

REPO="/Volumes/General/Coding/Python/Aboyeur"

# Overridable so the KeepAlive behaviour can be rehearsed against a scratch
# DB_PATH and a spare port before it is pointed at the beta. Defaults are the
# beta's values.
HOST="${ABOYEUR_HOST:-0.0.0.0}"
PORT="${ABOYEUR_PORT:-8000}"

# The code lives on the SMB share, which is not mounted yet during the first
# seconds of a login. Exit instead of starting half-configured: KeepAlive
# retries after ThrottleInterval, so a reboot heals itself once the volume
# appears. 69 is EX_UNAVAILABLE, which reads clearly in the launchd log.
cd "$REPO" || exit 69
[ -x .venv/bin/python ] || exit 69

# exec, not a subshell: launchd supervises the uvicorn process itself, so the
# PID in `launchctl list` is the PID that `ps` shows and the one KeepAlive
# restarts.
exec .venv/bin/python -m uvicorn backend.main:app --host "$HOST" --port "$PORT"
