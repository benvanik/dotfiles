# Distributed Compilation with sccache-dist

## Status: Deferred

Local ccache setup should be completed first. Revisit this after baseline is working.

## Overview

sccache-dist provides icecream-style distributed compilation with:
- Automatic toolchain packaging
- Authentication and TLS
- Sandboxed compiler execution (bubblewrap)

## Architecture

```
Linux Cluster:
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Workstation 1│  │ Workstation 2│  │ Workstation 3│
│  (client)    │  │  (client)    │  │  (client)    │
│  + server    │  │  + server    │  │  + server    │
└──────────────┘  └──────────────┘  └──────────────┘
        │                │                │
        └────────────────┼────────────────┘
                         │
                ┌────────┴────────┐
                │   Scheduler     │
                │ (on any machine)│
                └─────────────────┘

Mac Cluster (separate - ARM64 incompatible with Linux x86_64):
┌──────────────┐            ┌──────────────┐
│   MacBook    │            │   Mac Mini   │
│  (client)    │───────────▶│  (server)    │
└──────────────┘            └──────────────┘
```

## Key Constraints

- **Build servers must be Linux** (macOS clients supported)
- **Same architecture required** for native builds (can't mix ARM64/x86_64)
- **Mac cluster must be separate** from Linux cluster

## Setup Requirements

1. Install sccache with `dist-server` feature:
   ```bash
   cargo install sccache --features="dist-client dist-server"
   ```

2. Install bubblewrap >= 0.3.0 on all servers:
   ```bash
   sudo apt install bubblewrap
   ```

3. Configure scheduler (`scheduler.conf`)
4. Configure build servers (`server.conf`)
5. Configure clients (`~/.config/sccache/config`)

## Storage Backend Options

For shared cache (in addition to distributed compilation):

| Backend | Latency | Notes |
|---------|---------|-------|
| Redis on LAN | ~1-5ms | Good for shared cache across machines |
| Local disk | ~0.1ms | Best for cache hits |
| S3/Azure | ~50-200ms | For cloud CI |

**Recommendation**: Local ccache for hot path + sccache-dist for cold builds.

## Resources

- [sccache-dist quickstart](https://github.com/mozilla/sccache/blob/main/docs/DistributedQuickstart.md)
- [Firefox sccache-dist docs](https://firefox-source-docs.mozilla.org/build/buildsystem/sccache-dist.html)
- [2025 experience report](https://brokenco.de/2025/01/05/sccache-distributed-compilation.html)
- [sccache Redis setup](https://github.com/mozilla/sccache/blob/main/docs/Redis.md)

## Benchmarking Plan

Before investing in sccache-dist setup, benchmark:
1. Cold LLVM build time (no cache)
2. Warm ccache build time (cache hit)
3. Estimate: how often are cold builds the bottleneck?

If cache hits are >90%, local ccache may be sufficient.
