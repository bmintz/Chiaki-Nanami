import itertools

from .column import Column, ForeignKey, Index

__all__ = ['Table', 'all_tables']


class Table:
    def __init_subclass__(cls, *, table_name='', **kwargs):
        super().__init_subclass__(**kwargs)
        cls.__tablename__ = table_name or cls.__name__.lower()

        cls.columns = [v for v in cls.__dict__.values() if isinstance(v, (Column, ForeignKey))]
        cls.indexes = [v for v in cls.__dict__.values() if isinstance(v, Index)]

        cls.__create_extra__ = getattr(cls, '__create_extra__', [])

    @classmethod
    def create_sql(cls, *, exist_ok=True):
        """Return the CREATE TABLE statement for this table"""
        builder = ['CREATE TABLE']
        build = builder.append

        if exist_ok:
            build('IF NOT EXISTS')
        build(cls.__tablename__)

        column_sql = (c.create_sql() for c in cls.columns)
        column_statements = ',\n'.join(itertools.chain(column_sql, cls.__create_extra__))
        build(f'(\n{column_statements}\n);')

        statements = [' '.join(builder)]
        statements.extend(i.create_sql() for i in cls.indexes)
        return '\n'.join(statements)


def all_tables():
    return Table.__subclasses__()
