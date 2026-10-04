# Opt-in GPU access for shell

An ordinary shell action may include `"gpu":true`:

```json
{"action":"shell","command":"python workload.py","network":false,"timeout":1800,"release_worker":true,"gpu":true}
```

`gpu` must be a boolean. Omission or `false` leaves the sandbox unchanged and does not
inspect GPU devices. `gpu=true` changes only device access for this invocation;
`release_worker=true` independently releases/restores the Worker backend. Either option
can be used alone or together. Worker-selected `timeout` still uses the Core's
`--command-timeout` ceiling (default 300 seconds); requesting 1800 does not bypass it.
Network permissions, filesystem mounts, environment clearing, output admission and
process-group TERM/KILL behavior are unchanged. GPU access does not promise free VRAM
or exclusive GPU ownership, and does not inspect or manage other GPU processes.

The sandbox retains its private `/dev`. After creating it, Core adds individual
`--dev-bind` mounts for `/dev/nvidiactl`, `/dev/nvidia-uvm`, and the currently present
numbered `/dev/nvidiaN` compute nodes. Discovery is fresh for each opted-in command,
accepts only exact numbered names and requires actual character devices, rejecting
symlinks. Missing required nodes cause an ordinary shell launch failure; if Worker
release was requested, its existing finally-based restoration still applies.

No host `/dev` directory, `/dev/dri`, NVIDIA modeset/profiling nodes, `/sys`, host `/run`
or extra writable filesystem is mounted. Driver/CUDA libraries under the existing
read-only `/usr`, `/lib` and `/etc` mounts remain available without extra bindings or
inherited environment variables. The command must provide its own installed runtime
and dependencies in the usual sandbox-visible locations.

This minimal construction targets the inspected Ubuntu NVIDIA compute stack. It does
not grant MIG administration, display, profiling or container-runtime services. A stack
requiring additional devices or runtime resources must be investigated separately;
this option does not silently broaden passthrough.
