from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .patches import (
    PatchSet,
    apply_patch_operations,
    create_patch_set,
    list_modified_dm_files,
    read_git_file,
    write_patch_module,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dynamic-dm")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-patch", help="convert one modified DM file")
    add_common_source_args(create)
    create.add_argument("--target", required=True, help="DM file path relative to the host repo")
    create.add_argument("--out-dir", required=True, help="module output directory")
    create.add_argument("--module-id", required=True)
    create.add_argument("--module-name")
    create.set_defaults(handler=cmd_create_patch)

    migrate = subparsers.add_parser(
        "migrate-modified",
        help="convert modified DM files into a Dynamic DM patch module",
    )
    add_common_source_args(migrate)
    migrate.add_argument("--out-dir", required=True, help="module output directory")
    migrate.add_argument("--module-id", required=True)
    migrate.add_argument("--module-name")
    migrate.add_argument("--targets", help="comma-separated repo-relative DM paths")
    migrate.add_argument("--limit", type=int, help="maximum number of modified files to convert")
    migrate.set_defaults(handler=cmd_migrate_modified)

    verify = subparsers.add_parser(
        "verify-modified",
        help="try to infer patches for modified DM files without writing output",
    )
    add_common_source_args(verify)
    verify.add_argument("--targets", help="comma-separated repo-relative DM paths")
    verify.add_argument("--limit", type=int, help="maximum number of modified files to verify")
    verify.add_argument("--json", action="store_true", help="write a machine-readable report")
    verify.set_defaults(handler=cmd_verify_modified)

    args = parser.parse_args(argv)
    return args.handler(args)


def add_common_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", default=".", help="host repository root")
    parser.add_argument("--upstream-ref", required=True, help="base ref to compare against")
    parser.add_argument(
        "--no-full-file-fallback",
        action="store_true",
        help="fail instead of emitting a full-file replacement patch",
    )


def cmd_create_patch(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    target = normalize_target(args.target)
    patch_set = build_patch_set_for_target(args, repo_root, target)
    if not patch_set.changed:
        print_report([patch_set], as_json=False)
        return 1

    result = write_patch_module(
        Path(args.out_dir).resolve(),
        args.module_id,
        args.module_name or title_case_module_id(args.module_id),
        [patch_set],
        base_ref=args.upstream_ref,
    )
    print(f"WROTE {result.manifest_path}")
    for patch_file in result.patch_files:
        print(f"PATCH {patch_file}")
    print_report([patch_set], as_json=False)
    return 0


def cmd_migrate_modified(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    targets = selected_targets(args, repo_root)
    patch_sets = [build_patch_set_for_target(args, repo_root, target) for target in targets]
    converted = [patch_set for patch_set in patch_sets if patch_set.changed]
    failures = [patch_set for patch_set in patch_sets if not patch_set.changed and patch_set.warnings]

    if not converted:
        print_report(patch_sets, as_json=False)
        return 1

    result = write_patch_module(
        Path(args.out_dir).resolve(),
        args.module_id,
        args.module_name or title_case_module_id(args.module_id),
        converted,
        base_ref=args.upstream_ref,
    )
    print(f"WROTE {result.manifest_path}")
    for patch_file in result.patch_files:
        print(f"PATCH {patch_file}")
    print_report(patch_sets, as_json=False)
    return 1 if failures else 0


def cmd_verify_modified(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    targets = selected_targets(args, repo_root)
    patch_sets = [build_patch_set_for_target(args, repo_root, target) for target in targets]
    print_report(patch_sets, as_json=args.json)
    return 0 if all(patch_set.changed or not patch_set.warnings for patch_set in patch_sets) else 1


def build_patch_set_for_target(
    args: argparse.Namespace,
    repo_root: Path,
    target: str,
) -> PatchSet:
    local_path = repo_root / target
    local_source = local_path.read_text(encoding="utf-8")
    upstream_source = read_git_file(repo_root, args.upstream_ref, target)
    patch_set = create_patch_set(
        upstream_source,
        local_source,
        target,
        allow_full_file_fallback=not args.no_full_file_fallback,
    )
    if patch_set.changed:
        reproduced = apply_patch_operations(upstream_source, patch_set.operations)
        if reproduced != local_source:
            return PatchSet(
                target_file=target,
                warnings=["Generated operations did not reproduce the local file."],
            )
    return patch_set


def selected_targets(args: argparse.Namespace, repo_root: Path) -> list[str]:
    if args.targets:
        targets = [normalize_target(item) for item in args.targets.split(",") if item.strip()]
    else:
        targets = list_modified_dm_files(repo_root, args.upstream_ref)
    if args.limit is not None:
        targets = targets[: args.limit]
    return targets


def print_report(patch_sets: list[PatchSet], *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "files": [
                        {
                            "target_file": patch_set.target_file,
                            "changed": patch_set.changed,
                            "strategy": patch_set.strategy,
                            "operation_count": len(patch_set.operations),
                            "warnings": patch_set.warnings,
                            "operations": [
                                {
                                    "mode": operation.mode,
                                    "dm_path": operation.dm_path,
                                    "has_end_anchor": operation.end_anchor is not None,
                                    "occurrence": operation.occurrence,
                                    "strategy": operation.strategy,
                                }
                                for operation in patch_set.operations
                            ],
                        }
                        for patch_set in patch_sets
                    ]
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    for patch_set in patch_sets:
        if patch_set.changed:
            print(
                f"CONVERTED {patch_set.target_file}: "
                f"{len(patch_set.operations)} operation(s), {patch_set.strategy}"
            )
        elif patch_set.warnings:
            print(f"FAILED {patch_set.target_file}: {'; '.join(patch_set.warnings)}")
        else:
            print(f"UNCHANGED {patch_set.target_file}")
        for warning in patch_set.warnings:
            print(f"WARNING {patch_set.target_file}: {warning}")


def normalize_target(target: str) -> str:
    return target.strip().replace("\\", "/").lstrip("/")


def title_case_module_id(module_id: str) -> str:
    return " ".join(part.capitalize() for part in module_id.replace("_", "-").split("-") if part)


if __name__ == "__main__":
    sys.exit(main())
