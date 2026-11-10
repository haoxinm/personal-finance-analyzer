# Income and tax analysis

Spend Analyzer can read your W-2, 1099 and Form 1040 PDFs and show what you earned, what was
withheld, what you owed or got back, and your effective tax rate, year by year. It is a second,
separate import path next to bank and card statements: tax forms are parsed locally by
deterministic code and stay on your machine. An optional, off-by-default LLM fallback for the
parts the parsers cannot read is described under [LLM fallback](#llm-fallback-optional).

This page covers what is read from each form, how income and tax are aggregated, how the
federal tax split and the [per-user estimate](#per-user-estimate) work (and where they are only
approximate), and what to do when an import
doesn't go as expected. For the commands see [CLI.md](CLI.md#tax); for the privacy guarantees see
the [README's privacy model](../README.md#privacy-model).

## Getting forms in

```console
$ spend-analyzer tax import w2-2024.pdf 1099-composite-2024.pdf return-2024.pdf
```

or use **Income & tax › Import** in the web UI (`spend-analyzer serve`), which uploads the same
files (the imported documents are then listed under **Income & tax › Documents**). Import is a single step: there is no preview-and-confirm phase, because there is nothing to
choose (no issuer, no layout; the optional LLM fallback is a setting, not a step). A file is either recognised and stored, or reported as
unsupported or unreadable.

Requirements:

- **Native digital PDFs** with a text layer, as with statements. Scans and photos are not read.
- **Software-generated forms**: the PDF a payroll provider, broker or tax program produces. The
  parsers key on the printed box and line labels, so a hand-filled form that has been scanned
  won't work.
- Each file is imported for one **user** (`--user-id`, defaulting to the default user). In the web
  UI the Income & tax pages follow the same user bar as the Spending pages.

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
| `retirement_deferral` | Retirement plan deferrals, W-2 box 12 (memo) | **no**, memo |
| `hsa_employer` | Employer HSA contributions, W-2 box 12 (memo) | **no**, memo |

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
| | 12 (12a-12d) | `retirement_deferral` (memo) | Codes D, E, F, G, S; box `12-D` ... `12-S`; already excluded from box 1 |
| | 12 (12a-12d) | `hsa_employer` (memo) | Code W; box `12-W`. Other codes are ignored |
| | 3, 5, 14 | not stored | |
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

- **W-2 box 12** deferrals (codes D, E, F, G, S) and employer HSA contributions (code W) are
  already excluded from box 1 wages. They appear as "Retirement plan deferrals, W-2 box 12 (memo)"
  and "Employer HSA contributions, W-2 box 12 (memo)", are never subtracted from wages, and feed the
  deferral-limit warning of the [estimate](#per-user-estimate).

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
| All-in rate | headline total tax (federal + Social Security + Medicare + state) / total income |

The all-in rate is the **headline total tax** (see "Headline tax" below) divided by total income,
so it always matches the "Total tax" figure shown next to it. In the headline, "federal tax" is the
return's total federal tax when there is one, otherwise the [estimate](#per-user-estimate)'s total tax,
otherwise the federal tax withheld. "State tax" is the return's state liability when present,
otherwise the state and local tax withheld. The rate therefore mixes final, estimated and withheld
figures when you have only part of the paperwork, so read the warnings and the basis label under the
numbers.

**Additional Medicare Tax is counted once.** An employer withholds the 0.9% Additional Medicare Tax
inside W-2 box 6 only on wages over $200,000, so the Medicare figure from your W-2s contains it
only when it was actually withheld, and the return's total tax contains it again (Schedule 2
line 11). With a return, the headline federal figure therefore uses

```text
withheld = max(W-2 box 6 - 1.45% x Medicare wages, 0)
federal  = total federal tax - min(additional Medicare Tax, withheld)
all-in   = (federal + state tax + Social Security + Medicare) / total income
```

Medicare wages (W-2 box 5) are not stored; they are approximated as box 1 wages plus the box 12
elective deferrals (which box 1 excludes and box 5 includes). If the employer withheld none (box 6
is exactly 1.45% of wages), nothing is subtracted: the Additional Medicare Tax owed on the return
is then not inside the Medicare figure and stays in the federal tax. If no W-2 Medicare figure was
imported, nothing is subtracted either.

**Headline tax.** `GET /api/tax/summary` also returns `headline`: `federal_minor`, `federal_basis`,
`fica_minor`, `state_minor` and `total_minor`, so a UI does not have to repeat the logic above. The
federal figure is the return's total tax less the Additional Medicare Tax already inside the W-2
Medicare figure (`federal_basis` `return`); with no return, the [estimate](#per-user-estimate)'s
total tax, likewise less the Additional Medicare Tax withheld (`estimate`) when parameters exist
for the year, else the federal tax withheld (`withholding`);
`mixed` in an all-users view whose users differ. FICA is the Social Security and Medicare tax on
the source forms, and the state figure the state liability when printed, else state and local
withholding.

**Refund or owed** comes from the return's printed lines. `refund` is the refund you actually
received: line 35a when printed, otherwise line 34 (overpaid) minus line 36 (the part applied to
next year's estimated tax). A warning is raised when 35a plus 36 doesn't equal line 34. If the
return prints neither refund nor amount owed but does print total payments and total tax, it is
derived as their difference and flagged (the CLI notes "refund / owed derived from total payments
minus total tax"). When several users are added together (the All users view) refund and amount owed
are **netted** into one result: one user's refund of $1,000 and another's balance due of $400 is a
refund of $600, not both at once.

## The federal tax split is an estimate

The Form 1040 prints one number for income tax (line 16). Spend Analyzer splits it into

- **tax on ordinary income**, and
- **tax on qualified dividends and long-term gains** (the preferential part),

so you can see how much of your tax comes from investment income taxed at the lower rates. Both
parts are estimates (`is_estimate`) and are labelled "(est.)". Together they always add up to the
printed line 16 amount.

**Method.** The split re-runs the IRS *Qualified Dividends and Capital Gain Tax Worksheet* with
parameters bundled in the package (`core/tax_data/federal_params.yaml`, code in `core/tax_calc.py`):

1. Preferential income is qualified dividends (line 3a) plus net long-term gain (the lesser of
   Schedule D lines 15 and 16 when Schedule D is attached, otherwise line 7 when positive),
   clamped to taxable income.
2. Ordinary tax is the worksheet's tax on taxable income minus that preferential income, using the
   IRS Tax Table (midpoint of each row) below $100,000 and exact bracket arithmetic above, rounded
   to whole dollars like the printed form.
3. Preferential tax is the printed line 16 amount minus the ordinary tax.

**Coverage.** Tax years **2022 through 2026**, for single, married filing jointly, married
filing separately, head of household and qualifying surviving spouse (which uses the joint
tables). The values were copied from the IRS revenue procedures (the file lists each one per year; see
[Where the parameters come from](#where-the-parameters-come-from-and-the-opt-in-update));
verify them against the IRS publication before relying on them, and do the same when adding a year.

**Limits.** The split is flagged **approximate** (the warning says "federal tax split is
approximate") when the printed tax is more than $2 away from the worksheet recomputation, or when
the return reports 28%-rate gain or unrecaptured section 1250 gain. It does not model the
Schedule D Tax Worksheet, the Form 4952 investment-interest election, Form 8814 or 4972 tax,
the foreign earned income exclusion, the alternative minimum tax or credits. The split is simply
not computed (with a warning) when the filing status is missing or more than one box is marked,
there is no bracket table for the year, or taxable income or tax could not be read. This is a
reading aid for your own records, not tax advice and not a substitute for your return.

The 1040 *layouts* verified are 2022-2025. A Form 1040 for another year is still read, using the
newest layout table, with the warning "form layout for tax year N not verified"; its split is
estimated when bracket tables exist for the year (through 2026) and skipped with a warning otherwise.

## Joint returns

A married-filing-jointly 1040 is one return with one total tax covering **both** spouses, and it is
stored under the user you import it for. If you and your spouse are separate users and each
imported their own W-2s:

- link the spouses in the **tax profile** (`joint_with_user_id`, filing status `mfj`; either
  spouse's profile is enough). In the **All users** view the spouse's W-2/1099 income, which the
  joint return already contains, is then **not added again** (a warning says so), and a second copy
  of the same return imported for the spouse is ignored. Each user's withholding, FICA and state
  figures are still added, so the household totals are right. Without the link nothing says the two
  users are married, and the All users view adds both users' source-form income to the return's
  (the return's income is the whole household's, so it is counted twice): link them, or import the
  household's forms under one user;
- in the **per-user** rows each user keeps their own figures: the user who holds the return shows
  the joint return's income, tax and refund, with a warning that they cover both spouses; the
  other spouse's row shows their own W-2s;
- the return's tax compared with only one user's income makes that user's **effective rate not
  meaningful**, and the reconciliation warnings fire for that user;
- for a per-user view of the tax itself, use the [estimate](#per-user-estimate): it computes the
  household once and **allocates** it by share of income.

Your spouse's name and number on a return are never read.

## Corrected forms

A form marked CORRECTED is stored with a flag and a warning. A corrected form **replaces its
original automatically** when both are for the same user, tax year and form type and carry the
same payer identifier last four and the same account last four (at least one of the two must be
present, so unrelated manual forms never match). The replaced original is left in the database
(and in `tax list`) but is not counted in any total, summary, payer list or estimate; the year's
warnings say how many forms were replaced and which documents, and `superseded_forms` in the summary
API lists them (the Documents tab marks such a document **Replaced by corrected form**). Import order does not matter between an original and its correction.

**Several corrections of one original.** A second correction replaces the first, but documents carry
no issue or statement date, so "second" can only mean *imported later*: the correction imported last
(the highest form id) is used, and a warning says that more than one corrected form exists and that the
choice rests on import order. If an earlier correction is the right one, delete the later one.

When the match is not clear (no identifier was read, the identifiers differ, or two originals
share them, as with two W-2s from one employer) nothing is replaced and the old warning stays: the
corrected form and the original would both be counted. Delete the one it replaces:

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
| `GET` / `PUT` / `DELETE /api/tax/profiles/{user_id}/{tax_year}` | profile (see [Inputs](#inputs)) | profile (`PUT` 201 when created; 404, 409 or 422 on a bad user, spouse or value) |
| `GET /api/tax/estimate?user_id=&tax_year=` | | the [estimate](#per-user-estimate) (422 when the year has no parameters, 409 for conflicting spouse profiles) |
| `GET /api/tax/parameters`, `POST /api/tax/parameters/refresh` | | parameters, sources and update state; one update check |

Adding a form to a document that failed (`error` / `unsupported`) turns it into a `parsed`
document with the warning "Figures on this document were entered manually." A form whose year
differs from the other forms of its document is rejected (400); changing a form's year moves its
items too. Summaries read the tables live, so edits show up immediately.

Provenance is stored per form (`extraction_method`: `parser`, `llm` or `manual`; `is_user_edited`)
and per item (`origin`). Manual forms get a random fingerprint and never collide with imports.
Editing an imported form keeps its fingerprint on purpose: uploading the original file again is
still recognised as a duplicate and does not bring back the figures you changed.

## Per-user estimate

Copying the tax from a Form 1040 gives nothing for a year without one and mixes both spouses' tax
into one user for a joint return. The **estimate** is a simple, reproducible second figure computed
for one user and one year from the source forms (W-2, 1099, manual source forms) and a few manual
inputs. The 1040, when there is one, is only a **cross-check** (see below), never an input. It is a
planning aid, not tax advice and not a substitute for your return. Everything is local.

```console
$ spend-analyzer tax estimate --year 2024 --user-id 1
```

or **Income & tax › Estimate** in the web UI (see [In the web UI](#in-the-web-ui)), or
`GET /api/tax/estimate?user_id=&tax_year=`.

### Inputs

- **Source forms** of that user and year: wages, interest, ordinary and qualified dividends,
  short- and long-term gains, self-employment income, other income, federal tax withheld and the
  Social Security tax withheld. A corrected form replaces its original (see
  [Corrected forms](#corrected-forms)). W-2 box 12 is read as memo items: deferrals (codes D, E, F,
  G, S: `retirement_deferral`) and employer HSA contributions (code W: `hsa_employer`). They are
  *already excluded* from box 1, so they are **shown and checked against the year's elective deferral
  limit, never subtracted again**. Roth deferrals (codes AA, BB) are inside box 1 and are not read (so an excess that
  includes Roth contributions can go unnoticed). 457(b) deferrals (code G) are checked against the
  limit on their own, and the over-limit warning mentions the age-50 catch-up, which the app cannot
  apply because it does not know your age.
- **A tax profile** per user and year (`tax_profiles`; `GET`/`PUT`/`DELETE
  /api/tax/profiles/{user_id}/{tax_year}`): filing status, spouse (`joint_with_user_id`, only with
  `mfj`), standard or itemized deduction (and the itemized total), the number of age 65 / blind boxes
  (`age65_or_blind_count`) and how many people are 65 or older (`age65_count`, for the 2025-2028
  senior deduction). A profile describes its own user: at most 2 boxes and 1 person aged 65+, or
  4 and 2 for `mfj`. With a linked spouse who has a profile too, the estimate limits each profile
  to 2 / 1 and adds them together, so each spouse enters their own; when the spouse is not an app
  user (or has no profile), put both spouses' boxes on the one `mfj` profile (up to 4 / 2). The
  profile also holds adjustments to income, other income, capital loss carryover from the previous year, nonrefundable
  and refundable credits, other taxes, estimated payments and other payments. All amounts are
  non-negative cents. **Without a profile**: the filing status of the user's own 1040 if there is
  one, else single, and the standard deduction.

### Method

1. Net capital result = short term + long term - carryover. A net loss is limited to $3,000 ($1,500
   married filing separately) and the rest is reported as the carryover to next year.
2. Gross income = wages + interest + ordinary dividends (qualified dividends are inside them) +
   self-employment + other income + the limited capital result + the profile's other income.
3. Self-employment tax, per person: 92.35% of self-employment income; 12.4% on the part that fits
   under the Social Security wage base after that person's W-2 Social Security wages (implied by
   W-2 box 4 when present, else box 1 wages plus deferrals), plus 2.9%; nothing below $400 of net
   earnings. Half of it is deducted.
4. AGI = gross income - adjustments - half the self-employment tax. Deduction = the standard
   deduction (plus the age 65 / blind amount per box) or the itemized total you entered. For tax
   years **2025 through 2028** the **senior deduction** comes on top, in either mode: $6,000 per
   qualifying person aged 65 or over (two on a joint return, none for married filing separately),
   reduced by 6% of the AGI above $75,000 ($150,000 on a joint return) (IRC 151(d)(5)(C), added by
   P.L. 119-21). Taxable income = max(AGI - deduction - senior deduction, 0).
5. Income tax = the IRS *Qualified Dividends and Capital Gain Tax Worksheet* on taxable income, with
   preferential income = qualified dividends + the long-term gain that survives the net capital
   result (the same code as the [1040 split](#the-federal-tax-split-is-an-estimate): Tax Table
   below $100,000, exact brackets above, whole dollars).
6. Other taxes on the return:
   - **Net investment income tax** (Form 8960): 3.8% x min(net investment income, AGI - threshold),
     with the threshold $200,000 ($250,000 joint, $125,000 separate). Net investment income =
     interest + ordinary dividends + the capital result counted (a loss lowers it), not below zero.
   - **Additional Medicare Tax** (Form 8959): 0.9% of Medicare wages above the filing-status threshold
     plus 0.9% of the self-employment earnings above the part of the threshold the wages left unused.
     A joint return adds both spouses' wages. The part an employer already withheld (W-2 box 6 beyond
     1.45% of Medicare wages) is a *payment*, below.
7. Total tax = max(income tax - nonrefundable credits, 0) + self-employment tax + net investment
   income tax + Additional Medicare Tax + other taxes. It is **never negative**: the nonrefundable
   credits used are capped at the income tax (the breakdown shows the capped amount, and a warning
   says when part of what you entered is unused).
   **Payments** = federal tax withheld + estimated payments + other payments + **refundable
   credits** (they are payments, like lines 27-31 of the 1040, not negative tax) + the
   Additional Medicare Tax already withheld + the **excess Social Security tax credit**: when two or
   more employers withheld Social Security tax from one person, the part of that person's total above
   6.2% of the wage base comes back as a credit (Schedule 3 line 11); one employer's over-withholding
   is the employer's to refund and is not credited.
   **Result = payments - total tax**: positive is a refund, negative is an amount owed.

**Medicare wages are approximated.** W-2 box 5 is not stored, so Medicare wages are box 1 wages plus
the box 12 elective deferrals. An unusual box 5 (a taxable fringe benefit, dependent-care benefits
above $5,000) skews the withheld Additional Medicare Tax slightly; the figures are listed in the
warnings.

The effective rate is total tax / gross income, the marginal rate is the ordinary bracket the next
dollar falls in, and the preferential rate is the 0%, 15% or 20% that applies to the top of the
taxable income (when there is preferential income).

### Married filing jointly

When the profile is `mfj` with a spouse (the link may be on either spouse's profile) the
household's items and **both profiles' inputs are summed and the tax is computed once**: income
tax, the standard deduction for the joint status, the capital loss limit, the brackets. Self-employment tax,
the Social Security credit and the deferral limit stay per person; the Additional Medicare Tax uses
both spouses' wages together. Itemized deductions are **summed** across the two profiles: enter each
spouse's own share, or the household total on one profile and nothing on the other (a warning
appears when both profiles have an amount). The age 65 / blind boxes and the 65-or-older count are
summed too (each profile limited to 2 and 1 when both spouses have a profile, the household to 4 and 2). Each spouse's view is an **allocation**: the household tax
split by share of gross income (equal shares when there is no income; the parts always sum to the
household tax), with that user's own payments and the result that follows. It is labelled
`scope: household_allocation`; it is not a separate liability. If the two profiles disagree (the
spouse's profile is not `mfj`, or names someone else) the estimate is a conflict (HTTP 409) and the
profile endpoint refuses the save.

### Cross-check against the return

If a Form 1040 exists for the year, the estimate includes its total tax, federal withholding (line
25d), refund or amount owed and the **delta** (estimate minus return) for tax, withholding and the
result. For a joint household the household estimate is compared with the joint return, whichever
spouse imported it. A return filed with a different status than the estimate uses raises a warning.
A large delta usually means a credit, deduction or income that is not modeled (below) or a form
that is missing; the return is never changed by it.

### In the web UI

**Income & tax › Estimate** sits between Overview and Documents and follows the year picker
(`?year=`) and the user bar.

- **Inputs** (left): your tax profile for the year: filing status (and, for married filing jointly,
  the spouse, chosen among the other users), standard or itemized deduction with the itemized
  total, this person's age 65 / blind boxes (at most 2, whatever the filing status) and whether they are
  65 or older (0 or 1; a spouse enters their own on their own profile), and the dollar amounts the
  forms do not cover:
  adjustments, other income, capital loss carryover, nonrefundable and refundable credits, other
  taxes, estimated and other payments. Amounts are typed in dollars and saved in cents; **Save**
  replaces the profile, **Reset** returns to the saved values, **Delete profile** (with a
  confirmation) removes it. Do not enter 401(k) or HSA payroll deferrals as adjustments: W-2 box 1
  already excludes them. Without a saved profile the form starts from what the estimate assumed.
- **Result** (right and above): "Refund $X" or "Owe $X", the total tax, the effective rate and the
  marginal rate (with the preferential rate under it when there are qualified dividends or
  long-term gains), then the whole computation from wages to the result. Every input line is
  marked **Documents**, **Profile** or **Computed**; zero optional lines are left out.
- **Joint return**: the breakdown is the household's ("Household calculation"), and a card shows
  this person's share, labelled "allocated by share of income" (an allocation, not a separate
  liability), against their own payments. The first headline card is this person's allocated
  result and the tax card is the household's.
- **Cross-check**: when a Form 1040 exists, a card shows return, estimate and the difference
  (estimate minus return) for total tax, withholding and the result. A joint return is compared
  with the household totals.
- **Warnings** (for example "No tax profile: ... assumed"), a collapsible **Not modeled** list and
  the parameter source (the IRS revenue procedures, the parameter file's version and whether it is
  bundled, a downloaded update or your own file) sit under the numbers.
- **Problems are explained, not hidden.** A year without parameters (HTTP 422) says so and points to
  Settings › Tax parameters; spouses' profiles that disagree (409) name the spouse and leave the
  inputs editable so the conflict can be fixed there; a user with no imported forms sees a link to
  **Import**.
- **All users**: the estimate is per person, so with "All users" selected the screen shows one
  summary card per person (tax, effective rate, result) with a button to open that person. Spouses
  on a joint return each show their allocated share, never the household total twice.

The **Overview** shows the server's headline tax (the `headline` of `/api/tax/summary`) with its
basis next to the federal figure: *from return*, *estimated* (no return: the estimate),
*withholding only* or *mixed* (an all-users view whose users differ). The year-over-year chart marks
each year's basis (est., w/h, mixed) and draws years without a return lighter, so the bars are
never silently a mix of filed and estimated tax. The effective-rate card is the return's tax over
income and shows a dash without a return; its hint links to the estimate.

### Where the parameters come from, and the opt-in update

Brackets, standard deductions (and the age 65 / blind amounts), the long-term gain breakpoints, the
capital loss limit, the Social Security wage base, the 401(k) elective deferral limit and the NIIT
and Additional Medicare Tax thresholds are bundled in the package, per year, in
`core/tax_data/federal_params.yaml`. **Treasury Fiscal Data was considered and does not publish
these parameters**; the authoritative source is the IRS inflation-adjustment revenue procedure for
each year: 2021-45 (2022), 2022-38 (2023), 2023-34 (2024), 2024-40 (2025, with the standard
deduction raised to $15,750 / $31,500 / $23,625 by P.L. 119-21, the One Big Beautiful Bill Act, as
Rev. Proc. 2025-32 section 3 restates) and 2025-32 (2026), plus the Social Security Administration's
contribution and benefit base and the IRS's 401(k) limit announcements. Every year in the file
lists its sources (`GET /api/tax/parameters` returns them), the file is validated by a schema loader
(thresholds strictly increasing, every filing status present, a citation per year) and a
`params_version` records revisions of the file (informational). The 2025-2028 senior deduction
(IRC 151(d)(5)(C); IRS "One, Big, Beautiful Bill Act: tax deductions for working Americans and
seniors") is part of the 2025 and 2026 entries.

The app makes no network request for this by default. Two optional files can sit in
`<data dir>/tax_params/`, and the years in effect are merged **per tax year**, never wholesale:

1. every **bundled year always comes from the app**: a correction to a published year ships with an
   app update, never with a download;
2. the copy downloaded by the update check may only **add tax years the bundled file lacks**, and
   only after each added year passes sanity checks against the year before it: plausible ranges,
   the same bracket rates and count, indexed amounts (standard deductions, wage base, deferral limit,
   bracket and capital-gain thresholds) within +/-15% of the previous year, statutory amounts
   (loss limits, NIIT and Additional Medicare thresholds, the senior deduction) unchanged, no year
   far ahead. One violation rejects the whole download; a cached file that fails the checks is
   ignored when parameters are loaded;
3. **your own file** (`spend-analyzer tax params import FILE`, removed by `tax params clear`) replaces
   the years it lists (your deliberate correction, or a newer year before a release) and leaves the
   other years alone, so no year can disappear;
4. an invalid or unreadable optional file is ignored (the bundled data always loads).

**The update check is opt-in and off by default** (`[tax] auto_update_params = false`, the toggle under
**Settings › Tax parameters**, or `SPEND_ANALYZER_TAX__AUTO_UPDATE_PARAMS=true`). When on, the first estimate or summary
request in a **calendar year** starts one check in a background thread (the page never waits, and a
network error is ignored with an info log): a plain HTTPS `GET`, no query string, cookies,
credentials, proxy or user data, of `federal_params.yaml` from this project's GitHub repository
(`raw.githubusercontent.com/haoxinm/spending-analysis-from-bills/main/src/spend_analyzer/core/tax_data/federal_params.yaml`).
It is accepted only if it passes the same schema validation and adds at least one tax year that is
not already known, every added year passing the checks above; it is then stored with mode 0600. The date of
the attempt is recorded *before* the request (in `tax_params/state.json`), whatever the outcome, so
a machine that is offline is not retried on every page view until the next calendar year.
`spend-analyzer tax params update` (or `POST /api/tax/parameters/refresh`, the **Check now** button
in Settings › Tax parameters) runs one check on request whatever the setting says, because you asked
for it, and records the date too. That Settings card also shows the file in effect (source, version,
last check and its outcome, the tax years covered) and says what the toggle sends: nothing about you. `tax params show` and `GET /api/tax/parameters` report the file in effect, its
version, the last check and the setting. Only `api/services/tax_params_update.py` may make this
request (`tests/test_architecture_guards.py`).

When adding a year by hand: copy the figures from the revenue procedure, add its citation, raise
`params_version`, and run the tests. Releasing an app update is also how a published year is
corrected.

### Not modeled

The estimate lists these with every result instead of guessing: the alternative minimum tax; net
investment income other than interest, dividends and capital gains (rental, royalty, passive
business income, annuities) and the deductions allocable to investment income; credits that depend on
your situation (child tax credit, earned income credit, education credits: enter their total in the
profile, refundable ones as refundable credits so they count as payments); the 2025-2028
deductions for **qualified tips** (up to $25,000), **qualified overtime** (up to $12,500, $25,000
joint) and **car loan interest** (up to $10,000), phased out above $150,000 ($300,000 joint) of income
($100,000 / $200,000 for the loan interest): take the amount from Schedule 1-A and enter it as an
adjustment to income (the estimate then lowers AGI by it, which the real deduction does not, so the
net investment income tax and the senior deduction's phase-out can be slightly understated); the
qualified business income deduction; 28% rate gains and
unrecaptured section 1250 gains; phase-outs of deductions and credits and the SALT cap (enter your
itemized total after them); taxable Social Security benefits, IRA/pension, rental and partnership
income (enter the taxable amount as other income); and state and local income tax.

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

With an LLM configured, a document in **Income & tax › Documents** offers **Read pages with LLM...** (whatever
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
`tax_items`), per user; the manual estimate inputs in `tax_profiles`. The optional tax parameter files
and the date of the last update check live in `<data dir>/tax_params/` (see the
[estimate](#where-the-parameters-come-from-and-the-opt-in-update)). The file name you uploaded is stored for display, with any SSN or EIN shape
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
mark, the year is outside 2022-2026, or taxable income or tax was not read.

**`tax report` prints "No tax data matches this report."** There are no documents for that user and
year. `tax report` defaults to the default user; use `--user-id` or `--all-users`.
