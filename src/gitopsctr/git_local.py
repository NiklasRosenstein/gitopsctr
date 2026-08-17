"""In-process local Git object, ref, tree, commit, and materialization operations."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import cast

from dulwich.graph import can_fast_forward
from dulwich.index import commit_tree
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit, ObjectID, Tag, Tree
from dulwich.refs import Ref, check_ref_format
from dulwich.repo import Repo

from gitopsctr.errors import OperationError

_COMMIT_LENGTH = 40


@dataclass
class DulwichLocalRepository:
    """Semantic local-repository adapter that never invokes the Git executable."""

    root: Path
    _repository: Repo | None = field(default=None, init=False, repr=False)

    @contextmanager
    def _repo(self) -> Iterator[Repo]:
        if self._repository is None:
            try:
                self._repository = Repo(self.root)
            except Exception as exc:
                raise OperationError(f"could not open Git repository at {self.root}") from exc
        yield self._repository

    def refresh(self) -> None:
        """Discard caches after an external Git command may have changed refs or packs."""

        if self._repository is not None:
            self._repository.close()
            self._repository = None

    close = refresh

    @staticmethod
    def valid_ref(ref: str) -> bool:
        try:
            encoded = ref.encode()
        except UnicodeEncodeError:
            return False
        return check_ref_format(cast(Ref, encoded))

    @staticmethod
    def _object_id(revision: str) -> ObjectID:
        try:
            encoded = revision.encode("ascii")
        except UnicodeEncodeError as exc:
            raise OperationError("revision is not a valid commit") from exc
        if len(encoded) != _COMMIT_LENGTH or any(character not in b"0123456789abcdef" for character in encoded):
            raise OperationError("revision is not a valid commit")
        return cast(ObjectID, encoded)

    @staticmethod
    def _peel_commit(repo: Repo, object_id: ObjectID, message: str) -> Commit:
        seen: set[ObjectID] = set()
        while object_id not in seen:
            seen.add(object_id)
            try:
                value = repo.get_object(object_id)
            except Exception as exc:
                raise OperationError(message) from exc
            if isinstance(value, Commit):
                return value
            if isinstance(value, Tag):
                _object_type, object_id = value.object
                continue
            break
        raise OperationError(message)

    @staticmethod
    def _named_object_id(repo: Repo, revision: str, message: str) -> ObjectID:
        normalized_revision = revision.lower()
        if all(character in "0123456789abcdef" for character in normalized_revision):
            if len(normalized_revision) == _COMMIT_LENGTH:
                return cast(ObjectID, normalized_revision.encode())
            if 4 <= len(normalized_revision) < _COMMIT_LENGTH:
                matches = list(repo.object_store.iter_prefix(normalized_revision.encode()))
                if len(matches) == 1:
                    return matches[0]
                raise OperationError(message)
        candidates = [revision]
        if revision != "HEAD" and not revision.startswith("refs/"):
            candidates = [f"refs/heads/{revision}", f"refs/tags/{revision}", f"refs/remotes/origin/{revision}"]
        matches: list[ObjectID] = []
        for candidate in candidates:
            try:
                matches.append(repo.refs[cast(Ref, candidate.encode())])
            except (KeyError, ValueError):
                continue
        if len(set(matches)) != 1:
            raise OperationError(message)
        return matches[0]

    def resolve_commit(self, revision: str, message: str = "revision is not a valid commit") -> str:
        if not revision or revision.startswith("-") or any(character in revision for character in "\r\n\x00"):
            raise OperationError(message)
        with self._repo() as repo:
            object_id = self._named_object_id(repo, revision, message)
            return self._peel_commit(repo, object_id, message).id.decode()

    def ref_revision(self, ref: str) -> str | None:
        with self._repo() as repo:
            try:
                object_id = repo.refs[cast(Ref, ref.encode())]
            except (KeyError, ValueError):
                return None
            return self._peel_commit(repo, object_id, "local ref is invalid").id.decode()

    def has_ref(self, ref: str) -> bool:
        with self._repo() as repo:
            try:
                repo.refs[cast(Ref, ref.encode())]
            except (KeyError, ValueError):
                return False
            return True

    def remove_ref(self, ref: str) -> bool:
        with self._repo() as repo:
            encoded = cast(Ref, ref.encode())
            try:
                previous = repo.refs[encoded]
            except (KeyError, ValueError):
                return False
            return repo.refs.remove_if_equals(encoded, previous)

    def has_remote(self, name: str) -> bool:
        with self._repo() as repo:
            try:
                repo.get_config().get((b"remote", name.encode()), b"url")
            except KeyError:
                return False
            return True

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        with self._repo() as repo:
            first = self._peel_commit(
                repo, self._named_object_id(repo, ancestor, "ancestor is invalid"), "ancestor is invalid"
            )
            second = self._peel_commit(
                repo, self._named_object_id(repo, descendant, "descendant is invalid"), "descendant is invalid"
            )
            return first.id == second.id or can_fast_forward(repo, first.id, second.id)

    def parents(self, revision: str) -> tuple[str, ...]:
        with self._repo() as repo:
            commit = self._peel_commit(
                repo, self._named_object_id(repo, revision, "commit cannot be inspected"), "commit cannot be inspected"
            )
            return tuple(parent.decode() for parent in commit.parents)

    def tree_id(self, revision: str) -> str:
        with self._repo() as repo:
            commit = self._peel_commit(
                repo, self._named_object_id(repo, revision, "commit cannot be inspected"), "commit cannot be inspected"
            )
            return commit.tree.decode()

    def blob_ids(self, revision: str) -> dict[PurePosixPath, str]:
        with self._repo() as repo:
            commit = self._peel_commit(
                repo, self._named_object_id(repo, revision, "commit cannot be inspected"), "commit cannot be inspected"
            )
            return {
                PurePosixPath(os.fsdecode(entry.path)): entry.sha.decode()
                for entry in iter_tree_contents(repo.object_store, commit.tree)
            }

    def write_tree(self, directory: Path) -> str:
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        if not files:
            raise OperationError(f"tree is empty: {directory}")
        with self._repo() as repo:
            entries: list[tuple[bytes, ObjectID, int]] = []
            for path in files:
                if path.is_symlink():
                    raise OperationError(f"tree contains a symbolic link: {path}")
                relative = path.relative_to(directory).as_posix().encode()
                blob = Blob.from_string(path.read_bytes())
                repo.object_store.add_object(blob)
                entries.append((relative, blob.id, stat.S_IFREG | 0o644))
            return commit_tree(repo.object_store, entries).decode()

    def empty_tree(self) -> str:
        with self._repo() as repo:
            tree = Tree()
            repo.object_store.add_object(tree)
            return tree.id.decode()

    def create_commit(
        self,
        tree: str,
        parent: str | None,
        message: str,
        author_name: str,
        author_email: str,
        *,
        timestamp: float | None = None,
    ) -> str:
        with self._repo() as repo:
            commit = Commit()
            commit.tree = self._object_id(tree)
            commit.parents = [] if parent is None else [self._object_id(self.resolve_commit(parent))]
            identity = f"{author_name} <{author_email}>".encode()
            commit.author = identity
            commit.committer = identity
            commit.message = message.encode()
            moment = int(time.time() if timestamp is None else timestamp)
            commit.author_time = moment
            commit.commit_time = moment
            offset = time.localtime(moment).tm_gmtoff
            commit.author_timezone = offset
            commit.commit_timezone = offset
            repo.object_store.add_object(commit)
            return commit.id.decode()

    def materialize(self, revision: str, output: Path) -> None:
        if output.exists() and any(output.iterdir()):
            raise OperationError(f"output directory is not empty: {output}")
        output.mkdir(parents=True, exist_ok=True)
        with self._repo() as repo:
            commit = self._peel_commit(
                repo,
                self._named_object_id(repo, revision, "revision is not a valid commit"),
                "revision is not a valid commit",
            )
            for entry in iter_tree_contents(repo.object_store, commit.tree):
                relative = PurePosixPath(os.fsdecode(entry.path))
                if relative.is_absolute() or ".." in relative.parts:
                    raise OperationError("Git tree contains an unsafe path")
                file_type = stat.S_IFMT(entry.mode)
                if file_type == stat.S_IFLNK:
                    raise OperationError(f"Git tree contains an invalid or looping symbolic link: {relative}")
                if file_type != stat.S_IFREG:
                    raise OperationError(f"Git tree contains an unsupported entry: {relative}")
                blob = repo.get_object(entry.sha)
                if not isinstance(blob, Blob):
                    raise OperationError(f"Git tree entry is not a blob: {relative}")
                path = output.joinpath(*relative.parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(blob.data)
                path.chmod(0o755 if entry.mode & 0o111 else 0o644)
