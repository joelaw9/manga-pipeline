"""Set keys of a machine's config.yaml from the command line, at deploys (SPEC "Config"; deploy/README.md).

Each KEY=VALUE names a key of config.yaml.example by its dotted path (export.rewrites_open) and gives its value as YAML
text for 1 value, written into the file as given. Only that value's text changes: comments, order and formatting stay.
A block list's text, from the key's `:` to its last item, is replaced whole, so the new value sits on the key's line
(a comment on that line stays, after the value; comments between the items go with them).
A key missing from the file is added as a new line at the end of its section, which must exist as a block mapping;
no section is ever created, and a key whose value in the file is a mapping or a block scalar is refused.

2 checks prove the edited text before it replaces the file, all keys or none. The edit check: the edited file loads as
the original does with only the given keys set to their values, so a duplicate key, an alias or a value that breaks its
surroundings is refused. The program's own check: config.load of the edited file with this process's environment, as
the program loads it; on the LXC, run it like the other hand commands, with pipeline.env loaded in a subshell, since
secrets may live only there:
    ( cd /opt/manga-pipeline && set -a && . /etc/manga-pipeline/pipeline.env && set +a &&
      .venv/bin/python -m scripts.set_config KEY=VALUE )
The edit check runs on every run, `unchanged` keys included. The file is written as <file>.part (mode 0600, then the
file's own mode and, on POSIX, its owner) and replaced; .part never outlives the run, or the error says to delete it.

It prints 1 line per key, `set KEY`, `added KEY` or `unchanged KEY`, and never a value from the file, old or new, nor
an argument lacking its `=`: the file may hold secrets. An environment variable with a malformed value is quoted by
the program's own check, as every command quotes it. It reads and writes only the config file and takes no lock.

Exit 2 on bad input (a malformed KEY=VALUE, a key config.yaml.example lacks, a key refused as above, a missing,
unreadable or read-only file); exit 1 when a check fails or the file cannot be written. The file is untouched on either.

Usage, from the project root: python -m scripts.set_config [--config PATH] KEY=VALUE [KEY=VALUE ...]
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

import yaml

from manga_pipeline import config

BLOCK_SCALAR = frozenset({"|", ">"})
LINE_BREAKS = "\r\n\x85\u2028\u2029"  # YAML's line breaks, as PyYAML reads them


class RefusedError(Exception):
    """A KEY=VALUE this script will not apply; the message names the key, never a value."""


class CheckError(Exception):
    """The edited text fails a check, or the file cannot be written; the message names no value."""


def parse(n: int, item: str) -> tuple[str, list[str], str]:
    """Return (key, key parts, value text) of the n-th KEY=VALUE; RefusedError unless the value is 1 YAML value."""
    key, sep, value = item.partition("=")
    if not sep:
        msg = f"argument {n}: must be KEY=VALUE"
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


def _pair(node: yaml.Node | None, part: str) -> tuple[yaml.Node, yaml.Node] | None:
    """Return the (key, value) nodes of a mapping node's key part, None when the node is no mapping or lacks the key."""
    if not isinstance(node, yaml.MappingNode):
        return None
    pairs: list[tuple[yaml.Node, yaml.Node]] = node.value
    return next(((k, v) for k, v in pairs if isinstance(k, yaml.ScalarNode) and k.value == part), None)


def _child(node: yaml.Node, part: str) -> yaml.Node | None:
    """Return the value node of a mapping node's key part, None when the node is no mapping or lacks the key."""
    pair = _pair(node, part)
    return None if pair is None else pair[1]


def _walk(root: yaml.Node, parts: list[str]) -> yaml.Node | None:
    node: yaml.Node | None = root
    for part in parts:
        if node is None:
            return None
        node = _child(node, part)
    return node


def _last(node: yaml.Node) -> yaml.Node:
    """Return the node whose text ends a value: itself, or a block collection's last item's.

    A block collection's end mark lies at the next token, past trailing comments.
    """
    if isinstance(node, yaml.CollectionNode) and not node.flow_style:
        items: list[yaml.Node] | list[tuple[yaml.Node, yaml.Node]] = node.value
        last = items[-1]
        return _last(last[1] if isinstance(last, tuple) else last)
    return node


def _line_end(text: str, i: int) -> int:
    """Return the index of the first YAML line break at or after i, or the text's length."""
    return next((n for n in range(i, len(text)) if text[n] in LINE_BREAKS), len(text))


def _value_indicator_end(text: str, key_node: yaml.Node) -> int:
    """Return the index right after the `:` that follows a mapping key.

    Only blanks, line breaks and comments (`? key  # a: b`) lie between a key and its `:`.
    """
    i: int = key_node.end_mark.index
    while text[i] != ":":
        i = _line_end(text, i) if text[i] == "#" else i + 1
    return i + 1


def _block_scalar(node: yaml.Node) -> bool:
    return isinstance(node, yaml.ScalarNode) and node.style in BLOCK_SCALAR


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


def _replace(text: str, key: str, pair: tuple[yaml.Node, yaml.Node], value: str) -> tuple[str, str]:
    """Return (the text with the key's value replaced by the value text, "set" / "unchanged")."""
    key_node, target = pair
    if isinstance(target, yaml.MappingNode):
        msg = f"{key}: a mapping in the file, not a value"
        raise RefusedError(msg)
    last = _last(target)
    if _block_scalar(last):
        msg = f"{key}: a block scalar in the file; edit it by hand"
        raise RefusedError(msg)
    start, end = target.start_mark.index, last.end_mark.index
    if isinstance(target, yaml.SequenceNode) and not target.flow_style:
        # its items may sit on later lines at the key's indent: the value goes right after the key's `:`
        colon = _value_indicator_end(text, key_node)
        rest = text[colon : _line_end(text, colon)]
        comment = rest if rest.lstrip().startswith("#") else ""  # a comment on the key's line stays, after the value
        return f"{text[:colon]} {value}{comment}{text[end:]}", "set"
    if start == end:  # an empty value (null): `key:` or `key:   # comment`
        return f"{text[:start]} {value}{text[end:]}", "set"
    if text[start:end] == value:
        return text, "unchanged"
    return text[:start] + value + text[end:], "set"


def edit(text: str, key: str, parts: list[str], value: str) -> tuple[str, str]:
    """Return (the text with the key set to the value text, "set" / "added" / "unchanged")."""
    root = compose(text, "the config file")
    parent = _walk(root, parts[:-1])
    pair = _pair(parent, parts[-1])
    if pair is not None:
        return _replace(text, key, pair, value)
    if parent is None:
        msg = f"{key}: its section {'.'.join(parts[:-1])} is missing from the file"
        raise RefusedError(msg)
    if not isinstance(parent, yaml.MappingNode) or parent.flow_style:
        msg = f"{key}: its section {'.'.join(parts[:-1])} is not a block mapping in the file"
        raise RefusedError(msg)
    pairs: list[tuple[yaml.Node, yaml.Node]] = parent.value
    eol = "\r\n" if "\r\n" in text else "\n"
    line = f"{' ' * pairs[0][0].start_mark.column}{parts[-1]}: {value}"
    last = _last(pairs[-1][1])
    end = last.end_mark.index
    if _block_scalar(last):  # its end mark lies past its trailing blank lines: end at its last text
        end = len(text[:end].rstrip())
    newline = text.find("\n", end)
    if newline == -1:
        return f"{text}{eol}{line}", "added"
    return f"{text[: newline + 1]}{line}{eol}{text[newline + 1 :]}", "added"


def check_keys(example: yaml.Node, items: list[tuple[str, list[str], str]]) -> None:
    """RefusedError for a key that config.yaml.example lacks or holds as a section."""
    for key, parts, _ in items:
        node = _walk(example, parts)
        if node is None or isinstance(node, yaml.MappingNode):
            msg = f"{key}: names no value key of config.yaml.example"
            raise RefusedError(msg)


def _plain(value: config.Yaml) -> config.Yaml:
    """Return a copy of a loaded value sharing no mapping or list: an alias's 2 places compare apart."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def check_edit(original: str, edited: str, items: list[tuple[str, list[str], str]]) -> None:
    """CheckError unless the edited text loads as the original with only the given keys set to their values."""
    keys = ", ".join(key for key, _, _ in items)
    msg = f"the edited file does not load as the original with only {keys} set (a duplicate key, an alias, or a value"
    msg += " that breaks its surroundings)"
    try:
        expected = _plain(yaml.safe_load(original))
        loaded: config.Yaml = yaml.safe_load(edited)
    except yaml.YAMLError:
        raise CheckError(msg) from None
    for _, parts, value in items:
        node = expected
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            raise CheckError(msg)
        node[parts[-1]] = yaml.safe_load(value)
    if loaded != expected:
        raise CheckError(msg)


def write(path: Path, text: str) -> None:
    """Replace the file with the text through <file>.part, checked by config.load first; CheckError on any failure."""
    part = path.with_name(f"{path.name}.part")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if sys.platform == "win32":
        flags |= os.O_BINARY  # no CRLF translation
    error: str | None = None
    try:
        part.unlink(missing_ok=True)
        with os.fdopen(os.open(part, flags, 0o600), "wb") as f:
            f.write(text.encode("utf-8"))
        shutil.copymode(path, part)
        if sys.platform != "win32":
            st = path.stat()
            os.chown(part, st.st_uid, st.st_gid)
        config.load(part)
        part.replace(path)
    except config.ConfigError as e:
        error = f"the config check fails: {str(e).replace(str(part), str(path))}"
    except OSError as e:
        error = f"cannot write {path}: {type(e).__name__} ({e.strerror})"
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            error = f"{error or 'cannot remove the part file'}; {part.name} is left: delete it by hand"
    if error is not None:
        raise CheckError(error)


def writable(path: Path) -> None:
    """RefusedError when the file is read-only (on Windows, os.access reads the read-only attribute)."""
    if not os.access(path, os.W_OK):
        msg = f"{path}: read-only"
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
        items = [parse(n, item) for n, item in enumerate(raw, 1)]
        check_keys(compose(config.EXAMPLE.read_text(encoding="utf-8"), "config.yaml.example"), items)
        try:
            original = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            msg = f"{path}: cannot read as UTF-8 ({type(e).__name__})"
            raise RefusedError(msg) from None
        writable(path)
        text = original
        lines: list[str] = []
        for key, parts, value in items:
            text, done = edit(text, key, parts, value)
            lines.append(f"{done} {key}")
    except RefusedError as e:
        sys.stderr.write(f"set_config: {e}; {path} untouched\n")
        return 2
    try:
        check_edit(original, text, items)  # every run: an `unchanged` 1st copy of a duplicate key is still refused
        if text != original:
            write(path, text)
    except CheckError as e:
        sys.stderr.write(f"set_config: {e}; {path} untouched\n")
        return 1
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
