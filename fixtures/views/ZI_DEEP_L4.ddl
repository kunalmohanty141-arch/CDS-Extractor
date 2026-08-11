// R-29 — level 4 of the six-level stack fixture. See ZI_DEEP_L1.
define view entity ZI_DEEP_L4
  as select from ZI_DEEP_L5 as Lower
{
  key Lower.OrderId  as OrderId,
      Lower.Customer as Customer,
      Lower.Amount   as Amount
}
