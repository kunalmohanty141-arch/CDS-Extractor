// R-29 — level 3 of the six-level stack fixture. See ZI_DEEP_L1.
define view entity ZI_DEEP_L3
  as select from ZI_DEEP_L4 as Lower
{
  key Lower.OrderId  as OrderId,
      Lower.Customer as Customer,
      Lower.Amount   as Amount
}
