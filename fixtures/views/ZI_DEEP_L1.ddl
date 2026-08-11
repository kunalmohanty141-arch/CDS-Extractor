// R-29 — the top of a six-level stack. Each level is individually harmless.
// SAP publishes no depth limit, but the framework rejects views it considers
// "too complex for automatic CDC delta" (KBA 3467820) and deep stacks are the
// usual cause, so the tool asks for review rather than inventing a threshold.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_DEEP_L1
  as select from ZI_DEEP_L2 as Lower
{
  key Lower.OrderId  as OrderId,
      Lower.Customer as Customer,
      Lower.Amount   as Amount
}
