# Synthetic QBO Export — Alias Ambiguity Report (T2-9)

Generated from `readers.COLUMN_ALIASES` + `readers.CONTEXT_ALIASES`.
Run `apps/erp/.venv/bin/python /tmp/analyze_aliases.py` to reproduce.

## Disambiguated by context (the intentional trap)

| Header spelling | Without context | `context="coa"` | `context="transactions"` |
|---|---|---|---|
| `Name` | _no field_ (not in base table) | `account_name` | `entity_name` |
| `ID` / `Account ID` / `QBO ID` / `QuickBooks ID` | `qbo_id` | `qbo_id` | _no field_ — only `Transaction ID` / `Txn ID` / `Journal ID` / `QBO Txn ID` count |
| `Transaction ID` / `Txn ID` / `Journal ID` / `QBO Txn ID` | _no field_ | _no field_ | `qbo_id` |

Rationale: in a journal export `Name` must not resolve to the account (would file
every transaction against an account named after a customer) and bare `ID` must
not resolve to the transaction id (would collapse every row touching one account
into one journal entry). Those bare spellings live only in `CONTEXT_ALIASES`.

## True collisions (same normalized header -> two logical fields)

These are QBO vocabulary overlaps. A single column header legitimately maps to
two logical fields; `Row.get(field)` will return the same cell value for both.
This is not a bug — callers must select the right field by context.

| Normalized header | Fields | Why it overlaps |
|---|---|---|
| `number` | `account_number` + `doc_number` | QBO labels both "Account Number" and "Document/Ref Number" as "Number". |
| `num` | `account_number` + `doc_number` | Shorthand for either number. |
| `no` | `account_number` + `doc_number` | Same. |
| `type` | `account_type` + `transaction_type` | "Type" is both the account classification and the transaction type. |
| `description` | `description` + `memo` | "Description" is both the account description and the line memo. |
| `memo description` | `description` + `memo` | Same — QBO uses this for both. |

Which wins? Neither — **both fields read the same cell**. There is no priority;
`Row.get("account_number")` and `Row.get("doc_number")` both scan their alias
lists and find `normalize("Number")` in the column map. If a file has a lone
"Number" column, callers that need to distinguish must use out-of-band
knowledge (file context or column position). In practice:

- COA exports: the lone "Number" is the account number. Pass `context="coa"`;
  importer reads `account_number` and ignores `doc_number`.
- Journal exports: the lone "Num"/"Number" is the document number. Pass
  `context="transactions"`; importer reads `doc_number`.

The safest mitigation is to prefer the more specific header (`Account Number`,
`Doc Number`, `Account Type`, `Transaction Type`) when QBO offers it; the bare
form is legacy and best treated as fallback.

## Non-collisions worth noting (look similar, correctly distinct)

| Pair | Normalized forms | Why they don't collide |
|---|---|---|
| `Balance Total` vs `Total` (balance) | `balance total` vs `total` | Distinct; `Amount` field is `amount` not `total`. |
| `Account` vs `Account Name` | `account` vs `account name` | `account_name` owns both; no other field aliases `account`. |
| `Amount` vs `Amount USD` | `amount` vs `amount usd` | Both map to `amount` field only. No overlap with `debit`/`credit`. |

## Preamble / header-scan edge worth fuzzing

Files with only 1 recognized header must raise `QBOFormatError` (fixture
`coa_header_only_one_match.{csv,xlsx}`) — `_MIN_HEADER_MATCHES=2` prevents a
one-cell title row from being mistaken for a header.
