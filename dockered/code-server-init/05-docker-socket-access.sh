#!/usr/bin/env bash
# Docker socket access for the app lane (VS Code Docker extension).
#
# lsio's s6 user-drop discards the compose group_add supplementary GIDs: the
# live code-server process keeps only memberships found in /etc/group
# (observed: Groups: 100 1000 with group_add 967). Fresh `docker exec`
# processes inherit the socket GID and work, which masks the problem. Re-attach
# the HOST socket's GID — runtime-detected, so daemon group drift self-heals on
# every boot — to the app user here, before services start.
#
# Never chmod/chown the socket: it acts on the HOST's socket (see compose
# comment). Belt-and-braces with compose group_add (which covers root lanes
# and exec processes); this hook is what makes the dropped abc lane work.

SOCK=/var/run/docker.sock
[ -S "$SOCK" ] || exit 0

SOCKET_GID="$(stat -c '%g' "$SOCK")" || exit 0
GROUP_NAME=dockersock

# Reuse an existing group with that GID (previous boot or image default).
if getent group "$SOCKET_GID" >/dev/null 2>&1; then
    GROUP_NAME="$(getent group "$SOCKET_GID" | cut -d: -f1)"
elif ! getent group "$GROUP_NAME" >/dev/null 2>&1; then
    groupadd -g "$SOCKET_GID" "$GROUP_NAME" || exit 0
fi

APP_USER="$(getent passwd "${PUID:-1000}" 2>/dev/null | cut -d: -f1)"
if [ -n "$APP_USER" ]; then
    usermod -aG "$GROUP_NAME" "$APP_USER" 2>/dev/null || true
    echo "[code-server-init] docker socket gid $SOCKET_GID attached to app user $APP_USER (editor docker lane)"
fi

exit 0