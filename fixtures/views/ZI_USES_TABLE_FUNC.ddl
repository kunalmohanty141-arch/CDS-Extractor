// R-09 + R-27 — the branch into ZI_TABLE_FUNC cannot be resolved, so the
// constructs inside it were never checked and nothing above it can be trusted.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_USES_TABLE_FUNC
  as select from ZI_TABLE_FUNC as Calculated
{
  key Calculated.orderid as OrderId,
      Calculated.amount  as Amount
}
