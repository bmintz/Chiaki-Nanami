import json
import discord
import logging
import os
import random
import re
import traceback
from .transformdict import IDAbleDict

DB_FILE_PATH = 'data/databases/'
TEMP_FILE_NUM_PADDING = 8

def _load_json(name):
    try:
        with open(name, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.decoder.JSONDecodeError) as e:
        return {}

def _dump_json(name, data):
    with open(name, encoding='utf-8', mode="w") as f:
        json.dump(data, f, indent=4, sort_keys=True,
            separators=(',', ' : '))
    
class Database(IDAbleDict):
    # json sucks.
    # My idea was to put the actual discord objects (such as the actual server)
    # But that's not possible with json.
    # Only other way is to use str or hash, which is just a waste of
    # perfect Python dict capabilities
    # And pickle's out of the question due to security issues.
    # json sucks.
    def __init__(self, name, mapping=()):
        self.name = name
        self._get_dict().__init__(mapping)
        self.logger = logging.getLogger("nanami_data")
    
    def dump(self, name=None):
        if name is None:
            name = self.name
        # print(name, self)
        rnd = str(random.randrange(10 ** TEMP_FILE_NUM_PADDING))
        tmp_fname = "{}-{}.tmp".format(name, rnd.zfill(TEMP_FILE_NUM_PADDING))
        _dump_json(tmp_fname, self)
        try:
            _load_json(tmp_fname)
        except json.decoder.JSONDecodeError:
            self.logger.exception("Attempted to write file {} but JSON "
                                  "integrity check on temp file has failed. "
                                  "The original file is unaltered."
                                  "".format(filename))
            return False
        os.replace(tmp_fname, name)
        return True
    
    def get_storage(self, server : discord.Server):
        return self.get(server)
        
    @classmethod
    def from_json(cls, filename):
        data = _load_json(filename)
        name = re.search(r".*/(.*)\.json", filename)
        name = name.group(1) if name else 'None'
        # print(name, data)
        return cls(filename, data)
