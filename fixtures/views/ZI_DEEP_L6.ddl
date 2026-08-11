// R-29 — the bottom of the six-level stack fixture. Reads the real table.
define view entity ZI_DEEP_L6
  as select from zcustorder
{
  key orderid  as OrderId,
      customer as Customer,
      amount   as Amount
}
