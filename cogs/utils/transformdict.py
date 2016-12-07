import abc
from itertools import chain as _chain

class TransformedDict(dict):  
    __slots__ = ()
    
    def __init__(self, mapping=(), **kwargs):
        super().__init__(self._process_args(mapping, **kwargs))

    def __getitem__(self, k):
        return super().__getitem__(self.__keytransform__(k))
    
    def __setitem__(self, k, v):
        return super().__setitem__(self.__keytransform__(k), v)
    
    def __delitem__(self, k):
        return super().__delitem__(self.__keytransform__(k))

    def _get_dict(self):
        return super(type(self), self)
    
    def _process_args(self, mapping=(), **kwargs):
        if hasattr(mapping, "items"):
            mapping = getattr(mapping, "items")()
        print(mapping)
        return ((self.__keytransform__(k), v)
                for k, v in _chain(mapping, getattr(kwargs, "items")()))
    
    def get(self, k, default=None):
        return super().get(self.__keytransform__(k), default)
    
    def setdefault(self, k, default=None):
        return super().setdefault(self.__keytransform__(k), default)
    
    def pop(self, k):
        return super().pop(self.__keytransform__(k))
    
    def update(self, mapping=(), **kwargs):
        super().update(self._process_args(mapping, **kwargs))
        
    def __contains__(self, k):
        return super().__contains__(self.__keytransform__(k))
    
    @classmethod
    def fromkeys(cls, keys):
        return super(TransformedDict, cls).fromkeys(self.__keytransform__(k) for k in keys)
    
    def __keytransform__(self, k):
        raise NotImplementedError("__keytransform__ not implemented... for some reason")

# Best used for JSONs
# Only work around as far as I know
class StrDict(TransformedDict):
    def __keytransform__(self, k):
        return str(k)

class LowerDict(TransformedDict):
    def __keytransform__(self, k):
        return str(k).lower()

# For discord
class IDAbleDict(TransformedDict):
    def __keytransform__(self, k):
        return str(getattr(k, "id", k))

    
