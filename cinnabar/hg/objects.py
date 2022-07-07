import hashlib
from binascii import unhexlify
from collections import OrderedDict
from cinnabar.hg.changegroup import (
    ParentsTrait,
    RawRevChunk,
)
from cinnabar.git import NULL_NODE_ID
from cinnabar.util import TypedProperty

from cinnabar.bdiff import bdiff as textdiff


class HgObject(ParentsTrait):
    __slots__ = ('node', 'parent1', 'parent2', 'changeset')

    def __init__(self, node=NULL_NODE_ID, parent1=NULL_NODE_ID,
                 parent2=NULL_NODE_ID, changeset=NULL_NODE_ID):
        (self.node, self.parent1, self.parent2, self.changeset) = (
            node, parent1, parent2, changeset)

    def to_chunk(self, raw_chunk_type, delta_object=None):
        assert delta_object is None or isinstance(delta_object, type(self))
        assert issubclass(raw_chunk_type, RawRevChunk)
        raw_chunk = raw_chunk_type()
        node = self.node if self.node != NULL_NODE_ID else self.sha1
        (raw_chunk.node, raw_chunk.parent1, raw_chunk.parent2,
         raw_chunk.changeset) = (node, self.parent1, self.parent2,
                                 self.changeset)
        if delta_object:
            raw_chunk.delta_node = delta_object.node
        raw_chunk.patch = self.diff(delta_object)
        return raw_chunk

    def diff(self, delta_object):
        def flatten(s):
            return s if isinstance(s, bytes) else bytes(s)
        return textdiff(
            flatten(delta_object.raw_data) if delta_object else b'',
            flatten(self.raw_data))

    @property
    def sha1(self):
        p1 = unhexlify(self.parent1)
        p2 = unhexlify(self.parent2)
        h = hashlib.sha1(min(p1, p2) + max(p1, p2))
        h.update(self.raw_data)
        return h.hexdigest().encode('ascii')

    @property
    def raw_data(self):
        return b''.join(self._data_iter())

    @raw_data.setter
    def raw_data(self, data):
        raise NotImplementedError(
            '%s.raw_data is not implemented' % self.__class__.__name__)

    def _data_iter(self):
        raise NotImplementedError(
            '%s._data_iter is not implemented' % self.__class__.__name__)


class File(HgObject):
    __slots__ = ('content', '__weakref__')

    def __init__(self, *args, **kwargs):
        super(File, self).__init__(*args, **kwargs)
        self.content = b''
        self.metadata = {}

    @HgObject.raw_data.setter
    def raw_data(self, data):
        if data.startswith(b'\1\n'):
            _, self.metadata, self.content = data.split(b'\1\n', 2)
        else:
            self.content = data

    class Metadata(OrderedDict):
        @classmethod
        def from_str(cls, s):
            return cls(
                l.split(b': ', 1)
                for l in s.splitlines()
            )

        @classmethod
        def from_dict(cls, d):
            if isinstance(d, OrderedDict):
                return cls(d)
            return cls(sorted(d.items()))

        @classmethod
        def from_obj(cls, obj):
            if isinstance(obj, dict):
                return cls.from_dict(obj)
            return cls.from_str(obj)

        def __str__(self):
            raise RuntimeError('Use to_str()')

        def to_str(self):
            return b''.join(b'%s: %s\n' % i for i in self.items())

    metadata = TypedProperty(Metadata)

    def _data_iter(self):
        metadata = self.metadata.to_str()
        if metadata or self.content.startswith(b'\1\n'):
            metadata = b'\1\n%s\1\n' % metadata
        if metadata:
            yield metadata
        if self.content:
            yield self.content


class Changeset(HgObject):
    __slots__ = ('manifest', 'author', 'timestamp', 'utcoffset', 'body',
                 '__weakref__')

    def __init__(self, *args, **kwargs):
        super(Changeset, self).__init__(*args, **kwargs)
        self.manifest = NULL_NODE_ID
        self.author = b''
        self.timestamp = b''
        self.utcoffset = b''
        self.files = []
        self.body = b''

    @HgObject.raw_data.setter
    def raw_data(self, data):
        metadata, self.body = data.split(b'\n\n', 1)
        lines = metadata.splitlines()
        self.manifest, self.author, date = lines[:3]
        date = date.split(b' ', 2)
        self.timestamp = date[0]
        self.utcoffset = date[1]
        if len(date) == 3:
            self.extra = date[2]
        self.files = lines[3:]

    files = TypedProperty(list)

    class ExtraData(dict):
        @classmethod
        def from_str(cls, s):
            return cls(i.split(b':', 1) for i in s.split(b'\0') if i)

        @classmethod
        def from_obj(cls, obj):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return cls(obj)
            return cls.from_str(obj)

        def __str__(self):
            raise RuntimeError('Use to_str()')

        def to_str(self):
            return b'\0'.join(b':'.join(i) for i in sorted(self.items()))

    extra = TypedProperty(ExtraData)

    def _data_iter(self):
        yield self.manifest
        yield b'\n'
        yield self.author
        yield b'\n'
        yield self.timestamp
        yield b' '
        yield self.utcoffset
        if self.extra is not None:
            yield b' '
            yield self.extra.to_str()
        if self.files:
            yield b'\n'
            yield b'\n'.join(sorted(self.files))
        yield b'\n\n'
        yield self.body

    @property
    def changeset(self):
        return self.node

    @changeset.setter
    def changeset(self, value):
        assert value in (self.node, NULL_NODE_ID)

    class ExtraProperty(object):
        def __init__(self, name):
            self._name = name.encode('ascii')

        def __get__(self, obj, type=None):
            if obj.extra is None:
                return None
            return obj.extra.get(self._name)

        def __set__(self, obj, value):
            if not value:
                if obj.extra:
                    try:
                        del obj.extra[self._name]
                    except KeyError:
                        pass
                if not obj.extra:
                    obj.extra = None
            else:
                if obj.extra is None:
                    obj.extra = {}
                obj.extra[self._name] = value

    branch = ExtraProperty('branch')
    committer = ExtraProperty('committer')
    close = ExtraProperty('close')


class Manifest(HgObject):
    __slots__ = ('__weakref__', '_raw_data')

    def __init__(self, *args, **kwargs):
        super(Manifest, self).__init__(*args, **kwargs)
        self._items = []
        self._raw_data = None

    class ManifestItem(bytes):
        @classmethod
        def from_info(cls, path, sha1=None, attr=b''):
            if isinstance(path, cls):
                return path
            return cls(b'%s\0%s%s' % (path, sha1, attr))

        @property
        def path(self):
            attr_len = len(self.attr)
            assert self[-41 - attr_len:-40 - attr_len] == b'\0'
            return self[:-41 - attr_len]

        @property
        def attr(self):
            if self[-1] in b'lx':
                return self[-1:]
            return b''

        @property
        def sha1(self):
            attr_len = len(self.attr)
            if attr_len:
                return self[-40 - attr_len:-attr_len]
            return self[-40 - attr_len:]

    class ManifestList(list):
        def __init__(self, *args, **kwargs):
            super(Manifest.ManifestList, self).__init__(*args, **kwargs)
            self._last = None

        def append(self, value):
            assert isinstance(value, Manifest.ManifestItem)
            assert self._last is None or value > self._last
            super(Manifest.ManifestList, self).append(value)
            self._last = value

    _items = TypedProperty(ManifestList)

    @property
    def items(self):
        if self._raw_data is not None:
            self._items[:] = []
            for line in self._raw_data.splitlines():
                item = self.ManifestItem(line)
                self._items.append(item)
            self._raw_data = None
        return self._items

    def add(self, path, sha1=None, attr=b''):
        item = Manifest.ManifestItem.from_info(path, sha1, attr)
        self.items.append(item)

    def __iter__(self):
        return iter(self.items)

    def _data_iter(self):
        for item in self:
            yield item
            yield b'\n'

    @property
    def raw_data(self):
        if self._raw_data is not None:
            return self._raw_data
        return super(Manifest, self).raw_data

    @raw_data.setter
    def raw_data(self, data):
        self._raw_data = bytes(data)
