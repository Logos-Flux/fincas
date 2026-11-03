"""Connected accounts: per-company provider accounts, encrypted credentials,
the inbound webhook endpoint, OAuth grants, polling and rate limits
(docs/crm/phase6-adapters.md 2.3-2.9; the adapter contract is
``core.plugins.crm_adapter``; operator notes in docs/crm/adapters.md).

Modules: ``models`` (``IntegrationAccount``, ``IntegrationCredential``),
``accounts`` (the write services), ``transport`` (``Transport``,
``HttpxTransport``), ``ratelimit`` (token bucket, pauses), ``webhook`` (the
public endpoint), ``oauth`` (connect / callback / disconnect), ``poll``
(``integrations_poll``), ``views`` (the Connected accounts section of
/erp/integrations/), ``checks`` (``core.E040`` / ``E041``).
"""
