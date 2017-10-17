import inspect
import re


def _get_name_mangled_attrs(cls, attr):
    for c in cls.__mro__:
        try:
            yield getattr(c, f'_{c.__name__}__{attr}')
        except AttributeError:
            continue


async def _async_all(iterable, isawaitable=inspect.isawaitable):
    for elem in iterable:
        if isawaitable(elem):
            elem = await elem

        if not elem:
            return False

    return True


class Cog:
    def __init__(self, bot):
        self.bot = bot

    def __init_subclass__(cls, *, name=None, hidden=False, aliases=(), **kwargs):
        super().__init_subclass__(**kwargs)

        cls_name = cls.__name__
        # Protect names like AFK and BAGELS. We're assuming they are acronyms
        cls.name = name or (cls_name if cls_name.isupper() else re.sub(r"(\w)([A-Z])", r"\1 \2", cls_name))
        cls.__aliases__ = aliases
        cls.__hidden__ = hidden

        # Set the local/global checks
        for attr in ('local_check', 'global_check', 'global_check_once'):
            checks = list(_get_name_mangled_attrs(cls, attr))
            if not checks:
                # Don't waste space and time processing no checks.
                continue

            if len(checks) == 1:
                # Shortcut to save a for-loop and function call for each time
                # the check is processed
                check = checks[0]
                if check.__qualname__ == f'{cls_name}.__{attr}':
                    # No need to redefine the attribute
                    continue
            else:
                # thanks python
                async def check(self, ctx, checks=checks):
                    return await _async_all(c(self, ctx) for c in checks)

            # cls.__attr uses the base class's name, so we have to use setattr
            setattr(cls, f'_{cls_name}__{attr}', check)
