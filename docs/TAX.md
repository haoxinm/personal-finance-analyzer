# Income and tax analysis

Spend Analyzer can read your W-2, 1099 and Form 1040 PDFs and show what you earned, what was
withheld, what you owed or got back, and your effective tax rate, year by year. It is a second,
separate import path next to bank and card statements: tax forms are parsed locally by
deterministic code and stay on your machine. An optional, off-by-default LLM fallback for the
parts the parsers cannot read is described under [LLM fallback](#llm-fallback-optional).

This page covers what is read from each form, how income and tax are aggregated, how the
federal tax estimate works (and where it is only approximate), and what to do when an import
doesn't go as expected. For the commands see [CLI.md](CLI.md#tax); for the privacy guarantees see
the [README's privacy model](../README.md#privacy-model).

## Getting forms in

```console
$ spend-analyzer tax import w2-2024.pdf 1099-composite-2024.pdf return-2024.pdf
```

or use **Income & Tax › Import** in the web UI (`spend-analyzer serve`), which uploads the same
files (the imported documents are then listed under **Income & Tax › Documents**). Import is a single step: there is no preview-and-confirm phase, because there is nothing to
choose (no issuer, no layout; the optional LLM fallback is a setting, not a step). A file is either recognised and stored, or reported as
unsupported or unreadable.

Requirements:

- **Native digital PDFs** with a text layer, as with statements. Scans and photos are not read.
- **Software-generated forms**: the PDF a payroll provider, broker or tax program produces. The
  parsers key on the printed box and line labels, so a hand-filled form that has been scanned
  won't work.
- Each file is imported for one **user** (`--user-id`, defaulting to the default user). In the web
  UI the Income & Tax pages follow the same user bar as the Spending pages.

A file may hold several forms (two employers' W-2s, a consolidated 1099 with several accounts).
Each is stored separately.

### Layouts the importer copes with

The importer does not assume one form per file or any one broker's layout. Before parsing, it
cleans the text (glyph placeholders, dot leaders between a label and its amount, letter-spaced
headings such as `Form 1 0 9 9 - D I V`, detached signs and parentheses, the page header repeated
on every page, instruction pages that name every form but carry no amounts) and then cuts each
page into regions by form headings and word positions:

- two forms side by side on a landscape page (2-up) or four in a grid (4-up), and forms stacked
  on one page;
- one form that continues on the next page (`(continued)`, `Page 2 of 3`), a 1099-B transaction
  table over many pages (net gain per holding term is taken from the broker's own term totals,
  else its covered and noncovered subtotals, else the trades; subtotals are never added to
  totals);
- different form types in one PDF (a W-2, a 1099-INT and a 1099-DIV page; a 1040 with attached
  W-2s).

A part of a file that cannot be read does not fail the whole file: the readable forms are
imported and an **import warning** says which kind of form was skipped. Only a file from which
nothing at all could be read ends as an error. Sections whose boxes are all zero are recognised
(their zero items are stored), not treated as unreadable.

## Supported forms

| Form | Parser id | Role | Notes |
|---|---|---|---|
| W-2 | `tax_w2` | source | Copies B, C and 2 on one page collapse into one form; several employers in one file are fine |
| 1099-INT | `tax_1099_int` | source | |
| 1099-DIV | `tax_1099_div` | source | |
| 1099-B | `tax_1099_b` | source | Net gain or loss per holding term; proceeds and cost basis are not stored |
| 1099-NEC, 1099-MISC | `tax_1099_nec_misc` | source | One parser, two form types; the heading decides |
| Consolidated 1099 | `tax_1099_consolidated` | source | A broker's composite statement: 1099-INT, -DIV, -B and optionally -MISC / -OID sections, one form per account |
| Form 1040 | `tax_1040` | return | Tax years 2022-2025, with Schedules D, 1, 2 and C when attached. Amounts are read after the line number repeated at the right of each row, so form numbers printed inline (line 16's "Check if any from Form(s): 1 8814 2 4972") are never taken for amounts. The filing status may be one option per row or all five on one row. A Schedule 1-A page is ignored |

Not supported: Form 1040-SR, 1040-NR and 1040-X (amended) returns, state returns, and 1099
sections other than the above and 1099-OID inside a consolidated statement (for example 1099-R,
which a consolidated statement skips with a warning). Amounts are USD only.

## What is read, and where it goes

Every amount is stored as an integer count of cents. Each line lands in one **income category**
or one **tax category**; the category, not the form, is what the totals and the UI group by.

### Income categories

| Category | Label | Counted in total income |
|---|---|---|
| `salary_wages` | Wages and salary | yes |
| `interest` | Interest | yes |
| `ordinary_dividends` | Ordinary dividends | yes |
| `qualified_dividends` | Qualified dividends (memo) | **no**, memo |
| `short_term_capital_gain` | Short-term capital gain | yes (may be negative) |
| `long_term_capital_gain` | Long-term capital gain | yes (may be negative) |
| `self_employment` | Self-employment income | yes |
| `other` | Other income | yes, unless it is a memo item |

Only the two capital-gain categories can be negative (a net loss). Everything else is stored as
received and must be zero or positive.

### Tax categories

| Group | Categories |
|---|---|
| Withholding | `federal_income_tax_withheld`, `state_income_tax_withheld`, `local_income_tax_withheld` |
| FICA | `social_security_tax`, `medicare_tax` |
| Liability (from a return) | `total_federal_tax`, `federal_tax_ordinary_est`, `federal_tax_preferential_est`, `net_investment_income_tax`, `additional_medicare_tax`, `self_employment_tax` |
| Payments | `estimated_tax_payments`, `total_payments` |
| Result | `refund`, `amount_owed` |

Tax amounts are non-negative magnitudes; for the result group the category carries the direction.
State withholding keeps the state code it was printed under (box 15 on a W-2), and local
withholding keeps the locality name.

### Boxes to categories

| Form | Box | Category | Note |
|---|---|---|---|
| **W-2** | 1 | `salary_wages` | Emitted even when 0 |
| | 2 | `federal_income_tax_withheld` | |
| | 4 | `social_security_tax` | |
| | 6 | `medicare_tax` | |
| | 17 | `state_income_tax_withheld` | One item per state line |
| | 19 | `local_income_tax_withheld` | By locality name |
| | 3, 5, 12, 14 | not stored | |
| **1099-INT** | 1 | `interest` | Emitted even when 0 |
| | 3 | `interest` | Savings bond and Treasury interest, a second item |
| | 8 | `other` (memo) | Tax-exempt interest; shown as a memo line |
| | 4 | `federal_income_tax_withheld` | |
| | 17 | `state_income_tax_withheld` | |
| **1099-DIV** | 1a | `ordinary_dividends` | Emitted even when 0 |
| | 1b | `qualified_dividends` (memo) | A subset of 1a |
| | 2a | `long_term_capital_gain` | Capital-gain distributions are long term |
| | 12 | `other` (memo) | Exempt-interest dividends |
| | 4, state box | federal and state withholding | |
| **1099-B** | net per term | `short_term_capital_gain`, `long_term_capital_gain` | Summary rows win over transaction rows when both exist |
| | ordinary / undetermined | `other` | Only when positive; adds a warning |
| | 4, state | federal and state withholding | |
| **1099-NEC** | 1 | `self_employment` | Emitted even when 0 |
| | 4, 5 | federal and state withholding | |
| **1099-MISC** | 1, 2, 3 | `other` | Rents, royalties, other income; one item each |
| | 7 (pre-2020 layout) | `self_employment` | |
| | 4, 16 | federal and state withholding | |
| **Consolidated 1099** | each section | as for the single form | Items keep their own source form; one form per account |
| **Form 1040** | 1a (2022: 1) | `salary_wages` | Line 1z on 2023 and later |
| | 2b | `interest` | |
| | 3b | `ordinary_dividends` | |
| | 3a | `qualified_dividends` (memo) | |
| | Schedule D 7 and 15 | `short_term_capital_gain`, `long_term_capital_gain` | Without Schedule D, line 7 is treated as long term, with a warning |
| | Schedule 1 line 3 (or Schedule C line 31) | `self_employment` | A business loss is not recorded; warning |
| | 4b, 5b, 6b, 8 | `other` | Retirement, pension, Social Security, other Schedule 1 income |
| | 24 | `total_federal_tax` | |
| | 25d | `federal_income_tax_withheld` | |
| | 26 | `estimated_tax_payments` | |
| | 33 | `total_payments` | |
| | 35a (or 34 less 36) | `refund` | The refund actually received: line 35a, else the overpayment (34) minus the part applied to next year's estimated tax (36) |
| | 37 | `amount_owed` | |
| | Schedule 2 lines 4, 11, 12 | `self_employment_tax`, `additional_medicare_tax`, `net_investment_income_tax` | |
| | 16 | `federal_tax_ordinary_est`, `federal_tax_preferential_est` | An [estimate](#the-federal-tax-split-is-an-estimate) |

Names, addresses, and recipient or spouse details are never read. Of a taxpayer identifier, only
the last four digits are ever kept, and only for the payer or account, never your own number.

## Memo items

A **memo item** is shown but not added to total income, because it is either already inside
another line or isn't taxable income:

- **Qualified dividends** are a subset of ordinary dividends (1099-DIV box 1b inside 1a; 1040 line
  3a inside 3b). Counting both would double count them. They appear as "Qualified dividends
  (memo)".
- **Tax-exempt interest** (1099-INT box 8) and **exempt-interest dividends** (1099-DIV box 12) are
  not taxable income. They appear together as "Tax-exempt interest (memo)".

In `tax report --csv` memo rows have `section = memo` and are absent from `total_income`.

## Source forms versus the return

A W-2 or 1099 describes income **as reported to you by a payer**: a *source* form. A Form 1040
describes the same income **as you filed it**, plus the tax computed on it: a *return*. They
overlap, so adding them together would count every dollar twice. The return is authoritative: it
already applies the $3,000 limit on a net capital loss and any adjustment you made. The rule, per
user and tax year:

| Figure | With a 1040 imported | Without a 1040 |
|---|---|---|
| Total income and income by category | The return | Source forms (net capital result floored at -$3,000) |
| Federal withholding | Source forms; the return's line 25d only if no source form carries it | Source forms |
| State and local withholding, FICA | Source forms (the 1040 doesn't carry them) | Source forms |
| Total tax, Schedule 2 taxes, payments, refund, amount owed, ordinary/preferential split | The return | n/a |

The year is labelled with its **income basis**: `return` when a 1040 exists, `source documents`
when only W-2/1099 forms do, `mixed` in an all-users view where the users differ, `none` when
there is nothing to read. Rates are computed on the resulting total income.

**Reconciliation.** When both exist, the source-form sums are compared with the return one category
at a time (W-2 wages, 1099 interest, 1099 dividends, 1099-B net capital gain, 1099 self-employment,
other 1099 income). A difference of more than $1 raises a warning such as "1099 interest is
$405.44 less than the return reports - some forms may not be imported" (or "more than ... a corrected
or duplicate form may have been imported"). The usual cause of "less" is a form you haven't imported
yet. Other income only warns when the source forms exceed the return, since the return's pension
and Social Security lines have no supported source form. The source-form capital result is floored
at -$3,000 before it is compared, like the return. Federal withholding on the source forms is also
compared with the return's line 25d. These warnings never change the income shown, which stays on
the return.

**No return: the capital-loss limit.** Without a 1040 the tool can't know your filing status, so
a combined net capital loss (short plus long term) is limited to $3,000 (married filing separately
is really $1,500) and a warning reports the amount carried over to later years. The loss categories
are reduced so the category lines still add up to total income.

**Several returns for one year.** If you import more than one 1040 for the same user and year (a
draft and the final, or an amended copy), only the most recently imported one is used and a
warning says so; they are never added. Delete the superseded document with `tax delete`.

## Rates

All rates are fractions of **total income** (memo items excluded; a net capital loss lowers it).
A rate is shown as "n/a" (an em dash in the UI) when total income is zero or negative, or when the
numerator isn't available, for example no return was imported.

| Rate | Formula |
|---|---|
| Federal effective rate | total federal tax (return) / total income |
| Federal withholding rate | federal income tax withheld (source forms) / total income |
| All-in rate | (federal tax + state tax + local tax + Social Security + Medicare) / total income |

In the all-in rate, "federal tax" is the return's total federal tax when there is one, otherwise
the federal tax withheld. "State tax" is the return's state liability when present, otherwise the
state tax withheld. Local tax is local withholding. The all-in rate therefore mixes final and
withheld figures when you have only part of the paperwork, so read the warnings under the numbers.

**Additional Medicare Tax is counted once.** Employers withhold the 0.9% Additional Medicare Tax
inside W-2 box 6, so the Medicare figure from your W-2s already contains it, and the return's total
tax contains it again (Schedule 2 line 11). With a return, the all-in rate therefore uses

```text
all-in = (total federal tax - min(additional Medicare Tax, W-2 Medicare tax)
          + state tax + local withholding + Social Security + Medicare) / total income
```

If no W-2 Medicare figure was imported, nothing is subtracted (the Additional Medicare Tax would
otherwise vanish from the rate).

**Refund or owed** comes from the return's printed lines. `refund` is the refund you actually
received: line 35a when printed, otherwise line 34 (overpaid) minus line 36 (the part applied to
next year's estimated tax). A warning is raised when 35a plus 36 doesn't equal line 34. If the
return prints neither refund nor amount owed but does print total payments and total tax, it is
derived as their difference and flagged (the CLI notes "refund / owed derived from total payments
minus total tax").

## The federal tax split is an estimate

The Form 1040 prints one number for income tax (line 16). Spend Analyzer splits it into

- **tax on ordinary income**, and
- **tax on qualified dividends and long-term gains** (the preferential part),

so you can see how much of your tax comes from investment income taxed at the lower rates. Both
parts are estimates (`is_estimate`) and are labelled "(est.)". Together they always add up to the
printed line 16 amount.

**Method.** The split re-runs the IRS *Qualified Dividends and Capital Gain Tax Worksheet* with
bracket data bundled in the package (`ingest/tax/data/federal_brackets.yaml`):

1. Preferential income is qualified dividends (line 3a) plus net long-term gain (the lesser of
   Schedule D lines 15 and 16 when Schedule D is attached, otherwise line 7 when positive),
   clamped to taxable income.
2. Ordinary tax is the worksheet's tax on taxable income minus that preferential income, using the
   IRS Tax Table (midpoint of each row) below $100,000 and exact bracket arithmetic above, rounded
   to whole dollars like the printed form.
3. Preferential tax is the printed line 16 amount minus the ordinary tax.

**Coverage.** Tax years **2022, 2023, 2024 and 2025**, for single, married filing jointly, married
filing separately, head of household and qualifying surviving spouse (which uses the joint
tables). The values were copied from the IRS revenue procedures (the file header lists each one);
verify them against the IRS publication before relying on them, and do the same when adding a year.

**Limits.** The split is flagged **approximate** (the warning says "federal tax split is
approximate") when the printed tax is more than $2 away from the worksheet recomputation, or when
the return reports 28%-rate gain or unrecaptured section 1250 gain. It does not model the
Schedule D Tax Worksheet, the Form 4952 investment-interest election, Form 8814 or 4972 tax,
the foreign earned income exclusion, the alternative minimum tax or credits. The split is simply
not computed (with a warning) when the filing status is missing or more than one box is marked,
there is no bracket table for the year, or taxable income or tax could not be read. This is a
reading aid for your own records, not tax advice and not a substitute for your return.

A Form 1040 for a year other than 2022-2025 is still read, using the newest layout table, with the
warning "form layout for tax year N not verified"; the split is not estimated for it.

## Joint returns

The tool does not split a joint return between spouses. A married-filing-jointly 1040 is one
return with one total tax, and it is stored under the user you import it for. If you and your
spouse are separate users and each imported their own W-2s:

- the return's tax is compared with only that user's income, so the per-user **effective rate is
  not meaningful**, and the reconciliation warnings will fire for that user;
- the **All users** view adds each user's resolved figures: the return's income where it was
  imported, withholding from source forms, and the liability from the return, which gives the
  correct household figures when the return is imported under one user.

Either use All users for a joint-filing household, or import the whole household's forms under one
user. Your spouse's name and number on a return are never read.

## Corrected forms

A form marked CORRECTED is stored with a flag and a warning. It is **not** automatically matched to
the original: if both are imported, their amounts are added and the year's warnings say so. After
importing a corrected form, delete the document it replaces:

```console
$ spend-analyzer tax list
$ spend-analyzer tax delete 3
```

Importing the same file again for the same user does nothing (a parsed document that has lost all
its forms, because they were duplicates of a document you since deleted, is imported again). The same
PDF may be imported for two different users: each user keeps their own copy, and an existing
document is never moved to another user. A form whose content is identical to one already
stored (the same W-2 delivered as two different PDFs) is skipped and reported as a duplicate form.

## Manual entry and corrections

Figures can be typed in without a PDF, or corrected after an import. Everything is local and
validated: last-four fields are exactly four digits, payer names that look like an SSN/EIN are
rejected (an EIN inside a longer name is masked), categories come from the lists above, and
amounts follow the parser rules (only short- and long-term capital gains may be negative; tax
amounts are never negative). Amounts are integer cents.

| Method and path | Body | Response |
|---|---|---|
| `POST /api/tax/documents/manual` | `{user_id, tax_year, original_name?}` | document (201) |
| `POST /api/tax/documents/{id}/forms` | form fields plus optional `income_items[]`, `tax_items[]` | form detail (201) |
| `PATCH /api/tax/forms/{id}` | any form fields (`null` clears) | form detail |
| `DELETE /api/tax/forms/{id}` | | 204 |
| `POST /api/tax/forms/{id}/income-items`, `.../tax-items` | item | item (201) |
| `PATCH` / `DELETE /api/tax/income-items/{id}`, `/api/tax/tax-items/{id}` | item fields | item / 204 |
| `POST /api/tax/forms/{id}/confirm` | | form detail |
| `GET /api/tax/documents/{id}` | | document with forms and items |

Adding a form to a document that failed (`error` / `unsupported`) turns it into a `parsed`
document with the warning "Figures on this document were entered manually." A form whose year
differs from the other forms of its document is rejected (400); changing a form's year moves its
items too. Summaries read the tables live, so edits show up immediately.

Provenance is stored per form (`extraction_method`: `parser`, `llm` or `manual`; `is_user_edited`)
and per item (`origin`). Manual forms get a random fingerprint and never collide with imports.
Editing an imported form keeps its fingerprint on purpose: uploading the original file again is
still recognised as a duplicate and does not bring back the figures you changed.

## LLM fallback (optional)

Off by default. Deterministic parsing always runs first and its results always win. If you have an
LLM configured (Settings -> LLM) you can additionally tick **Use the LLM for tax forms the built-in
parsers can't read** (`[ingest] tax_llm_fallback = true`, or `spend-analyzer llm set
--tax-llm-fallback`). It then applies only to:

- regions the parsers reported as unreadable (an unrecognised section, a form with a blank key
  box, a consolidated-statement section with no recognisable amounts), and
- a whole file nothing could be read from, when it has a text layer and a tax year is printed on it
  (or you pass one).

A parseable file never triggers a request. `spend-analyzer tax import --no-llm` skips it for one run.

**What is sent.** Only the text of the affected pages (at most 2 pages / 12,000 characters per
request, at most 12 requests per file), after redaction: SSN / EIN shapes become `***-**-1234` /
`**-***1234`; account numbers and other long digit runs keep only their last 4 digits (amounts are
left alone); the recipient / employee name, address and SSN rows are removed (best effort, by
label and position); e-mail addresses are masked; and your **personal-info entries from Settings ->
Privacy** (names, address, phone and account numbers) are removed with exactly the rules used for
statements. Recipient identity detection is heuristic, so add your name and address to those
entries. File names are never sent.

**What is accepted.** The model answers in JSON. Only forms W-2 and 1099-INT/DIV/B/NEC/MISC and
consolidated 1099 are allowed (never 1040), only boxes from a fixed table (`core/tax_boxes.py`,
kept in step with the parsers by a test), and the category comes from that table, never from the
model. Every amount must appear in the text that was sent, be within range and non-negative (except
capital gains), and the tax year is never taken from the model. Anything else is dropped and counted
in one warning. A model or network error never fails the import: the deterministic result is kept
with a warning.

Forms read this way carry `extraction_method = llm` and their items `origin = llm`; they show as
**AI-extracted - review** until you confirm or edit them, and the document gets the warning "Some
values were read by your LLM; review them." Items filled into a consolidated statement keep the
form as `parser` and are marked per item. A file that still yields nothing says so, and suggests
turning the fallback on when an LLM is configured but it is off, or entering the figures manually.

### Reading chosen pages on demand

With an LLM configured, a document in **Income & Tax › Documents** offers **Read pages with LLM...** (whatever
`tax_llm_fallback` says): page thumbnails to pick from (preselected = the pages the automatic
fallback would send), a collapsed **Show what would be sent** preview of the exact redacted text
(no network call), and **Send selected pages to LLM** after a confirmation. Form 1040 pages and
pages without text are never sent. New forms are tagged like the automatic fallback's and a form
already stored is skipped.

**Without a kept PDF (the default) you select the file again.** The page tools need the PDF. When
none is kept (`store_tax_pdf_copies` off), the section says "To read pages with your LLM, select
the original file again (it isn't kept after import for privacy)" and shows a drop zone. The server
checks the file's sha256 against the imported document's; a different file is refused ("This isn't
the same file as <name>") and nothing is stored. The same file is held only temporarily:

- in `<data dir>/tax_staging/<document id>.pdf`, a private folder (mode 0700, file 0600) that is not
  part of the database, the statement copies, exports or backups;
- for 30 minutes (then you are asked again), and it is deleted after a successful send to the LLM,
  when the document is deleted, when it expires (checked on every use and whenever a file is
  staged) and when the app starts;
- so the privacy default is unchanged: no PDF copy stays behind unless you turn on
  `store_tax_pdf_copies`.

API: `GET /api/tax/documents/{id}/pages`, `.../pages/{n}/image`, `.../llm-preview?pages=1,2` and
`POST .../llm-fallback {"pages": [...]}`; `POST .../source` (multipart `file`) stages the
re-selected file and returns `{pages, expires_at}`. All take the optional `user_id` ownership
check. Without a kept or staged PDF the page endpoints answer 409 with `detail.code`
`pdf_needed`. A document shows `has_pdf` (kept copy) and `staged_until` (temporary copy).

## Where the data lives

Imported figures are stored in the local database (`tax_documents`, `tax_forms`, `income_items`,
`tax_items`), per user. The file name you uploaded is stored for display, with any SSN or EIN shape
and any run of six or more digits masked (`W2 123-45-6789.pdf` becomes `W2 ***-**-6789.pdf`). Beyond amounts, the database holds the payer name, the last four digits of
the payer's identifier and of the account number, the form type and the tax year. Full SSNs,
ITINs and EINs are never written to the database, the logs or the extract cache. After a tax import
the uploaded PDF is deleted; to keep a copy (it contains full identifiers, so think before you do),
set in `config.toml`:

```toml
[privacy]
store_tax_pdf_copies = true      # default false; a kept copy is removed by `tax delete`
```

This key is read from the file only; the Settings screen doesn't show it. Tax data is not sent to
an LLM unless you turn on the fallback below. If you hand a tax form to the statement `import` command, it prints a pointer to `tax import` instead of storing anything.

## Troubleshooting

**"Can't read this PDF — it looks like a scan or an image with no text layer."** The file has no
text to read. Download the form again from the payer or tax software instead of scanning it.

**"This PDF doesn't look like a supported tax form."** The status is `unsupported`. Check the form
is on the [supported list](#supported-forms): 1040-SR, 1040-NR, 1040-X and state returns are not
read. A row for the file stays in `tax list`; remove it with `tax delete <id>`.

**"Could not determine the tax year for this form."** The year isn't printed where the parser looks
for it. Supply it: `spend-analyzer tax import --tax-year 2024 form.pdf` (the web UI has a tax year
field).

**"This tax form could not be read."** The form was recognised, but a required box (for example W-2
box 1) is blank or unreadable. Run `tax list` to see the status; try the PDF the issuer provides
rather than a "print to PDF" copy of a screen.

**"W-2 wages is $X less than the return reports" (or the same for 1099 interest, dividends,
capital gain).** Usually a form is missing, or a corrected one was imported alongside the
original. Income stays on the return either way. See [Corrected forms](#corrected-forms), and for joint returns
[Joint returns](#joint-returns).

**"No W-2/1099 source documents were imported for this year."** Income and federal withholding come
from the return. Import the W-2 and 1099 forms to see income by payer, state and FICA figures.

**"N Form 1040 returns were imported for this year."** Only the latest is used. Delete the others
with `tax delete`.

**"This tax form could not be read." for a file that normally works.** An unexpected failure while
reading is recorded the same way (status `error`, nothing half-saved, no copy of the PDF left in the
upload folder); the log names only the kind of error.

**"federal tax split not estimated" / "no federal bracket table".** See the
[limits above](#the-federal-tax-split-is-an-estimate): the filing status wasn't a single clear
mark, the year is outside 2022-2025, or taxable income or tax was not read.

**`tax report` prints "No tax data matches this report."** There are no documents for that user and
year. `tax report` defaults to the default user; use `--user-id` or `--all-users`.
