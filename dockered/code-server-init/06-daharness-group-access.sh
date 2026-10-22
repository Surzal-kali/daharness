#!/usr/bin/env bash
# daharness shared-state group access for the app lane (daharness kernelspec
# / REPL / in-process BRAIN_DISPATCH).
#
# Same lsio s6 user-drop problem as 05-docker-socket-access.sh, different
# gate: the kernel lane must traverse /run/daharness (0770 u:daharness g:120)
# to reach brain.sock, and framework_doctor's gate checks /var/lib/daharness
# (2770 setgid) group membership. compose group_add "120" covers root/exec
# lanes only — the dropped app process keeps only /etc/group memberships,
# and nothing in the image owns gid 120, so every Brain socket connect came
# back EACCES and every dispatch fell back in-process (observed 2026-10-21).
# Re-attach the HOST daharness GID — runtime-detected, so package group
# drift self-heals on every boot — to the app user here, before services
# start. Never chmod/chown the mounted state: it acts on the HOST's tree.

STATE_DIR=/var/lib/daharness
[ -d "$STATE_DIR" ] || exit 0

DAH_GID="$(stat -c '%g' "$STATE_DIR")" || exit 0
GROUP_NAME=daharness

# Reuse an existing group with that GID (previous boot or image default).
if getent group "$DAH_GID" >/dev/null 2>&1; then
    GROUP_NAME="$(getent group "$DAH_GID" | cut -d: -f1)"
elif ! getent group "$GROUP_NAME" >/dev/null 2>&1; then
    groupadd -g "$DAH_GID" "$GROUP_NAME" || exit 0
fi

APP_USER="$(getent passwd "${PUID:-1000}" 2>/dev/null | cut -d: -f1)"
if [ -n "$APP_USER" ]; then
    usermod -aG "$GROUP_NAME" "$APP_USER" 2>/dev/null || true
    echo "[code-server-init] daharness gid $DAH_GID attached to app user $APP_USER (kernel brain-socket lane)"
fi

exit 0