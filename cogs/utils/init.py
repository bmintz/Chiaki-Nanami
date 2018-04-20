def _get_module_names(name, file):
    import pathlib
    path = pathlib.Path(file).parent
    return (name + '.' + m.stem for m in path.glob('*.py') if m.stem != '__init__')

def init(name, file):
    def setup(bot):
        import discord
        for module_name in _get_module_names(name, file):
            try:
                bot.load_extension(module_name)
            except discord.ClientException:
                pass
            except:
                teardown(bot)
                raise

    def teardown(bot):
        for module_name in _get_module_names(name, file):
            bot.unload_extension(module_name)

    return setup, teardown
