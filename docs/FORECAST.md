# Net worth forecast

**Net worth › Forecast** projects your net worth to a future year from your own history and a
handful of assumptions you can change. It is an *illustration*, not a prediction and not financial,
tax or investment advice: every rate is held constant, so the result answers "what would happen
if...", never "what will happen". Everything runs on your machine, and the figures never leave it
(the forecast code imports neither an LLM nor an HTTP client).

This page covers where the starting numbers come from, what each parameter does, the exact
arithmetic, the presets, and what the model leaves out.

## Where the history comes from

The Forecast tab shows a **History** table with one row per calendar year that has any data, and
suggests a starting value for each parameter from it. Nothing is written back; the suggestions are
only defaults you can override (each input shows the figure from your data and a **Reset to data**
button).

| Figure | Source | Notes |
|---|---|---|
| Year-end net worth | **Net worth** balance entries, else a value you typed in | The last month with data in the year, with each account's last balance carried forward, exactly as on the Overview tab. |
| Income | Imported tax forms (**Income & Tax**) | The table shows gross income on the forms for that year, with the **earned** part (wages + self-employment) underneath when it differs. The forecast's default income is the earned part only; see below. |
| Tax | Imported tax forms | The all-in figure: federal + state + local + Social Security + Medicare, from the same rules as the **Income & Tax** overview's all-in rate (rebuilt from that rate, which is rounded to four decimals, so it can be off by a few dollars). |
| Spending | Imported statements | Purchases, fees and interest with refunds netted, summed per year; the table shows how many months of the year have transactions. |
| Savings | Income - tax - spending | Shown only for a year with all three and twelve months of spending. |

**Entries beat manual values, per person.** For a person and year that has balance entries, that
person's manually entered net worth is ignored (the table says so and still shows it). Manual values
exist for years the entries do not cover, typically before you started tracking. Enter one under
**History** (a past year, up to the current year; negative is allowed for a net debt). With **All
users** selected the history is the sum across people and is read-only; pick a person to edit. The
choice is made for each person and year before summing, so one person's entries never hide another
person's manual value (the Source column then reads **Mixed (per person)**). A person whose entries
stop early is carried forward, as on the Overview tab.

**Suggested defaults**

- *Starting net worth*: the latest year's figure. The projection starts from the end of that
  year, which is also the **Starting year** (change it if your latest figure is older or newer).
- *Income*: **earned** income only (wages + self-employment) from the latest year that has any.
  Interest, dividends and capital gains are left out, because the investment return already
  compounds them; counting both would double-count.
- *Tax rate*: that year's all-in tax divided by its earned income (capped at 100%), so the tax
  dollars of that year are reproduced on the earned income the forecast taxes.
- *Spending*: the latest year with at least three months of spending. A partial year is scaled up
  to twelve months and labelled as such.
- *Growth rates*: the observed compound annual growth between the first and last year with data
  (earned income; spending: complete years only). Missing with fewer than two such years.

**The data years can differ, so the screen rebases them.** Net worth, income and spending are each
measured in their own latest year (tax forms often lag the balances; spending may be a partial
current year), and the model treats income and spending as the **starting-year** level and grows
them one step in the first projected year. Using an older year's figure unchanged would lose that
growth; a newer year's would count it twice. So each input shows its source year, and a default
(not one you typed) is rebased to the starting year at the scenario's own growth rate:
`default x (1 + growth)^(starting year - source year)`, in either direction (income at the income
growth rate, spending at the spending growth rate). The note beside the input shows the original
figure, the years and the rate. The tax rate is not rebased. Starting net worth cannot be
rebased (it depends on what you saved in between), so if you move the starting year away from the
net worth's year the screen warns instead. A figure you type is used as is.

## Parameters

| Parameter | Meaning |
|---|---|
| Forecast to year | The last projected year, from 1 to 60 years after the starting year (default: ten years after the later of the starting year and this year). Saved presets store it as years after the starting year plus the starting year itself, so it does not move when you reload one. |
| Starting year / Starting net worth | The balance the projection starts from, and the year it is as of. |
| Annual earned income, income growth | Wages and self-employment income in the starting year (not investment income), and its growth per year (compounded). |
| Effective tax rate | One flat rate applied to all income, including retirement income. |
| Annual spending, spending growth | Spending in the starting year and its percentage growth per year. |
| Fixed change in spending | An amount added to spending every year on top of the percentage growth (negative to shrink it). Spending never goes below zero. |
| Investment return | The yearly return on your balance. |
| Inflation | Used to show values in starting-year dollars, and to index retirement income. |
| Last working year, retirement income | Salary stops after the last working year; from the next year income is the retirement income, a pre-tax annual amount in starting-year dollars that grows with inflation. Empty last working year = never retire. |
| One-off events | A lump sum in a given year (a down payment, an inheritance), in that year's dollars. Negative = money out. Up to 50. |
| Goal (optional) | An amount, in the dollars the chart is showing (nominal or starting-year); the summary reports the first year net worth reaches it. |

## The arithmetic

For each projected year *t*, with *n* = *t* - starting year:

```
income_t   = income_0 x (1 + income growth)^n          while t <= last working year
           = retirement income x (1 + inflation)^n     afterwards
tax_t      = income_t x tax rate
spending_t = max(0, spending_(t-1) x (1 + spending growth) + fixed change)
savings_t  = income_t - tax_t - spending_t               negative = drawdown
return_t   = return rate x max(0, net worth_(t-1) + savings_t / 2)
net worth_t = net worth_(t-1) + savings_t + return_t + events_t
```

- **Mid-year convention.** The return is earned on the opening balance plus *half* of the year's
  savings, as if savings arrive evenly through the year. A drawdown year (negative savings) earns
  on a correspondingly smaller balance.
- **Negative balances** neither earn a return nor accrue interest: the model does not price debt.
- **Events** land at year end after the return, so they earn nothing in their own year. An event
  dated on or before the starting year (already in the starting balance) or after the target year is ignored.
- **Starting-year dollars** (the toggle reads, for example, "2024 dollars") divide each year's value
  by (1 + inflation)^*n*, so they are in the dollars of the starting year, which is not necessarily
  the current year. The history on the chart is not adjusted.
- **Contributions vs growth** in the summary: *saved and one-off events* is the sum of savings and
  events; *investment growth* is the sum of the returns; starting net worth + both = net worth at
  the target year. Growth is shown as a percentage of the total change only when saving and growth
  push the same way; when they oppose (a drawdown that returns partly offset, or losses against
  savings) the card says what offsets what, because the percentage could read 1000%.

The formula lives in `frontend/src/screens/net-worth/forecast/forecast-logic.ts` (a pure function,
so the screen recomputes as you drag a slider), with unit tests including hand-calculated cases.
There is no Python or CLI copy of it; the server only supplies the history and stores presets.

## Presets

Pick a preset to start from a set of assumptions.

| Built-in | Income growth | Investment return | Spending growth | Inflation |
|---|---|---|---|---|
| Conservative | 2% | 4% | 3% | 3% |
| Moderate | 3% | 6% | 2.5% | 2.5% |
| Aggressive | 5% | 8% | 2% | 2.5% |

These are illustrative round numbers, not forecasts or recommendations. A built-in preset changes
only these four rates and is read-only. The screen opens on Moderate.

**Saved presets** capture the whole scenario: the starting year (empty = the latest year with data) and
the target (as years after it), the four rates, tax rate, any starting values you overrode (a value you left on "use my data" stays
that way), spending change, retirement and events. **Save as preset...** stores it for the selected
user, or for everyone with **Share with everyone** (also what happens when **All users** is
selected). A saved preset can be updated with the current scenario, renamed or deleted. Names are
unique within their scope and cannot reuse a built-in name. A shared preset and a personal one
may have the same name; the picker marks shared ones "(shared)" so they can be told apart. The screen marks a scenario as
**Modified** once it differs from the chosen preset. A saved preset whose stored parameters no
longer pass validation (for example after editing the database by hand) cannot be loaded; rather
than failing the whole list it is listed separately by name under the picker (and logged as a
warning), where it can be deleted. The API also lets it be renamed or repaired by saving new
parameters, and its name can be reused by a new preset. Money inputs reject amounts of
$100 billion or more with a message, matching the API's limit.

## Limitations

- **Deterministic.** One path with constant rates. There is no Monte Carlo, no sequence-of-returns
  risk, no confidence band; real markets, careers and prices vary, and a bad early decade matters
  more than the average suggests.
- **One flat tax rate.** The effective rate is applied to all income, and taxes on investment
  gains, withdrawals and retirement accounts are not modelled separately; the investment return is
  treated as after-tax. Tax law, brackets and thresholds do not change over time.
- **Income is earned income only.** The default leaves out interest, dividends and capital gains
  (the investment return stands in for them), so a household living mainly on investment income
  gets a low or empty income default; type the figure you want. Income is also only what your
  imported forms show, e.g. no pensions or Social Security unless they appear as income lines.
- **Net worth is only what you track**: balance entries cover checking, savings, investment and
  credit-card accounts. Property, loans and anything else are not included unless you enter it as
  a manual year (a manual value is a single total).
- **Spending is only what you import**, so cash spending and accounts without statements are
  missing, and a partial year is scaled up linearly.
- **USD only**, like the tax and balance data it draws on.
- **Annual granularity.** Months, the timing of events within a year, and contribution schedules
  are not modelled.
