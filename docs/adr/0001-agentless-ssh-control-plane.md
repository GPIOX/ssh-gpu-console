# ADR-0001: Local Agentless SSH Control Plane

## Status

Accepted.

## Context

The product should manage multiple Linux/GPU servers from the user's own computer while minimizing remote overhead and avoiding unnecessary writes or resident services on managed servers.

## Decision

The default architecture uses a local FastAPI control plane and browser frontend.

Remote servers are managed over reusable SSH connections.

No custom remote monitoring daemon is required for the default mode.

Telemetry is normalized and stored only in bounded local memory.

Persistent storage is reserved for configuration.

## Consequences

Positive:

- minimal remote installation burden;
- no custom remote daemon lifecycle;
- no default remote telemetry writes;
- easy reuse of existing SSH configuration;
- centralized UI and policy.

Tradeoffs:

- remote metrics are limited by SSH-accessible tools;
- very high-frequency telemetry is intentionally avoided;
- some advanced capabilities may require optional future agents;
- SSH failure modes become a core part of the product.

This tradeoff is intentional and aligned with the lightweight product goal.
