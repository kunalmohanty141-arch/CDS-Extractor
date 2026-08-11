// R-29 — level 2 of the six-level stack fixture. See ZI_DEEP_L1.
define view entity ZI_DEEP_L2
  as select from ZI_DEEP_L3 as Lower
{
  key Lower.OrderId  as OrderId,
      Lower.Customer as Customer,
      Lower.Amount   as Amount
}
