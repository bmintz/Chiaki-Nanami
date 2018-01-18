import collections
from reprlib import recursive_repr


# For the sake of simplicity we will subclass collections.MutableMapping
# rather than dict. While subclassing dict is better for performance it
# will make the subclass more complex as there are more methods to extend.
#
# Besides, this is only used for cogs right now. If (and this is a big if)
# this gets used for commands AND performance really becomes and issue,
# then I might consider subclassing dict.
#
# See also: https://stackoverflow.com/questions/3387691/how-to-perfectly-override-a-dict

class TransformedDict(collections.MutableMapping):
    """A dictionary that applies an arbitrary key-altering
    function before accessing the keys.
    """
    __slots__ = ('store', )

    def __init__(self, *args, **kwargs):
        self.store = {}
        self.update(dict(*args, **kwargs))  # use the free update to set keys

    @recursive_repr()
    def __repr__(self):
        return f'{self.__class__.__name__}({list(self.items())})'

    def __getitem__(self, key):
        return self.store[self._transform_key(key)]

    def __setitem__(self, key, value):
        self.store[self._transform_key(key)] = value

    def __delitem__(self, key):
        del self.store[self._transform_key(key)]

    def __iter__(self):
        return iter(self.store)

    def __len__(self):
        return len(self.store)

    def copy(self):
        'Create a shallow copy of itself'
        return self.__class__(self)

    def _transform_key(self, key):
        return key


class CaseInsensitiveDict(TransformedDict):
    def _transform_key(self, k):
        return str(k).lower()


CIDict = CaseInsensitiveDict  # alias for ease of use.
