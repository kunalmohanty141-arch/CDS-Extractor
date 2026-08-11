// R-06 — set operation. A base-row change cannot be attributed to a branch.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_UNION
  as select from zcustorder
{
  key orderid  as OrderId,
      customer as Customer
}
union all
select from zorderitem
{
  key orderid  as OrderId,
      material as Customer
}
