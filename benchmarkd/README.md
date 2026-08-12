# Benchmark lease broker

`benchmark-lock` runs one foreground command inside an exclusive, FIFO machine
lease:

```bash
~/.dotfiles/bin/benchmark-lock --agents-md
~/.dotfiles/bin/benchmark-lock --status
~/.dotfiles/bin/benchmark-lock --label gfx1100-gemm -- \
  ./build/kernel_benchmark
~/.dotfiles/bin/benchmark-lock -- ctest --test-dir build -R gpu
```

The client waits, prints the current holder and its queue position when useful,
then replaces itself with the requested command. The command therefore keeps
the requesting PID, exit status, and signal behavior. ASLR is disabled only in
that process personality; the global kernel ASLR setting is never changed.

The root broker never launches benchmark commands. It owns only admission,
pidfds, and the fixed host policy selected by the administrator:

- one explicit CPU authority: a `power-profiles-daemon` performance hold when
  available, otherwise an already-fixed Linux cpufreq performance baseline;
- `power_dpm_force_performance_level=high` for the exact configured AMD PCI
  identities;
- a configured-GPU KFD ownership check immediately before every grant.

The policy baseline is journaled before the first mutation and restored after
the last lease. Direct FIFO handoff keeps one policy epoch across adjacent
commands. A manual power-profile selection wins rather than being overwritten.
GPU identity or restoration ambiguity fails closed and retains the recovery
journal.

This is a truthful cooperative benchmark boundary, not a global GPU
reservation. The pre-grant KFD check rejects queues or resident VRAM owned on
the configured GPUs. A process merely holding KFD open, or using another GPU,
does not block admission. The broker does not evict graphics users or
continuously reject expected KFD activity created by the running benchmark.

## Lifetime and recovery

Lease ownership is the exact requesting process pidfd. Closing the client
socket after a grant does not release a live command, and a dead command cannot
retain the lease.

Queued closures are stored by systemd as a pidfd, client channel, and sealed
canonical record. A daemon crash preserves their FIFO order. Any command that
was active across a restart is killed and never silently recertified; queued
commands resume after host-policy recovery. This is required both when a
process-lifetime PPD hold disappeared and when fixed kernel controls survived
without a broker left to audit them. A clean service stop likewise terminates
the active command, releases queued requests, and restores the host policy
before returning.

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
- `power-profiles-daemon` available on the system bus with the profile-hold API.
  When it exposes `performance`, that profile must be non-degraded. A host whose
  platform backend exposes no `performance` profile must instead have every
  policy under `/sys/devices/system/cpu/cpufreq` already fixed to the
  `performance` governor.
- The AMDGPU/KFD sysfs ABI for every configured GPU: immutable PCI display
  identity fields, an optional `unique_id`, a readable and writable
  `power_dpm_force_performance_level`, the PCI-to-GPU-ID topology under
  `/sys/class/kfd/kfd/topology/nodes`, and the queue and VRAM ownership ledger
  at `/sys/class/kfd/kfd/proc`.

ROCm userspace is not a broker dependency. The broker observes the kernel KFD
ownership ledger and fixed sysfs nodes directly, so an installation such as
`~/tools/rocm/latest` affects the benchmark command, not benchmarkd.

The current development host reports systemd 257, Linux 6.17, and
`/usr/bin/python3` 3.13 with both required modules. These are observed versions,
not tighter minimums than the capabilities above.

At admission time the selected PCI identities must still match, the selected
KFD GPU IDs must have no externally owned queues or resident VRAM, and no other
PPD profile hold may exist. The CPU authority is then selected once for the
lease epoch. A PPD authority is held and audited as `performance`; a fixed
cpufreq authority snapshots every policy's driver, governor, frequency limits,
optional energy preference, and the global boost control. The latter is
accepted only when every governor is already `performance`, is never mutated by
the broker, and is re-read as one exact drift boundary while the lease runs.
Those conditions are checked again for each grant; an installation succeeding
does not waive them. Work on another GPU may still contend for CPU, memory, or
interconnect resources, so a shared-host run is directional evidence rather
than whole-machine isolation.

## One-time installation

Installation is explicit and separate from `dotfiles install` and
`install-deps.sh`. Select each benchmark GPU by PCI BDF:

```bash
lspci -Dnn | grep -Ei 'vga|display|processing accelerator'
```

The first installation discovers and validates the complete immutable identity
at each selected BDF, then publishes the canonical root-owned configuration
with the broker generation:

```bash
sudo ~/.dotfiles/bin/benchmark-admin install \
  --gpu 0000:23:00.0 \
  --user "$USER"
```

Repeat `--gpu BDF` for a multi-GPU benchmark host. Discrete graphics GPUs
normally contribute a VGA-class identity and a `unique_id`; integrated GPUs
may use another PCI display subclass and omit that sysfs serial. Compute-only
Instinct GPUs use the PCI processing-accelerator class. The administrator
preserves those observed facts directly instead of asking the operator to
transcribe or invent them. An unavailable `unique_id` is recorded as `null`;
that policy constrains the identity fields the kernel exposed instead of
treating a serial added by a later kernel as a hardware replacement.

New group membership takes effect in a new login session. Later code upgrades
preserve the installed policy and omit `--gpu`:

```bash
sudo ~/.dotfiles/bin/benchmark-admin install --user "$USER"
sudo ~/.dotfiles/bin/benchmark-admin doctor --user "$USER"
```

The sole client executable is the user-owned
`~/.dotfiles/bin/benchmark-lock`. It runs directly from the dotfiles checkout;
root installation never copies, links, or executes it. `--agents-md` prints a
small project-instruction snippet for benchmark users. `bin/benchmark-unlock`
remains solely to recover `/tmp/benchmark-lock-*` state left by the retired
snapshot-style tool; it is not part of the process-scoped lease lifecycle.

An upgrade first fences new admissions, then stops and atomically replaces the
service. Root owns only the content-addressed broker generation under
`/usr/local/lib/benchmarkd`, its units and policy, and its runtime/state paths.
Configuration lives at `/etc/benchmarkd/config.json`, and the crash-recovery
epoch lives under `/var/lib/benchmarkd`.

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

The source client and administrator can be newer than the installed immutable
broker during every pull and cutover. The `benchmarkd.request.v1` and
`benchmarkd.event.v1` packet shapes are therefore a frozen rolling-upgrade ABI;
literal compatibility fixtures pin every request and event used by acquisition,
status, maintenance, activation proof, and rollback. A future wire version
cannot replace v1 in place. Its first deployment must add broker-side dual
version support while clients and administrator recovery remain on v1; only a
later source update may adopt the new version.

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

`benchmark-admin doctor` holds a shared lock on the same inode for its complete
filesystem and runtime audit. It therefore reports one coherent committed
generation rather than racing an install or uninstall cutover.

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

`benchmark-admin uninstall` removes verified broker software and units but
retains the user client, configuration, recovery state, and `benchmark` group.
It refuses to remove foreign or modified paths.

## Baseline pressure

The policy restores what it mutates after the first lease. If the machine is
already in the `performance` profile or a GPU is already forced to `high`, that
is the baseline it will faithfully restore. A fixed cpufreq authority is an
admission constraint rather than a mutation: external changes invalidate the
lease but are never overwritten during teardown. Put the host in its intended
idle state before relying on the broker to return it there.

`~/.dotfiles/bin/benchmark-lock --status` reports the active holder, queue
depth, and policy state. Infrastructure failures return 125; an unexecutable
command returns 126; a missing command returns 127. The benchmark command
otherwise owns its exact exit status.
