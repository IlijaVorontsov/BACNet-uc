"""Desired-state engine: the site manifest, JSON Patch, plan, apply and tests."""

from .apply import (
    ApplyProgress,
    BackupStore,
    GatewayApplier,
    MemoryBackupStore,
    TargetBackup,
    apply_plan,
    rollback_target,
)
from .diff import json_diff, text_diff, yaml_diff
from .load import Bridge, DeviceSpec, Policy, SiteManifest, Space, load_site, parse_yaml
from .nodedocs import DesiredApp, Link
from .patch import apply_patch, get_pointer
from .plan import GATEWAY, SitePlan, compute_plan, directory_resolver
from .testrun import TestTarget, compare, run_tests
from .validate import validate_site

__all__ = [
    "GATEWAY",
    "ApplyProgress",
    "BackupStore",
    "Bridge",
    "DesiredApp",
    "DeviceSpec",
    "GatewayApplier",
    "Link",
    "MemoryBackupStore",
    "Policy",
    "SiteManifest",
    "SitePlan",
    "Space",
    "TargetBackup",
    "TestTarget",
    "apply_patch",
    "apply_plan",
    "compare",
    "compute_plan",
    "directory_resolver",
    "get_pointer",
    "json_diff",
    "load_site",
    "parse_yaml",
    "rollback_target",
    "run_tests",
    "text_diff",
    "validate_site",
    "yaml_diff",
]
