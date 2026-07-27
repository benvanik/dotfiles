#!/usr/bin/env bash
set -euo pipefail

umask 077

readonly ssh_directory=/root/.ssh
readonly authorized_keys_path="$ssh_directory/authorized_keys"
readonly runtime_directory=/run/sshd
readonly sshd_configuration_path=/etc/ssh/runpod-sshd_config

fail() {
  echo "runpod-ssh-bootstrap: $*" >&2
  exit 1
}

if (($# != 0)); then
  fail "bootstrap arguments are unsupported"
fi

public_key=${SSH_PUBLIC_KEY-}
if [[ -z "$public_key" ]]; then
  fail "SSH_PUBLIC_KEY is required"
fi
if [[ "$public_key" == *$'\n'* ]] || [[ "$public_key" == *$'\r'* ]]; then
  fail "SSH_PUBLIC_KEY must contain exactly one line"
fi

printf 'runpod-ssh-bootstrap: phase=apt-start\n'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends openssh-server
unset DEBIAN_FRONTEND
printf 'runpod-ssh-bootstrap: phase=apt-complete\n'

for executable in ssh-keygen sshd; do
  command -v "$executable" >/dev/null ||
    fail "required executable is absent: $executable"
done

install -d -m 700 -- "$ssh_directory"
if [[ -e "$authorized_keys_path" ]] || [[ -L "$authorized_keys_path" ]]; then
  if [[ ! -f "$authorized_keys_path" ]] || [[ -L "$authorized_keys_path" ]]; then
    fail "authorized-keys path has an unsafe identity"
  fi
fi
install -m 600 /dev/null "$authorized_keys_path"
printf '%s\n' "$public_key" >"$authorized_keys_path"
if ! ssh-keygen -l -f "$authorized_keys_path" >/dev/null 2>&1; then
  fail "SSH_PUBLIC_KEY is not a valid OpenSSH public key"
fi
authorized_key_report=$(
  ssh-keygen -l -E sha256 -f "$authorized_keys_path"
)
authorized_key_fingerprint=${authorized_key_report#* }
authorized_key_fingerprint=${authorized_key_fingerprint%% *}
printf 'runpod-ssh-bootstrap: phase=authorized-key-ready fingerprint=%s\n' \
  "$authorized_key_fingerprint"
unset public_key SSH_PUBLIC_KEY PUBLIC_KEY
unset authorized_key_report authorized_key_fingerprint

install -d -m 755 -- "$runtime_directory"
host_key_directory=$(mktemp -d -p "$runtime_directory" ssh-host-key.XXXXXX)
readonly host_key_directory
readonly host_key_path="$host_key_directory/ssh_host_ed25519_key"
ssh-keygen -q -t ed25519 -N '' -f "$host_key_path"
chmod 600 -- "$host_key_path"
host_key_report=$(ssh-keygen -l -E sha256 -f "$host_key_path.pub")
host_key_fingerprint=${host_key_report#* }
host_key_fingerprint=${host_key_fingerprint%% *}
readonly host_key_report host_key_fingerprint
printf 'runpod-ssh-bootstrap: phase=host-key-ready fingerprint=%s\n' \
  "$host_key_fingerprint"

install -m 600 /dev/null "$sshd_configuration_path"
printf '%s\n' \
  'Port 22' \
  'AddressFamily inet' \
  'ListenAddress 0.0.0.0' \
  '' \
  "HostKey $host_key_path" \
  'AuthorizedKeysFile /root/.ssh/authorized_keys' \
  'AuthenticationMethods publickey' \
  'PubkeyAuthentication yes' \
  'PasswordAuthentication no' \
  'KbdInteractiveAuthentication no' \
  'PermitEmptyPasswords no' \
  'PermitRootLogin prohibit-password' \
  'StrictModes yes' \
  '' \
  'AllowUsers root' \
  'AllowAgentForwarding no' \
  'AllowTcpForwarding local' \
  'GatewayPorts no' \
  'PermitTunnel no' \
  'PermitUserEnvironment no' \
  'X11Forwarding no' \
  '' \
  'UsePAM no' \
  'UseDNS no' \
  'PrintMotd no' \
  'PrintLastLog yes' \
  'LoginGraceTime 30' \
  'MaxAuthTries 3' \
  'MaxSessions 16' \
  'ClientAliveInterval 60' \
  'ClientAliveCountMax 3' \
  'TCPKeepAlive yes' \
  '' \
  'PidFile /run/sshd.pid' \
  'LogLevel VERBOSE' \
  'Subsystem sftp internal-sftp' \
  >"$sshd_configuration_path"

/usr/sbin/sshd -t -f "$sshd_configuration_path"
printf 'runpod-ssh-bootstrap: phase=sshd-ready port=22\n'
exec /usr/sbin/sshd -D -e -f "$sshd_configuration_path"
