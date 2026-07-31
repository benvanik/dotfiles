# Benchmark lease broker

`benchmark-lock` runs one foreground command inside an exclusive, FIFO machine
lease:

```bash
benchmark-lock --status
benchmark-lock --label gfx1100-gemm -- ./build/kernel_benchmark
benchmark-lock -- ctest --test-dir build -R gpu
```

The client waits, prints the current holder and its queue position when useful,
then replaces itself with the requested command. The command therefore keeps
the requesting PID, exit status, and signal behavior. ASLR is disabled only in
that process personality; the global kernel ASLR setting is never changed.

The root broker never launches benchmark commands. It owns only admission,
pidfds, and the fixed host policy selected by the administrator:

- a `power-profiles-daemon` performance hold;
- `power_dpm_force_performance_level=high` for the exact configured AMD PCI
  identities;
- a KFD ownership check immediately before every grant.

The policy baseline is journaled before the first mutation and restored after
the last lease. Direct FIFO handoff keeps one policy epoch across adjacent
commands. A manual power-profile selection wins rather than being overwritten.
GPU identity or restoration ambiguity fails closed and retains the recovery
journal.

This is a truthful cooperative benchmark boundary, not a global GPU
reservation. The pre-grant KFD check rejects existing ROCm compute owners, but
the broker does not evict graphics users or continuously reject expected KFD
activity created by the running benchmark.

## Lifetime and recovery

Lease ownership is the exact requesting process pidfd. Closing the client
socket after a grant does not release a live command, and a dead command cannot
retain the lease.

Queued closures are stored by systemd as a pidfd, client channel, and sealed
canonical record. A daemon crash preserves their FIFO order. Because the
process-lifetime CPU policy hold disappears with the daemon, any command that
was active across a restart is killed and never silently recertified; queued
commands resume after host-policy recovery. A clean service stop likewise
terminates the active command, releases queued requests, and restores the host
policy before returning.

There is deliberately no lease TTL. Command lifetime is the lease lifetime.

## Host contract

The broker is intentionally narrower than “Linux with an AMD GPU.” Its
production boundary requires:

- Linux on `x86_64`, `aarch64`, or `riscv64`. Those are the architectures for
  which the implementation has an audited `SO_PEERPIDFD` mapping.
- Kernel support for Unix `SOCK_SEQPACKET`, `SO_PEERCRED`, `SO_PEERPIDFD`,
  `pidfd_open`, `pidfd_send_signal`, sealed `memfd` records, process
  personalities, and pidfd metadata in `/proc/self/fdinfo`.
- systemd 254 or newer. Queue recovery depends on
  `FileDescriptorStorePreserve=restart`, named descriptor activation, service
  notifications, and `systemd-sysusers`.
- Root administration with `/usr/bin/systemctl`,
  `/usr/bin/systemd-sysusers`, and `/usr/sbin/usermod` at those exact paths.
- `/usr/bin/python3` 3.11 or newer with the `systemd.daemon` and
  `gi.repository.Gio` modules. The service runs that system interpreter in
  isolated mode; a user virtual environment is not consulted.
- `libsystemd.so.0` with `sd_notify_barrier`.
- `power-profiles-daemon` available on the system bus, exposing a non-degraded
  `performance` profile and the profile-hold API.
- The AMDGPU/KFD sysfs ABI for every configured GPU: immutable PCI identity
  fields, a readable and writable
  `power_dpm_force_performance_level`, and the KFD ownership ledger at
  `/sys/class/kfd/kfd/proc`.

ROCm userspace is not a broker dependency. The broker observes the kernel KFD
ownership ledger and fixed sysfs nodes directly, so an installation such as
`~/tools/rocm/latest` affects the benchmark command, not benchmarkd.

The current development host reports systemd 257, Linux 6.17, and
`/usr/bin/python3` 3.13 with both required modules. These are observed versions,
not tighter minimums than the capabilities above.

At admission time the selected PCI identities must still match, the KFD
ownership ledger must be empty, the performance profile must not be degraded,
and no other PPD profile hold may exist. Those conditions are checked again
for each grant; an installation succeeding does not waive them.

## One-time installation

Installation is explicit and separate from `dotfiles install` and
`install-deps.sh`. Select each benchmark GPU by its immutable sysfs identity.
For example, after choosing a PCI BDF:

```bash
gpu_path=/sys/bus/pci/devices/0000:23:00.0
for field in vendor device subsystem_vendor subsystem_device revision unique_id class; do
  printf '%s=' "$field"
  <"$gpu_path/$field" tr -d '\n'
  printf '\n'
done
```

Create a machine-local file outside this repository, mode `0600`:

```json
{"gpus":[{"bdf":"0000:23:00.0","device":"0x744c","device_class":"0x030000","revision":"0xc8","subsystem_device":"0x0000","subsystem_vendor":"0x1002","unique_id":"1","vendor":"0x1002"}],"policy_identity":"amd-performance-v1","schema":"benchmarkd.config.v1"}
```

The values above are illustrative; every value must match the selected device.
Install the first immutable generation with:

```bash
chmod 600 ~/.config/benchmarkd/config.json
sudo ~/.dotfiles/bin/benchmark-admin install \
  --config ~/.config/benchmarkd/config.json \
  --user "$USER"
```

New group membership takes effect in a new login session. Later code upgrades
preserve the installed policy and omit `--config`:

```bash
sudo ~/.dotfiles/bin/benchmark-admin install --user "$USER"
sudo ~/.dotfiles/bin/benchmark-admin doctor --user "$USER"
```

The repository has no `bin/benchmark-lock`: only the installed, root-owned
projection owns that command name. `bin/benchmark-unlock` remains solely to
recover `/tmp/benchmark-lock-*` state left by the retired snapshot-style tool;
it is not part of the process-scoped lease lifecycle.

An upgrade first fences new admissions, then stops and atomically replaces the
service. The current release is a root-owned content-addressed generation
under `/usr/local/lib/benchmarkd`; `/usr/local/bin/benchmark-lock` is a stable
projection. Configuration lives at `/etc/benchmarkd/config.json`, and the
crash-recovery epoch lives under `/var/lib/benchmarkd`.

Generation publication is itself a recoverable transaction. A complete
manifest is atomically published before the fixed staging tree becomes
visible; every file, directory, hard link, sealing step, rename, and journal
cleanup has one strictly validated resumable state. A killed administrator
therefore cannot strand an anonymous staging directory that makes both install
and uninstall inoperable.

The installed configuration is fixed after the first installation. Supplying
the same canonical configuration is harmless, but a different policy is
rejected before cutover. Changing the GPU set or policy identity needs a
fenced, epoch-aware configuration replacement with atomic restart and
rollback; `benchmark-admin` does not expose an unsafe partial version of that
transaction. Because uninstall retains configuration and epoch state, a later
reinstall continues to use that same policy.

Every install or uninstall takes an exclusive, root-owned mode-`0600` flock at
`/var/lib/benchmarkd/admin.lock`. Its mode-`0700` parent is the retained
`StateDirectory=benchmarkd`, so the exact authority inode survives service
restart, reboot, and software uninstall and remains writable inside the
otherwise read-only service filesystem. Concurrent administrator commands wait
instead of interleaving filesystem and systemd mutations. The broker observes
that same inode under a short shared flock retained through each scheduler
admission and final active publication/grant. The shared broker transition and
exclusive administrator acquisition therefore have one kernel-ordered
boundary; a cached observation cannot open a grant race. If benchmarkd restarts
while an administrator owns the exclusive lock, it becomes ready for status and
root maintenance requests but cannot admit or grant benchmark work. A
successful upgrade clears that crash-visible fence only after the administrator
releases the exclusive lock.

An upgrade or uninstall of an active installation also acquires the broker's
root-only connection fence. It is granted only while no lease or waiter
exists. That empty-scheduler boundary precedes generation hashing, publication
inventory traversal, and other substantive installed-operation work. Admission
and recovered-FIFO grant paths honor both fences. If the maintenance channel
disappears, the broker refreshes the crash-visible fence before releasing its
connection owner, so a dying administrator cannot open an admission interval.

There is no ad-hoc offline maintenance bypass. Before a mutation is committed,
an unreachable or unhealthy broker makes upgrade and uninstall fail. Restore
the current generation and service to health, allow active and queued work to
finish, and retry.

Once a prepared intent is durable, it records that broker maintenance already
observed an empty scheduler. The administrator flock and every fixed journal
name continue to fence admission after the maintenance connection disappears.
Recovery can therefore stop the socket and service synchronously and advance
to `stopped` without executing the old broker again.

Uninstall has one narrower recovery authority. While holding maintenance it
publishes a root-owned write-ahead intent before stopping the broker. If that
prepared operation is interrupted, any publication staging name or committed
intent is a permanent broker admission fence. Retry revalidates the recorded
projection and generation inventory, stops the service, and advances the exact
intent. Only a durably recorded `stopped` transition permits filesystem
removal without a broker socket.

Generation deletion uses its own manifest-hard-link journal and retired-tree
name, so interruption after any individual file or directory removal remains
verifiable and resumable. Install and doctor refuse to proceed while this
committed removal exists.

`benchmark-admin uninstall` removes verified software and units but retains the
configuration, recovery state, and `benchmark` group. It refuses to remove
foreign or modified paths.

## Baseline pressure

The policy restores what it observes before the first lease. If the machine is
already in the `performance` profile or a GPU is already forced to `high`, that
is the baseline it will faithfully restore. Put the host in its intended idle
state before relying on the broker to return it there.

`benchmark-lock --status` reports the active holder, queue depth, and policy
state. Infrastructure failures return 125; an unexecutable command returns 126;
a missing command returns 127. The benchmark command otherwise owns its exact
exit status.
