# ADR-0002: AsyncSSH Transport

## Status

Accepted.

## Decision

Use AsyncSSH 2.24.x as the Python SSH transport for the local control plane.

Maintain one reusable managed connection per server for the MVP. Open a bounded number of concurrent channels on that connection. Guard total remote work with a global semaphore and each server with a smaller semaphore. Reconnect only when the connection is absent or closed.

Use the user's OpenSSH configuration and SSH agent. Keep host-key verification enabled and use the user's known-hosts file. Unknown keys require an explicit onboarding trust step; mismatches are never auto-accepted.

## Evaluation

### AsyncSSH

- native asyncio fits FastAPI lifecycle, cancellation, WebSocket delivery, and bounded scheduling;
- one connection supports multiple simultaneous channels;
- reads OpenSSH client configuration and known hosts;
- exposes command and connection timeouts;
- active maintenance and a current 2.24 release;
- cryptography is a material dependency but already required for secure SSH.

### Paramiko

- mature and widely deployed;
- good OpenSSH config parser and agent support;
- synchronous API would require a thread pool for this design;
- cancellation, shutdown, timeout composition, and bounded multi-host concurrency become more complex.

### System ssh with ControlMaster

- best fidelity with the user's complete OpenSSH environment;
- connection reuse is proven and GPUWatch demonstrates its value;
- subprocess lifecycle, portable control sockets, output cancellation, and product-level error classification are harder;
- remains a future fallback for unsupported OpenSSH features, not the default transport.

## Consequences

- AsyncSSH is the only transport dependency in the MVP.
- collectors depend on a typed executor protocol and never import AsyncSSH;
- a managed connection is reusable, timeout-bounded, backoff-aware, and closed during application shutdown;
- tests replace the executor with fakes and require no real SSH server.
