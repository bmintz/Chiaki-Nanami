"""Fun games to play! """

def _get_module_names():
    import pathlib
    path = pathlib.Path(__file__).parent
    return (__name__ + '.' + m.stem for m in path.glob('*.py') if m.stem != '__init__')


def setup(bot):
    import discord
    for module_name in _get_module_names():
        try:
            bot.load_extension(module_name)
        except discord.ClientException:
            pass
        except:
            teardown(bot)
            raise


def teardown(bot):
    for module_name in _get_module_names():
        bot.unload_extension(module_name)