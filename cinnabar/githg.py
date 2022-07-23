from collections.abc import Sequence
from cinnabar.exceptions import (
    SilentlyAbort,
)
from cinnabar.git import (
    EMPTY_BLOB,
    Git,
    NULL_NODE_ID,
)
from cinnabar.hg.objects import (
    Changeset,
    Manifest,
)
from cinnabar.helper import GitHgHelper

import logging


# An empty mercurial file with no parent has a fixed sha1 which is that of
# "\0" * 40 (incidentally, this is the same as for an empty manifest with
# no parent.
HG_EMPTY_FILE = b'b80de5d138758541c5f05265ad144ab9fa86d1db'


class FileFindParents(object):
    logger = logging.getLogger('generated_file')

    @staticmethod
    def set_parents(file, parent1=NULL_NODE_ID, parent2=NULL_NODE_ID):
        assert file.node == NULL_NODE_ID
        # Remove null nodes
        parents = tuple(p for p in (parent1, parent2) if p != NULL_NODE_ID)

        # On merges, a file with copy metadata has either no parent, or only
        # one. In that latter case, the parent is always set as second parent.
        # On non-merges, a file with copy metadata doesn't have a parent.
        if file.metadata or file.content.startswith(b'\1\n'):
            if len(parents) == 2:
                raise Exception('Trying to create an invalid file. '
                                'Please open an issue with details.')
            elif len(parents) == 1:
                parents = (NULL_NODE_ID, parents[0])
        elif len(parents) == 2:
            if parents[0] == parents[1]:
                parents = parents[:1]

        file.parents = parents


class GitCommit(object):
    __slots__ = ('sha1', 'body', 'parents', 'tree', 'author', 'committer')

    def __init__(self, sha1):
        self.sha1 = sha1
        commit = GitHgHelper.cat_file(b'commit', sha1)
        header, self.body = commit.split(b'\n\n', 1)
        parents = []
        for line in header.splitlines():
            if line == b'\n':
                break
            typ, data = line.split(b' ', 1)
            typ = typ.decode('ascii')
            if typ == 'parent':
                parents.append(data.strip())
            elif typ in self.__slots__:
                assert not hasattr(self, typ)
                setattr(self, typ, data)
        self.parents = tuple(parents)


class BranchMap(object):
    __slots__ = "_heads", "_all_heads"

    def __init__(self, store, remote_branchmap, remote_heads):
        self._heads = {}
        self._all_heads = tuple(remote_heads)
        git_sha1s = {}
        for branch, heads in remote_branchmap.items():
            # We can't keep track of tips if the list of heads is not sequenced
            sequenced = isinstance(heads, Sequence) or len(heads) == 1
            branch_heads = []
            for head in heads:
                branch_heads.append(head)
                sha1 = store.changeset_ref(head)
                if not sha1:
                    continue
                assert head not in git_sha1s
                git_sha1s[head] = sha1
            # Use last non-closed head as tip if there's more than one head.
            # Caveat: we don't know a head is closed until we've pulled it.
            if branch and heads and sequenced:
                for head in reversed(branch_heads):
                    if head in git_sha1s:
                        changeset = store.changeset(head)
                        if changeset.close:
                            continue
                    break
            if branch:
                self._heads[branch] = tuple(branch_heads)

    def names(self):
        return self._heads.keys()

    def heads(self, branch=None):
        if branch:
            return self._heads.get(branch, ())
        return self._all_heads


class GitHgStore(object):
    def metadata(self):
        if self._metadata_sha1:
            return GitCommit(self._metadata_sha1)

    def __init__(self):
        self._closed = False
        self._graft = False

        self._metadata_sha1 = None
        broken = None
        # While doing a for_each_ref, ensure refs/notes/cinnabar is in the
        # cache.
        for sha1, ref in Git.for_each_ref('refs/cinnabar',
                                          'refs/notes/cinnabar'):
            if ref.startswith(b'refs/cinnabar/replace/'):
                # Ignore replace refs, we'll fill from the metadata tree.
                pass
            elif ref == b'refs/cinnabar/metadata':
                self._metadata_sha1 = sha1
            elif ref == b'refs/cinnabar/broken':
                broken = sha1
        self._broken = broken and self._metadata_sha1 and \
            broken == self._metadata_sha1

        metadata = self.metadata()
        self._has_metadata = bool(metadata)
        if metadata:
            # Delete old tag-cache, which may contain incomplete data.
            Git.delete_ref(b'refs/cinnabar/tag-cache')
            # Delete new-type tag_cache, we don't use it anymore.
            Git.delete_ref(b'refs/cinnabar/tag_cache')

    def prepare_graft(self):
        with GitHgHelper.query(b'graft', b'init'):
            pass
        self._graft = True

    def heads(self, branches={}):
        if not isinstance(branches, (dict, set)):
            branches = set(branches)
        return set(h for (h, b) in GitHgHelper.heads(b'changesets')
                   if not branches or b in branches)

    def read_changeset_data(self, obj):
        assert obj is not None
        obj = bytes(obj)
        data = GitHgHelper.git2hg(obj)
        if data is None:
            return None
        return data

    def hg_changeset(self, sha1):
        data = self.read_changeset_data(sha1)
        if data:
            assert data.startswith(b'changeset ')
            return data[10:50]
        return None

    def hg_manifest(self, sha1):
        git_commit = GitCommit(sha1)
        assert len(git_commit.body) == 40
        return git_commit.body

    def _hg2git(self, sha1):
        if not self._has_metadata and not GitHgHelper._helper:
            return None
        gitsha1 = GitHgHelper.hg2git(sha1)
        if gitsha1 == NULL_NODE_ID:
            gitsha1 = None
        return gitsha1

    def changeset(self, sha1):
        return self._changeset_any(sha1)

    def _changeset(self, git_commit):
        return self._changeset_any(b'git:' + git_commit)

    def _changeset_any(self, sha1):
        with GitHgHelper.query(b'raw-changeset', sha1) as stdout:
            node, parent1, parent2, size = stdout.readline().strip().split()
            size = int(size)
            raw_data = stdout.read(size)

        changeset = Changeset(node, parent1, parent2)
        changeset.raw_data = raw_data
        return changeset

    ATTR = {
        b'100644': b'',
        b'100755': b'x',
        b'120000': b'l',
    }

    @staticmethod
    def manifest_path(path):
        return path[1:].replace(b'/_', b'/')

    def manifest(self, sha1, include_parents=False):
        manifest = Manifest(sha1)
        manifest.raw_data = GitHgHelper.manifest(sha1)
        if include_parents:
            git_sha1 = self.manifest_ref(sha1)
            commit = GitCommit(git_sha1)
            parents = (self.hg_manifest(p) for p in commit.parents)
            manifest.parents = tuple(parents)
        return manifest

    def manifest_ref(self, sha1):
        return self._hg2git(sha1)

    def changeset_ref(self, sha1):
        return self._hg2git(sha1)

    def git_file_ref(self, sha1):
        # Because an empty file and an empty manifest, both with no parents,
        # have the same sha1, we can't store both in the hg2git tree. So, we
        # choose to never store the file version, and make it forcibly resolve
        # to the empty blob. Which means we won't be storing an empty blob and
        # getting a mark for it, and will attempt to use it directly even if
        # it doesn't exist. The FastImport code works around this.
        # Theoretically, it is possible to have a non-modified child of the
        # empty file, and a non-modified child of the empty manifest, which
        # both would also have the same sha1, but, TTBOMK, it is only possible
        # to achieve with commands like hg debugparents.
        if sha1 == HG_EMPTY_FILE:
            return EMPTY_BLOB
        return self._hg2git(sha1)

    def close(self, refresh=()):
        if self._closed:
            return
        self._closed = True
        # If the helper is not running, we don't have anything to update.
        if not GitHgHelper._helper:
            return

        with GitHgHelper.query(b'done-and-check', *refresh) as stdout:
            resp = stdout.readline().rstrip()
            if resp != b'ok':
                raise SilentlyAbort()
