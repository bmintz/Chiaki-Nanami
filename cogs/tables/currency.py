import asyncqlio
from .base import TableBase

class Currency(TableBase):
    user_id = asyncqlio.Column(asyncqlio.BigInt, primary_key=True)
    amount = asyncqlio.Column(asyncqlio.Integer)


async def get_money(session, user_id):
    query = session.select.from_(Currency).where(Currency.user_id == user_id)
    return await query.first()


async def add_money(session, user_id, amount):
    # Refund the user. We must use raw SQL because asyncqlio
    # doesn't support UPDATE SET column = expression yet.
    query = """INSERT INTO currency (user_id, amount)
                VALUES ({user_id}, {amount})
                ON CONFLICT (user_id)
                -- currency.amount is there to prevent ambiguities.
                DO UPDATE SET amount = currency.amount + {amount}
            """
    await session.execute(query, {'user_id': user_id, 'amount': amount})
