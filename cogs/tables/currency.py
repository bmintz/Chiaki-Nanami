import asyncqlio
from .base import TableBase

class Currency(TableBase):
    user_id = asyncqlio.Column(asyncqlio.BigInt, primary_key=True)
    amount = asyncqlio.Column(asyncqlio.Integer)
