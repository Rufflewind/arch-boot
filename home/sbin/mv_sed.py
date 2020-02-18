#!/usr/bin/env python3
# Rename files using a custom transformation.

import abc
import argparse
import dataclasses
import enum
import itertools
import os
import pathlib
import re
import subprocess
import sys
from typing import (
    Dict,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union
)

class Action(enum.Enum):
    NOP = enum.auto()
    RENAME = enum.auto()

class Error(Exception):
    pass

T = TypeVar("T")

def lookup_with_path_compression(
        parents: MutableMapping[T, T],
        key: T,
) -> T:
    ancestors = []
    parent = parents.get(key, key)
    while True:
        grandparent = parents.get(parent, parent)
        if grandparent == parent:
            root = grandparent
            break
        ancestors.append(key)
        key, parent = parent, grandparent
    for ancestor in ancestors:
        parents[ancestor] = root
    return root

def find_conflicting_paths(
        paths: Sequence[pathlib.PurePath],
) -> Mapping[int, int]:
    """
    Precondition: All paths must be resolved (i.e. realpaths) otherwise false
      negatives can occur.

    Returns: [(conflict_index, cause_index)]
    """
    conflict_to_cause: Dict[int, int] = {}
    path_to_index: Dict[pathlib.PurePath, int] = {}
    for i, path in enumerate(paths):
        if path in path_to_index:
            conflict_to_cause[i] = path_to_index[path]
        else:
            path_to_index[path] = i
    for i, path in enumerate(paths):
        for ancestor in path.parents:
            if ancestor in path_to_index:
                conflict_to_cause[i] = path_to_index[ancestor]
                break
    return {
        conflict: lookup_with_path_compression(conflict_to_cause, conflict)
        for conflict in frozenset(itertools.chain(
            conflict_to_cause,
            conflict_to_cause.values(),
        ))
    }

def validate_mass_rename(
        olds: Sequence[pathlib.Path],
        news: Sequence[pathlib.Path],
) -> Sequence[Tuple[Union[Action, str], pathlib.Path, pathlib.Path]]:
    reals = [new.resolve() for new in news]
    conflicts = find_conflicting_paths(reals)
    actions: List[Tuple[
        Union[Action, str],
        pathlib.Path,
        pathlib.Path,
    ]] = []
    for i, (old, new, real) in enumerate(zip(olds, news, reals)):
        real_parent = real.parent
        action: Union[Action, str]
        if i in conflicts:
            action = f"DEST_CONFLICT:{news[conflicts[i]]}"
        elif not os.path.lexists(old):
            action = "SOURCE_NOT_EXIST"
        elif old == new:
            action = Action.NOP
        elif not real_parent.exists():
            action = "DEST_PARENT_NOT_EXIST"
        elif not real_parent.is_dir():
            action = "DEST_PARENT_NOT_DIR"
        elif os.path.lexists(real):
            action = "DEST_EXISTS"
        else:
            action = Action.RENAME
        actions.append((action, old, new))
    for old in olds[len(news):]:
        actions.append(("DEST_NOT_SET", old, old))
    for new in news[len(olds):]:
        actions.append(("SOURCE_NOT_SET", new, new))
    return actions

class Transformation(abc.ABC):

    @abc.abstractmethod
    def __call__(self, arg: str, paths: Sequence[str]) -> Sequence[str]:
        pass

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        TRANSFORMATIONS[cls.__name__] = cls

TRANSFORMATIONS: Dict[str, Type[Transformation]] = {}

@dataclasses.dataclass
class Command(Transformation):
    command: Sequence[str]

    def __call__(self, arg: str, paths: Sequence[str]) -> Sequence[str]:
        cmd = [*self.command, arg]
        inp = "".join(path + "\n" for path in paths)
        proc = subprocess.run(
            cmd,
            check=True,
            input=inp,
            stdout=subprocess.PIPE,
            universal_newlines=True,
        )
        return proc.stdout.splitlines()

def parse_sed_substitutions(script: str) -> Sequence[Tuple[str, str]]:
    substitutions: List[Tuple[str, str]] = []
    while True:
        script = script.lstrip()
        if not script:
            break
        if script[0] != "s":
            raise ValueError("only 's' sed-style command is supported")
        script = script[1:]
        if not script:
            raise ValueError("missing separator after 's'")
        sep = script[0]
        script = script[1:]
        parts = script.split(sep, 2)
        if len(parts) != 3:
            raise ValueError("expected 3 separators after 's'")
        pattern, replacement, script = parts
        match = re.fullmatch(r"(?s)[\s;]*(.*)", script)
        assert match
        [script] = match.groups()
        substitutions.append((pattern, replacement))
    return substitutions

@dataclasses.dataclass
class Date(Transformation):
    def __call__(self, arg: str, paths: Sequence[str]) -> Sequence[str]:
        substitutions = parse_sed_substitutions(arg)
        new_paths = []
        for path in paths:
            for pattern, replacement in substitutions:
                path = re.sub(pattern, replacement, path)
            new_paths.append(path)
        raise NotImplementedError("TODO: Migrate mv-datetime functionality here")
        return new_paths

def mv_sed(
        dry_run: bool,
        transformation: str,
        arg: str,
        files: Sequence[str],
) -> None:
    try:
        transform = eval(transformation, TRANSFORMATIONS)
    except Exception as e:
        raise Error(e)
    if not isinstance(transform, Transformation):
        raise Error("--transform be an instance of Transformation")

    seen: Set[pathlib.Path] = set()
    unique_files: List[str] = []
    for path in files:
        real = pathlib.Path(path).resolve()
        if real not in seen:
            unique_files.append(path)
            seen.add(real)

    try:
        transformed_files = transform(arg, unique_files)
    except subprocess.CalledProcessError as e:
        raise Error(e)

    olds = list(map(pathlib.Path, unique_files))
    news = list(map(pathlib.Path, transformed_files))
    actions = validate_mass_rename(olds, news)

    table = []
    for action, old, new in actions:
        if isinstance(action, Action):
            action_str = action.name.lower()
        else:
            action_str = action
        if action == Action.RENAME and dry_run:
            action_str += "_dry_run"
        table.append((action_str, str(old), str(new)))

    msgs = []
    widths = [max(map(len, col)) for col in zip(*table)]
    for row in table:
        line = " ".join(cell.ljust(width) for width, cell in zip(widths, row))
        msgs.append(line.rstrip() + "\n")

    for (action, old, new), msg in zip(actions, msgs):
        sys.stdout.write(msg)
        sys.stdout.flush()
        if action == Action.RENAME and not dry_run:
            if os.path.lexists(new):
                raise Error(f"destination already exists: {new!r}")
            os.rename(old, new)

def main() -> Optional[str]:
    parser = argparse.ArgumentParser(
        description="Renames every file using a Transformation.")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="don't actually rename anything")
    parser.add_argument("-t", "--transformation", default='Command(["sed"])',
                        help="Transformation expression")
    parser.add_argument("arg", help="Transformation argument")
    parser.add_argument("files", nargs="+", help="filename")
    args = parser.parse_args()
    try:
        mv_sed(
            dry_run=args.dry_run,
            transformation=args.transformation,
            arg=args.arg,
            files=args.files,
        )
    except Error as e:
        return f"{parser.prog}: {e}"
    return None

if __name__ == "__main__":
    sys.exit(main())
