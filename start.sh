#!/bin/bash
# Bring up a virtual X display for the shortlink resolver, then hand PID 1
# to the bot so signals and the container lifecycle behave normally.
#
# Why this cannot be "xvfb-run python3 -m bot": xvfb-run execs the child and
# becomes PID 1, which swallows the SIGTERM docker sends. The bot would then
# be killed with the container rather than shutting down cleanly, and
# in-flight downloads would not get a chance to flush.
set -e

source mltbenv/bin/activate

# jd / aria2 also read config.py through update.py; keep the existing order.
python3 update.py

if ! pgrep -x Xvfb >/dev/null 2>&1; then
    Xvfb "${DISPLAY:-:99}" -screen 0 1440x1000x24 -nolisten tcp &
    # Wait for the socket rather than sleeping blindly: a fixed sleep that is
    # too short makes chromium fail with "cannot open display" on slow boots.
    for _ in $(seq 1 50); do
        if [ -S "/tmp/.X11-unix/X${DISPLAY#:}" ]; then
            break
        fi
        sleep 0.1
    done
fi

exec python3 -m bot
