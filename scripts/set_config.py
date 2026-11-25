"""Set keys of a machine's config.yaml from the command line, at deploys (SPEC "Config"; deploy/README.md).

Each KEY=VALUE names a key of config.yaml.example by its dotted path (export.rewrites_open) and gives its value as YAML
text for 1 value, written into the file as given. Only that value's text changes: comments, order and formatting stay.
A key missing from the file is added as a new line at the end of its section, which must exist as a block mapping;
no section is ever created, and a key whose value in the file is a mapping is refused. The edited text must pass the
program's own config check (config.load, environment ignored) before it replaces the file, all keys or none. It prints
1 line per key, `set KEY`, `added KEY` or `unchanged KEY`, and never a value, old or new: the file may hold secrets.
It reads and writes only the config file and takes no lock.

Exit 2 on bad input (a malformed KEY=VALUE, a key config.yaml.example lacks, a key refused as above, a missing or
unreadable file); exit 1 when the edited text fails the config check. The file is untouched on either.

Usage, from the project root: python -m scripts.set_config [--config PATH] KEY=VALUE [KEY=VALUE ...]
"""

import argparse
import sys
from pathlib import Path

import yaml

from manga_pipeline import config


class RefusedError(Exception):
    """A KEY=VALUE this script will not apply; the message names the key, never a value."""


def parse(item: str) -> tuple[str, list[str], str]:
    """Return (key, key parts, value text) of a KEY=VALUE; RefusedError unless the value is 1 single-line YAML value."""
    key, sep, value = item.partition("=")
    if not sep:
        msg = f"{key}: must be KEY=VALUE"
        raise RefusedError(msg)
    parts = key.split(".")
    if "" in parts:
        msg = f"{key}: an empty key part"
        raise RefusedError(msg)
    if not value.strip() or "\n" in value or "\r" in value:
        msg = f"{key}: the value must be 1 non-empty line (null for a null)"
        raise RefusedError(msg)
    try:
        parsed: config.Yaml = yaml.safe_load(value)
    except yaml.YAMLError:
        msg = f"{key}: the value is not valid YAML"
        raise RefusedError(msg) from None
    if isinstance(parsed, dict):
        msg = f"{key}: the value is a mapping; only a value is set"
        raise RefusedError(msg)
    return key, parts, value


def _child(node: yaml.Node, part: str) -> yaml.Node | None:
    """Return the value node of a mapping node's key part, None when the node is no mapping or lacks the key."""
    if not isinstance(node, yaml.MappingNode):
        return None
    pairs: list[tuple[yaml.Node, yaml.Node]] = node.value
    return next((v for k, v in pairs if isinstance(k, yaml.ScalarNode) and k.value == part), None)


def _walk(root: yaml.Node, parts: list[str]) -> yaml.Node | None:
    node: yaml.Node | None = root
    for part in parts:
        if node is None:
            return None
        node = _child(node, part)
    return node


def _end(node: yaml.Node, key: str) -> int:
    """Return the text index where a value node's own text ends.

    A block collection's end mark lies at the next token, past trailing comments: its end is its last item's.
    """
    if isinstance(node, yaml.ScalarNode):
        if node.style in {"|", ">"}:
            msg = f"{key}: a block scalar in the file; edit it by hand"
            raise RefusedError(msg)
        return node.end_mark.index
    if isinstance(node, yaml.CollectionNode) and not node.flow_style:
        items: list[yaml.Node] | list[tuple[yaml.Node, yaml.Node]] = node.value
        last = items[-1]
        return _end(last[1] if isinstance(last, tuple) else last, key)
    return node.end_mark.index


def compose(text: str, where: str) -> yaml.Node:
    """Return the YAML node tree of a mapping file's text; RefusedError when it is not 1 mapping."""
    try:
        root = yaml.SafeLoader(text).get_single_node()
    except yaml.YAMLError:
        msg = f"{where}: not valid YAML"
        raise RefusedError(msg) from None
    if not isinstance(root, yaml.MappingNode):
        msg = f"{where}: not a YAML mapping"
        raise RefusedError(msg)
    return root


def edit(text: str, key: str, parts: list[str], value: str) -> tuple[str, str]:
    """Return (the text with the key set to the value text, "set" / "added" / "unchanged")."""
    root = compose(text, "the config file")
    target = _walk(root, parts)
    if isinstance(target, yaml.MappingNode):
        msg = f"{key}: a mapping in the file, not a value"
        raise RefusedError(msg)
    if target is not None:
        start, end = target.start_mark.index, _end(target, key)
        if text[start:end] == value:
            return text, "unchanged"
        return text[:start] + value + text[end:], "set"
    parent = _walk(root, parts[:-1])
    if parent is None:
        msg = f"{key}: its section {'.'.join(parts[:-1])} is missing from the file"
        raise RefusedError(msg)
    if not isinstance(parent, yaml.MappingNode) or parent.flow_style:
        msg = f"{key}: its section {'.'.join(parts[:-1])} is not a block mapping in the file"
        raise RefusedError(msg)
    pairs: list[tuple[yaml.Node, yaml.Node]] = parent.value
    indent = " " * pairs[0][0].start_mark.column
    line = f"{indent}{parts[-1]}: {value}"
    newline = text.find("\n", _end(pairs[-1][1], key))
    if newline == -1:
        return f"{text}\n{line}", "added"
    return f"{text[: newline + 1]}{line}\n{text[newline + 1 :]}", "added"


def check_keys(example: yaml.Node, items: list[tuple[str, list[str], str]]) -> None:
    """RefusedError for a key that config.yaml.example lacks or holds as a section."""
    for key, parts, _ in items:
        node = _walk(example, parts)
        if node is None or isinstance(node, yaml.MappingNode):
            msg = f"{key}: names no value key of config.yaml.example"
            raise RefusedError(msg)


def main(argv: list[str] | None = None) -> int:
    """Apply every KEY=VALUE to the config file in 1 checked, atomic write; print 1 line per key."""
    parser = argparse.ArgumentParser(description="Set config.yaml keys, keeping comments and formatting.")
    parser.add_argument("--config", type=Path, default=config.DEFAULT, help="config file (default: config.yaml)")
    parser.add_argument("items", nargs="+", metavar="KEY=VALUE", help="a dotted key and its YAML value")
    args = parser.parse_args(argv)
    path: Path = args.config
    raw: list[str] = args.items
    try:
        items = [parse(item) for item in raw]
        check_keys(compose(config.EXAMPLE.read_text(encoding="utf-8"), "config.yaml.example"), items)
        try:
            original = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            msg = f"{path}: cannot read as UTF-8 ({type(e).__name__})"
            raise RefusedError(msg) from None
        text = original
        lines: list[str] = []
        for key, parts, value in items:
            text, done = edit(text, key, parts, value)
            lines.append(f"{done} {key}")
    except RefusedError as e:
        sys.stderr.write(f"set_config: {e}; {path} untouched\n")
        return 2
    if text != original:
        part = path.with_name(f"{path.name}.part")
        part.write_bytes(text.encode("utf-8"))
        try:
            config.load(part, env={})
        except config.ConfigError as e:
            part.unlink()
            keys = ", ".join(key for key, _, _ in items)
            sys.stderr.write(f"set_config: the config check fails after setting {keys}: {e}; {path} untouched\n")
            return 1
        part.replace(path)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
