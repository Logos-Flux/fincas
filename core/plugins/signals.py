"""Canonical Django signals emitted across Fincas business events.

Plugins and extensions can connect to these signals to react to core
domain events without monkey-patching models or views.

Delivery contract (``core.plugins.dispatch.emit``):

- Each signal is sent from the model ``save()`` that persists the
  transition, so service, ERP form, admin, DRF and webhook paths all emit.
  ``QuerySet.update()``, ``bulk_create()`` and raw SQL bypass ``save()`` and
  do NOT emit.
- Receivers run after the emitting transaction commits and never see a
  rolled-back change. Sender is the model class; the instance and the
  documented keyword arguments follow.
- **Treat delivery as at-least-once: receivers must be idempotent.** The same
  business fact can arrive more than once (a status that leaves and
  re-enters a state, a refund recorded on both the payment and a refund
  row, a retried request). Key side effects on the object's pk + the
  transition, never on "first time I heard of it".
- Delivery to receivers is in-process and best-effort: a crash between
  commit and dispatch loses the call. Work that must not be lost (or does
  any I/O) is a durable ``core.outbox`` consumer instead: ``emit`` writes its
  row inside the emitting transaction, and ``manage.py process_outbox``
  delivers it at least once (docs/crm/phase6-adapters.md section 1).
- A receiver that raises is logged (signal, receiver, object pk) and
  skipped; it can never fail the emitting transaction. Per-receiver
  duration is logged at DEBUG on ``golden_erp.plugins.signals``.
- Dispatch is synchronous in the request thread: keep receivers fast, and
  put anything with I/O in an outbox consumer.
"""

from django.dispatch import Signal

# --- Order Lifecycle ---
# providing_args: order
# Sent when an order is created in a live status (not incomplete, cancelled
# or declined), or leaves "incomplete" for a live status.
# Duplicated orders start "incomplete" and only announce when moved on.
# A storefront guest order is placed header-first: its totals can be 0 until
# staff build the lines from its metadata.
order_placed = Signal()

# providing_args: order, old_status, new_status
order_status_changed = Signal()

# providing_args: order, reason
# Sent alongside order_status_changed on entering "cancelled". reason is ""
# (Order has no cancellation-reason field yet).
order_cancelled = Signal()

# --- Payments ---
# providing_args: payment
# Sent when a non-refund Payment first reaches captured/settled.
payment_completed = Signal()

# providing_args: payment, refund
# Sent when a Payment enters "refunded". For a refund row (payment_type
# "refund") payment is its parent_payment (may be None) and refund the row;
# for a payment marked refunded in place, payment is the row and refund None.
payment_refunded = Signal()

# --- Fulfillment & Shipping ---
# The shipment is an orders.SubOrder (it carries carrier + tracking).
# providing_args: shipment, tracking_number
# Sent when a SubOrder first enters "shipped" from a pre-shipment status.
shipment_dispatched = Signal()

# providing_args: shipment
# Sent when a SubOrder enters "delivered".
shipment_delivered = Signal()

# --- Inventory ---
# providing_args: warehouse_stock, delta, reason
# Sent when a WarehouseStock row's on-hand quantity changes (or a row is
# created with a non-zero quantity). delta is a Decimal; reason is "".
inventory_adjusted = Signal()

# --- Customers ---
# providing_args: customer, party, account_holder, schema_version
# Sent when a Customer row is created (portal sign-up, ERP, API or import).
# v2 (CRM Phase 2, 0.3): ``party`` is the countersigning Party (the person,
# or the organisation for a business customer), ``account_holder`` the
# person who logs in (None without a login), both None if the party sync
# failed; ``schema_version`` is 2. v1 receivers took only ``customer`` and
# keep working because receivers accept ``**kwargs``.
customer_registered = Signal()

# --- Parties (CRM Phase 2, docs/crm/phase2-party.md 5.1) ---
# Party rows written by the backfill (``parties.0002_backfill``,
# ``manage.py backfill_parties``) use bulk_create / update and send none of
# these, so a cutover sends no flood.

# providing_args: party
# Sent when a Party is created through save() (ERP, API, portal, the
# customer / contact / lead sync).
party_created = Signal()

# providing_args: party, update_fields
# Sent once per Party.save() of an existing row. ``update_fields`` is the
# sorted tuple of columns written. One customer edit can save its parties
# more than once, so receivers should key on (party pk, updated_at).
# ``QuerySet.update()`` (e.g. the denormalised primary email / phone, a
# tenant rename of the self party) does not send it.
party_updated = Signal()

# providing_args: survivor, merged, merge_id
# Sent when a merge commits (CRM Phase 2 step 6 wires the sender; defined
# now so integrations can connect). ``merged`` is the list of loser Party
# rows, now pointing at ``survivor`` via ``merged_into``. Anything keyed on
# a loser's id must re-key to the survivor.
party_merged = Signal()

# providing_args: survivor, restored, merge_id
# Sent when a merge is undone (step 6). ``restored`` lists the parties
# brought back.
party_unmerged = Signal()

# providing_args: party_id, company_id
# Sent when erasure commits (step 12). Only ids: the row is scrubbed.
# ``company_id`` is the tenant, so receivers never need the scrubbed row.
# Every integration that copied personal data must delete it.
party_erased = Signal()

# --- CRM engagements, tasks, communications (CRM Phase 3, ----------------------
# docs/crm/phase3-engagement.md 5.1) ---
# Same contract as above: after commit, at-least-once, idempotent receivers.
# Sender is the model class. Backfills and ``reconcile_engagements --fix``
# write with bulk_create or inside ``core.plugins.dispatch.suppressed()`` and
# send none of these, so a cutover sends no flood.

# providing_args: engagement
# Sent when an Engagement is created through save() (crm.engagements.create:
# ERP, API, lead intake, a new quote's lazy thread). Not by the step-5/6
# backfills (bulk_create).
engagement_created = Signal()

# providing_args: engagement, from_stage, to_stage, change
# Sent when crm.engagements writes a history row whose stage differs from
# the previous one (move, change_pipeline, a reopen at another stage).
# ``change`` is the EngagementStageChange row; ``from_stage`` may be None.
engagement_stage_changed = Signal()

# providing_args: engagement, change
# Sent when status becomes ``won`` (``crm.engagements.win``, a quote's conversion).
engagement_won = Signal()

# providing_args: engagement, change, loss_reason
# Sent when status becomes ``lost``. ``loss_reason`` is a GOLDEN_LOSS_REASONS key.
engagement_lost = Signal()

# providing_args: engagement, change, previous_status
# Sent when a won or lost engagement returns to ``open`` (``crm.engagements.reopen``).
engagement_reopened = Signal()

# providing_args: task
# Sent when a crm.Task is created through save() (crm.tasks.create, intake,
# the API). The step-5/6 backfills write tasks with bulk_create and send none.
task_created = Signal()

# providing_args: task, previous_assignee
# Sent when an existing task's ``assignee`` changes (not on create: see
# task_created). ``previous_assignee`` is the old UserProfile pk, or None.
task_assigned = Signal()

# providing_args: task
# Sent when a task's status becomes ``done``.
task_completed = Signal()

# providing_args: task, update_fields, changed_fields
# Sent once per Task.save() of an existing row (not on create: see
# task_created), whatever it changed: a reschedule, a cancel, a title edit,
# a completion. ``update_fields`` is the sorted tuple of field names the save
# wrote (every concrete column for a save without update_fields), the
# party_updated pattern. ``changed_fields`` is the subset whose value
# differs from what was loaded (columns golden does not snapshot are always
# in it), so a full save of an untouched task does not look like an edit of
# every column. Receivers that sync read the new values from ``task``; a
# durable consumer pins them in its outbox row at emit (phase6-adapters.md
# 3.1). No webhook event listens to it.
task_updated = Signal()

# providing_args: task
# Sent when a task leaves ``done`` or ``cancelled`` for an open status.
task_reopened = Signal()

# providing_args: task
# Time-driven, never sent from save(): ``manage.py crm_emit_due`` sends it
# once per task crossing its due time (``due_notified_at`` is the marker,
# claimed with a conditional UPDATE; rescheduling clears it). A missed run
# sends late, never twice for one due time. Durable consumers (the task.due
# webhook) get their outbox row in the claim's transaction, so a crash after
# the claim no longer loses the event.
task_due = Signal()

# providing_args: communication
# Sent when a communications.Communication is created (``ingest.record``,
# a golden-sent document email, the activity compat endpoint, lead intake).
# A provider re-import of an existing row does not send it again.
communication_logged = Signal()
