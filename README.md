# Dynamic DM

Dynamic DM is a core Dynamic SS13 Modules integration module for DM-side build
metadata and maintainer-side DM patch conversion. It keeps DM-aware release
behavior updateable as a module instead of hardcoded into the framework
bootstrap.

The prepare slice is intentionally conservative:

- registers itself through the generic prepare plugin API
- writes `.dynamic_modules_build/dm/index.json`
- exposes collected DM source files, unit-test files, hooks, and patches in a
  focused index for maintainer/debugging tools
- ships maintainer-side patch conversion tools that can infer Dynamic Modules
  patches from downstream DM edits

It does not force any host repo to rewrite DM source directly. Normal modules
should still prefer ordinary `.dm` files, components, signals, generated hook
points, and narrow structured patches only when a hook point does not exist.

## Module Manifest

Install this repo as a Dynamic SS13 module and include
`dynamic-dm.module.toml`. The prepare plugin runs during
`dynamic-modules prepare` and publishes the DM metadata index.

Modules that rely on Dynamic DM conversion or metadata should declare a normal
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

## Patch Conversion Workflow

Dynamic DM includes a converter for the same workflow used while developing
Dynamic TGUI:

1. read the local modified DM file
2. read the base version from an upstream/base Git ref
3. infer the narrowest Dynamic Modules patch operations it can
4. apply those operations back onto the base text
5. compare the generated result to the real local modified file

Run a dry verifier from a host checkout:

```bash
python3 -m dynamic_dm verify-modified \
  --repo-root /path/to/host \
  --upstream-ref upstream/master
```

Convert all modified DM files into a new patch module:

```bash
python3 -m dynamic_dm migrate-modified \
  --repo-root /path/to/host \
  --upstream-ref upstream/master \
  --module-id example-converted-dm \
  --module-name "Example Converted DM" \
  --out-dir /path/to/host/dynamic_modules/local/example-converted-dm
```

The converter emits ordinary Dynamic SS13 Modules `[[patches]]` entries plus
patch content files. It first parses DM definitions and tries semantic
operations around concrete paths such as `/datum/foo/proc/run` or
`/datum/foo/var/value`. The parser understands common tgstation-style forms,
including `/obj/item/foo/Initialize()` overrides, indented `proc/name()`
members, `var/name` declarations, and type variable assignments like
`name = "example"` while ignoring local proc variables. When semantic
conversion cannot reproduce the edit exactly, it falls back to line
replacements, single hunks, multiple hunks, bounded `replace_between` patches,
contextual block replacements, and finally a full-file replace only when
`--no-full-file-fallback` is not set. Full-file fallback is a maintainer escape
hatch, not the preferred output.

Machine-readable `--json` reports include `dm_path` for semantic operations.
Generated module manifests also mention the DM path in patch descriptions when
one is known.

## Local Development

Run the test suite from this repo:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile prepare_plugin.py dynamic_dm/*.py
```
