# Dynamic DM

Dynamic DM is a core Dynamic SS13 Modules integration module for DM-side build
metadata. It is the home for future DM-aware prepare behavior that should be
updateable as a module instead of hardcoded into the framework bootstrap.

The initial slice is intentionally conservative:

- registers itself through the generic prepare plugin API
- writes `.dynamic_modules_build/dm/index.json`
- exposes collected DM source files, unit-test files, hooks, and patches in a
  focused index for maintainer/debugging tools

It does not force any host repo to rewrite DM source directly. Normal modules
should still prefer ordinary `.dm` files, components, signals, generated hook
points, and narrow structured patches only when a hook point does not exist.

## Module Manifest

Install this repo as a Dynamic SS13 module and include
`dynamic-dm.module.toml`. The prepare plugin runs during
`dynamic-modules prepare` and publishes the DM metadata index.

Modules that want to rely on future Dynamic DM features should declare a normal
dependency:

```toml
[load]
requires = ["dynamic-dm"]
```

## Generated Output

```text
.dynamic_modules_build/dm/index.json
```

The file is disposable build output and should not be committed.
