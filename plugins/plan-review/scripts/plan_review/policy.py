"""Conservative tool classification while a formal plan awaits review."""

from __future__ import annotations

import re
import shlex

READ_TOOLS = {
    "Read",
    "Grep",
    "Glob",
    "read_file",
    "list_dir",
    "view_image",
    "update_plan",
    "wait_agent",
    "wait",
    "list_agents",
    "get_goal",
    "request_user_input",
    "request_user_input_async",
    "report_agent_job_result",
}


def read_only_shell(command: str) -> bool:
    if not isinstance(command, str) or not command or re.search(r"[`]|\$\(|\$\{|\n|\r", command):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token in {";", "&&", "||", "|"}:
            commands.append([])
        elif token and all(char in ";&|<>" for char in token):
            return False
        else:
            commands[-1].append(token)
    for parts in commands:
        if not parts:
            return False
        exe = parts[0].rsplit("/", 1)[-1]
        if exe in {"pwd", "ls", "cat", "head", "tail", "wc", "stat", "true"}:
            continue
        if exe in {"rg", "grep"} and not any(p.startswith("--pre") for p in parts[1:]):
            continue
        if exe == "find" and not any(
            p in {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf"}
            for p in parts[1:]
        ):
            continue
        if exe == "sed":
            operands = parts[1:]
            if operands and operands[0] in {"-n", "--quiet", "--silent"}:
                operands = operands[1:]
            if operands and operands[0] == "-e":
                operands = operands[1:]
            # Only simple print/quit/delete expressions; never e/w, -i or script files.
            if operands and re.fullmatch(r"(?:\d+(?:,\d+|,\$)?|\$)?[pqd]", operands[0]):
                if all(not value.startswith("-") for value in operands[1:]):
                    continue
            return False
        if exe == "git":
            operands = parts[1:]
            while operands:
                if operands[0] in {"--no-pager", "--literal-pathspecs"}:
                    operands = operands[1:]
                elif operands[0] == "-C" and len(operands) > 1:
                    operands = operands[2:]
                elif operands[0].startswith("-C") and len(operands[0]) > 2:
                    operands = operands[1:]
                else:
                    break
            # Config overrides can define executable aliases/helpers. Do not admit them.
            if not operands or operands[0] not in {
                "status",
                "diff",
                "show",
                "log",
                "rev-parse",
                "ls-files",
                "grep",
            }:
                return False
            if any(
                value.startswith(("--output", "--ext-diff", "--textconv")) for value in operands[1:]
            ):
                return False
            continue
        return False
    return True


def read_only_tool(name: str, args: dict) -> bool:
    if name in READ_TOOLS:
        return True
    if name in {"Bash", "exec_command", "shell_command"}:
        return read_only_shell(args.get("command", args.get("cmd", "")))
    return False
