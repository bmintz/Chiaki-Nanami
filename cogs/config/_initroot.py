from core.cog import Cog

class InitRoot(Cog):
    """Class used as a "stopper" for __init__"""
    def __init__(self, bot):
        self.bot = bot