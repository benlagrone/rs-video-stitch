#!/bin/sh
set -eu

expected_host="fortress-sextant"
if [ "$(hostname -s)" != "$expected_host" ]; then
  printf 'Refusing MediaStudio relay outside %s.\n' "$expected_host" >&2
  exit 65
fi

lima_home="$HOME/.colima/_lima"
ssh_config="$lima_home/colima/ssh.config"
if [ ! -f "$ssh_config" ]; then
  printf 'Colima SSH config not found: %s\n' "$ssh_config" >&2
  exit 66
fi

exec /usr/bin/ssh \
  -F "$ssh_config" \
  -g \
  -o ControlMaster=no \
  -o ControlPath=none \
  -o ControlPersist=no \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -N \
  -L 0.0.0.0:8082:127.0.0.1:8082 \
  lima-colima
