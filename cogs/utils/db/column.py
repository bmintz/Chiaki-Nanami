import inspect as _inspect

class SchemaError(Exception):
    pass


class Type:
    def __init_subclass__(cls, *, sql=None, real_type=True, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.real_type = real_type
        if sql is not None:
            cls.sql = sql

class Binary(Type, sql='BYTEA'): pass
class Boolean(Type, sql='BOOLEAN'): pass
class Date(Type, sql='DATE'): pass
class Double(Type, sql='REAL'): pass
class Float(Type, sql='FLOAT'): pass
class Integer(Type, sql='INTEGER'): pass
class BigInteger(Type, sql='BIGINT'): pass
BigInt = BigInteger
class SmallInteger(Type, sql='SMALLINT'): pass
SmallInt = SmallInteger
class Serial(Type, sql='SERIAL', real_type=False): pass
class BigSerial(Type, sql='BIGSERIAL', real_type=False): pass
class SmallSerial(Type, sql='SMALLSERIAL', real_type=False): pass

class Timestamp(Type):
    def __init__(self, *, timezone=False):
        self.timezone = timezone

    @property
    def sql(self):
        if self.timezone:
            return 'TIMESTAMP WITH TIMEZONE'
        return 'TIMESTAMP'

class Interval(Type):
    def __init__(self, *, field=None):
        self.field = field

    @property
    def sql(self):
        if self.field:
            return 'INTERVAL ' + self.field
        return 'INTERVAL'

class Numeric(Type):
    def __init__(self, *, precision=None, scale=0):
        if precision is not None:
            if not 0 <= precision <= 1000:
                raise SchemaError('precision must be 0 <= precision <= 1000')

        self.precision = precision
        self.scale = scale

    @property
    def sql(self):
        return 'NUMERIC'

class String(Type):
    def __init__(self, *, length=None, fixed=False):
        self.length = length
        self.fixed = fixed

        if fixed and length is None:
            raise SchemaError('Cannot have fixed string with no length')

    @property
    def sql(self):
        if self.length is None:
            return 'TEXT'
        if self.fixed:
            return f'CHAR({self.length})'
        return f'VARCHAR({self.length})'

class Text(String, sql='TEXT'):
    def __init__(self):
        super().__init__()

class JSON(Type, sql='JSON'): pass
class JSONB(Type, sql='JSONB'): pass


def _check_type(type):
    if _inspect.isclass(type):
        type = type()

    if not isinstance(type, Type):
        raise TypeError('type should be derived from Type')

    return type

class Array(Type):
    def __init__(self, type, size=None):
        self._sql_type = _check_type(type).sql

    @property
    def sql(self):
        return f'{self._sql_type}[]'


def _check_action(action, name):
    action = action.upper()
    valid_actions = ['NO ACTION', 'RESTRICT', 'CASCADE', 'SET NULL', 'SET DEFAULT']
    if action not in valid_actions:
        raise SchemaError(f'{name!r} must be one of {valid_actions}')
    return action
 
class ForeignKey(Type, real_type=False):
    def __init__(self, column, *, type=None, on_delete='CASCADE', on_update='NO ACTION'):
        if type is None:
            type = Integer

        self.name = None    # Will be set via descriptor protocol.
        self.column = column
        self.type = _check_type(type)
        self.on_delete = _check_action(on_delete, 'on_delete')
        self.on_update = _check_action(on_update, 'on_update')

    def __set_name__(self, owner, name):
        self.name = name

    @property
    def table(self):
        return self.column.table

    def create_sql(self):
        return (
            '{0.name} {0.type.sql} REFERENCES {0.table.__tablename__} ({0.column.name}) '
            'ON DELETE {0.on_delete} ON UPDATE {0.on_update}'
        ).format(self)

    @property
    def sql(self):
        return self.create_sql()


class Column:
    __slots__ = ('type', 'primary_key', 'nullable', 'default', 'unique', 'name', 'table')

    def __init__(self, type, *, primary_key=False, nullable=False, unique=False, default=None):
        if sum(map(bool, (unique, primary_key, default is not None))) > 1:
            raise ValueError('cannot specify primary_key, unique, and default at the same time')

        self.type = _check_type(type)
        self.nullable = nullable
        self.unique = unique
        self.primary_key = primary_key
        self.default = default
        self.name = None    # Will be set via descriptor protocol.
        self.table = None   # This too.

    def __set_name__(self, owner, name):
        self.name = name
        self.table = owner

    def create_sql(self):
        if self.name is None:
            raise RuntimeError('Column should be defined inside a Table subclass')

        builder = [self.name, self.type.sql]
        build = builder.append

        default = self.default
        if default is not None:
            build('DEFAULT')
            if isinstance(default, str) and isinstance(self.type, String):
                build(f"'{default}'")
            elif isinstance(default, bool):
                build(str(default).upper())
            else:
                build(f"({default})")
        elif self.unique:
            build('UNIQUE')
        elif self.primary_key:
            build('PRIMARY KEY')

        nullable_string = 'NULL'
        if not self.nullable:
            nullable_string = f'NOT NULL'
        build(nullable_string)

        return ' '.join(builder)


class Index:
    def __init__(self, *columns, unique=False):
        self.columns = columns
        self.unique = unique
        self.name = None    # Will be set via descriptor protocol.
        self.table = None   # This too.

    def __set_name__(self, owner, name):
        self.table = owner
        self.name = name

    def create_sql(self):
        builder = ['CREATE']

        if self.unique:
            builder.append('UNIQUE')

        builder.extend([
            'INDEX IF NOT EXISTS',
            self.name,
            'ON',
            self.table.__tablename__,
            f'({", ".join(c.name if isinstance(c, Column) else c for c in self.columns)});'
        ])
        return ' '.join(builder)
