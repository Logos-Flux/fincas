"""Custom ORM lookups.

``ne`` (``field__ne=value``) compiles to ``<>``. Django has no inequality
lookup, and ``~Q(field=value)`` compiles to ``NOT (field = value)``, which
Postgres stores as ``WHERE NOT field = value``. Partial indexes whose stored
predicate must read ``field <> value`` (for example to match an index created
by hand-written SQL elsewhere, byte for byte) need this lookup.

Beware: ``<>`` is SQL inequality, so ``field__ne=value`` never matches rows
where ``field`` IS NULL (unlike ``~Q(field=value)``, which Django rewrites to
include them). Use it on NOT NULL columns, or add an explicit ``isnull`` arm.

Registered on every field class at import; ``orders.models`` imports this
module before using it in a constraint condition.
"""

from django.db.models import Field, Lookup


@Field.register_lookup
class NotEqual(Lookup):
    lookup_name = "ne"

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return f"{lhs} <> {rhs}", (*lhs_params, *rhs_params)
