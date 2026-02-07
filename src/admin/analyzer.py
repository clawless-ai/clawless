"""Static analysis and composition checks for proposed skill code.

Performs:
  1. AST safety scan — forbidden imports, builtins, dynamic execution
  2. Composition analysis — capability conflicts with active skills
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Modules that proposed skills must NOT import
FORBIDDEN_IMPORTS = frozenset({
    "os", "sys", "subprocess", "shutil", "socket", "http",
    "urllib", "requests", "httpx", "importlib", "ctypes",
    "multiprocessing", "threading", "signal", "pathlib",
    "tempfile", "glob", "fnmatch", "io", "pickle", "shelve",
    "marshal", "code", "codeop", "compileall", "py_compile",
})

# Built-in functions/names that proposed skills must NOT use
FORBIDDEN_BUILTINS = frozenset({
    "eval", "exec", "compile", "__import__", "open",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "breakpoint", "exit", "quit",
})


@dataclass
class AnalysisResult:
    """Result of analyzing proposed skill code."""

    clean: bool = True
    issues: list[str] = field(default_factory=list)
    composition_warnings: list[str] = field(default_factory=list)

    @property
    def has_critical_issues(self) -> bool:
        return not self.clean


def analyze_code_safety(code: str) -> list[str]:
    """Static analysis of generated Python code for forbidden patterns.

    Returns a list of warning strings. Empty list means no issues found.
    """
    warnings: list[str] = []

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Syntax error in generated code: {e}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_module = alias.name.split(".")[0]
                if top_module in FORBIDDEN_IMPORTS:
                    warnings.append(f"Forbidden import: '{alias.name}' (line {node.lineno})")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_module = node.module.split(".")[0]
                if top_module in FORBIDDEN_IMPORTS:
                    warnings.append(
                        f"Forbidden import from: '{node.module}' (line {node.lineno})"
                    )

        elif isinstance(node, ast.Call):
            func_name = _get_call_name(node)
            if func_name in FORBIDDEN_BUILTINS:
                warnings.append(f"Forbidden builtin call: '{func_name}' (line {node.lineno})")

        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value
            if any(pattern in val for pattern in ("__import__", "eval(", "exec(")):
                warnings.append(
                    f"Suspicious string containing code execution pattern (line {node.lineno})"
                )

    return warnings


def analyze_composition(
    proposal_capabilities: list[str],
    active_skill_capabilities: dict[str, list[str]],
) -> list[str]:
    """Check for capability conflicts between proposed and active skills.

    Returns a list of warnings about potential composition issues.
    """
    warnings: list[str] = []
    proposed_set = set(proposal_capabilities)

    for skill_name, caps in active_skill_capabilities.items():
        overlap = proposed_set & set(caps)
        if overlap:
            warnings.append(
                f"Proposed skill shares capabilities with '{skill_name}': "
                f"{', '.join(sorted(overlap))}"
            )

    # Warn about particularly sensitive capability combinations
    sensitive = {"user:input", "memory:write"}
    if sensitive.issubset(proposed_set):
        warnings.append(
            "Proposed skill has both user:input and memory:write — "
            "can write arbitrary data to memory from user input"
        )

    return warnings


def analyze_file(code_path: Path, proposal: dict, active_skills: dict) -> AnalysisResult:
    """Full analysis of a proposed skill implementation.

    Args:
        code_path: Path to the generated Python file.
        proposal: The proposal dict (from YAML).
        active_skills: Map of skill name → capability list for active skills.
    """
    result = AnalysisResult()

    # Read code
    try:
        code = code_path.read_text(encoding="utf-8")
    except Exception as e:
        result.clean = False
        result.issues.append(f"Cannot read implementation file: {e}")
        return result

    # AST safety scan
    safety_issues = analyze_code_safety(code)
    if safety_issues:
        result.clean = False
        result.issues.extend(safety_issues)

    # Composition analysis
    spec = proposal.get("proposal", {})
    capabilities = spec.get("capabilities", [])
    comp_warnings = analyze_composition(capabilities, active_skills)
    result.composition_warnings.extend(comp_warnings)

    return result


def _get_call_name(node: ast.Call) -> str:
    """Extract the function name from a Call node."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""
